"""OpenCode local session-aggregate ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.local\\share\\opencode\\opencode.db`` table ``session``,
    opened with SQLite ``mode=ro`` + ``PRAGMA query_only=ON``.

Official vs observed:
  Observed local session aggregates (not per-turn). ``cost`` is kept only when
  the session reports a positive value. Sessions with
  ``providerID=traycer-openrouter`` are skipped; Traycer is the per-turn
  authority for that mirror.

Not stored:
  Prompts, messages, titles, or full filesystem paths. Project is the
  directory name of ``directory`` / ``path`` / ``project_id``. Token columns
  and optional session cost only.

Live-session rule:
  This adapter has no live-session collector. Opening the OpenCode editor or
  an idle shell is not live.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    number,
    optional_number,
    parse_millis,
    positive_cost,
    sqlite_read_only,
)
from spend_app.pricing import PricingEngine


SOURCE = "opencode_local"
# Traycer also writes these sessions into OpenCode. Per-turn authority is the
# Traycer chat projection, so the session aggregate would double-count.
MIRRORED_PROVIDERS = {"traycer-openrouter"}


def canonical_model(model: str) -> str:
    return f"opencode:{model.strip().lower()}"


def parse_database(path: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    try:
        connection = sqlite_read_only(path)
    except sqlite3.DatabaseError:
        return rows
    try:
        query = """
            SELECT id,project_id,directory,path,model,cost,tokens_input,tokens_output,
                   tokens_reasoning,tokens_cache_read,tokens_cache_write,time_updated
            FROM session
            WHERE COALESCE(tokens_input,0)+COALESCE(tokens_output,0)+
                  COALESCE(tokens_cache_read,0)+COALESCE(tokens_cache_write,0)>0
        """
        try:
            cursor = connection.execute(query)
        except sqlite3.OperationalError:
            return rows
        for row in cursor:
            (
                session_id,
                project_id,
                directory,
                project_path,
                model_json,
                cost,
                input_value,
                output_value,
                reasoning_value,
                cache_read_value,
                cache_write_value,
                updated_at,
            ) = row
            try:
                model_info = json.loads(model_json) if model_json else {}
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(model_info, dict):
                continue
            provider_id = str(model_info.get("providerID") or "unknown")
            if provider_id in MIRRORED_PROVIDERS:
                continue
            occurred_at = parse_millis(updated_at)
            if occurred_at is None:
                continue
            model_id = str(model_info.get("id") or "unknown")
            fresh = number(input_value)
            cached = number(cache_read_value)
            writes = number(cache_write_value)
            project_source = directory or project_path or project_id
            project = Path(project_source).name if isinstance(project_source, str) else None
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="opencode",
                    model_key=canonical_model(model_id),
                    occurred_at=occurred_at,
                    session_id=str(session_id),
                    project=project,
                    input_tokens=fresh + cached,
                    cached_input_tokens=cached,
                    cache_write_tokens=writes,
                    cache_write_1h_tokens=0,
                    output_tokens=number(output_value),
                    reasoning_tokens=optional_number(reasoning_value),
                    cost_usd=positive_cost(cost),
                    raw_id=stable_id("opencode-local", session_id, provider_id),
                )
            )
    finally:
        connection.close()
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine, source_database: Path) -> dict:
    files = int(source_database.is_file())
    try:
        usage_rows = parse_database(source_database) if files else []
    except sqlite3.DatabaseError:
        usage_rows = []
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    return {**result, "files": files}
