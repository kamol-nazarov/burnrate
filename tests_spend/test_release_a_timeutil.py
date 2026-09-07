"""Release A contract tests: time semantics (task A02)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from spend_app.timeutil import (
    epoch_micros,
    from_epoch_micros,
    iso_utc,
    local_day,
    month_start,
    next_month_start,
    overlap_seconds,
    parse_utc,
    previous_calendar_period,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def test_parse_utc_accepts_mixed_precision_and_offsets():
    second = parse_utc("2026-09-01T12:00:00Z")
    millis = parse_utc("2026-09-01T12:00:00.500Z")
    micros = parse_utc("2026-09-01T12:00:00.500123+00:00")
    offset = parse_utc("2026-09-01T08:00:00-04:00")
    assert second is not None and millis is not None and micros is not None
    assert second < millis < micros
    assert offset == second
    assert parse_utc("2026-09-01 12:00:00") == second  # zoneless means UTC


def test_parse_utc_rejects_garbage_without_raising():
    assert parse_utc(None) is None
    assert parse_utc("") is None
    assert parse_utc("not-a-time") is None
    assert parse_utc(12345) is None


def test_epoch_micros_orders_within_one_second():
    # ISO TEXT ordering loses a within-second event; the integer form does not.
    early = datetime(2026, 9, 1, 12, 0, 0, 100000, tzinfo=UTC)
    late = datetime(2026, 9, 1, 12, 0, 0, 900000, tzinfo=UTC)
    assert epoch_micros(early) < epoch_micros(late)
    assert from_epoch_micros(epoch_micros(late)) == late
    assert epoch_micros(from_epoch_micros(1_700_000_000_000_123)) == 1_700_000_000_000_123


def test_half_open_range_boundary_selection():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    inside = datetime(2026, 9, 1, 12, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    assert start <= inside < end
    assert not (start <= end < end)  # end is excluded


def test_fixed_day_is_24_hours_even_across_dst():
    spring = local_day(date(2026, 3, 8), NY)  # DST starts in the US
    fall = local_day(date(2026, 11, 1), NY)  # DST ends in the US
    assert spring.duration == timedelta(hours=23)
    assert fall.duration == timedelta(hours=25)
    normal = local_day(date(2026, 9, 1), NY)
    assert normal.duration == timedelta(hours=24)


def test_mtd_honors_local_midnight_not_system_zone():
    moment = datetime(2026, 9, 6, 3, 30, tzinfo=UTC)  # 23:30 on Sep 5 in New York
    assert month_start(moment, NY) == datetime(2026, 9, 1, 4, tzinfo=UTC)  # local Sep 1 midnight
    assert next_month_start(moment, NY) == datetime(2026, 10, 1, 4, tzinfo=UTC)


def test_leap_day_calendar_is_exact():
    leap = local_day(date(2024, 2, 29), UTC)
    assert leap.duration == timedelta(hours=24)
    # February 2024 accrues 29 daily fractions.
    days = [leap]
    total = sum(overlap_seconds(leap.start, leap.end, day)[0] for day in days)
    assert total == 24 * 3600


def test_previous_period_is_full_calendar_span_not_equal_wall_days():
    moment = datetime(2026, 9, 6, 18, tzinfo=UTC)
    prior = previous_calendar_period("mtd", moment, NY)
    # Prior MTD is the whole previous month (Aug 1..Sep 1 local), not
    # "six wall days of August".
    assert prior.start == datetime(2026, 8, 1, 4, tzinfo=UTC)
    assert prior.end == datetime(2026, 9, 1, 4, tzinfo=UTC)
    prior_year = previous_calendar_period("ytd", moment, NY)
    assert prior_year.start == datetime(2025, 1, 1, 5, tzinfo=UTC)
    assert prior_year.end == datetime(2026, 1, 1, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        previous_calendar_period("1d", moment, NY)


def test_iso_round_trip_is_stable():
    moment = datetime(2026, 9, 6, 12, 30, 15, 123456, tzinfo=UTC)
    assert parse_utc(iso_utc(moment)) == moment
