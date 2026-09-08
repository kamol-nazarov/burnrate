"""Exact ZCode Coding Plan usage from its local metrics database.

Files read (read-only):
  - ``%USERPROFILE%\\.zcode\\cli\\db\\db.sqlite`` — ``model_usage`` joined to
    ``session``, opened with SQLite ``mode=ro`` + ``PRAGMA query_only=ON``.
  - Live cards (activity poll, not this ingest): ``turn_usage`` on the same
    database, same read-only connection mode.

Official vs observed:
  Official ZCode metrics tables. One ``model_usage`` row per completed Coding
  Plan request (``builtin:zai-coding-plan`` / ``builtin:bigmodel-coding-plan``).
  Spend is derived published GLM rates, not a credit invoice.

Not stored:
  Prompts, responses, or tool content. Message/part/I/O retention tables are
  never queried. Project is the directory name of ``session.directory`` only.

Live-session rule:
  Live only for ``turn_usage.status='running'`` with ``completed_at`` NULL, a
  non-archived session, and ``time_updated`` within 6 hours. An idle ZCode
  window is not live.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import optional_number, parse_millis, sqlite_read_only
from spend_app.pricing import PricingEngine


SOURCE = "zcode_local"
TOOL_KEY = "zcode"
PLAN_PROVIDERS = frozenset({"builtin:zai-coding-plan", "builtin:bigmodel-coding-plan"})


def canonical_model(model: object) -> str:
    return "zcode:" + (str(model or "unknown").strip().lower() or "unknown")


def _project_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).name or None


def _reasoning_tokens(value: object, raw_usage: object) -> int | None:
    # Older rows default this SQL column to zero even when provider metadata
    # omitted reasoning. Preserve unknown as NULL in that case.
    try:
        payload = json.loads(raw_usage) if isinstance(raw_usage, str) and raw_usage else {}
    except json.JSONDecodeError:
        payload = {}
    reported = isinstance(payload, dict) and any(
        key in payload for key in ("reasoningTokens", "reasoning_tokens", "thoughtsTokenCount")
    )
    count = optional_number(value)
    return count if reported or (count is not None and count > 0) else None


def parse_database(path: Path, issues=None) -> tuple[list[UsageRow], int]:
    from spend_app.adapters.zcode_schema import select_rows, parse_row
    issues = issues if issues is not None else []
    rows, skipped = [], 0
    connection = sqlite_read_only(path)
    try:
        records, modern = select_rows(connection)
        for record in records:
            row, issue = parse_row(record, modern)
            if issue == "non_plan":
                skipped += 1
            elif issue:
                issues.append(issue)
            if row:
                rows.append(row)
    finally:
        connection.close()
    return rows, skipped


def ingest(*, database_path: Path, pricing: PricingEngine, source_database: Path) -> dict:
    from spend_app.connection_paths import source_files, confined
    from spend_app.adapters.zcode_transcript import reduce_records, reconcile
    from spend_app.adapters.event_identity import Observation
    patterns = ("cli/db/db.sqlite", "projects/**/*.jsonl") if source_database.name == ".zcode" else ("**/*.jsonl",)
    usage_rows, transcript_rows, issues = [], [], []
    skipped_non_plan, files = 0, 0
    for file in source_files(source_database, patterns):
        files += 1
        try:
            if file.suffix.lower() == ".jsonl":
                records = []
                with confined(file).open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            issues.append("malformed_zcode_transcript")
                parsed, errors = reduce_records(records)
                transcript_rows.extend(parsed)
                issues.extend(errors)
            else:
                parsed, skipped = parse_database(file, issues)
                usage_rows.extend(parsed)
                skipped_non_plan += skipped
        except (OSError, ValueError, sqlite3.Error):
            issues.append("zcode_source_unavailable_or_incompatible")
    database_sessions = {row.session_id for row in usage_rows}
    for row in transcript_rows:
        if row.session_id in database_sessions and not row.raw_id.startswith("zcode-local:"):
            issues.append("zcode_cross_format_identity_unproven")
            continue
        usage_rows.append(row)
    observations = [Observation(row, (), row.occurred_at.timestamp(), "zcode") for row in usage_rows]
    result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE,
                          usage_rows=usage_rows, issues=issues,
                          prepare=lambda connection, _: reconcile(connection, observations, issues))
    return {**result, "files": files, "nonPlanCallsSkipped": skipped_non_plan, "issues": sorted(set(issues))}
