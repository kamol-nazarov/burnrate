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


def parse_database(path: Path) -> tuple[list[UsageRow], int]:
    rows: list[UsageRow] = []
    skipped_non_plan = 0
    connection = sqlite_read_only(path)
    try:
        try:
            cursor = connection.execute(
                """
                SELECT m.id,m.session_id,m.provider_id,m.model_id,m.completed_at,
                       m.input_tokens,m.output_tokens,m.reasoning_tokens,
                       m.cache_creation_input_tokens,m.cache_read_input_tokens,
                       m.raw_usage_json,s.directory
                FROM model_usage AS m
                LEFT JOIN session AS s ON s.id=m.session_id
                WHERE m.status='completed'
                  AND COALESCE(m.input_tokens,0)+COALESCE(m.output_tokens,0)+
                      COALESCE(m.cache_creation_input_tokens,0)+
                      COALESCE(m.cache_read_input_tokens,0)>0
                """
            )
        except sqlite3.OperationalError:
            return rows, skipped_non_plan
        for row in cursor:
            (
                usage_id,
                session_id,
                provider_id,
                model_id,
                completed_at,
                input_value,
                output_value,
                reasoning_value,
                write_value,
                cache_value,
                raw_usage,
                directory,
            ) = row
            if str(provider_id or "") not in PLAN_PROVIDERS:
                skipped_non_plan += 1
                continue
            occurred_at = parse_millis(completed_at)
            if occurred_at is None:
                continue
            total_input = max(0, int(input_value or 0))
            cached = min(max(0, int(cache_value or 0)), total_input)
            writes = min(max(0, int(write_value or 0)), max(0, total_input - cached))
            output = max(0, int(output_value or 0))
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key=TOOL_KEY,
                    model_key=canonical_model(model_id),
                    occurred_at=occurred_at,
                    session_id=str(session_id or "") or None,
                    project=_project_name(directory),
                    input_tokens=max(0, total_input - writes),
                    cached_input_tokens=cached,
                    cache_write_tokens=writes,
                    cache_write_1h_tokens=0,
                    output_tokens=output,
                    reasoning_tokens=_reasoning_tokens(reasoning_value, raw_usage),
                    cost_usd=None,
                    raw_id=stable_id("zcode-local", usage_id),
                )
            )
    finally:
        connection.close()
    return rows, skipped_non_plan


def ingest(*, database_path: Path, pricing: PricingEngine, source_database: Path) -> dict:
    files = int(source_database.is_file())
    try:
        usage_rows, skipped_non_plan = parse_database(source_database) if files else ([], 0)
    except sqlite3.DatabaseError:
        usage_rows, skipped_non_plan = [], 0
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    return {**result, "files": files, "nonPlanCallsSkipped": skipped_non_plan}
