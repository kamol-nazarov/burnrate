"""Cursor local SDK-agent history from read-only agent-store SQLite.

Authoritative only before the 2026-09-02 usage-service cutover. Current events
come from the experimental undocumented DashboardService in ``cursor_usage``.
"""

from __future__ import annotations

import glob
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    number,
    optional_number,
    parse_iso_time,
    positive_cost,
    sqlite_read_only,
)
from spend_app.pricing import PricingEngine


SOURCE = "cursor_local"
USAGE_SERVICE_AUTHORITY_START = datetime(2026, 9, 2, tzinfo=UTC)


def canonical_model(model: str) -> str:
    return f"cursor:{model.strip().lower()}"


def project_name(path: Path) -> str:
    parts = list(path.parts)
    try:
        return parts[parts.index("projects") + 1]
    except (ValueError, IndexError):
        return path.parent.name


def parse_database(path: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    connection = sqlite_read_only(path)
    try:
        query = """
            SELECT run_id,agent_id,model,usage_json,
                   COALESCE(finished_at,updated_at,created_at)
            FROM runs WHERE usage_json IS NOT NULL
        """
        try:
            cursor = connection.execute(query)
        except sqlite3.OperationalError:
            # Agent stores are created lazily; a database without a runs
            # table (yet) simply holds no usage.
            return rows
        for run_id, agent_id, model, usage_json, timestamp_text in cursor:
            occurred_at = parse_iso_time(timestamp_text)
            if occurred_at is None:
                continue
            try:
                usage = json.loads(usage_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(usage, dict):
                continue
            fresh = number(usage.get("inputTokens"))
            cached = number(usage.get("cacheReadTokens"))
            writes = number(usage.get("cacheWriteTokens"))
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="cursor",
                    model_key=canonical_model(str(model or "unknown")),
                    occurred_at=occurred_at,
                    session_id=str(agent_id or run_id or "") or None,
                    project=project_name(path),
                    input_tokens=fresh + cached,
                    cached_input_tokens=cached,
                    cache_write_tokens=writes,
                    cache_write_1h_tokens=0,
                    output_tokens=number(usage.get("outputTokens")),
                    reasoning_tokens=optional_number(usage.get("reasoningTokens")),
                    cost_usd=positive_cost(usage.get("costUsd")),
                    raw_id=stable_id("cursor-local", str(path.resolve()), run_id),
                )
            )
    finally:
        connection.close()
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine, database_glob: str) -> dict:
    usage_rows: list[UsageRow] = []
    files = 0
    for file_name in sorted(glob.glob(database_glob, recursive=True)):
        path = Path(file_name)
        if not path.is_file():
            continue
        files += 1
        try:
            # The signed-in usage service is authoritative from this cutover
            # and covers both native and SDK-agent calls. Keeping the local
            # store only for older history prevents cross-source duplicates.
            usage_rows.extend(
                row for row in parse_database(path)
                if row.occurred_at < USAGE_SERVICE_AUTHORITY_START
            )
        except sqlite3.DatabaseError:
            # Unreadable or mid-checkpoint stores are picked up on the next
            # poll; never fail the whole ingest for one file.
            continue
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    return {**result, "files": files}
