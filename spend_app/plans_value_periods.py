"""Pure period bounds and configured-cost accrual for Plans & Value.

No database or clock access: callers inject ``as_of``, timezone, and terms.
Intervals are half-open UTC ``[start, end)``; calendar days use true local
midnight spans (23/24/25 hours across DST).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from spend_app.subscriptions import daily_cost
from spend_app.timeutil import local_day, month_start, overlap_seconds, previous_calendar_period

UTC = timezone.utc
ZERO = Decimal(0)

SUPPORTED_PERIODS = ("this_month", "last_month")


@dataclass(frozen=True)
class PeriodBounds:
    """Resolved half-open period with inclusive local display dates."""

    start_utc: datetime
    end_utc: datetime
    local_start_date: date
    local_end_date: date

    def as_tuple(self) -> tuple[datetime, datetime, date, date]:
        return self.start_utc, self.end_utc, self.local_start_date, self.local_end_date


def _ensure_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _inclusive_local_end(start_utc: datetime, end_utc: datetime, zone: ZoneInfo) -> date:
    """Last local calendar date that intersects ``[start_utc, end_utc)``."""
    if end_utc <= start_utc:
        return start_utc.astimezone(zone).date()
    return (end_utc - timedelta(microseconds=1)).astimezone(zone).date()


def resolve_period_bounds(
    period_key: str, as_of: datetime, zone: ZoneInfo
) -> tuple[datetime, datetime, date, date]:
    """Resolve ``this_month`` / ``last_month`` in ``zone``.

    Returns ``(start_utc, end_utc, local_start_date, local_end_date)`` where the
    UTC pair is half-open and the local dates are the inclusive calendar span
    that intersects that interval.
    """
    if period_key not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period key: {period_key}")

    as_of_utc = _ensure_utc(as_of)
    if period_key == "this_month":
        start_utc = month_start(as_of_utc, zone)
        end_utc = as_of_utc
    else:
        prior = previous_calendar_period("mtd", as_of_utc, zone)
        start_utc = prior.start
        end_utc = prior.end

    local_start = start_utc.astimezone(zone).date()
    local_end = _inclusive_local_end(start_utc, end_utc, zone)
    return start_utc, end_utc, local_start, local_end


def period_bounds(period_key: str, as_of: datetime, zone: ZoneInfo) -> PeriodBounds:
    start_utc, end_utc, local_start, local_end = resolve_period_bounds(period_key, as_of, zone)
    return PeriodBounds(start_utc, end_utc, local_start, local_end)


def term_effective_window(
    start_date: date, end_date: date | None, zone: ZoneInfo
) -> tuple[datetime, datetime | None]:
    """Convert inclusive local term dates to a half-open UTC window.

    ``start_date`` becomes local midnight; inclusive ``end_date`` becomes the
    next local midnight. ``end_date is None`` means no end bound.
    """
    start_utc = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
    if end_date is None:
        return start_utc, None
    end_utc = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start_utc, end_utc


def _intersect(
    window_start: datetime,
    window_end: datetime,
    term_start: datetime,
    term_end: datetime | None,
) -> tuple[datetime, datetime] | None:
    start = max(_ensure_utc(window_start), _ensure_utc(term_start))
    end = _ensure_utc(window_end)
    if term_end is not None:
        end = min(end, _ensure_utc(term_end))
    if end <= start:
        return None
    return start, end


def accrue_term_cost(
    amount_usd: float | Decimal,
    cadence: str,
    term_start: date,
    term_end: date | None,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
) -> Decimal:
    """Accrue configured cost for one term over ``[window_start, window_end)``.

    Each local day that intersects the effective overlap contributes
    ``daily_rate * (overlap_seconds / day_duration_seconds)``. Intermediate
    day amounts stay unrounded; callers round for presentation.
    """
    term_start_utc, term_end_utc = term_effective_window(term_start, term_end, zone)
    overlap_window = _intersect(window_start, window_end, term_start_utc, term_end_utc)
    if overlap_window is None:
        return ZERO

    start, end = overlap_window
    cursor = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    total = ZERO
    while cursor <= last:
        day = local_day(cursor, zone)
        overlap, day_seconds = overlap_seconds(start, end, day)
        if overlap > 0 and day_seconds > 0:
            fraction = Decimal(str(overlap / day_seconds))
            total += daily_cost(amount_usd, cadence, cursor) * fraction
        cursor += timedelta(days=1)
    return total


def _coerce_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def accrue_plans_cost(
    terms: list[dict],
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
) -> Decimal:
    """Sum configured accrual for all terms that intersect the window.

    Ended plans still accrue inside their effective overlap. Future-only terms
    contribute nothing until their start date intersects the window.
    """
    total = ZERO
    for term in terms:
        start_date = _coerce_date(term["start_date"])
        if start_date is None:
            continue
        end_date = _coerce_date(term.get("end_date"))
        total += accrue_term_cost(
            term["amount_usd"],
            term["cadence"],
            start_date,
            end_date,
            window_start,
            window_end,
            zone,
        )
    return total
