"""Read-only Plans & Value query adapter with fingerprint-keyed caching.

Loads effective plans, a single bounded usage window (priced + unpriced), and
ingest health — then hands rows to the pure period / group / valuation helpers.
No migrations, writes, schedulers, or secret reads.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from spend_app.db import connect
from spend_app.plan_service import list_plans
from spend_app.plans_value_groups import assign_events_to_groups, build_groups
from spend_app.plans_value_periods import resolve_period_bounds
from spend_app.timeutil import epoch_micros, from_epoch_micros, iso_utc, parse_utc

# API-only admin lanes are org-scoped charges/usage — never subscription value.
ADMIN_USAGE_SOURCES = frozenset({"openai_admin", "anthropic_admin"})
_ADMIN_SQL = "('openai_admin', 'anthropic_admin')"

_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 16


def clear_plans_value_cache() -> None:
    """Drop memoized reports (tests / process-local invalidation)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _memo(key: tuple, compute) -> dict:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    value = compute()
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return value


def _connection_identity(connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    return str(row[2] if row is not None else "")


def _as_of_slot(as_of: datetime) -> str:
    """Cache slot for the captured as-of instant (second precision UTC)."""
    moment = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    return iso_utc(moment.replace(microsecond=0))


def _plan_revision(connection) -> tuple:
    """Plan table versions + term rows — any edit changes this fingerprint."""
    plans = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, version, end_date FROM subscription_plans ORDER BY id"
        )
    )
    terms = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, plan_id, name, tool_key, amount_usd, cadence, start_date, end_date "
            "FROM subscriptions ORDER BY id"
        )
    )
    return plans, terms


def _usage_fingerprint(connection, start: datetime, end: datetime) -> tuple:
    """Bounded period fingerprint (not an all-history scan)."""
    start_us = epoch_micros(start)
    end_us = epoch_micros(end)
    priced = tuple(
        connection.execute(
            f"""
            SELECT COUNT(*), MAX(id), MAX(ingested_at),
                   COALESCE(SUM(computed_cost_usd), 0),
                   COALESCE(SUM(input_tokens + cached_input_tokens + cache_write_tokens
                                + cache_write_1h_tokens + output_tokens), 0)
            FROM usage_events
            WHERE occurred_epoch_us IS NOT NULL
              AND occurred_epoch_us >= ? AND occurred_epoch_us < ?
              AND source NOT IN {_ADMIN_SQL}
            """,
            (start_us, end_us),
        ).fetchone()
    )
    unpriced = tuple(
        connection.execute(
            f"""
            SELECT COUNT(*), MAX(id), MAX(ingested_at),
                   COALESCE(SUM(input_tokens + cached_input_tokens + cache_write_tokens
                                + cache_write_1h_tokens + output_tokens
                                + unclassified_tokens), 0)
            FROM unpriced_usage_events
            WHERE occurred_epoch_us IS NOT NULL
              AND occurred_epoch_us >= ? AND occurred_epoch_us < ?
              AND source NOT IN {_ADMIN_SQL}
            """,
            (start_us, end_us),
        ).fetchone()
    )
    ingest = tuple(
        connection.execute(
            "SELECT COUNT(*), MAX(id), MAX(finished_at) FROM ingest_runs"
        ).fetchone()
    )
    return priced, unpriced, ingest


def _normalize_event(row: dict) -> dict:
    event = dict(row)
    epoch = event.get("occurred_epoch_us")
    if epoch is not None:
        event["occurred_at"] = from_epoch_micros(int(epoch))
    else:
        parsed = parse_utc(event.get("occurred_at"))
        if parsed is not None:
            event["occurred_at"] = parsed
    return event


def load_priced_usage_events(connection, start: datetime, end: datetime) -> list[dict]:
    """Single bounded read of priced usage for ``[start, end)``, admin sources excluded."""
    rows = connection.execute(
        f"""
        SELECT * FROM usage_events
        WHERE occurred_epoch_us IS NOT NULL
          AND occurred_epoch_us >= ? AND occurred_epoch_us < ?
          AND source NOT IN {_ADMIN_SQL}
        ORDER BY occurred_epoch_us
        """,
        (epoch_micros(start), epoch_micros(end)),
    )
    return [_normalize_event(dict(row)) for row in rows]


def load_unpriced_usage_events(connection, start: datetime, end: datetime) -> list[dict]:
    """Single bounded read of unpriced usage for ``[start, end)``, admin sources excluded."""
    rows = connection.execute(
        f"""
        SELECT * FROM unpriced_usage_events
        WHERE occurred_epoch_us IS NOT NULL
          AND occurred_epoch_us >= ? AND occurred_epoch_us < ?
          AND source NOT IN {_ADMIN_SQL}
        ORDER BY occurred_epoch_us
        """,
        (epoch_micros(start), epoch_micros(end)),
    )
    events = []
    for row in rows:
        event = _normalize_event(dict(row))
        event["telemetry_complete"] = bool(event.get("telemetry_complete", 1))
        events.append(event)
    return events


def load_source_health(connection) -> list[dict]:
    """Latest ingest_runs row per source (collection evidence for the report)."""
    rows = connection.execute(
        """
        SELECT source, started_at, finished_at, status, events_written, error
        FROM ingest_runs
        WHERE id IN (SELECT MAX(id) FROM ingest_runs GROUP BY source)
        ORDER BY source
        """
    )
    health: list[dict] = []
    for row in rows:
        item = dict(row)
        status = item.get("status") or "never"
        error = item.get("error")
        if status == "skipped" and error and "not configured" in error:
            status = "unavailable"
            error = "unavailable — credential missing"
        health.append(
            {
                "source": item["source"],
                "startedAt": item.get("started_at"),
                "finishedAt": item.get("finished_at"),
                "lastSuccess": item.get("finished_at")
                if status in ("success", "partial")
                else None,
                "status": status,
                "eventsWritten": item.get("events_written") or 0,
                "error": error,
            }
        )
    return health


def _flatten_plans_for_groups(connection, today: date) -> list[dict]:
    """list_plans shape → per-term dicts expected by ``build_groups``."""
    listed = list_plans(connection, today)
    flat: list[dict] = []
    for plan in listed["plans"]:
        plan_end = plan.get("end_date")
        for term in plan["terms"]:
            ends = [value for value in (term.get("end_date"), plan_end) if value]
            flat.append(
                {
                    "plan_id": plan["id"],
                    "id": plan["id"],
                    "version": plan["version"],
                    "term_id": term.get("id"),
                    "name": term["name"],
                    "tool_key": term["tool_key"],
                    "amount_usd": term["amount_usd"],
                    "cadence": term["cadence"],
                    "start_date": term["start_date"],
                    "end_date": min(ends) if ends else None,
                    "status": term.get("status"),
                }
            )
    return flat


def _assemble_report(
    *,
    connection,
    settings,
    pricing: Any,
    period: str,
    as_of: datetime,
) -> dict:
    from spend_app.plans_value import (
        assemble_group_valuation,
        assemble_plans_value_report,
        assemble_unassigned_summary,
    )

    zone = ZoneInfo(settings.timezone)
    as_of_utc = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    start_utc, end_utc, _local_start, _local_end = resolve_period_bounds(
        period, as_of_utc, zone
    )
    today = as_of_utc.astimezone(zone).date()
    plans = _flatten_plans_for_groups(connection, today)
    groups = build_groups(plans, start_utc, end_utc, zone)

    priced = load_priced_usage_events(connection, start_utc, end_utc)
    unpriced = load_unpriced_usage_events(connection, start_utc, end_utc)
    group_priced, unassigned_priced = assign_events_to_groups(priced, groups)
    group_unpriced, unassigned_unpriced = assign_events_to_groups(unpriced, groups)
    sources_data = load_source_health(connection)

    groups_data = [
        assemble_group_valuation(
            group,
            group_priced.get(group.group_id, []),
            group_unpriced.get(group.group_id, []),
            sources_data,
            pricing=pricing,
        )
        for group in groups
    ]
    unassigned_data = assemble_unassigned_summary(
        unassigned_priced,
        unassigned_unpriced,
        pricing=pricing,
    )
    return assemble_plans_value_report(
        period,
        as_of_utc,
        zone,
        groups_data,
        unassigned_data,
        sources_data,
        start_utc,
        end_utc,
    )


def fetch_plans_value_report(
    connection,
    settings,
    pricing,
    period: str,
    as_of: datetime | None = None,
) -> dict:
    """Build a Plans & Value report from an open read connection.

    Cache key includes database identity, period, as-of slot, plan revision,
    pricing identity, and the bounded usage/ingest fingerprint so plan edits
    and new events invalidate without a process restart.
    """
    if period not in ("this_month", "last_month"):
        raise ValueError(f"unsupported period: {period}")
    as_of_utc = (as_of or datetime.now(UTC)).astimezone(UTC)
    zone = ZoneInfo(settings.timezone)
    start_utc, end_utc, _, _ = resolve_period_bounds(period, as_of_utc, zone)
    key = (
        _connection_identity(connection),
        period,
        _as_of_slot(as_of_utc),
        settings.timezone,
        id(pricing),
        _plan_revision(connection),
        _usage_fingerprint(connection, start_utc, end_utc),
    )
    return _memo(
        key,
        lambda: _assemble_report(
            connection=connection,
            settings=settings,
            pricing=pricing,
            period=period,
            as_of=as_of_utc,
        ),
    )


def get_plans_value(
    database_path: Path,
    settings,
    pricing,
    period: str,
    as_of: datetime | None = None,
) -> dict:
    """Open ``database_path`` read-only for the request and return the report.

    Uses the same fingerprint cache as ``fetch_plans_value_report``; distinct
    database paths stay isolated.
    """
    path = Path(database_path)
    as_of_utc = (as_of or datetime.now(UTC)).astimezone(UTC)
    with connect(path) as connection:
        return fetch_plans_value_report(
            connection, settings, pricing, period, as_of=as_of_utc
        )
