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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    number,
    optional_number,
    parse_millis,
    positive_cost,
    sqlite_read_only,
)
from spend_app.db import (
    connect,
    initialize,
    read_opencode_progress,
    record_coverage_gap,
    write_opencode_progress,
)
from spend_app.pricing import PricingEngine


SOURCE = "opencode_local"
# Traycer also writes these sessions into OpenCode. Per-turn authority is the
# Traycer chat projection, so the session aggregate would double-count.
MIRRORED_PROVIDERS = {"traycer-openrouter"}
BASELINE_ISSUE = (
    "Initial cumulative baseline attributed to the first observation; the "
    "store keeps no per-turn timestamps for earlier history."
)
DELTA_ISSUE = (
    "Observed-interval delta from cumulative session counters; timing is the "
    "observation instant and model attribution uses the session's current model."
)


def canonical_model(model: str) -> str:
    return f"opencode:{model.strip().lower()}"


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    provider_id: str
    model_id: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    cost_usd: float | None
    observed_at: datetime


def read_session_snapshots(path: Path) -> list[SessionSnapshot]:
    """Cumulative per-session totals exactly as the store reports them."""
    snapshots: list[SessionSnapshot] = []
    try:
        connection = sqlite_read_only(path)
    except sqlite3.DatabaseError:
        return snapshots
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
            return snapshots
        for row in cursor:
            (
                session_id,
                _project_id,
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
            observed_at = parse_millis(updated_at)
            if observed_at is None:
                continue
            project_source = directory or project_path or None
            snapshots.append(
                SessionSnapshot(
                    session_id=str(session_id),
                    provider_id=provider_id,
                    model_id=str(model_info.get("id") or "unknown"),
                    project=Path(project_source).name if isinstance(project_source, str) else None,
                    input_tokens=number(input_value),
                    cached_input_tokens=number(cache_read_value),
                    cache_write_tokens=number(cache_write_value),
                    output_tokens=number(output_value),
                    reasoning_tokens=optional_number(reasoning_value),
                    cost_usd=positive_cost(cost),
                    observed_at=observed_at,
                )
            )
    finally:
        connection.close()
    return snapshots


def parse_database(path: Path) -> list[UsageRow]:
    """Raw cumulative snapshot reader (compatibility): one row per session.

    Rows carry the store's own cumulative totals at its update time. Ingest
    no longer persists these directly; it derives baseline/delta rows from
    them. Mirrored providers stay excluded.
    """
    return [
        _snapshot_row(snapshot)
        for snapshot in read_session_snapshots(path)
    ]


def _snapshot_row(snapshot: SessionSnapshot) -> UsageRow:
    return UsageRow(
        source=SOURCE,
        tool_key="opencode",
        model_key=canonical_model(snapshot.model_id),
        occurred_at=snapshot.observed_at,
        session_id=snapshot.session_id,
        project=snapshot.project,
        input_tokens=snapshot.input_tokens + snapshot.cached_input_tokens,
        cached_input_tokens=snapshot.cached_input_tokens,
        cache_write_tokens=snapshot.cache_write_tokens,
        cache_write_1h_tokens=0,
        output_tokens=snapshot.output_tokens,
        reasoning_tokens=snapshot.reasoning_tokens,
        cost_usd=snapshot.cost_usd,
        raw_id=stable_id("opencode-local", snapshot.session_id, snapshot.provider_id),
    )


def _counts(snapshot: SessionSnapshot) -> tuple[int, int, int, int]:
    return (
        snapshot.input_tokens,
        snapshot.cached_input_tokens,
        snapshot.cache_write_tokens,
        snapshot.output_tokens,
    )


def plan_rows(
    snapshots: list[SessionSnapshot], previous: dict[str, dict | None]
) -> tuple[list[UsageRow], dict[str, dict], list[tuple[str, str]]]:
    """Baseline/delta rows plus the progress state to commit with them.

    ``previous`` maps ``session_id\\x1fprovider_id`` to the stored progress
    row (or None). Rows keep stable identities so a repeated planning of the
    same snapshot is an idempotent upsert, never a duplicate.
    """
    rows: list[UsageRow] = []
    next_state: dict[str, dict] = {}
    labels: list[tuple[str, str]] = []
    for snapshot in snapshots:
        key = f"{snapshot.session_id}\x1f{snapshot.provider_id}"
        prev = previous.get(key)
        totals = _counts(snapshot)
        if prev is None:
            # Initial unattributed baseline: count once, label the coarseness.
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="opencode",
                    model_key=canonical_model(snapshot.model_id),
                    occurred_at=snapshot.observed_at,
                    session_id=snapshot.session_id,
                    project=snapshot.project,
                    # Normalized storage: input includes fresh + cached reads.
                    input_tokens=snapshot.input_tokens + snapshot.cached_input_tokens,
                    cached_input_tokens=snapshot.cached_input_tokens,
                    cache_write_tokens=snapshot.cache_write_tokens,
                    cache_write_1h_tokens=0,
                    output_tokens=snapshot.output_tokens,
                    reasoning_tokens=snapshot.reasoning_tokens,
                    cost_usd=snapshot.cost_usd,
                    raw_id=stable_id(
                        "opencode-baseline",
                        snapshot.session_id,
                        snapshot.provider_id,
                        int(snapshot.observed_at.timestamp() * 1000),
                        totals,
                    ),
                )
            )
            labels.append((rows[-1].raw_id, BASELINE_ISSUE))
        else:
            prev_totals = (
                prev["input_tokens"],
                prev["cached_input_tokens"],
                prev["cache_write_tokens"],
                prev["output_tokens"],
            )
            if any(now < was for now, was in zip(totals, prev_totals)):
                # Counter reset/decrease: re-baseline conservatively instead
                # of emitting negative deltas.
                rows.append(
                    UsageRow(
                        source=SOURCE,
                        tool_key="opencode",
                        model_key=canonical_model(snapshot.model_id),
                        occurred_at=snapshot.observed_at,
                        session_id=snapshot.session_id,
                        project=snapshot.project,
                        # Normalized storage: input includes fresh + cached reads.
                        input_tokens=snapshot.input_tokens + snapshot.cached_input_tokens,
                        cached_input_tokens=snapshot.cached_input_tokens,
                        cache_write_tokens=snapshot.cache_write_tokens,
                        cache_write_1h_tokens=0,
                        output_tokens=snapshot.output_tokens,
                        reasoning_tokens=snapshot.reasoning_tokens,
                        cost_usd=snapshot.cost_usd,
                        raw_id=stable_id(
                            "opencode-rebaseline",
                            snapshot.session_id,
                            snapshot.provider_id,
                            int(snapshot.observed_at.timestamp() * 1000),
                            totals,
                        ),
                    )
                )
                labels.append(
                    (rows[-1].raw_id, "Cumulative counters decreased; totals re-baselined at this observation.")
                )
            elif totals != prev_totals:
                delta_input = max(0, snapshot.input_tokens - prev["input_tokens"])
                delta_cached = max(0, snapshot.cached_input_tokens - prev["cached_input_tokens"])
                delta_write = max(0, snapshot.cache_write_tokens - prev["cache_write_tokens"])
                delta_output = max(0, snapshot.output_tokens - prev["output_tokens"])
                if delta_input + delta_cached + delta_write + delta_output > 0:
                    rows.append(
                        UsageRow(
                            source=SOURCE,
                            tool_key="opencode",
                            model_key=canonical_model(snapshot.model_id),
                            occurred_at=snapshot.observed_at,
                            session_id=snapshot.session_id,
                            project=snapshot.project,
                            input_tokens=delta_input + delta_cached,
                            cached_input_tokens=delta_cached,
                            cache_write_tokens=delta_write,
                            cache_write_1h_tokens=0,
                            output_tokens=delta_output,
                            reasoning_tokens=snapshot.reasoning_tokens
                            if delta_output
                            else None,
                            cost_usd=None
                            if snapshot.cost_usd is None or prev.get("cost_usd") is None
                            else max(0.0, snapshot.cost_usd - float(prev["cost_usd"])),
                            raw_id=stable_id(
                                "opencode-delta",
                                snapshot.session_id,
                                snapshot.provider_id,
                                int(snapshot.observed_at.timestamp() * 1000),
                                totals,
                            ),
                        )
                    )
                    labels.append((rows[-1].raw_id, DELTA_ISSUE))
        next_state[key] = {
            "input_tokens": snapshot.input_tokens,
            "cached_input_tokens": snapshot.cached_input_tokens,
            "cache_write_tokens": snapshot.cache_write_tokens,
            "output_tokens": snapshot.output_tokens,
            "reasoning_tokens": snapshot.reasoning_tokens,
            "cost_usd": snapshot.cost_usd,
            "model_id": snapshot.model_id,
            "observed_at": snapshot.observed_at.isoformat().replace("+00:00", "Z"),
        }
    return rows, next_state, labels


def ingest(*, database_path: Path, pricing: PricingEngine, source_database: Path) -> dict:
    files = int(source_database.is_file())
    snapshots = read_session_snapshots(source_database) if files else []
    initialize(database_path)
    with connect(database_path) as connection:
        previous = {
            f"{snapshot.session_id}\x1f{snapshot.provider_id}": read_opencode_progress(
                connection,
                session_id=snapshot.session_id,
                provider_id=snapshot.provider_id,
            )
            for snapshot in snapshots
        }
    rows, next_state, labels = plan_rows(snapshots, previous)

    def finalize(connection: sqlite3.Connection) -> None:
        for key, state in next_state.items():
            session_id, provider_id = key.split("\x1f", 1)
            write_opencode_progress(
                connection,
                session_id=session_id,
                provider_id=provider_id,
                input_tokens=state["input_tokens"],
                cached_input_tokens=state["cached_input_tokens"],
                cache_write_tokens=state["cache_write_tokens"],
                output_tokens=state["output_tokens"],
                reasoning_tokens=state["reasoning_tokens"],
                cost_usd=state["cost_usd"],
                model_id=state["model_id"],
                observed_at=state["observed_at"],
            )
        for raw_id, issue in labels:
            row = next((row for row in rows if row.raw_id == raw_id), None)
            if row is None:
                continue
            record_coverage_gap(
                connection,
                raw_id=raw_id,
                source=SOURCE,
                tool_key=row.tool_key,
                model_key=row.model_key,
                occurred_at=row.occurred_at.isoformat().replace("+00:00", "Z"),
                issue=issue,
            )

    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=rows,
        finalize=finalize,
    )
    return {**result, "files": files, "eventsSeen": len(rows)}
