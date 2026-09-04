"""Traycer local usage from read-only ``chat.db`` projections.

Only Grok and OpenRouter harnesses are ingested here. Quota and live-agent
signals that spawn the Traycer CLI (``agent profile-rate-limits`` /
``agent list``) live in ``spend_app.limits`` and are experimental undocumented
interfaces.
"""

from __future__ import annotations

import glob
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    classify_traycer_usage,
    number,
    positive_cost,
    sqlite_read_only,
)
from spend_app.pricing import PricingEngine


SOURCE = "traycer_local"
INGESTED_HARNESSES = {"grok", "openrouter"}
# (resolved chat.db path, chat_id) -> (version, parsed rows). Traycer keeps
# every projection (hundreds of MB of JSON) in one table; re-reading and
# re-parsing all of it every 15 seconds cost more than a CPU-second per cycle.
# A projection only changes when its op-log position (through_seq) advances,
# so unchanged chats reuse their parsed rows without touching the JSON.
# through_seq is stored before the JSON column, so listing it is a few
# milliseconds; updated_at sits after the JSON and would walk every overflow
# page (about 0.1 s per scan on a 1 GB store).
_PROJECTION_CACHE: dict[tuple[str, str], tuple[object, tuple[UsageRow, ...]]] = {}
_VERSION_COLUMNS = ("through_seq", "updated_at")


def reset_projection_cache() -> None:
    _PROJECTION_CACHE.clear()


def projection_index(connection: sqlite3.Connection) -> list[tuple[str, object]] | None:
    """List (chat_id, version) for every projection, cheapest column first.

    Returns None when the table is missing. A version of None means the store
    exposes no change marker (fixtures), so callers re-parse every projection.
    """
    for column in _VERSION_COLUMNS:
        try:
            rows = connection.execute(f"SELECT chat_id,{column} FROM chat_projection").fetchall()
        except sqlite3.OperationalError:
            continue
        return [(str(chat_id), version) for chat_id, version in rows]
    try:
        rows = connection.execute("SELECT chat_id FROM chat_projection").fetchall()
    except sqlite3.OperationalError:
        return None
    return [(str(chat_id), None) for (chat_id,) in rows]


def canonical_model(harness: str, model: str) -> str:
    lowered = model.strip().lower()
    if harness == "grok":
        return f"supergrok:{lowered}"
    if harness == "openrouter":
        lowered = lowered.removeprefix("openrouter:")
        if lowered == "z-ai/glm-5.3-flash":
            lowered = "glm-5.3-flash"
        return f"openrouter:{lowered}"
    return f"{harness}:{lowered}"


def settings_candidates(metadata: dict):
    item = metadata.get("item")
    if isinstance(item, dict):
        yield item
    items = metadata.get("items")
    if isinstance(items, list):
        for candidate in items:
            if isinstance(candidate, dict):
                yield candidate


def parse_projection(*, path: Path, chat_id: str, projection_json: str) -> list[UsageRow]:
    projection = json.loads(projection_json)
    root = projection.get("settings") if isinstance(projection.get("settings"), dict) else {}
    current_harness = root.get("harnessId")
    current_model = root.get("model")
    rows: list[UsageRow] = []
    events = projection.get("events")
    if not isinstance(events, list):
        return rows

    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        body = event.get("body")
        if not isinstance(body, dict):
            continue
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        for item in settings_candidates(metadata):
            settings = item.get("settings")
            if not isinstance(settings, dict):
                continue
            current_harness = settings.get("harnessId") or current_harness
            current_model = settings.get("model") or current_model

        usage = metadata.get("usage")
        if current_harness not in INGESTED_HARNESSES or not isinstance(usage, dict):
            continue
        timestamp_ms = number(body.get("timestamp"))
        if timestamp_ms <= 0:
            continue
        tokens = classify_traycer_usage(usage)
        model_key = canonical_model(str(current_harness), str(current_model or "unknown"))
        raw_id = stable_id(
            "traycer-local",
            str(path.resolve()),
            chat_id,
            event_index,
            timestamp_ms,
            current_harness,
            current_model,
        )
        rows.append(
            UsageRow(
                source=SOURCE,
                tool_key=str(current_harness),
                model_key=model_key,
                occurred_at=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                session_id=f"traycer:{chat_id}",
                project=path.parents[2].name if len(path.parents) > 2 else None,
                input_tokens=tokens.input_tokens,
                cached_input_tokens=tokens.cached_input_tokens,
                cache_write_tokens=tokens.cache_write_tokens,
                cache_write_1h_tokens=0,
                output_tokens=tokens.output_tokens,
                reasoning_tokens=None,
                cost_usd=positive_cost(usage.get("costUsd")),
                raw_id=raw_id,
                unclassified_tokens=tokens.unclassified_tokens,
                telemetry_complete=tokens.telemetry_complete,
            )
        )
    return rows


def parse_database(path: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    try:
        connection = sqlite_read_only(path)
    except sqlite3.DatabaseError:
        return rows
    cache_prefix = str(path.resolve())
    seen: set[tuple[str, str]] = set()
    try:
        index = projection_index(connection)
        if index is None:
            return rows
        for chat_id, version in index:
            key = (cache_prefix, chat_id)
            seen.add(key)
            cached = _PROJECTION_CACHE.get(key) if version is not None else None
            if cached is not None and cached[0] == version:
                rows.extend(cached[1])
                continue
            fetched = connection.execute(
                "SELECT projection_json FROM chat_projection WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if fetched is None or fetched[0] is None:
                continue
            try:
                parsed = parse_projection(
                    path=path,
                    chat_id=chat_id,
                    projection_json=fetched[0],
                )
            except (TypeError, json.JSONDecodeError):
                continue
            rows.extend(parsed)
            if version is not None:
                _PROJECTION_CACHE[key] = (version, tuple(parsed))
    finally:
        connection.close()
    for key in [key for key in _PROJECTION_CACHE if key[0] == cache_prefix and key not in seen]:
        _PROJECTION_CACHE.pop(key, None)
    return rows


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    database_glob: str,
    grok_covered_from: datetime | None = None,
) -> dict:
    usage_rows: list[UsageRow] = []
    files = 0
    mirrored = 0
    for file_name in sorted(glob.glob(database_glob, recursive=True)):
        path = Path(file_name)
        if not path.is_file():
            continue
        files += 1
        try:
            parsed = parse_database(path)
        except sqlite3.DatabaseError:
            continue
        for row in parsed:
            # Traycer runs Grok through the grok CLI, whose own log (grok_local)
            # records every call. Where that log exists, it is the authority;
            # emitting the projection's copy too would count each turn twice.
            if row.tool_key == "grok" and grok_covered_from is not None and row.occurred_at >= grok_covered_from:
                mirrored += 1
                continue
            usage_rows.append(row)
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    return {
        **result,
        "files": files,
        "eventsSeen": len(usage_rows),
        "mirroredGrokEvents": mirrored,
        "unclassifiedEvents": sum(not row.telemetry_complete for row in usage_rows),
    }
