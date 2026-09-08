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
from spend_app.connection_paths import adapter_files
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, failed_result, public_error
from spend_app.source_health import SourceHealth, parse_jsonl_record
from spend_app.timeutil import parse_utc
from spend_app.adapters.local_common import open_text_read_only
from spend_app.db import connect, initialize, upsert_session
from spend_app.pricing import PricingEngine


SOURCE = "claude_local"
from spend_app.adapters.claude_records import PARSER_VERSION
_FILE_COUNTS = {}
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}


def reset_file_cache() -> None:
    _FILE_SIGNATURES.clear()
    _FILE_COUNTS.clear()


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
    observation: object = None


def parse_file(path: Path) -> tuple[dict, list[ParsedEvent]]:
    session, events, _health = parse_file_with_health(path)
    return session, events


def parse_file_with_health(path: Path) -> tuple[dict, list[ParsedEvent], SourceHealth]:
    from spend_app.adapters.claude_records import reduce_records
    health = SourceHealth()
    records = []
    with open_text_read_only(path) as handle:
        for index, line in enumerate(handle, 1):
            record, _ = parse_jsonl_record(line, line_number=index, location="claude", health=health)
            if record is not None:
                records.append(record)
    session, observations = reduce_records(records, health, path.stem)
    events = [ParsedEvent(session_id=o.row.session_id, project=o.row.project, model_key=o.row.model_key,
                          occurred_at=o.row.occurred_at, input_tokens=o.row.input_tokens, cached_input_tokens=o.row.cached_input_tokens,
                          cache_write_tokens=o.row.cache_write_tokens, cache_write_1h_tokens=o.row.cache_write_1h_tokens,
                          output_tokens=o.row.output_tokens, reasoning_tokens=o.row.reasoning_tokens,
                          raw_id=o.row.raw_id, observation=o) for o in observations]
    return session, events, health


def ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    try:
        return _ingest(database_path=database_path, pricing=pricing, session_glob=session_glob)
    except Exception as exc:
        return failed_result(
            database_path=database_path, source=SOURCE, reason=public_error(exc)
        )


def _ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    initialize(database_path)
    parsed_files = 0
    parsed_events = 0
    confirmed_events = 0
    observations = []
    issues = []
    duplicates_removed = 0
    skipped_files = 0
    quarantined = 0
    usage_rows: list[UsageRow] = []
    session_rows: list[dict] = []
    pending_signatures: list[tuple[str, tuple[int, int]]] = []
    for file_name in sorted(adapter_files(session_glob)):
        path = Path(file_name)
        try:
            stat = path.stat()
        except OSError:
            continue
        from spend_app.connection_paths import cache_identity, file_signature
        cache_key = cache_identity(database_path, path, PARSER_VERSION)
        try:
            signature = file_signature(path, stat)
        except OSError:
            issues.append("claude_file_unavailable")
            continue
        if _FILE_SIGNATURES.get(cache_key) == signature:
            skipped_files += 1
            confirmed_events += _FILE_COUNTS.get(cache_key, 0)
            continue
        try:
            session, events, health = parse_file_with_health(path)
        except OSError:
            issues.append("claude_file_unavailable")
            continue
        raw_usage_lines = health.parsed
        if health.partial:
            issues.append("claude_partial_source_metadata")
        quarantined += health.quarantined + health.skipped
        if not session:
            _FILE_SIGNATURES[cache_key] = signature
            continue
        parsed_files += 1
        duplicates_removed += max(0, raw_usage_lines - len(events))
        parsed_events += len(events)
        latest_time: datetime | None = None
        latest_model: str | None = None
        for parsed in events:
            if parsed.observation:
                observations.append(parsed.observation)
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
                    telemetry_complete=parsed.observation.row.telemetry_complete if parsed.observation else True,
                    unclassified_tokens=parsed.observation.row.unclassified_tokens if parsed.observation else 0,
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
        pending_signatures.append((cache_key, signature, len(events)))
    def finalize(connection):
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
    from spend_app.adapters.event_identity import reconcile
    result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=usage_rows,
                          prepare=(lambda connection, rows: reconcile(connection, observations, issues)) if observations else None,
                          finalize=finalize, issues=issues)
    if not issues:
        for cache_key, signature, count in pending_signatures:
            _FILE_SIGNATURES[cache_key] = signature
            _FILE_COUNTS[cache_key] = count
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["eventsSeen"] = parsed_events + confirmed_events
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + confirmed_events
    result["issues"] = sorted(set(issues))
    result["duplicatesRemoved"] = duplicates_removed
    result["quarantined"] = result.get("quarantined", 0) + quarantined
    return result
