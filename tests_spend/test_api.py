from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app import __version__
from spend_app.api import create_app
import spend_app.api as api_module
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def make_settings(database: Path) -> Settings:
    return Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        cursor_import_path=database.parent / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def add_fixture_event(database: Path) -> None:
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    occurred = datetime.now(UTC)
    computed = pricing.compute(
        model_key="gpt-5.6-sol",
        occurred_at=occurred,
        input_tokens=1000,
        cached_input_tokens=800,
        cache_write_tokens=0,
        output_tokens=100,
    )
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                occurred_at=occurred.isoformat().replace("+00:00", "Z"),
                session_id="api-session",
                project="api-project",
                input_tokens=1000,
                cached_input_tokens=800,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=100,
                reasoning_tokens=20,
                cost_usd=None,
                computed_cost_usd=float(computed),
                raw_id="api-event",
                ingested_at=occurred.isoformat().replace("+00:00", "Z"),
                is_exact=True,
            ),
        )


def test_api_contracts_and_generated_at(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    summary = client.get("/api/spend/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["generatedAt"]
    assert body["window"]["from"]
    assert body["window"]["to"]
    assert body["window"]["buckets"] == 24
    assert body["window"]["key"] == "1d"
    assert "nav" not in body
    assert "total" not in body
    assert "navigation" in body
    assert "burnRatePerDay" in body["navigation"]
    assert "todayUsd" in body["navigation"]
    assert "capacity" in body
    assert "activity" in body
    assert "perDay" in body["waste"]
    assert body["totals"]["trackedValue"] is not None
    nav = client.get("/api/spend/nav")
    assert nav.status_code == 200
    assert set(("todayUsd", "burnRatePerDay", "lastRefreshAt", "cadenceSeconds", "cadenceMinutes", "status")) <= set(nav.json())
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    )
    assert entity.status_code == 200
    payload = entity.json()
    assert payload["sessions"]["shownTotal"] is not None
    assert payload["sessions"]["rows"][0]["id"] == "api-session"
    assert "opportunity" in payload
    health = client.get("/api/spend/health")
    assert health.status_code == 200
    assert "pricingGaps" in health.json()
    assert "quotas" in health.json()


def test_api_rejects_unknown_window(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/summary", params={"window": "24h"})
    assert response.status_code == 400


def test_api_accepts_requested_windows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    for window in ("15m", "30m", "1h", "3h", "6h", "12h", "1d", "1w", "1mo", "mtd", "ytd", "all", "7d", "30d"):
        response = client.get("/api/spend/summary", params={"window": window})
        assert response.status_code == 200, window


def test_summary_does_not_call_collect_limits(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)

    def boom():
        raise AssertionError("collect_limits must not run for summary")

    monkeypatch.setattr(api_module, "collect_limits", boom)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/summary")
    assert response.status_code == 200
    assert "capacity" in response.json()


def test_api_frozen_payload_keys_and_injected_now(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    frozen = datetime(2026, 8, 30, 23, tzinfo=UTC)
    with connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    client = TestClient(create_app(make_settings(database), enable_scheduler=False, now=frozen))
    summary = client.get("/api/spend/summary").json()
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    health = client.get("/api/spend/health").json()
    for key in (
        "window",
        "generatedAt",
        "cadenceSeconds",
        "cadenceMinutes",
        "status",
        "navigation",
        "coverage",
        "totals",
        "capacity",
        "activity",
        "waste",
        "cacheSavings",
        "projected",
        "mix",
        "series",
        "tools",
        "models",
        "subscriptions",
        "heatmap",
    ):
        assert key in summary
    for key in (
        "trackedValue",
        "priced",
        "publishedRate",
        "tokens",
        "records",
        "sessions",
        "meanSessionValue",
        "cacheReusePct",
        "effectiveCostPerMillionTokens",
        "knownEffectiveCostPerMillionTokens",
        "effectiveCostPricedTokens",
        "effectiveCostCoveragePct",
        "effectiveCostComplete",
    ):
        assert key in summary["totals"]
    assert "subscriptionUsd" in summary["totals"]
    for key in (
        "generatedAt",
        "name",
        "kind",
        "value",
        "shareOfTrackedValue",
        "tokens",
        "mix",
        "opportunity",
        "providerLimits",
        "sessions",
    ):
        assert key in entity
    for key in ("ingest", "quotas", "pricingGaps", "providerVsComputedVariancePct", "generatedAt"):
        assert key in health
    assert {"nav", "total", "knownSpend", "referenceCost"}.isdisjoint(summary)
    assert summary["generatedAt"].startswith("2026-08-30T23:00:00")
    assert entity["generatedAt"].startswith("2026-08-30T23:00:00")
    assert health["generatedAt"].startswith("2026-08-30T23:00:00")
    with connect(database) as connection:
        after = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    assert after == before


def test_api_window_bucket_counts_are_frozen(tmp_path: Path) -> None:
    from spend_app.aggregate import WINDOW_SPECS

    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(
        create_app(
            make_settings(database),
            enable_scheduler=False,
            now=datetime(2026, 8, 30, 23, tzinfo=UTC),
        )
    )
    expected = {
        "15m": 15,
        "30m": 15,
        "1h": 20,
        "3h": 18,
        "6h": 24,
        "12h": 24,
        "1d": 24,
        "1w": 28,
        "1mo": 30,
        "mtd": 28,
        "ytd": 32,
        "all": 32,
    }
    for window, buckets in expected.items():
        payload = client.get("/api/spend/summary", params={"window": window}).json()
        assert payload["window"]["buckets"] == buckets == WINDOW_SPECS[window][1]
        assert len(payload["series"]) == buckets


def test_limits_endpoint_contract(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    monkeypatch.setattr(
        api_module,
        "collect_limits",
        lambda: {
            "generatedAt": "2026-08-31T12:00:00Z",
            "providers": [
                {
                    "key": "codex",
                    "name": "Codex",
                    "status": "exact",
                    "windows": [{"key": "primary", "usedPct": 11}],
                }
            ],
        },
    )
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/limits")
    assert response.status_code == 200
    assert response.json()["providers"][0]["windows"][0]["usedPct"] == 11


def test_api_title_is_burnrate(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    app = create_app(make_settings(database), enable_scheduler=False)
    assert app.title == "BURNRATE"
    assert app.version == "0.1.0-beta.1"
    assert app.version == __version__
    client = TestClient(app)
    payload = client.get("/api/spend").json()
    assert payload["service"] == "BURNRATE"
    assert payload["version"] == "0.1.0-beta.1"


def test_api_security_headers_include_csp(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp


def test_frontend_served_from_packaged_spend_web(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    html = client.get("/")
    assert html.status_code == 200
    assert "BURNRATE" in html.text
    packaged_css = files("spend_web").joinpath("spend.css").read_bytes()
    css = client.get("/spend.css")
    assert css.status_code == 200
    assert css.content == packaged_css
