"""Read-only Plans & Value query adapter with fingerprint-keyed caching.

Loads effective plans, a single bounded usage window (priced + unpriced), and
ingest health — then hands rows to the pure period / group / valuation helpers.
No migrations, writes, schedulers, or secret reads.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from spend_app.db import connect
from spend_app.diagnostics import collection_stale_after, safe_reason
from spend_app.plan_service import list_plans
from spend_app.plans_value_groups import (
    SUPPORTED_TOOLS,
    assign_events_to_groups,
    build_groups,
    canonical_tool_key,
    event_tool_keys_for,
)
from spend_app.plans_value_periods import (
    SUPPORTED_PERIODS,
    cycle_window_for_limits,
    resolve_period_bounds,
)
from spend_app.providers import REGISTRY
from spend_app.source_evidence import sanitize_reason, source_reason_text
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


_SUCCESS_STATUSES = frozenset({"success", "partial"})
_FAILURE_STATUSES = frozenset({"failed", "error"})
_MISSING_MARKERS = frozenset(
    {"not configured", "credential missing", "missing", "disabled", "not enabled"}
)

def _rows(result) -> list[Any]:
    """Materialize one bounded query result for sqlite and unit fakes."""
    if result is None:
        return []
    try:
        return list(result)
    except TypeError:
        row = result.fetchone()
        return [] if row is None else [row]


def _health_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return parse_utc(value)


def _health_iso(value: Any) -> str | None:
    parsed = _health_time(value)
    return iso_utc(parsed) if parsed is not None else None


def _captured_as_of(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    return as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)


def _safe_health_reason(raw: Any, source: str) -> str | None:
    """Return bounded, useful, non-sensitive source-attempt text."""
    if raw is None or not str(raw).strip():
        return None

    # Keep the existing diagnostic vocabulary for known categories.  It maps
    # credential/configuration and permission details to safe explanations.
    normalized = sanitize_reason(raw)
    lowered = normalized.lower()
    if any(marker in lowered for marker in _MISSING_MARKERS):
        return safe_reason(normalized, source)
    if any(marker in lowered for marker in ("permission", "access denied", "permissionerror")):
        return safe_reason(normalized, source)
    if "429" in lowered or "throttl" in lowered:
        return safe_reason(normalized, source)

    # The persisted ingest reason is already redacted at write time, but a
    # report must defend against older rows and synthetic fixtures too.
    import re

    text = source_reason_text(normalized)
    if re.fullmatch(r"redacted (?:provider|cursor|codex|source) failure", text.lower()):
        return text
    if text.lower() == "adaptive cadence":
        return "Collection deferred to the established cadence."
    counter = re.match(r"\d+ (?:quarantined|skipped) record\(s\)", text)
    if counter:
        return counter.group(0)
    # Unknown adapter text is intentionally reduced to the established safe
    # diagnostic vocabulary instead of being copied into the API.
    return safe_reason(text, source)


def _explicit_tool_keys(item: dict) -> tuple[str, ...]:
    values = item.get("toolKeys")
    if values is None:
        values = item.get("tool_keys")
    if values is None:
        value = item.get("toolKey")
        if value is None:
            value = item.get("tool_key")
        values = () if value is None else (value,)
    if isinstance(values, str):
        values = (values,)
    output: list[str] = []
    for value in values or ():
        key = str(value or "").strip().lower()
        if not key or key not in SUPPORTED_TOOLS:
            continue
        canonical = canonical_tool_key(key)
        for alias in event_tool_keys_for(canonical):
            if alias not in output:
                output.append(alias)
    return tuple(output)


def _source_tool_keys(source: str, item: dict) -> tuple[str, ...]:
    spec = REGISTRY.get(source)
    registered = getattr(spec, "tool_keys", ()) if spec is not None else ()
    if not registered:
        explicit = _explicit_tool_keys(item)
        if explicit:
            return explicit
    output: list[str] = []
    for value in registered or ():
        canonical = canonical_tool_key(str(value).strip().lower())
        for alias in event_tool_keys_for(canonical):
            if alias and alias not in output:
                output.append(alias)
    return tuple(output)


def _source_label(source: str) -> str:
    spec = REGISTRY.get(source)
    connection = getattr(spec, "connection", None) if spec else None
    label = getattr(connection, "name", None) if connection else None
    if label:
        return str(label)
    return source.replace("_", " ").strip().title()


def _safe_source_key(value: Any) -> str:
    """Only registered source identities are public reporting metadata."""
    source = str(value or "").strip().lower()
    if REGISTRY.get(source) is not None:
        return source
    return "unattributed"


def _registered_sources() -> set[str]:
    return {str(spec.key) for spec in REGISTRY}


def _read_bindings(connection) -> dict[str, dict]:
    """Read only safe connection state; never touch credential references."""
    try:
        rows = _rows(
            connection.execute(
                "SELECT value FROM app_meta WHERE key=?", ("connections.v1",)
            )
        )
    except Exception:
        return {}
    if not rows:
        return {}
    raw = rows[0]
    try:
        value = raw["value"] if isinstance(raw, dict) else raw[0]
        state = json.loads(value) if value else {}
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
    bindings = state.get("bindings") if isinstance(state, dict) else None
    if not isinstance(bindings, dict):
        return {}
    return {
        _safe_source_key(source): value
        for source, value in bindings.items()
        if isinstance(value, dict)
    }


def _configured_state(source: str, item: dict, bindings: dict[str, dict], observed_success=False) -> str:
    binding = bindings.get(source)
    if isinstance(binding, dict):
        if binding.get("enabled") is False:
            return "disabled"
        if binding.get("enabled") is True:
            return "configured"
    status = str(item.get("status") or "never").lower()
    reason = str(item.get("error") or "").lower()
    if status == "skipped" and any(marker in reason for marker in _MISSING_MARKERS):
        return "missing"
    # A recorded terminal attempt proves that this lane was selected for
    # collection even when it is experimental and disabled by default.
    if status in {"running", "success", "partial", "failed"} or observed_success:
        return "configured"
    spec = REGISTRY.get(source)
    if spec is not None and getattr(spec, "enabled_by_default", False):
        return "configured"
    if status == "skipped":
        return "missing"
    return "unknown"


def _freshness(
    source: str,
    last_success: datetime | None,
    as_of: datetime,
) -> dict:
    as_of_iso = iso_utc(as_of)
    if last_success is None or last_success > as_of:
        return {"state": "unknown", "ageSeconds": None, "asOf": as_of_iso}
    age = max(0.0, (as_of - last_success).total_seconds())
    threshold = collection_stale_after(source)
    return {
        "state": "recent" if age <= threshold else "stale",
        "ageSeconds": age,
        "asOf": as_of_iso,
    }


def _association_key(tool_keys: tuple[str, ...]) -> str | None:
    canonical = {canonical_tool_key(key) for key in tool_keys if key}
    if len(canonical) == 1:
        return next(iter(canonical))
    return None


def _usage_capability_note(source: str) -> str:
    spec = REGISTRY.get(source)
    if spec is None:
        return ""
    exactness = spec.exactness_for("usage")
    if exactness in {"partial", "derived", "unavailable"}:
        return (
            f" Source usage capability is {exactness}; pricing limitations are "
            "separate from collection failure."
        )
    return ""


def load_source_health(connection, as_of: datetime | None = None) -> list[dict]:
    """Load one safe, explicit collection-health row per registered source.

    The latest attempt and historical successful timestamp are separate bounded
    aggregate reads.  A newer failure therefore cannot erase the previous
    successful import, while no report GET needs to read every ingest attempt.
    """
    captured = _captured_as_of(as_of)
    latest_rows = _rows(
        connection.execute(
            """
            SELECT r.source, r.started_at, r.finished_at, r.status,
                   r.events_written, r.error
            FROM ingest_runs AS r
            JOIN (
                SELECT source, MAX(id) AS latest_id
                FROM ingest_runs
                GROUP BY source
            ) AS latest ON latest.source = r.source AND latest.latest_id = r.id
            ORDER BY r.source
            """
        )
    )
    success_rows = _rows(
        connection.execute(
            """
            SELECT source, MAX(finished_at) AS last_success_at
            FROM ingest_runs
            WHERE status IN ('success', 'partial') AND finished_at IS NOT NULL
            GROUP BY source
            ORDER BY source
            """
        )
    )
    bindings = _read_bindings(connection)

    latest_by_source: dict[str, dict] = {}
    for row in latest_rows:
        item = dict(row)
        source = _safe_source_key(item.get("source"))
        if source:
            latest_by_source[source] = item
    success_by_source: dict[str, Any] = {}
    for row in success_rows:
        item = dict(row)
        source = _safe_source_key(item.get("source"))
        if source:
            success_by_source[source] = (
                item.get("last_success_at")
                or item.get("lastSuccessAt")
                or item.get("lastSuccess")
            )

    source_names = _registered_sources() | set(latest_by_source) | set(success_by_source) | set(bindings)
    health: list[dict] = []
    for source in sorted(source_names):
        item = latest_by_source.get(source, {})
        status = str(item.get("status") or "never").lower()
        if status not in {"running", "success", "partial", "failed", "skipped", "never"}:
            status = "unknown"
        last_attempt = _health_time(item.get("finished_at") or item.get("started_at"))
        last_success = _health_time(success_by_source.get(source))
        # Fixtures and older databases may not have a separate success query
        # row; only a terminal successful latest attempt may fill that gap.
        if last_success is None and status in _SUCCESS_STATUSES:
            last_success = _health_time(item.get("finished_at"))
        tool_keys = _source_tool_keys(source, item)
        configured = _configured_state(source, item, bindings, last_success is not None)
        reason = _safe_health_reason(item.get("error"), source)
        if configured == "disabled" and not reason:
            reason = "This source is disabled in BURNRATE connection settings."
        if configured == "missing" and not reason:
            reason = "This source is not configured for collection."
        # Admin/charge-only lanes are visible at report scope but cannot prove
        # subscription collection for a plan group.
        comparable = source not in {"openai_admin", "anthropic_admin", "cursor_admin", "openrouter"}
        freshness = _freshness(source, last_success, captured)
        capability_note = _usage_capability_note(source)
        coverage = (
            "Current attempt was partial; historical capture is unverified."
            if status == "partial"
            else "Current attempt only; historical capture is unverified."
            if status == "success"
            else "No successful current capture; historical capture is unverified."
            if status in _FAILURE_STATUSES or status == "skipped"
            else "No collection attempt observed; historical capture is unverified."
            if status == "never"
            else "Collection history is unavailable for this source."
        ) + capability_note
        health.append(
            {
                "source": source,
                "sourceLabel": _source_label(source),
                "toolKeys": list(tool_keys),
                "associationKey": _association_key(tool_keys),
                "relevant": comparable and bool(tool_keys),
                "configuredState": configured,
                "status": status,
                "lastAttemptAt": _health_iso(last_attempt),
                "lastSuccessAt": _health_iso(last_success),
                "freshness": freshness,
                "coverage": coverage,
                "eventsWritten": int(item.get("events_written") or 0),
                "reason": reason,
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


def _stored_resets_by_provider(connection) -> dict[str, list[tuple[str, str | None]]]:
    """Latest stored quota reset per provider and limit. Pay-as-you-go rows are skipped."""
    try:
        rows = _rows(
            connection.execute(
                "SELECT provider_key, limit_key, resets_at, is_payg, polled_at, id "
                "FROM quotas ORDER BY polled_at DESC, id DESC"
            )
        )
    except Exception:
        return {}
    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[tuple[str, str | None]]] = {}
    for row in rows:
        provider = str(row["provider_key"])
        limit_key = str(row["limit_key"])
        if row["is_payg"]:
            continue
        identity = (provider, limit_key)
        if identity in seen:
            continue
        seen.add(identity)
        grouped.setdefault(provider, []).append((limit_key, row["resets_at"]))
    return grouped


def _quota_reset_fingerprint(connection) -> tuple:
    stored = _stored_resets_by_provider(connection)
    return tuple(
        (provider, limit_key, resets_at)
        for provider in sorted(stored)
        for limit_key, resets_at in stored[provider]
    )


def _resets_for_canonical(
    stored: dict[str, list[tuple[str, str | None]]], canonical: str
) -> list[tuple[str, str | None]]:
    keys = {canonical, *event_tool_keys_for(canonical)}
    limits: list[tuple[str, str | None]] = []
    for key in keys:
        limits.extend(stored.get(key, []))
    return limits


def _unavailable_cycle_row(group) -> dict:
    reason = (
        "Cycle comparison is unavailable for this plan because no future "
        "quota reset is stored."
    )
    plan_rows = [
        {
            "planId": plan.get("plan_id"),
            "name": plan.get("name"),
            "toolKey": plan.get("tool_key"),
        }
        for plan in group.plans
    ]
    return {
        "groupId": group.group_id,
        "name": group.name,
        "toolKeys": list(group.tool_keys),
        "planIds": list(group.plan_ids),
        "plans": plan_rows,
        "configuredCostUsd": None,
        "usageValueUsd": None,
        "usageValueStatus": "unavailable",
        "multiple": None,
        "multipleBasis": "unavailable",
        "multipleReason": reason,
        "cycleAvailable": False,
        "cycleReason": reason,
        "pricingCoverage": {
            "status": "none",
            "label": "Cycle comparison unavailable",
            "unpricedModels": [],
        },
        "collectionEvidence": {"status": "unknown", "label": "Not used for this cycle"},
        "attribution": {
            "basis": "configured_tool_association",
            "label": "Configured tool association",
        },
        "explanation": {
            "period": {"note": reason},
            "configuredCost": {"note": reason},
            "multiple": {
                "note": (
                    "Configured cost and recorded usage are not compared without "
                    "a stored quota reset. This is not a calendar-month fallback."
                )
            },
        },
    }


def _cycle_label(limit_key: str) -> str:
    if limit_key in {"5h", "five_hour", "gemini-5h", "3p-5h"}:
        return "5-hour session window"
    return "Weekly quota window"


def _annotate_cycle(row: dict, cycle) -> dict:
    row["cycleAvailable"] = True
    row["cycleLimitKey"] = cycle.limit_key
    row["cycleResetsAt"] = iso_utc(cycle.resets_at)
    row["cycleFromUtc"] = iso_utc(cycle.start_utc)
    row["cycleToUtc"] = iso_utc(cycle.end_utc)
    row["cycleLabel"] = _cycle_label(cycle.limit_key)
    period = row.setdefault("explanation", {}).setdefault("period", {})
    period["note"] = (
        f"{row['cycleLabel']} {row['cycleFromUtc']} – {row['cycleToUtc']} "
        f"(stored reset {row['cycleResetsAt']}). "
        "Weekly is used when both a weekly reset and a 5-hour reset are stored."
    )
    return row


def _assemble_cycle_report(
    *,
    connection,
    pricing: Any,
    as_of_utc: datetime,
    zone: ZoneInfo,
    plans: list[dict],
    sources_data: list[dict],
    assemble_group_valuation,
    assemble_plans_value_report,
    assemble_unassigned_summary,
) -> dict:
    # Identity of current plans, not their calendar-month cost. Groups with
    # no future reset stay in the report and say the cycle is unavailable.
    identity = build_groups(
        plans,
        as_of_utc - timedelta(seconds=1),
        as_of_utc + timedelta(seconds=1),
        zone,
    )
    plans_by_tool: dict[str, list[dict]] = {}
    for plan in plans:
        tool_key = str(plan.get("tool_key") or "").strip().lower()
        if not tool_key:
            continue
        plans_by_tool.setdefault(canonical_tool_key(tool_key), []).append(plan)
    stored = _stored_resets_by_provider(connection)
    real_groups = []
    cycles = {}
    groups_data = []
    for shell in identity:
        canonical = shell.group_id.removeprefix("tool:")
        cycle = cycle_window_for_limits(_resets_for_canonical(stored, canonical), as_of_utc)
        built = (
            build_groups(plans_by_tool.get(canonical, []), cycle.start_utc, cycle.end_utc, zone)
            if cycle is not None
            else []
        )
        if cycle is None or not built:
            groups_data.append(_unavailable_cycle_row(shell))
            continue
        group = built[0]
        real_groups.append(group)
        cycles[group.group_id] = cycle
        groups_data.append(group)

    if cycles:
        start_utc = min(cycle.start_utc for cycle in cycles.values())
        end_utc = max(cycle.end_utc for cycle in cycles.values())
        priced = load_priced_usage_events(connection, start_utc, end_utc)
        unpriced = load_unpriced_usage_events(connection, start_utc, end_utc)
    else:
        start_utc = end_utc = as_of_utc
        priced = []
        unpriced = []
    group_priced, unassigned_priced = assign_events_to_groups(priced, real_groups)
    group_unpriced, unassigned_unpriced = assign_events_to_groups(unpriced, real_groups)
    covered = {tool for group in real_groups for tool in group.tool_keys}
    for row in groups_data:
        if isinstance(row, dict) and row.get("cycleAvailable") is False:
            covered.update(row.get("toolKeys") or [])
    unassigned_priced = [
        event for event in unassigned_priced
        if str(event.get("tool_key") or "").strip().lower() not in covered
    ]
    unassigned_unpriced = [
        event for event in unassigned_unpriced
        if str(event.get("tool_key") or "").strip().lower() not in covered
    ]
    valued = []
    for row in groups_data:
        if not isinstance(row, dict):
            valued.append(
                _annotate_cycle(
                    assemble_group_valuation(
                        row,
                        group_priced.get(row.group_id, []),
                        group_unpriced.get(row.group_id, []),
                        sources_data,
                        pricing=pricing,
                        as_of=as_of_utc,
                    ),
                    cycles[row.group_id],
                )
            )
        else:
            valued.append(row)
    report = assemble_plans_value_report(
        "this_cycle",
        as_of_utc,
        zone,
        valued,
        assemble_unassigned_summary(unassigned_priced, unassigned_unpriced, pricing=pricing),
        sources_data,
        start_utc,
        end_utc,
    )
    report["periodLabel"] = "Each plan's current quota window"
    return report


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
    today = as_of_utc.astimezone(zone).date()
    plans = _flatten_plans_for_groups(connection, today)
    sources_data = load_source_health(connection, as_of=as_of_utc)
    if period == "this_cycle":
        return _assemble_cycle_report(
            connection=connection,
            pricing=pricing,
            as_of_utc=as_of_utc,
            zone=zone,
            plans=plans,
            sources_data=sources_data,
            assemble_group_valuation=assemble_group_valuation,
            assemble_plans_value_report=assemble_plans_value_report,
            assemble_unassigned_summary=assemble_unassigned_summary,
        )

    start_utc, end_utc, _local_start, _local_end = resolve_period_bounds(
        period, as_of_utc, zone
    )
    groups = build_groups(plans, start_utc, end_utc, zone)

    priced = load_priced_usage_events(connection, start_utc, end_utc)
    unpriced = load_unpriced_usage_events(connection, start_utc, end_utc)
    group_priced, unassigned_priced = assign_events_to_groups(priced, groups)
    group_unpriced, unassigned_unpriced = assign_events_to_groups(unpriced, groups)

    groups_data = [
        assemble_group_valuation(
            group,
            group_priced.get(group.group_id, []),
            group_unpriced.get(group.group_id, []),
            sources_data,
            pricing=pricing,
            as_of=as_of_utc,
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
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    as_of_utc = (as_of or datetime.now(UTC)).astimezone(UTC)
    zone = ZoneInfo(settings.timezone)
    if period == "this_cycle":
        # Longest recognized quota window is 7 days, so this bounds the fingerprint.
        fingerprint = (
            _usage_fingerprint(connection, as_of_utc - timedelta(days=7), as_of_utc),
            _quota_reset_fingerprint(connection),
        )
    else:
        start_utc, end_utc, _, _ = resolve_period_bounds(period, as_of_utc, zone)
        fingerprint = _usage_fingerprint(connection, start_utc, end_utc)
    key = (
        _connection_identity(connection),
        period,
        _as_of_slot(as_of_utc),
        settings.timezone,
        id(pricing),
        _plan_revision(connection),
        fingerprint,
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
