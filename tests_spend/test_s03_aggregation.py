from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.aggregate import (
    aggregate_entity,
    aggregate_health,
    aggregate_summary,
)
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize, upsert_quota
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription, materialize_subscription_days
from tests_spend.test_codex_local import write_session
from tests_spend.test_s01_admin_fixtures import _paged_client
from spend_app.adapters.codex_local import ingest as ingest_codex
from spend_app.adapters.codex_local import reset_file_cache as reset_codex_cache


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
TZ = "America/New_York"
CENTS = Decimal("0.01")

NAMED_WINDOWS = (
    ("15m", 15),
    ("30m", 15),
    ("1h", 20),
    ("3h", 18),
    ("6h", 24),
    ("12h", 24),
    ("1d", 24),
    ("1w", 28),
    ("1mo", 30),
    ("mtd", 28),
    ("ytd", 32),
    ("all", 32),
)
ALIASES = (("7d", "1w", 28), ("30d", "1mo", 30), ("MTD", "mtd", 28), ("YTD", "ytd", 32), ("All", "all", 32))

SUMMARY_REQUIRED = (
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
)
ENTITY_REQUIRED = (
    "generatedAt",
    "window",
    "name",
    "kind",
    "providerKey",
    "providerName",
    "plan",
    "isExact",
    "color",
    "value",
    "shareOfTrackedValue",
    "tokens",
    "shareOfTokens",
    "cachePct",
    "outputTokens",
    "runs",
    "valuePerRun",
    "tokensPerRun",
    "series",
    "mix",
    "opportunity",
    "providerLimits",
    "sessions",
)
HEALTH_REQUIRED = (
    "generatedAt",
    "ingest",
    "quotas",
    "pricingGaps",
    "providerVsComputedVariancePct",
)
FORBIDDEN = {"nav", "total", "knownSpend", "referenceCost"}
MONEY_LEAVES = {
    "trackedValue",
    "priced",
    "publishedRate",
    "subscriptionUsd",
    "burnRatePerDay",
    "todayUsd",
    "planCost",
    "value",
    "amountUsd",
    "monthlyEquivalent",
    "shownTotal",
    "perDay",
    "perMonth",
    "cacheSavings",
}


def make_settings(database: Path) -> Settings:
    return Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        cursor_import_path=database.parent / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone=TZ,
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def client_for(database: Path, now: datetime = NOW) -> TestClient:
    return TestClient(create_app(make_settings(database), enable_scheduler=False, now=now))


def _usage(**overrides) -> UsageRow:
    base = dict(
        source="codex_local",
        tool_key="codex",
        model_key="gpt-5.6-sol",
        occurred_at=datetime(2026, 8, 30, 22, 55, tzinfo=UTC),
        session_id="session-a",
        project="fixture",
        input_tokens=100_000,
        cached_input_tokens=20_000,
        cache_write_tokens=10_000,
        cache_write_1h_tokens=0,
        output_tokens=5_000,
        reasoning_tokens=50,
        cost_usd=None,
        raw_id="codex-local:s03:a",
    )
    base.update(overrides)
    return UsageRow(**base)


def _persist(database: Path, rows: list[UsageRow], source: str = "codex_local") -> dict:
    return persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source=source,
        usage_rows=rows,
    )


def mixed_database(tmp_path: Path) -> Path:
    database = tmp_path / "mixed.db"
    _persist(
        database,
        [
            _usage(),
            _usage(
                raw_id="codex-local:s03:b",
                session_id="session-b",
                occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
                input_tokens=80_000,
                cached_input_tokens=10_000,
                cache_write_tokens=0,
                output_tokens=2_000,
            ),
        ],
        source="codex_local",
    )
    _persist(
        database,
        [
            _usage(
                source="cursor_local",
                tool_key="cursor",
                model_key="cursor:grok-4.6",
                raw_id="cursor-local:s03",
                session_id="cursor-1",
                occurred_at=datetime(2026, 8, 30, 18, 0, tzinfo=UTC),
                input_tokens=200_000,
                cached_input_tokens=40_000,
                cache_write_tokens=0,
                output_tokens=10_000,
            )
        ],
        source="cursor_local",
    )
    _persist(
        database,
        [
            _usage(
                source="grok_local",
                tool_key="grok",
                model_key="supergrok:grok-4.6",
                raw_id="grok-local:s03",
                session_id="grok-1",
                occurred_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
                input_tokens=2_000,
                cached_input_tokens=1_800,
                cache_write_tokens=0,
                output_tokens=100,
            )
        ],
        source="grok_local",
    )
    _persist(
        database,
        [
            _usage(
                source="opencode_local",
                tool_key="opencode",
                model_key="opencode:unlisted-model",
                raw_id="opencode-local:s03:unlisted",
                session_id="opencode-1",
                occurred_at=datetime(2026, 8, 30, 16, 0, tzinfo=UTC),
                input_tokens=1_000,
                cached_input_tokens=900,
                cache_write_tokens=0,
                output_tokens=50,
            )
        ],
        source="opencode_local",
    )
    _persist(
        database,
        [
            _usage(
                source="claude_local",
                tool_key="claude-code",
                model_key="claude-opus-5",
                raw_id="claude-local:s03:aug11",
                session_id="claude-aug11",
                occurred_at=datetime(2026, 8, 11, 16, tzinfo=UTC),
                input_tokens=2_030,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=20,
            )
        ],
        source="claude_local",
    )
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("cursor", "Cursor Pro", 20, "monthly", "2026-08-01"),
        )
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("cursor", "Cursor Free", 0, "monthly", "2026-08-01"),
        )
        upsert_quota(
            connection,
            provider_key="codex",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="codex_local",
            polled_at="2026-08-30T22:00:00Z",
            used=23.0,
            allowance=100.0,
            pct=23.0,
            resets_at="2026-09-06T02:25:00Z",
            is_payg=False,
        )
        upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="traycer_local",
            polled_at="2026-08-30T22:00:00Z",
            used=80.0,
            allowance=100.0,
            pct=80.0,
            resets_at="2026-09-06T00:00:00Z",
            is_payg=False,
        )
        upsert_quota(
            connection,
            provider_key="claude-code",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="claude_local",
            polled_at="2026-08-30T22:00:00Z",
            used=None,
            allowance=None,
            pct=None,
            resets_at=None,
            is_payg=False,
        )
    return database


def priced_database(tmp_path: Path) -> Path:
    database = tmp_path / "priced.db"
    _persist(
        database,
        [
            _usage(),
            _usage(
                raw_id="codex-local:s03:priced-b",
                session_id="session-b",
                occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            ),
        ],
    )
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("codex", "ChatGPT Pro", 200, "monthly", "2026-08-01"),
        )
    return database


def measured_sql(connection, start: str, end: str) -> int:
    sql = """
        SELECT COALESCE(SUM(
            cached_input_tokens
            + CASE WHEN input_tokens > cached_input_tokens
                   THEN input_tokens - cached_input_tokens ELSE 0 END
            + cache_write_tokens
            + output_tokens
        ), 0)
        FROM {table}
        WHERE occurred_at >= ? AND occurred_at < ?
    """
    priced = connection.execute(sql.format(table="usage_events"), (start, end)).fetchone()[0]
    unpriced = connection.execute(sql.format(table="unpriced_usage_events"), (start, end)).fetchone()[0]
    return int(priced) + int(unpriced)


def walk_money(payload: object) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []

    def inner(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                inner(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                inner(value, f"{path}[{index}]")
        else:
            leaf = path.rsplit(".", 1)[-1]
            if leaf in MONEY_LEAVES:
                found.append((path, node))

    inner(payload, "")
    return found


def test_s03_01_waste_items_sum_to_headline(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    summary = client_for(database).get("/api/spend/summary", params={"window": "1d"}).json()
    items = summary["waste"]["items"]
    assert any(item["key"] == "cache_gap" for item in items)
    item_sum = sum((Decimal(str(item["perDay"])) for item in items), Decimal(0)).quantize(CENTS)
    per_day = Decimal(str(summary["waste"]["perDay"])).quantize(CENTS)
    assert item_sum == per_day
    assert Decimal(str(summary["waste"]["perMonth"])) == (
        Decimal(str(summary["waste"]["perDay"])).quantize(CENTS, rounding=ROUND_HALF_UP) * Decimal(30)
    )


def test_s03_01_empty_waste_is_zero_not_leftover(tmp_path: Path) -> None:
    database = tmp_path / "empty-waste.db"
    initialize(database)
    summary = client_for(database).get("/api/spend/summary").json()
    if not summary["waste"]["items"]:
        assert summary["waste"]["perDay"] in {0, 0.0, None}


def test_s03_02_series_matches_token_kpi(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    client = client_for(database)
    for window, buckets in (("1d", 24), ("1w", 28), ("1mo", 30), ("mtd", 28)):
        payload = client.get("/api/spend/summary", params={"window": window}).json()
        assert len(payload["series"]) == payload["window"]["buckets"] == buckets
        series_total = sum(point["total"] for point in payload["series"])
        if window in {"1w", "1mo", "mtd"}:
            assert int(round(series_total)) == payload["totals"]["tokens"]
        else:
            assert abs(series_total - payload["totals"]["tokens"]) <= len(payload["tools"]) + 1e-6
        for tool in payload["tools"]:
            tool_series = sum(point["byTool"].get(tool["key"], 0) for point in payload["series"])
            remainder = abs(tool_series - tool["tokens"])
            if window in {"1w", "1mo", "mtd"}:
                assert int(round(tool_series)) == tool["tokens"]
            else:
                assert remainder <= 1 + 1e-9
        for point in payload["series"]:
            assert abs(point["total"] - sum(point["byTool"].values())) < 1e-9
        last = payload["series"][-1]["total"]
        assert last != payload["totals"]["tokens"] or payload["totals"]["tokens"] == 0
    month = client.get("/api/spend/summary", params={"window": "1mo"}).json()
    labeled = [point for point in month["series"] if point["label"] == "Aug 11"]
    assert labeled
    assert sum(point["total"] for point in labeled) > 0


def test_s03_03_tools_subscriptions_and_tracked_value(tmp_path: Path) -> None:
    priced = priced_database(tmp_path)
    client = client_for(priced)
    summary = client.get("/api/spend/summary", params={"window": "1d"}).json()
    assert summary["totals"]["trackedValue"] is not None
    assert sum(tool["tokens"] for tool in summary["tools"]) == summary["totals"]["tokens"]
    usage = sum(tool["value"] for tool in summary["tools"] if tool["value"] is not None)
    assert "subscriptionUsd" in summary["totals"]
    monthly_sum = sum(
        row["monthlyEquivalent"]
        for row in summary["subscriptions"]
        if row["monthlyEquivalent"] is not None
    )
    assert abs(summary["totals"]["subscriptionUsd"] - monthly_sum) > 1e-6 or monthly_sum == 0
    assert abs(
        summary["totals"]["priced"]
        + summary["totals"]["publishedRate"]
        + summary["totals"]["subscriptionUsd"]
        - summary["totals"]["trackedValue"]
    ) < 1e-6
    assert abs(usage + summary["totals"]["subscriptionUsd"] - summary["totals"]["trackedValue"]) < 1e-6

    mixed = mixed_database(tmp_path)
    mixed_summary = client_for(mixed).get("/api/spend/summary", params={"window": "1d"}).json()
    assert mixed_summary["totals"]["trackedValue"] is None
    cursor = client_for(mixed).get("/api/spend/summary", params={"window": "1d", "tool": "cursor"}).json()
    if cursor["totals"]["trackedValue"] is not None:
        tool_value = cursor["tools"][0]["value"] or 0
        assert abs(cursor["totals"]["trackedValue"] - (tool_value + cursor["totals"]["subscriptionUsd"])) < 1e-6


def test_s03_03_cross_source_tokens_sum(tmp_path: Path) -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    now = datetime.fromtimestamp(1788220800, UTC)
    session = tmp_path / "desktop.jsonl"
    write_session(session)
    combined = tmp_path / "combined.db"
    reset_codex_cache()
    ingest_codex(database_path=combined, pricing=pricing, session_glob=str(session))
    ingest_openai_admin(
        database_path=combined,
        pricing=pricing,
        admin_key="test-admin-key",
        start=datetime.fromtimestamp(1788048000, UTC),
        end=now,
        client=_paged_client(),
    )
    payload = client_for(combined, now=now).get(
        "/api/spend/summary", params={"window": "all", "tool": "codex"}
    ).json()
    with connect(combined) as connection:
        sources = {
            row[0]: row[1]
            for row in connection.execute("SELECT raw_id, source FROM usage_events")
        }
        union = measured_sql(connection, payload["window"]["from"], payload["window"]["to"])
    assert payload["totals"]["tokens"] == union
    assert all(
        (raw_id.startswith("codex-local:") and source == "codex_local")
        or (raw_id.startswith("openai-usage:") and source == "openai_admin")
        for raw_id, source in sources.items()
    )
    assert not (
        {raw_id for raw_id in sources if raw_id.startswith("codex-local:")}
        & {raw_id for raw_id in sources if raw_id.startswith("openai-usage:")}
    )


def test_s03_04_mix_shares_and_empty_null(tmp_path: Path) -> None:
    mixed = mixed_database(tmp_path)
    client = client_for(mixed)
    summary = client.get("/api/spend/summary", params={"window": "1d"}).json()
    entity = client.get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    for payload, token_total in ((summary, summary["totals"]["tokens"]), (entity, entity["tokens"])):
        assert [item["key"] for item in payload["mix"]] == ["cached_input", "fresh_input", "output"]
        assert abs(sum(item["share"] for item in payload["mix"]) - 100) <= 0.1
        assert sum(item["tokens"] for item in payload["mix"]) == token_total
    empty = tmp_path / "empty.db"
    initialize(empty)
    empty_summary = client_for(empty).get("/api/spend/summary").json()
    assert empty_summary["totals"]["tokens"] == 0
    for item in empty_summary["mix"]:
        assert item["share"] is None


def test_s03_05_share_two_fixtures(tmp_path: Path) -> None:
    priced = priced_database(tmp_path)
    client = client_for(priced)
    summary = client.get("/api/spend/summary", params={"window": "1d"}).json()
    entity = client.get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    assert summary["totals"]["trackedValue"] is not None
    assert entity["value"] is not None
    assert entity["shareOfTrackedValue"] is not None
    expected = entity["value"] / summary["totals"]["trackedValue"] * 100
    assert abs(entity["shareOfTrackedValue"] - expected) < 1e-6 or abs(
        entity["shareOfTrackedValue"] - expected
    ) < 1e-9

    mixed = mixed_database(tmp_path)
    mixed_client = client_for(mixed)
    mixed_summary = mixed_client.get("/api/spend/summary", params={"window": "1d"}).json()
    mixed_entity = mixed_client.get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    assert mixed_summary["totals"]["trackedValue"] is None
    assert mixed_entity["value"] is not None
    assert mixed_entity["shareOfTrackedValue"] is None


def test_s03_06_sessions_equality_and_no_pad(tmp_path: Path) -> None:
    priced = priced_database(tmp_path)
    entity = client_for(priced).get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    assert entity["runs"] == 2
    assert len(entity["sessions"]["rows"]) == 2
    shown = sum(row["value"] for row in entity["sessions"]["rows"] if row["value"] is not None)
    assert abs(shown - entity["sessions"]["shownTotal"]) < 1e-6
    assert entity["sessions"]["shownTotal"] < entity["value"]
    assert {row["id"] for row in entity["sessions"]["rows"]} == {"session-a", "session-b"}

    mixed = mixed_database(tmp_path)
    unattributed = tmp_path / "unattr.db"
    _persist(
        unattributed,
        [
            _usage(),
            _usage(
                raw_id="codex-local:s03:none",
                session_id=None,
                occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            ),
        ],
    )
    unattr_entity = client_for(unattributed).get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    assert unattr_entity["runs"] == 1
    assert len(unattr_entity["sessions"]["rows"]) == 1
    assert unattr_entity["sessions"]["shownTotal"] < unattr_entity["value"]

    many = tmp_path / "many.db"
    rows = [
        _usage(
            raw_id=f"codex-local:s03:s{index}",
            session_id=f"session-{index}",
            occurred_at=datetime(2026, 8, 30, 22, 0, tzinfo=UTC) + timedelta(minutes=index),
            input_tokens=1_000 + index,
            cached_input_tokens=100,
            cache_write_tokens=0,
            output_tokens=50,
        )
        for index in range(8)
    ]
    _persist(many, rows)
    many_entity = client_for(many).get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    assert many_entity["runs"] == 8
    assert len(many_entity["sessions"]["rows"]) == 6
    assert many_entity["sessions"]["shownTotal"] < many_entity["value"]
    assert {row["id"] for row in many_entity["sessions"]["rows"]} <= {f"session-{index}" for index in range(8)}
    _ = mixed


def test_s03_07_shared_rate_basis(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    pricing = PricingEngine.load(ROOT / "pricing")
    mixed_all = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    assert mixed_all["cacheSavings"] is None
    baseline = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="cursor",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    baseline_entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="cursor:grok-4.6",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    mutated = PricingEngine(
        [
            replace(price, cached_input_per_mtok=Decimal("0.05"))
            if price.model_key == "cursor:grok-4.6"
            else price
            for price in pricing.prices
        ]
    )
    changed = aggregate_summary(
        database_path=database,
        pricing=mutated,
        window_key="1d",
        tool="cursor",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    changed_entity = aggregate_entity(
        database_path=database,
        pricing=mutated,
        kind="model",
        key="cursor:grok-4.6",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    baseline_gap = next(item["perDay"] for item in baseline["waste"]["items"] if item["key"] == "cache_gap")
    changed_gap = next(item["perDay"] for item in changed["waste"]["items"] if item["key"] == "cache_gap")
    assert baseline["cacheSavings"] != changed["cacheSavings"]
    assert baseline_gap != changed_gap
    assert baseline_entity["opportunity"]["alreadySaved"] != changed_entity["opportunity"]["alreadySaved"]
    assert baseline_entity["opportunity"]["amount"] != changed_entity["opportunity"]["amount"]


def test_s03_08_and_10_all_twelve_windows(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    client = client_for(database)
    with connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    tokens = {}
    records = {}
    shares = {}
    for window, buckets in NAMED_WINDOWS:
        response = client.get("/api/spend/summary", params={"window": window})
        assert response.status_code == 200, window
        payload = response.json()
        for key in SUMMARY_REQUIRED:
            assert key in payload, key
        assert FORBIDDEN.isdisjoint(payload)
        assert payload["window"]["key"] == canonicalize_or_key(window)
        assert payload["window"]["buckets"] == buckets
        assert len(payload["series"]) == buckets
        tokens[window] = payload["totals"]["tokens"]
        records[window] = payload["totals"]["records"]
        total = payload["totals"]["tokens"]
        shares[window] = {
            tool["key"]: tool["tokens"] / total for tool in payload["tools"] if total
        }
    default = client.get("/api/spend/summary").json()
    assert default["window"]["key"] == "1d"
    assert default["window"]["buckets"] == 24
    assert tokens["15m"] > 0
    assert tokens["15m"] < tokens["1d"] < tokens["1w"]
    assert records["15m"] < records["1d"] < records["1w"]
    assert shares["15m"] != shares["1d"]
    assert shares["1d"] != shares["1w"]
    for alias, key, buckets in ALIASES:
        payload = client.get("/api/spend/summary", params={"window": alias}).json()
        assert payload["window"]["key"] == key
        assert payload["window"]["buckets"] == buckets
    with connect(database) as connection:
        after = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    assert after == before


def canonicalize_or_key(window: str) -> str:
    from spend_app.aggregate import canonicalize_window

    return canonicalize_window(window)


def test_s03_09_approx_marker_and_missing_not_zero(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    payload = client_for(database).get("/api/spend/summary", params={"window": "1d"}).json()
    cursor = next(model for model in payload["models"] if model["key"] == "cursor:grok-4.6")
    sol = next(model for model in payload["models"] if model["key"] == "gpt-5.6-sol")
    unlisted = next(model for model in payload["models"] if model["key"] == "opencode:unlisted-model")
    assert cursor["isExact"] is False
    assert cursor["value"] is not None
    assert cursor["valueMarker"] == "≈"
    assert sol["isExact"] is True
    assert sol["valueMarker"] is None
    assert unlisted["value"] is None
    assert unlisted["valueMarker"] is None
    for path, value in walk_money(payload):
        assert "$" not in str(value)
        if "unlisted" in path.lower():
            assert value is None
    free = next(row for row in payload["subscriptions"] if row["amountUsd"] is None)
    assert free["planState"] == "no paid plan"
    assert free["monthlyEquivalent"] is None
    claude = next(card for card in payload["capacity"] if card["providerKey"] == "claude-code")
    for row in claude["rows"]:
        assert row["pct"] is None
        assert row["used"] is None


def test_s03_11_entity_keys_and_bad_kind(tmp_path: Path) -> None:
    database = priced_database(tmp_path)
    client = client_for(database)
    payload = client.get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    for key in ENTITY_REQUIRED:
        assert key in payload, key
    for key in ("kind", "amount", "detail", "fix", "alreadySaved"):
        assert key in payload["opportunity"]
    assert "shownShare" in payload["sessions"]
    assert "shownTotal" in payload["sessions"]
    assert "rows" in payload["sessions"]
    bad = client.get("/api/spend/entity", params={"kind": "session", "key": "x", "window": "1d"})
    assert bad.status_code >= 400


def test_s03_12_health_partial_and_missing_credential(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    client = client_for(database)
    health = client.get("/api/spend/health").json()
    for key in HEALTH_REQUIRED:
        assert key in health
    ingest = {row["source"]: row for row in health["ingest"]}
    assert ingest["opencode_local"]["status"] == "partial"
    assert ingest["opencode_local"]["status"] != "failed"
    assert ingest["opencode_local"]["lastSuccess"]
    from spend_app.adapters.openai_admin import ingest as ingest_openai

    ingest_openai(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        admin_key=None,
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
    )
    health2 = client_for(database).get("/api/spend/health").json()
    openai = next(row for row in health2["ingest"] if row["source"] == "openai_admin")
    assert openai["status"] == "unavailable"
    assert "credential missing" in (openai["error"] or "")


def test_s03_13_http_matches_aggregate_same_now(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    pricing = PricingEngine.load(ROOT / "pricing")
    with connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    client = client_for(database)
    http_summary = client.get("/api/spend/summary", params={"window": "1d"}).json()
    http_entity = client.get(
        "/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}
    ).json()
    http_health = client.get("/api/spend/health").json()
    fn_summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    fn_entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="gpt-5.6-sol",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    fn_health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    for left, right, fields in (
        (http_summary["totals"], fn_summary["totals"], ("trackedValue", "priced", "publishedRate", "tokens", "records")),
        (http_entity, fn_entity, ("value", "tokens", "shareOfTrackedValue", "runs")),
        (http_health, fn_health, ("pricingGaps", "providerVsComputedVariancePct")),
    ):
        for field in fields:
            assert left[field] == right[field], field
    with connect(database) as connection:
        after = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    assert after == before


def test_s03_14_capacity_order_and_heatmap_fallback(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    summary = client_for(database).get("/api/spend/summary", params={"window": "1d"}).json()
    cards = summary["capacity"]
    payg = [card for card in cards if card["isPayg"]]
    non_payg = [card for card in cards if not card["isPayg"]]
    numeric = [card["peakPct"] for card in non_payg if card["peakPct"] is not None]
    assert numeric == sorted(numeric, reverse=True)
    if payg:
        assert cards[-1]["isPayg"] is True
    assert summary["heatmapFallback"] is True
    for point in summary["heatmap"]:
        assert set(point) >= {"weekday", "hour", "value"}
        assert 0 <= point["weekday"] <= 6
        assert 0 <= point["hour"] <= 23
    by_key = {card["providerKey"]: card for card in cards}
    for provider in ("claude-code", "grok", "codex", "cursor", "opencode", "openrouter"):
        assert provider in by_key
    assert cards[-1]["providerKey"] == "openrouter"
    openrouter = by_key["openrouter"]
    assert openrouter["rows"][0]["etaLabel"] == "available"
    assert openrouter["rows"][0]["pct"] is None


def test_s03_capacity_emits_six_providers_when_quotas_empty(tmp_path: Path) -> None:
    database = tmp_path / "empty-quotas.db"
    initialize(database)
    summary = client_for(database).get("/api/spend/summary").json()
    by_key = {card["providerKey"]: card for card in summary["capacity"]}
    for provider in ("claude-code", "grok", "codex", "cursor", "opencode", "openrouter"):
        assert provider in by_key, provider
        for row in by_key[provider]["rows"]:
            assert row["pct"] is None
            assert row["pct"] not in {0, 0.0}
    assert summary["capacity"][-1]["providerKey"] == "openrouter"
    assert summary["capacity"][-1]["isPayg"] is True
    assert "no persisted quota snapshot" in (by_key["codex"]["rows"][0]["source"] or "")
    assert by_key["openrouter"]["rows"][0]["etaLabel"] == "available"


def test_s03_15_no_regression_partial_replay_and_gaps(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    import sqlite3

    with sqlite3.connect(database) as raw:
        raw.executescript(
            """
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, tool_key TEXT NOT NULL, model_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL, session_id TEXT, project TEXT,
                input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER,
                cost_usd REAL, computed_cost_usd REAL NOT NULL, raw_id TEXT NOT NULL UNIQUE,
                ingested_at TEXT NOT NULL
            );
            CREATE TABLE unpriced_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, tool_key TEXT NOT NULL, model_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL, session_id TEXT, project TEXT,
                input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                cache_write_tokens INTEGER NOT NULL, cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER,
                unclassified_tokens INTEGER NOT NULL DEFAULT 0,
                telemetry_complete INTEGER NOT NULL DEFAULT 1, cost_usd REAL,
                raw_id TEXT NOT NULL UNIQUE, ingested_at TEXT NOT NULL
            );
            CREATE TABLE pricing_gaps (
                model_key TEXT PRIMARY KEY, source TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1, sample_raw_id TEXT NOT NULL
            );
            CREATE TABLE pricing_gap_events (
                raw_id TEXT PRIMARY KEY, model_key TEXT NOT NULL,
                source TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            """
        )
        raw.execute("INSERT INTO app_meta(key, value) VALUES('schema_version', '7')")
        raw.execute(
            "INSERT INTO pricing_gap_events(raw_id,model_key,source,occurred_at) "
            "VALUES('claude-local:hist:1','claude-opus-5','claude_local','2026-07-25T12:00:00Z')"
        )
        raw.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,occurrences,sample_raw_id) "
            "VALUES('claude-opus-5','claude_local','2026-07-25T12:00:00Z','2026-07-25T12:00:00Z',1,'claude-local:hist:1')"
        )
        raw.commit()
    initialize(database)
    empty = persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=[],
    )
    assert empty["status"] in {"success", "partial", "skipped"}
    health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    assert "claude-opus-5" in health["pricingGaps"]

    rows = [_usage(), _usage(model_key="unpriced-model", raw_id="codex-local:s03:gap")]
    first = persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=rows,
    )
    assert first["status"] == "partial"
    with connect(database) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "usage_events",
                "unpriced_usage_events",
                "provider_cost_buckets",
                "pricing_gap_events",
            )
        }
    second = persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=rows,
    )
    assert second["eventsWritten"] == 0
    with connect(database) as connection:
        for table, count in counts.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count


def test_s03_17_sql_token_oracle(tmp_path: Path) -> None:
    database = mixed_database(tmp_path)
    payload = client_for(database).get("/api/spend/summary", params={"window": "1d"}).json()
    with connect(database) as connection:
        union = measured_sql(connection, payload["window"]["from"], payload["window"]["to"])
        priced_only = connection.execute(
            """
            SELECT COALESCE(SUM(
                cached_input_tokens
                + CASE WHEN input_tokens > cached_input_tokens
                       THEN input_tokens - cached_input_tokens ELSE 0 END
                + cache_write_tokens + output_tokens
            ), 0)
            FROM usage_events
            WHERE occurred_at >= ? AND occurred_at < ?
            """,
            (payload["window"]["from"], payload["window"]["to"]),
        ).fetchone()[0]
    assert payload["totals"]["tokens"] == union
    assert union != priced_only


def test_s03_18_no_future_subscription_money(tmp_path: Path) -> None:
    database = priced_database(tmp_path)
    today = date(2026, 8, 30)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscription_daily_costs")
        connection.execute("DELETE FROM subscriptions")
        add_subscription(
            connection,
            tool_key="codex",
            name="Codex",
            amount_usd=310.0,
            cadence="monthly",
            start_date="2026-08-01",
            end_date=None,
        )
        materialize_subscription_days(connection, start=today.replace(day=1), end=today)
    client = client_for(database)
    before = client.get("/api/spend/summary", params={"window": "1mo"}).json()
    with connect(database) as connection:
        materialize_subscription_days(
            connection, start=today + timedelta(days=1), end=today + timedelta(days=40)
        )
        future_rows = connection.execute(
            "SELECT COUNT(*) FROM subscription_daily_costs WHERE date > ?",
            (today.isoformat(),),
        ).fetchone()[0]
    assert future_rows > 0
    after = client.get("/api/spend/summary", params={"window": "1mo"}).json()
    assert walk_money(before) == walk_money(after)
    assert before["projected"]["planCost"] == after["projected"]["planCost"]
    assert before["totals"]["subscriptionUsd"] == after["totals"]["subscriptionUsd"]
