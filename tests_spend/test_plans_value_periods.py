"""Unit tests for Plans & Value period bounds and configured-cost accrual."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from spend_app.plans_value_periods import (
    PeriodBounds,
    accrue_plans_cost,
    accrue_term_cost,
    period_bounds,
    resolve_period_bounds,
    term_effective_window,
)
from spend_app.subscriptions import daily_cost
from spend_app.timeutil import local_day

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Period resolution
# ---------------------------------------------------------------------------


def test_this_month_from_local_month_start_through_as_of():
    # asOf = Sep 9 00:00 America/New_York → half-open [Sep 1, Sep 9)
    as_of = datetime(2026, 9, 9, 0, 0, tzinfo=NY).astimezone(UTC)
    start, end, local_start, local_end = resolve_period_bounds("this_month", as_of, NY)

    assert start == datetime(2026, 9, 1, 4, tzinfo=UTC)  # EDT midnight
    assert end == as_of
    assert local_start == date(2026, 9, 1)
    assert local_end == date(2026, 9, 8)


def test_this_month_partial_day_includes_as_of_local_date():
    as_of = datetime(2026, 9, 9, 12, 0, tzinfo=NY).astimezone(UTC)
    start, end, local_start, local_end = resolve_period_bounds("this_month", as_of, NY)
    assert start == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert end == as_of
    assert local_start == date(2026, 9, 1)
    assert local_end == date(2026, 9, 9)


def test_last_month_is_prior_full_calendar_month():
    as_of = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    start, end, local_start, local_end = resolve_period_bounds("last_month", as_of, NY)
    assert start == datetime(2026, 8, 1, 4, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert local_start == date(2026, 8, 1)
    assert local_end == date(2026, 8, 31)


def test_last_month_across_year_boundary():
    as_of = datetime(2026, 1, 15, 12, 0, tzinfo=NY).astimezone(UTC)
    start, end, local_start, local_end = resolve_period_bounds("last_month", as_of, NY)
    # EST: Jan 1 midnight = 05:00 UTC; Dec 1 midnight = 05:00 UTC
    assert start == datetime(2025, 12, 1, 5, tzinfo=UTC)
    assert end == datetime(2026, 1, 1, 5, tzinfo=UTC)
    assert local_start == date(2025, 12, 1)
    assert local_end == date(2025, 12, 31)


def test_period_bounds_dataclass_matches_tuple():
    as_of = datetime(2026, 9, 9, 0, 0, tzinfo=NY).astimezone(UTC)
    bounds = period_bounds("this_month", as_of, NY)
    assert isinstance(bounds, PeriodBounds)
    assert bounds.as_tuple() == resolve_period_bounds("this_month", as_of, NY)


def test_unsupported_period_key_raises():
    with pytest.raises(ValueError, match="unsupported period"):
        resolve_period_bounds("ytd", datetime(2026, 9, 1, tzinfo=UTC), NY)


def test_naive_as_of_treated_as_utc():
    as_of = datetime(2026, 9, 9, 4, 0, 0)  # naive == UTC Sep 9 04:00 == NY Sep 9 00:00
    start, end, local_start, local_end = resolve_period_bounds("this_month", as_of, NY)
    assert end == datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
    assert local_end == date(2026, 9, 8)


# ---------------------------------------------------------------------------
# Term effective windows
# ---------------------------------------------------------------------------


def test_term_window_open_ended():
    start, end = term_effective_window(date(2026, 9, 1), None, NY)
    assert start == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert end is None


def test_term_window_inclusive_end_becomes_next_local_midnight():
    start, end = term_effective_window(date(2026, 9, 1), date(2026, 9, 30), NY)
    assert start == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert end == datetime(2026, 10, 1, 4, tzinfo=UTC)


def test_term_window_single_day():
    start, end = term_effective_window(date(2026, 9, 15), date(2026, 9, 15), NY)
    assert start == datetime(2026, 9, 15, 4, tzinfo=UTC)
    assert end == datetime(2026, 9, 16, 4, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Case 1 — September monthly $300, asOf Sep 9 00:00 NY → $80
# ---------------------------------------------------------------------------


def test_case1_september_monthly_eight_complete_days():
    """Acceptance Case 1: Sep 1–8 inclusive at $300/30 = $80."""
    as_of = datetime(2026, 9, 9, 0, 0, tzinfo=NY).astimezone(UTC)
    start, end, _, _ = resolve_period_bounds("this_month", as_of, NY)

    accrued = accrue_term_cost(
        Decimal("300"),
        "monthly",
        date(2026, 9, 1),
        None,
        start,
        end,
        NY,
    )
    assert accrued == Decimal("80")
    # Independent expectation: 8 full days * ($300 / 30)
    assert accrued == daily_cost(300, "monthly", date(2026, 9, 1)) * 8


def test_case1_via_accrue_plans_cost():
    as_of = datetime(2026, 9, 9, 0, 0, tzinfo=NY).astimezone(UTC)
    start, end, _, _ = resolve_period_bounds("this_month", as_of, NY)
    terms = [
        {
            "amount_usd": 300,
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": None,
        }
    ]
    assert accrue_plans_cost(terms, start, end, NY) == Decimal("80")


# ---------------------------------------------------------------------------
# Case 6 — cadence variants, leap day, partial days, DST, price changes
# ---------------------------------------------------------------------------


def test_case6_monthly_quarterly_annual_full_days_match_calendar():
    window_start = datetime(2026, 8, 1, 4, tzinfo=UTC)  # Aug 1 NY
    window_end = datetime(2026, 8, 4, 4, tzinfo=UTC)  # Aug 4 NY → 3 full days

    monthly = accrue_term_cost(310, "monthly", date(2026, 8, 1), None, window_start, window_end, NY)
    assert monthly == daily_cost(310, "monthly", date(2026, 8, 1)) * 3
    assert monthly == Decimal("30")  # 310/31 * 3

    quarterly = accrue_term_cost(
        400, "quarterly", date(2026, 8, 1), None, window_start, window_end, NY
    )
    # Q3 2026 = Jul(31)+Aug(31)+Sep(30) = 92 days
    assert quarterly == Decimal("400") / Decimal(92) * 3

    annual = accrue_term_cost(365, "annual", date(2026, 1, 1), None, window_start, window_end, NY)
    assert annual == Decimal("1") * 3  # 2026 not leap → 365 days


def test_case6_leap_year_feb29_annual_uses_366():
    # Full Feb 29 2024 in America/New_York
    day = local_day(date(2024, 2, 29), NY)
    accrued = accrue_term_cost(
        Decimal("366"),
        "annual",
        date(2024, 1, 1),
        None,
        day.start,
        day.end,
        NY,
    )
    assert accrued == Decimal("1")
    assert daily_cost(366, "annual", date(2024, 2, 29)) == Decimal("1")


def test_case6_partial_day_half_of_daily_rate():
    # Window covers noon→midnight on Sep 1 NY (half of a normal 24h day)
    day = local_day(date(2026, 9, 1), NY)
    noon_local = datetime(2026, 9, 1, 12, 0, tzinfo=NY).astimezone(UTC)
    accrued = accrue_term_cost(
        300, "monthly", date(2026, 9, 1), None, noon_local, day.end, NY
    )
    expected = daily_cost(300, "monthly", date(2026, 9, 1)) * Decimal("0.5")
    assert accrued == expected


def test_case6_dst_spring_forward_23_hour_day():
    # 2026-03-08 America/New_York: clocks spring forward → 23-hour local day
    day = local_day(date(2026, 3, 8), NY)
    assert day.duration == timedelta(hours=23)

    full = accrue_term_cost(
        310, "monthly", date(2026, 3, 1), None, day.start, day.end, NY
    )
    assert full == daily_cost(310, "monthly", date(2026, 3, 8))

    # First 11.5 hours of the 23-hour day → half day
    mid = day.start + timedelta(hours=11, minutes=30)
    half = accrue_term_cost(
        310, "monthly", date(2026, 3, 1), None, day.start, mid, NY
    )
    assert half == daily_cost(310, "monthly", date(2026, 3, 8)) * Decimal("0.5")


def test_case6_dst_fall_back_25_hour_day():
    # 2026-11-01 America/New_York: clocks fall back → 25-hour local day
    day = local_day(date(2026, 11, 1), NY)
    assert day.duration == timedelta(hours=25)

    full = accrue_term_cost(
        300, "monthly", date(2026, 11, 1), None, day.start, day.end, NY
    )
    assert full == daily_cost(300, "monthly", date(2026, 11, 1))

    # First 12.5 hours of the 25-hour day → half day
    mid = day.start + timedelta(hours=12, minutes=30)
    half = accrue_term_cost(
        300, "monthly", date(2026, 11, 1), None, day.start, mid, NY
    )
    assert half == daily_cost(300, "monthly", date(2026, 11, 1)) * Decimal("0.5")


def test_case6_price_change_across_terms():
    # $200 monthly through Sep 15 inclusive, then $300 from Sep 16
    window_start = datetime(2026, 9, 1, 4, tzinfo=UTC)
    window_end = datetime(2026, 9, 21, 4, tzinfo=UTC)  # through Sep 20 inclusive

    terms = [
        {
            "amount_usd": "200",
            "cadence": "monthly",
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 9, 15),
        },
        {
            "amount_usd": Decimal("300"),
            "cadence": "monthly",
            "start_date": "2026-09-16",
            "end_date": None,
        },
    ]
    accrued = accrue_plans_cost(terms, window_start, window_end, NY)
    expected = daily_cost(200, "monthly", date(2026, 9, 1)) * 15 + daily_cost(
        300, "monthly", date(2026, 9, 16)
    ) * 5
    assert accrued == expected


def test_ended_plan_still_accrues_inside_overlap():
    window_start = datetime(2026, 9, 1, 4, tzinfo=UTC)
    window_end = datetime(2026, 9, 10, 4, tzinfo=UTC)
    # Plan ended Sep 5 inclusive → accrues Sep 1–5 (5 days)
    accrued = accrue_term_cost(
        300, "monthly", date(2026, 8, 1), date(2026, 9, 5), window_start, window_end, NY
    )
    assert accrued == daily_cost(300, "monthly", date(2026, 9, 1)) * 5


def test_future_only_plan_accrues_nothing():
    window_start = datetime(2026, 9, 1, 4, tzinfo=UTC)
    window_end = datetime(2026, 9, 9, 4, tzinfo=UTC)
    accrued = accrue_term_cost(
        300, "monthly", date(2026, 10, 1), None, window_start, window_end, NY
    )
    assert accrued == Decimal(0)


def test_no_accrual_outside_effective_term_before_start():
    day = local_day(date(2026, 9, 1), NY)
    accrued = accrue_term_cost(
        300, "monthly", date(2026, 9, 2), None, day.start, day.end, NY
    )
    assert accrued == Decimal(0)


def test_empty_window_returns_zero():
    moment = datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert (
        accrue_term_cost(300, "monthly", date(2026, 9, 1), None, moment, moment, NY)
        == Decimal(0)
    )


def test_intermediate_days_not_rounded():
    # 3 days of $100/30 should keep full Decimal precision (not rounded per day)
    window_start = datetime(2026, 9, 1, 4, tzinfo=UTC)
    window_end = datetime(2026, 9, 4, 4, tzinfo=UTC)
    accrued = accrue_term_cost(
        Decimal("100"), "monthly", date(2026, 9, 1), None, window_start, window_end, NY
    )
    assert accrued == Decimal("100") / Decimal(30) * 3
    # Not rounded to cents
    assert accrued != accrued.quantize(Decimal("0.01"))
