import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from spend_app.adapters.anthropic_admin import fetch_pages as fetch_anthropic_pages
from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic_admin
from spend_app.adapters.anthropic_admin import make_client as make_anthropic_client
from spend_app.adapters.anthropic_admin import parse_costs as parse_anthropic_costs
from spend_app.adapters.anthropic_admin import parse_usage as parse_anthropic_usage
from spend_app.adapters.cursor_admin import fetch_event_pages
from spend_app.adapters.cursor_admin import ingest as ingest_cursor_admin
from spend_app.adapters.cursor_admin import make_client as make_cursor_client
from spend_app.adapters.cursor_admin import parse_events as parse_cursor_events
from spend_app.adapters.openai_admin import fetch_pages
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.adapters.openai_admin import make_client as make_openai_client
from spend_app.adapters.openai_admin import parse_costs as parse_openai_costs
from spend_app.adapters.openai_admin import parse_usage as parse_openai_usage
from spend_app.db import connect
from spend_app.pricing import PricingEngine


FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page_transport(pages: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json=pages[page - 1])

    return httpx.MockTransport(handler)


def test_openai_official_usage_shape_and_cost_bucket() -> None:
    usage = parse_openai_usage(fixture("openai_usage.json"))
    costs = parse_openai_costs(fixture("openai_costs.json"))
    assert len(usage) == 1
    assert usage[0].model_key == "gpt-5.6-sol"
    assert usage[0].input_tokens == 1000
    assert usage[0].cached_input_tokens == 800
    assert usage[0].cache_write_tokens == 50
    assert usage == parse_openai_usage(fixture("openai_usage.json"))
    assert parse_openai_usage(fixture("openai_usage_result.json")) == usage
    assert len(costs) == 1
    assert costs[0].cost_usd == 1.25
    assert costs[0].model_key is None
    singular_costs = parse_openai_costs(
        {
            "data": [
                {
                    "start_time": 1788048000,
                    "end_time": 1788134400,
                    "result": {
                        "amount": {"value": 1.25, "currency": "usd"},
                        "line_item": "Model usage",
                        "project_id": "proj_fixture",
                    },
                }
            ]
        }
    )
    assert singular_costs == costs


def test_anthropic_official_usage_shape_preserves_cache_ttls() -> None:
    usage = parse_anthropic_usage(fixture("anthropic_usage.json"))
    costs = parse_anthropic_costs(fixture("anthropic_costs.json"))
    assert len(usage) == 1
    row = usage[0]
    assert row.model_key == "claude-opus-5"
    assert row.input_tokens == 1700
    assert row.cached_input_tokens == 200
    assert row.cache_write_tokens == 1500
    assert row.cache_write_1h_tokens == 1000
    assert row.project == "wrkspc_fixture"
    assert usage == parse_anthropic_usage(fixture("anthropic_usage.json"))
    assert costs[0].cost_usd == 2.505
    assert costs[0].model_key == "claude-opus-5"
    assert costs == parse_anthropic_costs(fixture("anthropic_costs.json"))
    fractional = parse_anthropic_costs(fixture("anthropic_costs_fractional.json"))
    assert len(fractional) == 1
    assert fractional[0].cost_usd == 1.2378912
    singular_usage = parse_anthropic_usage(
        {
            "data": [
                {
                    "starting_at": "2026-08-30T00:00:00Z",
                    "ending_at": "2026-08-31T00:00:00Z",
                    "result": {
                        "model": "claude-opus-5",
                        "workspace_id": "wrkspc_fixture",
                        "api_key_id": "apikey_fixture",
                        "uncached_input_tokens": 1500,
                        "cache_read_input_tokens": 200,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 500,
                            "ephemeral_1h_input_tokens": 1000,
                        },
                        "output_tokens": 500,
                        "service_tier": "standard",
                        "context_window": "0-200k",
                    },
                }
            ]
        }
    )
    assert singular_usage == usage


def test_cursor_official_event_shape_uses_charged_cents_authority() -> None:
    rows = parse_cursor_events(fixture("cursor_events.json"))
    assert len(rows) == 1
    row = rows[0]
    assert row.model_key == "cursor:grok-4.6"
    assert row.input_tokens == 1400
    assert row.cached_input_tokens == 400
    assert row.cache_write_tokens == 100
    assert row.cost_usd == 0.125
    assert row.raw_id == "cursor-admin:event_fixture_1"


def test_openai_pagination_stops_when_has_more_lacks_cursor() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"object": "page", "data": [], "has_more": True, "next_page": None},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = fetch_pages(
        client,
        path="/organization/usage/completions",
        params={"start_time": 1},
    )
    assert len(pages) == 1
    assert calls["count"] == 1


def test_openai_pagination_follows_next_page_then_stops() -> None:
    seen_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen_pages.append(page)
        if page == "cursor-2":
            return httpx.Response(
                200,
                json={"object": "page", "data": [], "has_more": False, "next_page": None},
            )
        return httpx.Response(
            200,
            json={"object": "page", "data": [], "has_more": True, "next_page": "cursor-2"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = fetch_pages(
        client,
        path="/organization/costs",
        params={"start_time": 1},
    )
    assert len(pages) == 2
    assert seen_pages == [None, "cursor-2"]


def _mock_openai_client(usage: dict, costs: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/organization/usage/completions"):
            return httpx.Response(200, json=usage)
        if path.endswith("/organization/costs"):
            return httpx.Response(200, json=costs)
        return httpx.Response(404, json={"error": {"message": "unexpected path"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_admin_skips_without_key_and_makes_no_http(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("OpenAI HTTP must not run without OPENAI_ADMIN_KEY")

    result = ingest_openai_admin(
        database_path=tmp_path / "spend.db",
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key=None,
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert result["status"] == "skipped"
    assert result["eventsWritten"] == 0
    assert result["reason"] == "OPENAI_ADMIN_KEY is not configured"


def test_openai_admin_mock_ingest_is_exact_and_reingest_writes_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    start = datetime.fromtimestamp(1788048000, UTC)
    end = datetime.fromtimestamp(1788134400, UTC)
    kwargs = {
        "database_path": database,
        "pricing": pricing,
        "admin_key": "test-admin-key",
        "start": start,
        "end": end,
        "client": _mock_openai_client(fixture("openai_usage.json"), fixture("openai_costs.json")),
    }
    first = ingest_openai_admin(**kwargs)
    second = ingest_openai_admin(**kwargs)
    assert first["status"] == "success"
    assert first["eventsWritten"] == 1
    assert first["costBucketsWritten"] == 1
    assert second["eventsWritten"] == 0
    assert second["costBucketsWritten"] == 0
    with connect(database) as connection:
        event = connection.execute(
            "SELECT cache_write_tokens, is_exact, source FROM usage_events"
        ).fetchone()
        buckets = connection.execute("SELECT COUNT(*) FROM provider_cost_buckets").fetchone()[0]
        event_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert tuple(event) == (50, 1, "openai_admin")
    assert buckets == 1
    assert event_count == 1


def test_cursor_current_documented_event_shape() -> None:
    rows = parse_cursor_events(fixture("cursor_events_current_p1.json"))
    assert len(rows) == 2
    row = rows[0]
    assert row.model_key == "cursor:grok-4.6"
    assert row.occurred_at == datetime(2026, 8, 30, 12, tzinfo=UTC)
    assert row.session_id == "agent_fixture_1"
    assert row.input_tokens == 1400
    assert row.cached_input_tokens == 400
    assert row.cache_write_tokens == 100
    assert row.output_tokens == 200
    assert row.cost_usd == 0.125
    assert row.raw_id == "cursor-admin:ident_cur_1"


def test_cursor_pagination_honors_num_pages_and_has_next_page() -> None:
    pages = [
        fixture("cursor_events_current_p1.json"),
        fixture("cursor_events_current_p2.json"),
    ]
    with httpx.Client(transport=page_transport(pages)) as client:
        fetched = fetch_event_pages(client, {"pageSize": 2})
    assert fetched == pages
    rows = [row for payload in fetched for row in parse_cursor_events(payload)]
    assert [row.raw_id for row in rows] == [
        "cursor-admin:ident_cur_1",
        "cursor-admin:ident_cur_2",
        "cursor-admin:ident_cur_3",
    ]


def test_cursor_pagination_honors_legacy_total_pages() -> None:
    pages = [
        fixture("cursor_events_legacy_p1.json"),
        fixture("cursor_events_legacy_p2.json"),
    ]
    with httpx.Client(transport=page_transport(pages)) as client:
        fetched = fetch_event_pages(client, {"pageSize": 25})
    assert fetched == pages
    rows = [row for payload in fetched for row in parse_cursor_events(payload)]
    assert [row.raw_id for row in rows] == [
        "cursor-admin:event_legacy_p1_1",
        "cursor-admin:event_legacy_p2_1",
    ]
    # Legacy billed cost authority: chargedCents, not tokenUsage.totalCents.
    assert rows[0].cost_usd == 0.1
    assert rows[0].input_tokens == 1200
    assert rows[0].cached_input_tokens == 300


def test_anthropic_pagination_stops_when_has_more_lacks_cursor() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"data": [], "has_more": True, "next_page": None},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = fetch_anthropic_pages(
        client,
        path="/v1/organizations/usage_report/messages",
        params={"starting_at": "2026-08-30T00:00:00Z"},
    )
    assert len(pages) == 1
    assert calls["count"] == 1


def test_anthropic_pagination_follows_next_page_then_stops() -> None:
    seen_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen_pages.append(page)
        if page == "page_xyz":
            return httpx.Response(
                200,
                json={"data": [], "has_more": False, "next_page": None},
            )
        return httpx.Response(
            200,
            json={"data": [], "has_more": True, "next_page": "page_xyz"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = fetch_anthropic_pages(
        client,
        path="/v1/organizations/cost_report",
        params={"starting_at": "2026-08-30T00:00:00Z"},
    )
    assert len(pages) == 2
    assert seen_pages == [None, "page_xyz"]


def _mock_anthropic_client(usage: dict, costs: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v1/organizations/usage_report/messages"):
            return httpx.Response(200, json=usage)
        if path.endswith("/v1/organizations/cost_report"):
            return httpx.Response(200, json=costs)
        return httpx.Response(404, json={"error": {"message": "unexpected path"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_anthropic_admin_skips_without_key_and_makes_no_http(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Anthropic HTTP must not run without ANTHROPIC_ADMIN_KEY")

    result = ingest_anthropic_admin(
        database_path=tmp_path / "spend.db",
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key=None,
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert result["status"] == "skipped"
    assert result["eventsWritten"] == 0
    assert result["reason"] == "ANTHROPIC_ADMIN_KEY is not configured"


def test_anthropic_admin_mock_ingest_is_exact_and_reingest_writes_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    start = datetime(2026, 8, 30, tzinfo=UTC)
    end = datetime(2026, 8, 31, tzinfo=UTC)
    kwargs = {
        "database_path": database,
        "pricing": pricing,
        "admin_key": "test-admin-key",
        "start": start,
        "end": end,
        "client": _mock_anthropic_client(
            fixture("anthropic_usage.json"),
            fixture("anthropic_costs.json"),
        ),
    }
    first = ingest_anthropic_admin(**kwargs)
    second = ingest_anthropic_admin(**kwargs)
    assert first["status"] == "success"
    assert first["eventsWritten"] == 1
    assert first["costBucketsWritten"] == 1
    assert second["eventsWritten"] == 0
    assert second["costBucketsWritten"] == 0
    with connect(database) as connection:
        event = connection.execute(
            """
            SELECT cache_write_tokens, cache_write_1h_tokens, is_exact, source, project
            FROM usage_events
            """
        ).fetchone()
        buckets = connection.execute("SELECT COUNT(*) FROM provider_cost_buckets").fetchone()[0]
        event_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        raw_ids = [
            row[0]
            for row in connection.execute("SELECT raw_id FROM usage_events")
        ]
    assert tuple(event) == (1500, 1000, 1, "anthropic_admin", "wrkspc_fixture")
    assert buckets == 1
    assert event_count == 1
    assert raw_ids == [parse_anthropic_usage(fixture("anthropic_usage.json"))[0].raw_id]


def test_openai_missing_cost_amount_is_not_stored_as_zero() -> None:
    rows = parse_openai_costs(
        {
            "data": [
                {
                    "start_time": 1788048000,
                    "end_time": 1788134400,
                    "results": [
                        {
                            "amount": {"currency": "usd"},
                            "line_item": "Model usage",
                            "project_id": "proj_missing",
                        },
                        {
                            "amount": {"value": 0, "currency": "usd"},
                            "line_item": "Model usage",
                            "project_id": "proj_zero",
                        },
                    ],
                }
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0].project_id == "proj_zero"
    assert rows[0].cost_usd == 0.0


def _status_client(status: int, body: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_admin_clients_disable_proxy_env_and_identify_as_burnrate() -> None:
    anthropic = make_anthropic_client("sk-ant-secret")
    openai = make_openai_client("sk-test-openai-secret")
    cursor = make_cursor_client("cursor-secret-key")
    try:
        assert anthropic.headers["user-agent"] == "BURNRATE/0.1.0-beta.1"
        assert anthropic.trust_env is False
        assert openai.trust_env is False
        assert cursor.trust_env is False
    finally:
        anthropic.close()
        openai.close()
        cursor.close()


def test_openai_admin_http_error_records_failed_run_without_key(tmp_path: Path) -> None:
    result = ingest_openai_admin(
        database_path=tmp_path / "spend.db",
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key="sk-test-openai-secret",
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=_status_client(401, {"error": {"message": "invalid_api_key"}}),
    )
    assert result["status"] == "failed"
    assert result["eventsWritten"] == 0
    dumped = json.dumps(result)
    assert "sk-test-openai-secret" not in dumped
    assert "Bearer" not in dumped or "[redacted]" in dumped
    with connect(tmp_path / "spend.db") as connection:
        run = connection.execute(
            "SELECT status, error, events_written FROM ingest_runs WHERE source='openai_admin'"
        ).fetchone()
        events = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert tuple(run[:2])[0] == "failed"
    assert run[2] == 0
    assert "sk-test-openai-secret" not in (run[1] or "")
    assert events == 0


def test_anthropic_admin_http_error_records_failed_run_without_key(tmp_path: Path) -> None:
    result = ingest_anthropic_admin(
        database_path=tmp_path / "spend.db",
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key="sk-ant-secret",
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=_status_client(401, {"error": {"message": "invalid x-api-key"}}),
    )
    assert result["status"] == "failed"
    assert "sk-ant-secret" not in json.dumps(result)
    with connect(tmp_path / "spend.db") as connection:
        error = connection.execute(
            "SELECT error FROM ingest_runs WHERE source='anthropic_admin'"
        ).fetchone()[0]
    assert "sk-ant-secret" not in (error or "")


def test_cursor_admin_http_error_records_failed_run_without_key(tmp_path: Path) -> None:
    result = ingest_cursor_admin(
        database_path=tmp_path / "spend.db",
        pricing=PricingEngine.load(ROOT / "pricing"),
        api_key="cursor-secret-key",
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=_status_client(401, {"message": "Unauthorized"}),
    )
    assert result["status"] == "failed"
    assert result["reason"]
    assert "cursor-secret-key" not in json.dumps(result)
    with connect(tmp_path / "spend.db") as connection:
        row = connection.execute(
            "SELECT status, error FROM ingest_runs WHERE source='cursor_admin'"
        ).fetchone()
    assert row[0] == "failed"
    assert "cursor-secret-key" not in (row[1] or "")

