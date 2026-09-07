"""Plan comparison mathematics (Release A task A10).

A pure helper: every function takes explicit inputs and never reads the
database or the clock. The plan lifecycle service (``plan_service.py``) stays
the owner of configured plan state; this module only compares what callers
hand it.

Core rule: reference usage and configured accrual are compared over the SAME
interval. Month-to-date usage versus a full month of plan cost proves
nothing; the ratio is usage value over the prorated accrual for the identical
span. Parity means reference-value parity with configured expense — never
profitability — and no verdict is produced when coverage or plan attribution
is insufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from spend_app.timeutil import CalendarDay, days_in_month, local_day, overlap_seconds

ZERO = Decimal(0)


@dataclass(frozen=True)
class PlanTerms:
    """One configured plan active over part of the compared interval."""

    tool_key: str
    name: str
    amount: Decimal
    cadence: str  # monthly | quarterly | annual
    start_date: date
    end_date: date | None = None
    account_key: str | None = None

    def daily_rate(self, day: date) -> Decimal:
        """Configured expense for one calendar day, effective-dated.

        Monthly plans divide by the true length of each month (31-day months
        and leap Februaries accrue differently per day but sum to the plan
        price); quarterly and annual divide by their fixed day counts so the
        calendar sum still equals the configured amount.
        """
        if day < self.start_date or (self.end_date is not None and day > self.end_date):
            return ZERO
        if self.amount <= 0:
            return ZERO
        if self.cadence == "monthly":
            return self.amount / Decimal(days_in_month(day))
        if self.cadence == "quarterly":
            return self.amount / Decimal(91)
        if self.cadence == "annual":
            return self.amount / Decimal(366 if _is_leap(day.year) else 365)
        raise ValueError(f"unsupported cadence: {self.cadence}")


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


@dataclass(frozen=True)
class Expense:
    """A verified extra expense (overage, correction), already deduplicated."""

    tool_key: str
    amount: Decimal
    account_key: str | None = None
    note: str | None = None


@dataclass
class PlanComparison:
    tool_key: str
    plan_names: list[str] = field(default_factory=list)
    accrued_cost: Decimal = ZERO
    extra_expenses: Decimal = ZERO
    usage_value: Decimal = ZERO
    usage_complete: bool = False
    cost_scope: str = "configured"
    observations_seconds: float = 0.0
    interval_seconds: float = 0.0
    ratio: dict = field(default_factory=dict)

    @property
    def cost_total(self) -> Decimal:
        return self.accrued_cost + self.extra_expenses


def accrue(
    plans: list[PlanTerms],
    days: list[CalendarDay],
    *,
    tool_key: str | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> Decimal:
    """Configured accrual over the given half-open calendar days.

    With ``window`` only the part of each day inside ``[start, end)`` books
    accrual, prorated by the day's true length; without it every day counts
    in full.
    """
    total = ZERO
    for day in days:
        for plan in plans:
            if tool_key is not None and plan.tool_key != tool_key:
                continue
            if window is not None:
                overlap, total_seconds = overlap_seconds(window[0], window[1], day)
            else:
                overlap, total_seconds = day.duration.total_seconds(), day.duration.total_seconds()
            if not overlap or total_seconds <= 0:
                continue
            total += plan.daily_rate(day.local_date) * Decimal(str(overlap / total_seconds))
    return total


def compare(
    *,
    plans: list[PlanTerms],
    window: tuple[datetime, datetime],
    zone,
    usage_value: Decimal,
    usage_complete: bool,
    expenses: list[Expense] | None = None,
    tool_key: str | None = None,
    coverage_ratio: float | None = None,
) -> PlanComparison:
    """Reference usage versus configured accrual over one identical interval.

    ``coverage_ratio`` (0..1) is the fraction of the interval for which usage
    telemetry is actually present; ratios and verdicts need enough of it.
    """
    start, end = window
    days: list[CalendarDay] = []
    cursor = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    while cursor <= last:
        day = local_day(cursor, zone)
        days.append(day)
        cursor += timedelta(days=1)
    accrued = accrue(plans, days, tool_key=tool_key, window=window)
    interval_seconds = sum(
        max(0.0, (min(end, day.end) - max(start, day.start)).total_seconds()) for day in days
    )
    extras = sum(
        (expense.amount for expense in expenses or () if tool_key is None or expense.tool_key == tool_key),
        ZERO,
    )
    contributing = {
        plan.name
        for plan in plans
        if tool_key is None or plan.tool_key == tool_key
        for day in days
        if plan.daily_rate(day.local_date) > 0
        and (window is None or (min(end, day.end) - max(start, day.start)).total_seconds() > 0)
    }
    names = sorted(contributing)
    comparison = PlanComparison(
        tool_key=tool_key or "all",
        plan_names=names,
        accrued_cost=accrued,
        extra_expenses=extras,
        usage_value=usage_value,
        usage_complete=usage_complete,
        cost_scope="configured+reported extras" if extras else "configured",
        observations_seconds=float(coverage_ratio or 0.0) * interval_seconds,
        interval_seconds=interval_seconds,
    )
    comparison.ratio = ratio(comparison)
    return comparison


def ratio(comparison: PlanComparison) -> dict:
    denominator = comparison.cost_total
    if denominator <= 0:
        return {"value": None, "basis": "unavailable", "reason": "zero or unknown plan cost"}
    if comparison.usage_value < 0:
        return {"value": None, "basis": "unavailable", "reason": "unknown usage value"}
    if comparison.usage_complete:
        return {"value": float(comparison.usage_value / denominator), "basis": "ratio"}
    if comparison.usage_value > 0:
        return {
            "value": float(comparison.usage_value / denominator),
            "basis": "lower_bound",
            "note": "priced subset over configured accrual for the same interval",
        }
    return {"value": None, "basis": "unavailable", "reason": "no priced usage in this interval"}


def underuse_verdict(comparison: PlanComparison, *, minimum_coverage: float = 0.8) -> dict:
    """A plan 'returns less than its cost' claim, only when evidence allows.

    Requires near-complete usage coverage, at least one active plan, and a
    positive cost denominator. A partial-window ratio — however it looks — is
    never a verdict, and low usage is never an instruction to create usage.
    """
    if not comparison.plan_names:
        return {"verdict": "unavailable", "reason": "no configured plan for this scope"}
    if comparison.cost_total <= 0:
        return {"verdict": "unavailable", "reason": "plan cost is zero or unknown"}
    coverage = (
        comparison.observations_seconds / comparison.interval_seconds
        if comparison.interval_seconds > 0
        else 0.0
    )
    if coverage < minimum_coverage:
        return {
            "verdict": "unavailable",
            "reason": f"usage coverage {coverage:.0%} below the {minimum_coverage:.0%} needed for a verdict",
        }
    if not comparison.usage_complete:
        return {"verdict": "unavailable", "reason": "usage value is a partial priced subset"}
    if comparison.ratio.get("basis") != "ratio" or comparison.ratio.get("value") is None:
        return {"verdict": "unavailable", "reason": "ratio unavailable"}
    value = comparison.ratio["value"]
    if value < 0.5:
        return {
            "verdict": "underuse",
            "detail": (
                f"{comparison.plan_names[0]} produced ${comparison.usage_value:.2f} of reference value "
                f"against ${float(comparison.cost_total):.2f} accrued over the same interval."
            ),
            "note": "comparison only; no downgrade or routing instruction",
        }
    return {"verdict": "none", "detail": "reference value is comparable to accrued cost"}


def forecast(
    *,
    plans: list[PlanTerms],
    month_local_date: date,
    zone,
    mtd_usage_value: Decimal,
    mtd_usage_complete: bool,
    elapsed_seconds: float,
    scheduled_changes: list[tuple[date, PlanTerms]] | None = None,
) -> dict:
    """Full-month forecast at the observed pace, with explicit assumptions.

    The projection multiplies the observed reference value by the fraction of
    the month still to run and adds configured cost for the full month,
    including dated scheduled plan changes. It is a pace forecast, not a bill,
    and it refuses to guess without enough observed time.
    """
    total_days = days_in_month(month_local_date)
    month_days: list[CalendarDay] = []
    cursor = month_local_date.replace(day=1)
    for offset in range(total_days):
        month_days.append(local_day(date.fromordinal(cursor.toordinal() + offset), zone))
    month_seconds = sum(day.duration.total_seconds() for day in month_days)
    if month_seconds <= 0 or elapsed_seconds <= 0:
        return {"value": None, "planCost": None, "available": False, "reason": "no elapsed window"}
    coverage = elapsed_seconds / month_seconds
    if coverage < 0.1 or mtd_usage_value is None:
        return {
            "value": None,
            "planCost": float(accrue(plans, month_days)),
            "available": False,
            "reason": f"only {coverage:.0%} of the month observed; forecast withheld",
        }
    pace_value = mtd_usage_value * Decimal(str(month_seconds / elapsed_seconds))
    projected_cost = accrue(plans, month_days)
    for change_date, terms in scheduled_changes or ():
        remaining: list[CalendarDay] = []
        for day in month_days:
            if day.local_date >= change_date and (terms.end_date is None or day.local_date <= terms.end_date):
                overlap, total = overlap_seconds(day.start, day.end, day)
                if overlap > 0:
                    remaining.append(day)
        projected_cost += accrue([terms], remaining)
    if not mtd_usage_complete:
        pace_value_label = "lower-bound forecast"
    else:
        pace_value_label = "forecast at observed pace"
    return {
        "value": float(pace_value),
        "valueBasis": pace_value_label,
        "planCost": float(projected_cost),
        "available": True,
        "assumptions": (
            f"observed {coverage:.0%} of the month; remaining days extrapolated at the observed pace; "
            "scheduled plan changes included; not a bill"
        ),
    }
