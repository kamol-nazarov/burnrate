from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import httpx

from spend_app.adapters.common import (
    UsageRow,
    failed_result,
    persist_rows,
    public_error,
    skipped_result,
    stable_id,
)
from spend_app.adapters.local_common import parse_iso_time
from spend_app.pricing import PricingEngine


SOURCE = "cursor_admin"
BASE_URL = "https://api.cursor.com"


def _millis_time(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, UTC)


def _occurred_at(value: object) -> datetime | None:
    # The current Admin API documents an ISO-8601 `timestamp` string; legacy
    # payloads use epoch milliseconds. Both identify the same event instant.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _millis_time(value)
    return parse_iso_time(value)


def _token_usage(event: dict) -> dict:
    usage = event.get("tokenUsage")
    return usage if isinstance(usage, dict) else event


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 1 else None


def _has_next_page(payload: dict, page: int) -> bool:
    # Current Admin API pages report `numPages` plus a boolean
    # `hasNextPage`; legacy responses only expose `totalPages` (nested or
    # top-level). All shapes must be honored so multi-page exports are
    # never truncated.
    pagination = payload.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    num_pages = _positive_int(pagination.get("numPages"))
    has_next = pagination.get("hasNextPage")
    if isinstance(has_next, bool):
        if not has_next:
            return False
        return page < num_pages if num_pages is not None else True
    if num_pages is not None:
        return page < num_pages
    total_pages = _positive_int(pagination.get("totalPages"))
    if total_pages is None:
        total_pages = _positive_int(payload.get("totalPages"))
    if total_pages is not None:
        return page < total_pages
    return False


def _charged_cost(event: dict) -> float | None:
    # `chargedCents` is the documented billing authority for the event; the
    # token-level breakdown (e.g. legacy `totalCents`) is not billed cost.
    cents = event.get("chargedCents")
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return None
    return max(0.0, float(cents)) / 100


def parse_events(payload: dict) -> list[UsageRow]:
    rows: list[UsageRow] = []
    events = payload.get("usageEvents") or payload.get("events") or []
    for event in events:
        if not isinstance(event, dict):
            continue
        occurred_at = _occurred_at(event.get("timestamp"))
        if occurred_at is None:
            continue
        usage = _token_usage(event)
        fresh = _count(usage.get("inputTokens"))
        cached = _count(usage.get("cacheReadTokens"))
        writes = _count(usage.get("cacheWriteTokens"))
        output = _count(usage.get("outputTokens"))
        if fresh + cached + writes + output == 0 and event.get("isTokenBasedCall") is False:
            continue
        event_id = event.get("id") or stable_id(
            "cursor-event-fallback",
            event.get("timestamp"),
            event.get("userId"),
            event.get("model"),
            usage,
        )
        rows.append(
            UsageRow(
                source=SOURCE,
                tool_key="cursor",
                model_key=f"cursor:{event.get('model') or 'unknown'}",
                occurred_at=occurred_at,
                session_id=(
                    event.get("cloudAgentId") or event.get("agent") or event.get("automationId")
                ),
                project=None,
                input_tokens=fresh + cached,
                cached_input_tokens=cached,
                cache_write_tokens=writes,
                cache_write_1h_tokens=0,
                output_tokens=output,
                reasoning_tokens=None,
                cost_usd=_charged_cost(event),
                raw_id=f"cursor-admin:{event_id}",
            )
        )
    return rows


def fetch_event_pages(client: httpx.Client, body: dict) -> list[dict]:
    pages: list[dict] = []
    page = 1
    while True:
        request_body = {**body, "page": page, "pageSize": body.get("pageSize", 1000)}
        response = client.post(f"{BASE_URL}/teams/filtered-usage-events", json=request_body)
        response.raise_for_status()
        payload = response.json()
        pages.append(payload)
        if not _has_next_page(payload, page):
            return pages
        page += 1


def make_client(api_key: str) -> httpx.Client:
    # The Basic credential is sent only in the Authorization header and is
    # never logged or included in error messages.
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return httpx.Client(
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    )


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    api_key: str | None,
    start: datetime,
    end: datetime,
    client: httpx.Client | None = None,
) -> dict:
    if not api_key:
        return skipped_result(
            database_path=database_path,
            source=SOURCE,
            reason="CURSOR_API_KEY is not configured; use the CSV drop folder instead",
        )
    rows: list[UsageRow] = []
    body = {
        "startDate": int(start.timestamp() * 1000),
        "endDate": int(end.timestamp() * 1000),
        "pageSize": 1000,
    }

    def collect(http_client: httpx.Client) -> None:
        for payload in fetch_event_pages(http_client, body):
            rows.extend(parse_events(payload))

    try:
        if client is not None:
            collect(client)
        else:
            with make_client(api_key) as owned:
                collect(owned)
    except Exception as exc:
        return failed_result(
            database_path=database_path,
            source=SOURCE,
            reason=public_error(exc),
        )
    return persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=rows,
    )
