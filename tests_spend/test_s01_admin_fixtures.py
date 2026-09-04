from datetime import UTC, datetime
from pathlib import Path

import httpx

from spend_app.adapters.codex_local import ingest as ingest_codex
from spend_app.adapters.codex_local import reset_file_cache as reset_codex_cache
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.aggregate import aggregate_health, aggregate_summary
from spend_app.db import connect
from spend_app.pricing import PricingEngine
from tests_spend.test_admin_adapters import fixture
from tests_spend.test_codex_local import write_session


ROOT = Path(__file__).resolve().parents[1]
START = datetime.fromtimestamp(1788048000, UTC)
END = datetime.fromtimestamp(1788134400, UTC)


def _paged_client() -> httpx.Client:
    usage_page_1 = {
        "object": "page",
        "data": fixture("openai_usage.json")["data"],
        "has_more": True,
        "next_page": "cursor-2",
    }
    usage_page_2 = {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1788134400,
                "end_time": 1788220800,
                "results": [
                    {
                        "object": "organization.usage.completions.result",
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "input_cached_tokens": 0,
                        "input_cache_write_tokens": 0,
                        "num_model_requests": 1,
                        "project_id": "proj_page2",
                        "model": "gpt-5.6-sol",
                        "batch": False,
                        "service_tier": "default",
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    cost_page_1 = {
        "object": "page",
        "data": fixture("openai_costs.json")["data"],
        "has_more": True,
        "next_page": "cost-2",
    }
    cost_page_2 = {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1788134400,
                "end_time": 1788220800,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": 0.5, "currency": "usd"},
                        "line_item": "Model usage",
                        "project_id": "proj_page2",
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = request.url.params.get("page")
        if path.endswith("/organization/usage/completions"):
            payload = usage_page_2 if page == "cursor-2" else usage_page_1
            return httpx.Response(200, json=payload)
        if path.endswith("/organization/costs"):
            payload = cost_page_2 if page == "cost-2" else cost_page_1
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": {"message": "unexpected path"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_s01_10_multi_page_admin_ingest_lands_cost_buckets(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    result = ingest_openai_admin(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key="test-admin-key",
        start=START,
        end=datetime.fromtimestamp(1788220800, UTC),
        client=_paged_client(),
    )
    assert result["status"] == "success"
    assert result["eventsWritten"] == 2
    assert result["costBucketsWritten"] == 2
    with connect(database) as connection:
        buckets = connection.execute("SELECT COUNT(*) FROM provider_cost_buckets").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        ids = [row[0] for row in connection.execute("SELECT raw_id FROM usage_events")]
    assert buckets == 2
    assert events == 2
    assert all(raw_id.startswith("openai-usage:") for raw_id in ids)


def test_s01_11_missing_credential_is_unavailable_not_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    result = ingest_openai_admin(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key=None,
        start=START,
        end=END,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("no http")))),
    )
    assert result["status"] == "skipped"
    assert "not configured" in result["reason"]
    health = aggregate_health(
        database_path=database,
        now=datetime(2026, 8, 30, 23, tzinfo=UTC),
        timezone="UTC",
    )
    ingest = next(item for item in health["ingest"] if item["source"] == "openai_admin")
    assert ingest["status"] == "unavailable"
    assert ingest["error"] == "unavailable — credential missing"
    assert ingest.get("value") not in {0, 0.0}
    assert "0" != ingest["status"]


def test_s01_12_disjoint_codex_local_and_openai_admin(tmp_path: Path) -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    now = datetime.fromtimestamp(1788220800, UTC)
    session = tmp_path / "desktop.jsonl"
    write_session(session)

    codex_db = tmp_path / "codex.db"
    reset_codex_cache()
    ingest_codex(database_path=codex_db, pricing=pricing, session_glob=str(session))
    openai_db = tmp_path / "openai.db"
    ingest_openai_admin(
        database_path=openai_db,
        pricing=pricing,
        admin_key="test-admin-key",
        start=START,
        end=END,
        client=_paged_client(),
    )
    combined_db = tmp_path / "combined.db"
    reset_codex_cache()
    ingest_codex(database_path=combined_db, pricing=pricing, session_glob=str(session))
    ingest_openai_admin(
        database_path=combined_db,
        pricing=pricing,
        admin_key="test-admin-key",
        start=START,
        end=datetime.fromtimestamp(1788220800, UTC),
        client=_paged_client(),
    )

    def tokens(database: Path) -> int:
        return aggregate_summary(
            database_path=database,
            pricing=pricing,
            window_key="all",
            tool="codex",
            timezone="UTC",
            cache_threshold=0.75,
            now=now,
        )["totals"]["tokens"]

    assert tokens(combined_db) == tokens(codex_db) + tokens(openai_db)
    with connect(combined_db) as connection:
        sources = {
            row[0]: row[1]
            for row in connection.execute("SELECT raw_id, source FROM usage_events")
        }
    assert all(
        (raw_id.startswith("codex-local:") and source == "codex_local")
        or (raw_id.startswith("openai-usage:") and source == "openai_admin")
        for raw_id, source in sources.items()
    )
    assert not (
        {raw_id for raw_id in sources if raw_id.startswith("codex-local:")}
        & {raw_id for raw_id in sources if raw_id.startswith("openai-usage:")}
    )
