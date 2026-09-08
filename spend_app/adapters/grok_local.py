"""Observed Grok unified logs plus explicitly selected native usage metadata.

Historical models are session/process-generation scoped. Changed logs replay
with stable 0.3.0 IDs; only committed reads are cached. Native update/signals
reconciliation and coarse remainders live in grok_native. No token estimates,
current-model guesses, credentials or inference are used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import number, optional_number, parse_iso_time, sqlite_read_only
from spend_app.pricing import PricingEngine


SOURCE = "grok_local"
TOOL_KEY = "grok"
MODEL_PREFIX = "supergrok:"
DEFAULT_MODEL = "grok-4.6"
TURN_MESSAGE = "shell.turn.inference_done"
MODEL_MESSAGE = "model changed"
SESSION_MESSAGE = "session created"

# Reader state per log path: byte offset already persisted plus the
# session -> model / project maps learned from lines before that offset.
_STATE: dict[str, dict] = {}


def reset_state() -> None:
    _STATE.clear()


def canonical_model(model: str | None) -> str:
    return MODEL_PREFIX + (str(model or DEFAULT_MODEL).strip().lower() or DEFAULT_MODEL)


def _fresh_state() -> dict:
    return {"offset": 0, "models": {}, "cwds": {}, "generations": {}}


def parse_log(path: Path, state: dict | None = None) -> tuple[list[UsageRow], dict]:
    """Read appended complete lines after checking generation/prefix anchors."""
    from spend_app.connection_paths import confined, file_signature
    from spend_app.adapters.grok_records import reduce_records
    signature = file_signature(path)
    if state and tuple(state.get("signature", ())) == signature:
        return [], dict(state, confirmed=state.get("accepted", 0))
    import hashlib
    stat = path.stat()
    generation = (stat.st_dev, stat.st_ino, getattr(stat, 'st_birthtime_ns', None))
    records, issues = [], []
    with confined(path).open("rb") as handle:
        def anchors(offset):
            handle.seek(0)
            head = handle.read(min(4096, offset))
            handle.seek(max(0, offset-4096))
            tail = handle.read(min(4096, offset))
            return hashlib.sha256(head+tail).hexdigest()
        offset = state.get('offset', 0) if state else 0
        append = bool(state and offset and stat.st_size >= offset and tuple(state.get('generation', ())) == generation
                      and state.get('anchors') == anchors(offset))
        offset = offset if append else 0
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError):
                issues.append("malformed_grok_metadata")
        prefix = anchors(offset)
    rows, next_state, parsed_issues = reduce_records(records, state if append else None)
    confirmed = state.get('accepted', 0) if append else 0
    next_state.update(signature=signature, accepted=confirmed + len(rows), confirmed=confirmed,
                      offset=offset, generation=generation, anchors=prefix,
                      issues=issues + parsed_issues)
    return rows, next_state


def coverage_start(path: Path, database_path: Path | None = None) -> datetime | None:
    """Earliest Grok-local coverage instant.

    The CLI truncates ``unified.jsonl``, so the first remaining line can move
    forward. Traycer skip uses the minimum of that head timestamp and any
    already-persisted ``grok_local`` ``occurred_at``, so rotation cannot
    re-open history Traycer already lost to grok_local.
    """
    file_start = None
    try:
        from spend_app.connection_paths import confined
        fd = os.open(confined(path), os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            first = handle.readline()
        record = json.loads(first.decode("utf-8", errors="replace"))
        if isinstance(record, dict):
            file_start = parse_iso_time(record.get("ts"))
    except (OSError, ValueError):
        file_start = None
    db_start = None
    db_file = Path(database_path) if database_path is not None else None
    if db_file is not None and db_file.is_file():
        try:
            connection = sqlite_read_only(db_file)
            try:
                row = connection.execute(
                    "SELECT MIN(occurred_at) FROM (SELECT occurred_at FROM usage_events WHERE source=? UNION ALL SELECT occurred_at FROM unpriced_usage_events WHERE source=?)",
                    (SOURCE, SOURCE),
                ).fetchone()
            finally:
                connection.close()
            if row and row[0]:
                db_start = parse_iso_time(row[0])
        except (OSError, sqlite3.Error):
            db_start = None
    candidates = [stamp for stamp in (file_start, db_start) if stamp is not None]
    return min(candidates) if candidates else None


def ingest(*, database_path: Path, pricing: PricingEngine, log_path: Path) -> dict:
    from spend_app.connection_paths import cache_identity
    from spend_app.adapters.grok_native import read_source, reconcile
    if log_path.is_dir() or log_path.name in {"updates.jsonl", "signals.json"}:
        rows, issues, files = read_source(log_path)
        result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=rows,
                              prepare=lambda connection, _: reconcile(connection, rows, issues), issues=issues)
        return {**result, "files": files, "issues": sorted(set(issues))}
    key = cache_identity(database_path, log_path, "grok-generation-v1")
    if not log_path.is_file():
        from spend_app.adapters.common import failed_result
        return failed_result(database_path=database_path, source=SOURCE, reason="Grok usage log is missing or moved. Recheck the configured path.")
    usage_rows, next_state = parse_log(log_path, _STATE.get(key))
    issues = next_state.get("issues", [])
    result = persist_rows(
        database_path=database_path, pricing=pricing, source=SOURCE,
        usage_rows=usage_rows, issues=issues,
        prepare=lambda connection, rows: reconcile(connection, rows, issues),
    )
    if not issues:
        _STATE[key] = next_state
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + next_state.get("confirmed", 0)
    result["issues"] = sorted(set(issues))
    return {**result, "files": 1, "rows": len(usage_rows)}
