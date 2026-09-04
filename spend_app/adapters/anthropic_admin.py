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


SOURCE = "anthropic_admin"
BASE_URL = "https://api.anthropic.com"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


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


def _cents_to_usd(value: object) -> float | None:
    # Official Cost API amount is a decimal string in lowest units (cents),
    # including fractional cents such as "123.78912".
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value) / 100
    except (TypeError, ValueError):
        return None


def parse_usage(payload: dict) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        occurred_at = _time(bucket["starting_at"])
        for result in _bucket_results(bucket):
            model = result.get("model")
            if not model:
                continue
            cache_creation = result.get("cache_creation") or {}
            if not isinstance(cache_creation, dict):
                cache_creation = {}
            write_5m = _nonneg_int(cache_creation.get("ephemeral_5m_input_tokens"))
            write_1h = _nonneg_int(cache_creation.get("ephemeral_1h_input_tokens"))
            cached = _nonneg_int(result.get("cache_read_input_tokens"))
            uncached = _nonneg_int(result.get("uncached_input_tokens"))
            raw_id = stable_id(
                "anthropic-usage",
                bucket.get("starting_at"),
                bucket.get("ending_at"),
                model,
                result.get("workspace_id"),
                result.get("api_key_id"),
                result.get("service_tier"),
                result.get("context_window"),
            )
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key="claude-code",
                    model_key=str(model),
                    occurred_at=occurred_at,
                    session_id=None,
                    project=result.get("workspace_id"),
                    input_tokens=uncached + cached,
                    cached_input_tokens=cached,
                    cache_write_tokens=write_5m + write_1h,
                    cache_write_1h_tokens=write_1h,
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
        start = _time(bucket["starting_at"])
        end = _time(bucket["ending_at"])
        for result in _bucket_results(bucket):
            if str(result.get("currency", "USD")).upper() != "USD":
                continue
            cost_usd = _cents_to_usd(result.get("amount"))
            if cost_usd is None:
                continue
            raw_id = stable_id(
                "anthropic-cost",
                bucket.get("starting_at"),
                bucket.get("ending_at"),
                result.get("workspace_id"),
                result.get("description"),
                result.get("model"),
                result.get("cost_type"),
                result.get("token_type"),
            )
            rows.append(
                CostRow(
                    source=SOURCE,
                    starting_at=start,
                    ending_at=end,
                    project_id=result.get("workspace_id"),
                    line_item=result.get("description") or result.get("cost_type"),
                    model_key=result.get("model"),
                    cost_usd=cost_usd,
                    raw_id=raw_id,
                )
            )
    return rows


def fetch_pages(client: httpx.Client, *, path: str, params: dict) -> list[dict]:
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
        headers={
            "x-api-key": admin_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "BURNRATE/0.1.0-beta.1",
        },
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
            reason="ANTHROPIC_ADMIN_KEY is not configured",
        )
    usage_rows: list[UsageRow] = []
    cost_rows: list[CostRow] = []

    def collect(http_client: httpx.Client) -> None:
        for payload in fetch_pages(
            http_client,
            path="/v1/organizations/usage_report/messages",
            params={
                "starting_at": start.isoformat().replace("+00:00", "Z"),
                "ending_at": end.isoformat().replace("+00:00", "Z"),
                "bucket_width": "1h",
                "group_by[]": ["model", "workspace_id", "service_tier", "context_window"],
                "limit": 168,
            },
        ):
            usage_rows.extend(parse_usage(payload))
        for payload in fetch_pages(
            http_client,
            path="/v1/organizations/cost_report",
            params={
                "starting_at": start.isoformat().replace("+00:00", "Z"),
                "ending_at": end.isoformat().replace("+00:00", "Z"),
                "bucket_width": "1d",
                "group_by[]": ["workspace_id", "description"],
                "limit": 31,
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
