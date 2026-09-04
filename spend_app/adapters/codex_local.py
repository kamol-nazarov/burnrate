"""Codex Desktop local usage ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.codex\\sessions\\**\\*.jsonl`` — ``token_count`` telemetry
  - Live cards (activity poll, not this ingest): ``state_5.sqlite`` and
    ``thread_history_1.sqlite``, opened with SQLite ``mode=ro`` +
    ``PRAGMA query_only=ON``

Official vs observed:
  Official local Codex Desktop session logs. JSONL fields are observed from
  Codex Desktop. Only ``originator = "Codex Desktop"`` is ingested so
  Traycer-launched sessions are not double-counted.

Not stored:
  Prompts, responses, tool calls, or full filesystem paths. Project is the
  directory name of ``cwd`` only. Persisted fields are tokens, model, session
  id, timestamps, and ``raw_id``.

Live-session rule:
  A thread is live only while a turn is ``inprogress``/``running``,
  ``completed_at`` is NULL, the thread is not archived, and ``updated_at`` is
  within 6 hours. An idle Codex window is not live.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.adapters.local_common import open_text_read_only
from spend_app.db import connect, initialize, upsert_session
from spend_app.pricing import PricingEngine


SOURCE = "codex_local"
DESKTOP_ORIGINATOR = "Codex Desktop"
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}


def reset_file_cache() -> None:
    _FILE_SIGNATURES.clear()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ParsedEvent:
    session_id: str
    project: str | None
    model_key: str
    occurred_at: datetime
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    raw_id: str


def parse_file(path: Path) -> tuple[dict, list[ParsedEvent]]:
    session: dict = {}
    current_model: str | None = None
    events: list[ParsedEvent] = []
    token_index = 0
    with open_text_read_only(path) as handle:
        for line in handle:
            try:
                outer = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = outer.get("payload")
            if not isinstance(payload, dict):
                continue
            if outer.get("type") == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or path.stem)
                cwd = payload.get("cwd")
                session = {
                    "id": session_id,
                    "started_at": _parse_time(str(payload.get("timestamp") or outer.get("timestamp"))),
                    "project": Path(cwd).name if isinstance(cwd, str) and cwd else None,
                    "originator": payload.get("originator"),
                }
                continue
            if outer.get("type") == "turn_context" and payload.get("model"):
                current_model = str(payload["model"])
                continue
            if outer.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
                continue
            if not session or current_model is None:
                continue
            usage = info["last_token_usage"]
            occurred_at = _parse_time(str(outer["timestamp"]))
            raw_id = f"codex-local:{session['id']}:{outer['timestamp']}:{token_index}"
            token_index += 1
            events.append(
                ParsedEvent(
                    session_id=session["id"],
                    project=session["project"],
                    model_key=current_model,
                    occurred_at=occurred_at,
                    input_tokens=max(0, int(usage.get("input_tokens") or 0)),
                    cached_input_tokens=max(0, int(usage.get("cached_input_tokens") or 0)),
                    cache_write_tokens=max(0, int(usage.get("cache_write_input_tokens") or 0)),
                    output_tokens=max(0, int(usage.get("output_tokens") or 0)),
                    reasoning_tokens=(
                        None
                        if usage.get("reasoning_output_tokens") is None
                        else max(0, int(usage.get("reasoning_output_tokens") or 0))
                    ),
                    raw_id=raw_id,
                )
            )
    return session, events


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    session_glob: str,
) -> dict:
    initialize(database_path)
    parsed_files = 0
    parsed_events = 0
    skipped_files = 0
    skipped_originator = 0
    usage_rows: list[UsageRow] = []
    session_rows: list[dict] = []
    pending_signatures: list[tuple[str, tuple[int, int]]] = []
    for file_name in sorted(glob.glob(session_glob, recursive=True)):
        path = Path(file_name)
        try:
            stat = path.stat()
        except OSError:
            continue
        cache_key = str(path.resolve())
        signature = (stat.st_size, stat.st_mtime_ns)
        if _FILE_SIGNATURES.get(cache_key) == signature:
            skipped_files += 1
            continue
        session, events = parse_file(path)
        if not session:
            _FILE_SIGNATURES[cache_key] = signature
            continue
        if session.get("originator") != DESKTOP_ORIGINATOR:
            skipped_originator += 1
            _FILE_SIGNATURES[cache_key] = signature
            continue
        parsed_files += 1
        parsed_events += len(events)
        latest_time: datetime | None = None
        latest_model: str | None = None
        for parsed in events:
            usage_rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="codex",
                    model_key=parsed.model_key,
                    occurred_at=parsed.occurred_at,
                    session_id=parsed.session_id,
                    project=parsed.project,
                    input_tokens=parsed.input_tokens,
                    cached_input_tokens=parsed.cached_input_tokens,
                    cache_write_tokens=parsed.cache_write_tokens,
                    cache_write_1h_tokens=0,
                    output_tokens=parsed.output_tokens,
                    reasoning_tokens=parsed.reasoning_tokens,
                    cost_usd=None,
                    raw_id=parsed.raw_id,
                )
            )
            latest_time = parsed.occurred_at
            latest_model = parsed.model_key
        if latest_time and latest_model:
            session_rows.append(
                {
                    "session_id": session["id"],
                    "project": session.get("project"),
                    "started_at": _iso(session["started_at"]),
                    "ended_at": _iso(latest_time),
                    "model_key": latest_model,
                }
            )
        pending_signatures.append((cache_key, signature))
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    with connect(database_path) as connection:
        for session in session_rows:
            upsert_session(
                connection,
                session_id=session["session_id"],
                tool_key="codex",
                project=session["project"],
                started_at=session["started_at"],
                ended_at=session["ended_at"],
                model_key=session["model_key"],
            )
    for cache_key, signature in pending_signatures:
        _FILE_SIGNATURES[cache_key] = signature
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["filesSkippedOriginator"] = skipped_originator
    result["eventsSeen"] = parsed_events
    return result
