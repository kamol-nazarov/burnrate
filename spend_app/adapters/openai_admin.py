from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from spend_app.adapters.common import (
    CostRow,
    UsageRow,
    failed_result,
    persist_rows,
    public_error,
    skipped_result,
    stable_id,
)
from spend_app.pricing import PricingEngine


SOURCE = "openai_admin"
BASE_URL = "https://api.openai.com/v1"


def _time(epoch: int | float) -> datetime:
    return datetime.fromtimestamp(float(epoch), UTC)


def _nonneg_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bucket_results(bucket: dict) -> list[dict]:
    raw = bucket.get("results")
    if raw is None:
        raw = bucket.get("result")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def parse_usage(payload: dict) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        occurred_at = _time(bucket["start_time"])
        for result in _bucket_results(bucket):
            model = result.get("model")
            if not model:
                continue
            raw_id = stable_id(
                "openai-usage",
                bucket.get("start_time"),
                bucket.get("end_time"),
                model,
                result.get("project_id"),
                result.get("api_key_id"),
                result.get("service_tier"),
                result.get("batch"),
            )
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="codex",
                    model_key=str(model),
                    occurred_at=occurred_at,
                    session_id=None,
                    project=result.get("project_id"),
                    # Official Usage API input_tokens includes cached tokens.
                    input_tokens=_nonneg_int(result.get("input_tokens")),
                    cached_input_tokens=_nonneg_int(result.get("input_cached_tokens")),
                    cache_write_tokens=_nonneg_int(result.get("input_cache_write_tokens")),
                    cache_write_1h_tokens=0,
                    output_tokens=_nonneg_int(result.get("output_tokens")),
                    reasoning_tokens=None,
                    cost_usd=None,
                    raw_id=raw_id,
                )
            )
    return rows


def parse_costs(payload: dict) -> list[CostRow]:
    rows: list[CostRow] = []
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        start = _time(bucket["start_time"])
        end = _time(bucket["end_time"])
        for result in _bucket_results(bucket):
            amount = result.get("amount") or {}
            if not isinstance(amount, dict):
                continue
            if str(amount.get("currency", "usd")).lower() != "usd":
                continue
            value = amount.get("value")
            if isinstance(value, bool) or value is None or value == "":
                continue
            try:
                cost_usd = float(value)
            except (TypeError, ValueError):
                continue
            raw_id = stable_id(
                "openai-cost",
                bucket.get("start_time"),
                bucket.get("end_time"),
                result.get("project_id"),
                result.get("line_item"),
            )
            rows.append(
                CostRow(
                    source=SOURCE,
                    starting_at=start,
                    ending_at=end,
                    project_id=result.get("project_id"),
                    line_item=result.get("line_item"),
                    model_key=None,
                    cost_usd=cost_usd,
                    raw_id=raw_id,
                )
            )
    return rows


def fetch_pages(
    client: httpx.Client,
    *,
    path: str,
    params: dict,
) -> list[dict]:
    pages: list[dict] = []
    page: str | None = None
    seen: set[str] = set()
    while True:
        request_params = dict(params)
        if page:
            request_params["page"] = page
        response = client.get(f"{BASE_URL}{path}", params=request_params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return pages
        pages.append(payload)
        if not payload.get("has_more"):
            return pages
        next_page = payload.get("next_page")
        if not isinstance(next_page, str) or not next_page or next_page in seen:
            return pages
        seen.add(next_page)
        page = next_page


def make_client(admin_key: str) -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"},
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    )


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    admin_key: str | None,
    start: datetime,
    end: datetime,
    client: httpx.Client | None = None,
) -> dict:
    if not admin_key:
        return skipped_result(
            database_path=database_path,
            source=SOURCE,
            reason="OPENAI_ADMIN_KEY is not configured",
        )
    usage_rows: list[UsageRow] = []
    cost_rows: list[CostRow] = []

    def collect(http_client: httpx.Client) -> None:
        for payload in fetch_pages(
            http_client,
            path="/organization/usage/completions",
            params={
                "start_time": int(start.timestamp()),
                "end_time": int(end.timestamp()),
                "bucket_width": "1h",
                "group_by": ["model", "project_id", "service_tier", "batch"],
                "limit": 168,
            },
        ):
            usage_rows.extend(parse_usage(payload))
        for payload in fetch_pages(
            http_client,
            path="/organization/costs",
            params={
                "start_time": int(start.timestamp()),
                "end_time": int(end.timestamp()),
                "bucket_width": "1d",
                "group_by": ["project_id", "line_item"],
                "limit": 180,
            },
        ):
            cost_rows.extend(parse_costs(payload))

    try:
        if client is not None:
            collect(client)
        else:
            with make_client(admin_key) as owned:
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
        usage_rows=usage_rows,
        cost_rows=cost_rows,
    )
