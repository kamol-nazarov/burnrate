"""Cursor local SDK-agent history from read-only agent-store SQLite.

Authoritative only before the 2026-09-02 usage-service cutover. Current events
come from the experimental undocumented DashboardService in ``cursor_usage``.
"""

from __future__ import annotations

import glob
from spend_app.connection_paths import adapter_files
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


def parse_database(path: Path, *, observations=False) -> list[UsageRow]:
    rows: list[UsageRow] = []
    connection = sqlite_read_only(path)
    try:
        query = """
            SELECT run_id,agent_id,model,usage_json,
                   COALESCE(finished_at,updated_at,created_at)
            FROM runs WHERE usage_json IS NOT NULL
        """
        cursor = connection.execute(query)
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
            row = UsageRow(
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
            from dataclasses import replace
            from spend_app.adapters.event_identity import Observation
            logical = stable_id("cursor-sdk", agent_id, run_id)
            observed = Observation(replace(row, raw_id=logical), (row.raw_id,), occurred_at.timestamp(), "cursor-sdk")
            rows.append(observed if observations else observed.row)
    finally:
        connection.close()
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine, database_glob: str) -> dict:
    usage_rows, observations, issues = [], [], []
    files = 0
    for file_name in sorted(adapter_files(database_glob)):
        path = Path(file_name)
        files += 1
        try:
            for observation in parse_database(path, observations=True):
                row = observation.row
                if row.occurred_at >= USAGE_SERVICE_AUTHORITY_START:
                    issues.append("cursor_sdk_after_legacy_authority_cutover")
                    continue
                observations.append(observation)
                usage_rows.append(row)
        except (OSError, ValueError, sqlite3.DatabaseError):
            issues.append("cursor_sdk_source_unavailable_or_incompatible")
    from spend_app.adapters.event_identity import reconcile
    result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=usage_rows,
                          prepare=lambda connection, _: reconcile(connection, observations, issues), issues=issues)
    return {**result, "files": files, "issues": sorted(set(issues))}
