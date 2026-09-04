from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from spend_app.db import (
    EXACT_USAGE_SOURCES,
    ProviderCostBucket,
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    record_coverage_gap,
    record_pricing_gap,
    sync_model_prices,
    upsert_cost_bucket,
    upsert_usage_event,
    upsert_unpriced_event,
    utc_now,
)
from spend_app.ingest import IngestRun
from spend_app.pricing import Price, PricingEngine, UnpricedModelError

HEALTHY_INGEST_STATUSES = frozenset({"success", "partial"})
INCOMPLETE_TELEMETRY_ISSUE = (
    "Token components are incomplete; cache rate is unavailable for this event."
)
_CREDENTIAL_RE = re.compile(
    r"(?i)(bearer\s+)\S+|(basic\s+)\S+|(sk-[a-z0-9-]+)|(x-api-key\s*[:=]\s*)\S+"
)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, separators=(",", ":"), sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


@dataclass(frozen=True)
class UsageRow:
    source: str
    tool_key: str
    model_key: str
    occurred_at: datetime
    session_id: str | None
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    cost_usd: float | None
    raw_id: str
    unclassified_tokens: int = 0
    telemetry_complete: bool = True


@dataclass(frozen=True)
class CostRow:
    source: str
    starting_at: datetime
    ending_at: datetime
    project_id: str | None
    line_item: str | None
    model_key: str | None
    cost_usd: float
    raw_id: str


def public_error(exc: BaseException) -> str:
    """Strip credential-shaped tokens from adapter errors before persistence."""
    text = _CREDENTIAL_RE.sub(
        lambda match: (match.group(1) or match.group(2) or match.group(4) or "") + "[redacted]"
        if match.group(1) or match.group(2) or match.group(4)
        else "[redacted]",
        str(exc),
    )
    return text[:1000]


def event_is_exact(source: str, price: Price) -> bool:
    """Invoice-exact only when both the source and the YAML card are exact."""
    return source in EXACT_USAGE_SOURCES and price.is_exact


def skipped_result(*, database_path: Path, source: str, reason: str) -> dict:
    return _terminal_result(
        database_path=database_path, source=source, status="skipped", reason=reason
    )


def failed_result(*, database_path: Path, source: str, reason: str) -> dict:
    return _terminal_result(
        database_path=database_path, source=source, status="failed", reason=reason
    )


def _terminal_result(*, database_path: Path, source: str, status: str, reason: str) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        run = IngestRun.start(connection, source)
        run.finish(status=status, error=reason)
    return {
        "source": source,
        "eventsSeen": 0,
        "eventsWritten": 0,
        "costBucketsSeen": 0,
        "costBucketsWritten": 0,
        "unpricedEventsWritten": 0,
        "coverageGapsWritten": 0,
        "unpricedModels": [],
        "quarantined": 0,
        "status": status,
        "reason": reason,
    }


def _parse_occurred(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _unpriced_from_row(row: UsageRow) -> UnpricedUsageEvent:
    return UnpricedUsageEvent(
        source=row.source,
        tool_key=row.tool_key,
        model_key=row.model_key,
        occurred_at=iso_utc(row.occurred_at),
        session_id=row.session_id,
        project=row.project,
        input_tokens=row.input_tokens,
        cached_input_tokens=row.cached_input_tokens,
        cache_write_tokens=row.cache_write_tokens,
        cache_write_1h_tokens=row.cache_write_1h_tokens,
        output_tokens=row.output_tokens,
        reasoning_tokens=row.reasoning_tokens,
        unclassified_tokens=row.unclassified_tokens,
        telemetry_complete=row.telemetry_complete,
        cost_usd=row.cost_usd,
        raw_id=row.raw_id,
        ingested_at=utc_now(),
    )


def promote_priced_unpriced_events(connection, pricing: PricingEngine) -> int:
    promoted = 0
    rows = connection.execute("SELECT * FROM unpriced_usage_events").fetchall()
    for row in rows:
        if not row["telemetry_complete"]:
            continue
        occurred = _parse_occurred(row["occurred_at"])
        try:
            computed = pricing.compute(
                model_key=row["model_key"],
                occurred_at=occurred,
                input_tokens=row["input_tokens"],
                cached_input_tokens=row["cached_input_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                cache_write_1h_tokens=row["cache_write_1h_tokens"],
                output_tokens=row["output_tokens"],
            )
        except (UnpricedModelError, ValueError):
            continue
        price = pricing.resolve(row["model_key"], occurred)
        written = upsert_usage_event(
            connection,
            UsageEvent(
                source=row["source"],
                tool_key=row["tool_key"],
                model_key=row["model_key"],
                occurred_at=row["occurred_at"],
                session_id=row["session_id"],
                project=row["project"],
                input_tokens=row["input_tokens"],
                cached_input_tokens=row["cached_input_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                cache_write_1h_tokens=row["cache_write_1h_tokens"],
                output_tokens=row["output_tokens"],
                reasoning_tokens=row["reasoning_tokens"],
                cost_usd=row["cost_usd"],
                computed_cost_usd=float(computed),
                raw_id=row["raw_id"],
                ingested_at=utc_now(),
                is_exact=event_is_exact(row["source"], price),
            ),
        )
        connection.execute("DELETE FROM pricing_gap_events WHERE raw_id=?", (row["raw_id"],))
        if written:
            promoted += 1
    return promoted


def resolve_pricing_gaps(connection, pricing: PricingEngine) -> None:
    del pricing
    # A price is not resolution. Clear a gap event only after that raw_id has
    # been staged into usage_events. Unstaged historical observations stay.
    connection.execute(
        """
        DELETE FROM pricing_gap_events
        WHERE EXISTS (
            SELECT 1 FROM usage_events WHERE usage_events.raw_id = pricing_gap_events.raw_id
        )
        """
    )
    remaining = connection.execute(
        """
        SELECT model_key, source,
               MIN(occurred_at) AS first_seen_at,
               MAX(occurred_at) AS last_seen_at,
               COUNT(*) AS occurrences,
               MIN(raw_id) AS sample_raw_id
        FROM pricing_gap_events
        GROUP BY model_key
        """
    ).fetchall()
    keep = {row["model_key"] for row in remaining}
    if keep:
        placeholders = ",".join("?" for _ in keep)
        connection.execute(
            f"DELETE FROM pricing_gaps WHERE model_key NOT IN ({placeholders})",
            tuple(keep),
        )
    else:
        connection.execute("DELETE FROM pricing_gaps")
    for row in remaining:
        connection.execute(
            """
            INSERT INTO pricing_gaps(model_key, source, first_seen_at, last_seen_at, occurrences, sample_raw_id)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET
                source=excluded.source,
                first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at,
                occurrences=excluded.occurrences,
                sample_raw_id=excluded.sample_raw_id
            """,
            (
                row["model_key"],
                row["source"],
                row["first_seen_at"],
                row["last_seen_at"],
                row["occurrences"],
                row["sample_raw_id"],
            ),
        )


def persist_rows(
    *,
    database_path: Path,
    pricing: PricingEngine,
    source: str,
    usage_rows: Iterable[UsageRow],
    cost_rows: Iterable[CostRow] = (),
) -> dict:
    initialize(database_path)
    usage_rows = tuple(usage_rows)
    cost_rows = tuple(cost_rows)
    unpriced: set[str] = set()
    unpriced_written = 0
    coverage_written = 0
    cost_buckets_written = 0
    quarantined = 0
    status = "success"
    with connect(database_path) as connection:
        sync_model_prices(connection, pricing)
        run = IngestRun.start(connection, source)
        try:
            for row in usage_rows:
                if not row.telemetry_complete:
                    try:
                        pricing.resolve(row.model_key, row.occurred_at)
                    except UnpricedModelError:
                        unpriced.add(row.model_key)
                        record_pricing_gap(
                            connection,
                            model_key=row.model_key,
                            source=source,
                            occurred_at=iso_utc(row.occurred_at),
                            raw_id=row.raw_id,
                        )
                        if upsert_unpriced_event(connection, _unpriced_from_row(row)):
                            unpriced_written += 1
                        record_coverage_gap(
                            connection,
                            raw_id=row.raw_id,
                            source=row.source,
                            tool_key=row.tool_key,
                            model_key=row.model_key,
                            occurred_at=iso_utc(row.occurred_at),
                            issue=INCOMPLETE_TELEMETRY_ISSUE,
                        )
                        continue
                    # Priced but unclassifiable: never invent a $0 computed cost.
                    if upsert_unpriced_event(connection, _unpriced_from_row(row)):
                        coverage_written += 1
                    record_coverage_gap(
                        connection,
                        raw_id=row.raw_id,
                        source=row.source,
                        tool_key=row.tool_key,
                        model_key=row.model_key,
                        occurred_at=iso_utc(row.occurred_at),
                        issue=INCOMPLETE_TELEMETRY_ISSUE,
                    )
                    continue
                try:
                    computed = pricing.compute(
                        model_key=row.model_key,
                        occurred_at=row.occurred_at,
                        input_tokens=row.input_tokens,
                        cached_input_tokens=row.cached_input_tokens,
                        cache_write_tokens=row.cache_write_tokens,
                        cache_write_1h_tokens=row.cache_write_1h_tokens,
                        output_tokens=row.output_tokens,
                    )
                    price = pricing.resolve(row.model_key, row.occurred_at)
                except UnpricedModelError:
                    unpriced.add(row.model_key)
                    record_pricing_gap(
                        connection,
                        model_key=row.model_key,
                        source=source,
                        occurred_at=iso_utc(row.occurred_at),
                        raw_id=row.raw_id,
                    )
                    if upsert_unpriced_event(connection, _unpriced_from_row(row)):
                        unpriced_written += 1
                    continue
                except ValueError as exc:
                    quarantined += 1
                    record_coverage_gap(
                        connection,
                        raw_id=row.raw_id,
                        source=row.source,
                        tool_key=row.tool_key,
                        model_key=row.model_key,
                        occurred_at=iso_utc(row.occurred_at),
                        issue=str(exc),
                    )
                    continue
                if upsert_usage_event(
                    connection,
                    UsageEvent(
                        source=row.source,
                        tool_key=row.tool_key,
                        model_key=row.model_key,
                        occurred_at=iso_utc(row.occurred_at),
                        session_id=row.session_id,
                        project=row.project,
                        input_tokens=row.input_tokens,
                        cached_input_tokens=row.cached_input_tokens,
                        cache_write_tokens=row.cache_write_tokens,
                        cache_write_1h_tokens=row.cache_write_1h_tokens,
                        output_tokens=row.output_tokens,
                        reasoning_tokens=row.reasoning_tokens,
                        cost_usd=row.cost_usd,
                        computed_cost_usd=float(computed),
                        raw_id=row.raw_id,
                        ingested_at=utc_now(),
                        is_exact=event_is_exact(source, price),
                    ),
                ):
                    run.events_written += 1
            for row in cost_rows:
                if upsert_cost_bucket(
                    connection,
                    ProviderCostBucket(
                        source=row.source,
                        starting_at=iso_utc(row.starting_at),
                        ending_at=iso_utc(row.ending_at),
                        project_id=row.project_id,
                        line_item=row.line_item,
                        model_key=row.model_key,
                        cost_usd=row.cost_usd,
                        raw_id=row.raw_id,
                        ingested_at=utc_now(),
                    ),
                ):
                    cost_buckets_written += 1
            promoted = promote_priced_unpriced_events(connection, pricing)
            run.events_written += promoted
            resolve_pricing_gaps(connection, pricing)
            error = f"Unpriced models: {', '.join(sorted(unpriced))}" if unpriced else None
            if quarantined and error is None:
                error = f"Quarantined malformed rows: {quarantined}"
            elif quarantined:
                error = f"{error}; quarantined malformed rows: {quarantined}"
            status = "partial" if unpriced or quarantined else "success"
            run.finish(status=status, error=error)
        except Exception as exc:
            run.finish(status="failed", error=public_error(exc))
            raise
    return {
        "source": source,
        "eventsSeen": len(usage_rows),
        "eventsWritten": run.events_written,
        "unpricedEventsWritten": unpriced_written,
        "coverageGapsWritten": coverage_written,
        "costBucketsSeen": len(cost_rows),
        "costBucketsWritten": cost_buckets_written,
        "unpricedModels": sorted(unpriced),
        "quarantined": quarantined,
        "status": status,
    }
