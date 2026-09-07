"""Canonical time semantics for BURNRATE (Release A contract 4.2).

Every persisted instant is a UTC datetime; ranges are half-open ``[start, end)``.
Fixed-duration controls (1h/1d/1w) are elapsed UTC durations, while calendar
controls (MTD/YTD, subscription dates) resolve against the configured IANA
timezone. Calendar-day arithmetic here is DST-safe: a local day is the real
23/25-hour span between local midnights, never a fixed 24 hours.

No function in this module reads the system clock; callers inject ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = 1_000_000


def parse_utc(value: object) -> datetime | None:
    """Parse the mixed ISO forms seen in sources and the database.

    Second/millisecond/microsecond precision, ``Z`` or numeric offsets, and
    zoneless values (interpreted as UTC) all normalize to one aware UTC
    datetime. Anything unparsable returns None so callers can quarantine the
    record instead of raising mid-ingest.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def epoch_micros(moment: datetime) -> int:
    """Fixed-width canonical form for persistence and query ordering.

    Integer microseconds since the epoch keep sub-second precision that TEXT
    ISO ordering loses when source precision is mixed (one event inside
    another's second must never sort out of order).
    """
    return round((moment.astimezone(UTC) - EPOCH) / timedelta(microseconds=1))


def from_epoch_micros(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=int(value))


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CalendarDay:
    """One local calendar day as a half-open UTC interval.

    ``duration`` is the true elapsed time between the local midnights (23 or
    25 hours across DST transitions), so accrual split by day stays exact.
    """

    local_date: date
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def local_day(local_date: date, zone: ZoneInfo) -> CalendarDay:
    start_local = datetime.combine(local_date, time.min, tzinfo=zone)
    end_local = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return CalendarDay(
        local_date=local_date,
        start=start_local.astimezone(UTC),
        end=end_local.astimezone(UTC),
    )


def overlap_seconds(
    start: datetime, end: datetime, day: CalendarDay
) -> tuple[float, float]:
    """Seconds of ``[start, end)`` inside ``day`` and the day's total seconds.

    The fraction of the day uses the true day length, so a window that covers
    half of a 25-hour day books half of that day's accrual.
    """
    overlap = max(0.0, (min(end, day.end) - max(start, day.start)).total_seconds())
    total = day.duration.total_seconds()
    return overlap, total


def month_start(moment: datetime, zone: ZoneInfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date().replace(day=1), time.min, tzinfo=zone).astimezone(UTC)


def next_month_start(moment: datetime, zone: ZoneInfo) -> datetime:
    local = moment.astimezone(zone)
    if local.month == 12:
        first = local.replace(year=local.year + 1, month=1, day=1)
    else:
        first = local.replace(month=local.month + 1, day=1)
    return datetime.combine(first.date(), time.min, tzinfo=zone).astimezone(UTC)


def days_in_month(local_date: date) -> int:
    if local_date.month == 12:
        following = local_date.replace(year=local_date.year + 1, month=1, day=1)
    else:
        following = local_date.replace(month=local_date.month + 1, day=1)
    return (following - local_date.replace(day=1)).days


@dataclass(frozen=True)
class PreviousPeriod:
    """A defined comparable interval, not an equal count of wall days.

    For calendar windows the previous period is the immediately preceding
    calendar span of the same kind (the whole prior month for MTD, the prior
    calendar year for YTD): prior-MTD versus a truncated slice of the prior
    month would compare different fractions of a month.
    """

    start: datetime
    end: datetime
    label: str


def previous_calendar_period(
    kind: str, moment: datetime, zone: ZoneInfo
) -> PreviousPeriod:
    local = moment.astimezone(zone)
    if kind == "mtd":
        start = datetime.combine(local.date().replace(day=1), time.min, tzinfo=zone)
        previous_month_last = start.astimezone(zone).date() - timedelta(days=1)
        previous_start = datetime.combine(previous_month_last.replace(day=1), time.min, tzinfo=zone)
        return PreviousPeriod(
            start=previous_start.astimezone(UTC),
            end=start.astimezone(UTC),
            label="prior full month",
        )
    if kind == "ytd":
        year_start = datetime.combine(date(local.year, 1, 1), time.min, tzinfo=zone)
        previous_year_start = datetime.combine(date(local.year - 1, 1, 1), time.min, tzinfo=zone)
        return PreviousPeriod(
            start=previous_year_start.astimezone(UTC),
            end=year_start.astimezone(UTC),
            label=f"prior full year {local.year - 1}",
        )
    raise ValueError(f"unsupported calendar period: {kind}")
