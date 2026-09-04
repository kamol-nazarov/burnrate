"""Claude Code local transcript ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.claude\\projects\\**\\*.jsonl`` — assistant ``usage``
    envelopes. JSONL is opened ``O_RDONLY``.
  - Live cards (activity poll, not this ingest): tail (~1 MiB) of those same
    project JSONL files.

Official vs observed:
  Official local Claude Code transcripts. Usage fields (including 5m/1h cache
  writes) are taken from the ``message.usage`` object. This module does not
  implement Claude OAuth refresh or write ``~/.claude/.credentials.json``.

Not stored:
  Prompts, responses, tool input/output, or attachments. Full JSONL lines are
  parsed in memory for usage extraction only. Project is the directory name of
  ``cwd`` (else the parent folder name).

Live-session rule:
  Live only while the trailing turn is non-terminal. A user event without a
  later assistant ``stop_reason`` in ``{end_turn, stop_sequence, max_tokens,
  refusal}`` counts as in-flight. An idle editor with no in-progress turn is
  not live. Message text is not stored.
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


SOURCE = "claude_local"
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}


def reset_file_cache() -> None:
    _FILE_SIGNATURES.clear()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _nonneg_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_nonneg_int(value: object) -> int | None:
    """Missing detail stays NULL; only a reported number becomes a count."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ParsedEvent:
    session_id: str
    project: str | None
    model_key: str
    occurred_at: datetime
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    raw_id: str


def parse_file(path: Path) -> tuple[dict, list[ParsedEvent]]:
    seen_messages: set[str] = set()
    session: dict = {}
    events: list[ParsedEvent] = []
    with open_text_read_only(path) as handle:
        for line in handle:
            try:
                outer = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = outer.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            model = str(message.get("model") or "")
            if not model or model == "<synthetic>":
                continue
            message_id = str(message.get("id") or outer.get("uuid") or "")
            if not message_id or message_id in seen_messages:
                continue
            seen_messages.add(message_id)
            timestamp = _parse_time(str(outer["timestamp"]))
            session_id = str(outer.get("sessionId") or path.stem)
            cwd = outer.get("cwd")
            project = Path(cwd).name if isinstance(cwd, str) and cwd else path.parent.name
            usage = message["usage"]
            cache_creation = usage.get("cache_creation")
            cache_creation = cache_creation if isinstance(cache_creation, dict) else {}
            write_5m = _nonneg_int(cache_creation.get("ephemeral_5m_input_tokens"))
            write_1h = _nonneg_int(cache_creation.get("ephemeral_1h_input_tokens"))
            cache_write_total = _nonneg_int(usage.get("cache_creation_input_tokens"))
            if write_5m or write_1h:
                cache_write_tokens = write_5m + write_1h
                cache_write_1h_tokens = write_1h
            else:
                cache_write_tokens = cache_write_total
                cache_write_1h_tokens = min(cache_write_total, write_1h)
            uncached_input = _nonneg_int(usage.get("input_tokens"))
            cached_input = _nonneg_int(usage.get("cache_read_input_tokens"))
            details = usage.get("output_tokens_details")
            details = details if isinstance(details, dict) else {}
            event = ParsedEvent(
                session_id=session_id,
                project=project,
                model_key=model,
                occurred_at=timestamp,
                # Normalized schema: input includes fresh + cache reads; writes remain separate.
                input_tokens=uncached_input + cached_input,
                cached_input_tokens=cached_input,
                cache_write_tokens=cache_write_tokens,
                cache_write_1h_tokens=cache_write_1h_tokens,
                output_tokens=_nonneg_int(usage.get("output_tokens")),
                reasoning_tokens=_optional_nonneg_int(details.get("thinking_tokens")),
                raw_id=f"claude-local:{session_id}:{message_id}",
            )
            events.append(event)
            if not session:
                session = {
                    "id": session_id,
                    "project": project,
                    "started_at": timestamp,
                    "model_key": model,
                }
    return session, events


def ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    initialize(database_path)
    parsed_files = 0
    parsed_events = 0
    duplicates_removed = 0
    skipped_files = 0
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
        raw_usage_lines = 0
        with open_text_read_only(path) as handle:
            for line in handle:
                if '"usage"' in line and '"message"' in line:
                    raw_usage_lines += 1
        session, events = parse_file(path)
        if not session:
            _FILE_SIGNATURES[cache_key] = signature
            continue
        parsed_files += 1
        duplicates_removed += max(0, raw_usage_lines - len(events))
        parsed_events += len(events)
        latest_time: datetime | None = None
        latest_model: str | None = None
        for parsed in events:
            usage_rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="claude-code",
                    model_key=parsed.model_key,
                    occurred_at=parsed.occurred_at,
                    session_id=parsed.session_id,
                    project=parsed.project,
                    input_tokens=parsed.input_tokens,
                    cached_input_tokens=parsed.cached_input_tokens,
                    cache_write_tokens=parsed.cache_write_tokens,
                    cache_write_1h_tokens=parsed.cache_write_1h_tokens,
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
                    "project": session["project"],
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
                tool_key="claude-code",
                project=session["project"],
                started_at=session["started_at"],
                ended_at=session["ended_at"],
                model_key=session["model_key"],
            )
    for cache_key, signature in pending_signatures:
        _FILE_SIGNATURES[cache_key] = signature
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["eventsSeen"] = parsed_events
    result["duplicatesRemoved"] = duplicates_removed
    return result
