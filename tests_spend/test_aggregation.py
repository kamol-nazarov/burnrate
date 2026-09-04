from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app.aggregate import (
    WINDOW_SPECS,
    _bucket_plan,
    aggregate_entity,
    aggregate_health,
    aggregate_nav,
    aggregate_summary,
    canonicalize_window,
    resolve_window,
)
from spend_app.db import (
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_agent_run,
    upsert_quota,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
TZ = "America/New_York"
CENTS = Decimal("0.01")


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def test_antigravity_flash_reasoning_levels_share_one_reporting_model(tmp_path: Path) -> None:
    database = tmp_path / "antigravity-combined.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    now = datetime(2026, 9, 3, 18, tzinfo=UTC)
    for raw_id, model, session, input_tokens, output_tokens in (
        ("flash-medium", "antigravity:gemini-3.8-flash", "session-medium", 1_000, 100),
        ("flash-high", "antigravity:gemini-3.8-flash-high", "session-high", 2_000, 200),
    ):
        add_event(
            database,
            pricing,
            raw_id=raw_id,
            tool="antigravity",
            model=model,
            session=session,
            occurred=datetime(2026, 9, 3, 17, tzinfo=UTC),
            input_tokens=input_tokens,
            cached=0,
            writes=0,
            output=output_tokens,
        )

    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=now,
        quotas=[],
        activity=[],
    )
    flash = [model for model in summary["models"] if "gemini-3.8-flash" in model["key"]]
    assert len(flash) == 1
    assert flash[0]["key"] == "antigravity:gemini-3.8-flash"
    assert flash[0]["name"] == "Antigravity Gemini 3.8 Flash"
    assert flash[0]["tokens"] == 3_300
    assert flash[0]["runs"] == 2

    entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="antigravity:gemini-3.8-flash",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=now,
        quotas=[],
    )
    assert entity["name"] == "Antigravity Gemini 3.8 Flash"
    assert entity["tokens"] == 3_300
    assert entity["runs"] == 2
    high_entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="antigravity:gemini-3.8-flash-high",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=now,
        quotas=[],
    )
    assert high_entity["name"] == "Antigravity Gemini 3.8 Flash"
    assert high_entity["tokens"] == entity["tokens"]
    assert high_entity["runs"] == entity["runs"]


def test_cursor_flash_reasoning_levels_share_one_reporting_model(tmp_path: Path) -> None:
    database = tmp_path / "cursor-flash-combined.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    now = datetime(2026, 9, 3, 18, tzinfo=UTC)
    for raw_id, model, session, input_tokens, output_tokens in (
        ("flash-medium", "cursor:gemini-3.8-flash", "session-medium", 1_000, 100),
        ("flash-high", "cursor:gemini-3.8-flash-high", "session-high", 2_000, 200),
    ):
        add_event(
            database,
            pricing,
            raw_id=raw_id,
            tool="cursor",
            model=model,
            session=session,
            occurred=datetime(2026, 9, 3, 17, tzinfo=UTC),
            input_tokens=input_tokens,
            cached=0,
            writes=0,
            output=output_tokens,
        )

    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=now,
        quotas=[],
        activity=[],
    )
    flash = [model for model in summary["models"] if "gemini-3.8-flash" in model["key"]]
    assert len(flash) == 1
    assert flash[0]["key"] == "cursor:gemini-3.8-flash"
    assert flash[0]["name"] == "Cursor Gemini 3.8 Flash"
    assert flash[0]["tokens"] == 3_300
    assert flash[0]["runs"] == 2
    with connect(database) as connection:
        raw_keys = {
            row[0]
            for row in connection.execute("SELECT DISTINCT model_key FROM usage_events")
        }
    assert raw_keys == {"cursor:gemini-3.8-flash", "cursor:gemini-3.8-flash-high"}


def add_event(
    database: Path,
    pricing: PricingEngine,
    *,
    raw_id: str,
    tool: str,
    model: str,
    session: str | None,
    occurred: datetime,
    input_tokens: int,
    cached: int,
    writes: int,
    output: int,
    source: str | None = None,
    cost_usd: float | None = None,
) -> None:
    computed = pricing.compute(
        model_key=model,
        occurred_at=occurred,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=writes,
        output_tokens=output,
    )
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source=source or f"{tool}_local",
                tool_key=tool,
                model_key=model,
                occurred_at=_iso(occurred),
                session_id=session,
                project="fixture-project",
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                cache_write_tokens=writes,
                cache_write_1h_tokens=0,
                output_tokens=output,
                reasoning_tokens=0,
                cost_usd=cost_usd,
                computed_cost_usd=float(computed),
                raw_id=raw_id,
                ingested_at="2026-08-30T23:00:00Z",
                is_exact=tool in {"codex", "claude-code"},
            ),
        )


def fixture_database(tmp_path: Path) -> tuple[Path, PricingEngine]:
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    add_event(
        database,
        pricing,
        raw_id="codex-session-a",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-a",
        occurred=datetime(2026, 8, 30, 22, 55, tzinfo=UTC),
        input_tokens=100_000,
        cached=20_000,
        writes=10_000,
        output=5_000,
    )
    add_event(
        database,
        pricing,
        raw_id="codex-session-b",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-b",
        occurred=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        input_tokens=100_000,
        cached=90_000,
        writes=0,
        output=2_000,
    )
    add_event(
        database,
        pricing,
        raw_id="codex-unattributed",
        tool="codex",
        model="gpt-5.6-sol",
        session=None,
        occurred=datetime(2026, 8, 30, 11, 0, tzinfo=UTC),
        input_tokens=10_000,
        cached=1_000,
        writes=0,
        output=500,
    )
    add_event(
        database,
        pricing,
        raw_id="cursor-low-cache",
        tool="cursor",
        model="cursor:grok-4.6",
        session="cursor-1",
        occurred=datetime(2026, 8, 30, 18, 0, tzinfo=UTC),
        input_tokens=200_000,
        cached=40_000,
        writes=0,
        output=10_000,
    )
    add_event(
        database,
        pricing,
        raw_id="grok-light",
        tool="grok",
        model="supergrok:grok-4.6",
        session="grok-1",
        occurred=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        input_tokens=2_000,
        cached=1_800,
        writes=0,
        output=100,
    )
    with connect(database) as connection:
        upsert_unpriced_event(
            connection,
            UnpricedUsageEvent(
                source="opencode_local",
                tool_key="opencode",
                model_key="opencode:unlisted-model",
                occurred_at="2026-08-30T16:00:00Z",
                session_id="opencode-1",
                project="fixture-project",
                input_tokens=1_000,
                cached_input_tokens=900,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=50,
                reasoning_tokens=0,
                unclassified_tokens=0,
                telemetry_complete=True,
                cost_usd=None,
                raw_id="opencode-unlisted",
                ingested_at="2026-08-30T23:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("cursor", "Cursor Pro", 20, "monthly", "2026-08-01"),
        )
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("grok", "SuperGrok Heavy", 300, "monthly", "2026-08-01"),
        )
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("opencode", "Z.AI Coding Plan", 400, "quarterly", "2026-08-01"),
        )
        upsert_quota(
            connection,
            provider_key="opencode",
            limit_key="5h",
            label="5-hour credits",
            unit="credits",
            source="official_coding_plan",
            polled_at="2026-08-30T22:00:00Z",
            used=0.0,
            allowance=30000.0,
            pct=0.0,
            resets_at="2026-08-31T02:00:00Z",
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
            used=35.0,
            allowance=100.0,
            pct=35.0,
            resets_at="2026-09-06T00:00:00Z",
            is_payg=False,
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
        upsert_agent_run(
            connection,
            id="run-live",
            name="Northwind invoicing",
            model_key="claude-opus-5",
            state="LIVE",
            started_at="2026-08-30T20:00:00Z",
            last_seen_at="2026-08-30T22:59:00Z",
        )
        upsert_agent_run(
            connection,
            id="run-idle",
            name="Inventory weekly close",
            model_key="grok-4.6",
            state="NO DATA",
            started_at="2026-08-30T18:00:00Z",
            last_seen_at="2026-08-30T18:10:00Z",
        )
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES(?,?,?,?,?)",
            ("codex_local", "2026-08-30T22:58:00Z", "2026-08-30T22:59:00Z", "success", 4),
        )
        connection.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,occurrences,sample_raw_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                "opencode:unlisted-model",
                "opencode_local",
                "2026-08-30T16:00:00Z",
                "2026-08-30T16:00:00Z",
                1,
                "opencode-unlisted",
            ),
        )
    return database, pricing


def summarize(database: Path, pricing: PricingEngine, window: str = "1d", tool: str = "all") -> dict:
    return aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key=window,
        tool=tool,
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )


def test_window_resolution_maps_aliases_and_bucket_counts() -> None:
    now = datetime(2026, 8, 30, 23, tzinfo=UTC)
    window = resolve_window("30d", now=now, timezone=TZ)
    assert window.key == "1mo"
    assert window.bucket_count == 30
    assert window.end - window.start == window.previous_end - window.previous_start
    counts = {
        canonicalize_window(key): spec[1]
        for key, spec in {
            "15m": WINDOW_SPECS["15m"],
            "30m": WINDOW_SPECS["30m"],
            "1h": WINDOW_SPECS["1h"],
            "3h": WINDOW_SPECS["3h"],
            "6h": WINDOW_SPECS["6h"],
            "12h": WINDOW_SPECS["12h"],
            "1d": WINDOW_SPECS["1d"],
            "1w": WINDOW_SPECS["1w"],
            "1mo": WINDOW_SPECS["1mo"],
            "mtd": WINDOW_SPECS["mtd"],
            "ytd": WINDOW_SPECS["ytd"],
            "all": WINDOW_SPECS["all"],
        }.items()
    }
    assert list(counts.values()) == [15, 15, 20, 18, 24, 24, 24, 28, 30, 28, 32, 32]


def test_rolling_chart_keeps_interior_bucket_boundaries_stable() -> None:
    first_now = datetime(2026, 9, 1, 18, 23, 1, tzinfo=UTC)
    second_now = datetime(2026, 9, 1, 18, 23, 8, tzinfo=UTC)
    first = _bucket_plan(resolve_window("1d", now=first_now, timezone=TZ), ZoneInfo(TZ))
    second = _bucket_plan(resolve_window("1d", now=second_now, timezone=TZ), ZoneInfo(TZ))
    assert len(first) == 24
    assert len(second) == 24
    assert first[0][0] != second[0][0]
    assert [bucket[0] for bucket in first[1:]] == [bucket[0] for bucket in second[1:]]
    assert first[-1][1] != second[-1][1]


def test_internal_consistency_invariants(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    result = summarize(database, pricing, "1d")

    item_sum = sum((Decimal(str(item["perDay"])) for item in result["waste"]["items"]), Decimal(0))
    per_day = Decimal(str(result["waste"]["perDay"]))
    per_month = Decimal(str(result["waste"]["perMonth"]))
    assert item_sum.quantize(CENTS) == per_day.quantize(CENTS)
    assert (per_day.quantize(CENTS, rounding=ROUND_HALF_UP) * Decimal(30)) == per_month
    assert {item["key"] for item in result["waste"]["items"]} >= {"cache_gap", "plan_underuse", "idle_plan"}
    assert result["cacheSavings"] is None

    series_total = sum(Decimal(str(point["total"])) for point in result["series"])
    assert abs(float(series_total) - result["totals"]["tokens"]) < 1e-6
    tools_by_key = {tool["key"]: tool for tool in result["tools"]}
    for tool in result["tools"]:
        tool_series = sum(Decimal(str(point["byTool"].get(tool["key"], 0))) for point in result["series"])
        assert abs(float(tool_series) - tool["tokens"]) < 1e-6
    assert sum(tool["tokens"] for tool in result["tools"]) == result["totals"]["tokens"]

    usage_values = sum((Decimal(str(tool["value"])) for tool in result["tools"] if tool["value"] is not None), Decimal(0))
    window = resolve_window("1d", now=NOW, timezone=TZ)
    from spend_app.aggregate import _subscription_cost
    with connect(database) as connection:
        subs, _by_tool = _subscription_cost(connection, window.start, window.end, TZ)
    if result["totals"]["trackedValue"] is not None:
        assert abs(float(usage_values + subs) - result["totals"]["trackedValue"]) < 1e-6
        assert abs(
            result["totals"]["priced"] + result["totals"]["publishedRate"] + float(subs)
            - result["totals"]["trackedValue"]
        ) < 1e-6

    assert abs(sum(item["share"] for item in result["mix"]) - 100) <= 0.1
    measured = sum(item["tokens"] for item in result["mix"])
    assert measured == result["totals"]["tokens"]

    cursor = next(model for model in result["models"] if model["key"] == "cursor:grok-4.6")
    unlisted = next(model for model in result["models"] if model["key"] == "opencode:unlisted-model")
    sol = next(model for model in result["models"] if model["key"] == "gpt-5.6-sol")
    assert cursor["isExact"] is False
    assert sol["isExact"] is True
    assert unlisted["value"] is None
    assert unlisted["tokens"] == 1_050
    assert result["totals"]["trackedValue"] is None
    assert isinstance(result["totals"]["priced"], float)
    assert isinstance(result["totals"]["publishedRate"], float)
    assert "$" not in str(result["totals"]["priced"])
    assert result["totals"]["effectiveCostComplete"] is False
    assert result["totals"]["effectiveCostPerMillionTokens"] is None
    assert result["totals"]["knownEffectiveCostPerMillionTokens"] is not None
    assert result["totals"]["effectiveCostPricedTokens"] < result["totals"]["tokens"]
    expected_coverage = (
        result["totals"]["effectiveCostPricedTokens"] / result["totals"]["tokens"] * 100
    )
    assert abs(result["totals"]["effectiveCostCoveragePct"] - expected_coverage) < 1e-9

    for row in result["capacity"]:
        for limit in row["rows"]:
            for field in ("pct", "used", "allowance"):
                assert limit[field] is None or isinstance(limit[field], (int, float))
            assert isinstance(limit["unit"], str)

    assert result["window"]["buckets"] == 24
    assert result["heatmapFallback"] is True
    assert result["activity"][0]["id"] == "run-live"
    assert any(sub["cadence"] == "quarterly" and abs(sub["monthlyEquivalent"] - 400 / 3) < 1e-6 for sub in result["subscriptions"])


def test_tool_filter_reconciles_subscription_only_window(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    result = summarize(database, pricing, "1d", tool="cursor")
    usage = result["tools"][0]["value"] or 0
    assert result["tools"][0]["key"] == "cursor"
    window = resolve_window("1d", now=NOW, timezone=TZ)
    from spend_app.aggregate import _subscription_cost
    with connect(database) as connection:
        _total, by_tool = _subscription_cost(connection, window.start, window.end, TZ)
    expected = usage + float(by_tool.get("cursor", 0))
    if result["totals"]["trackedValue"] is not None:
        assert abs(result["totals"]["trackedValue"] - expected) < 1e-6


def test_entity_share_mix_and_sessions(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    summary = summarize(database, pricing, "1d")
    entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="gpt-5.6-sol",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    assert abs(sum(item["share"] for item in entity["mix"]) - 100) <= 0.1
    assert entity["mix"][0]["key"] == "cached_input"
    # Unpriced rows null the headline tracked total; share must not use the incomplete known total.
    assert entity["value"] is not None
    if summary["totals"]["trackedValue"] is None:
        assert entity["shareOfTrackedValue"] is None
    else:
        assert abs(entity["shareOfTrackedValue"] - (entity["value"] / summary["totals"]["trackedValue"] * 100)) < 1e-6
        assert abs(
            entity["value"] - (entity["shareOfTrackedValue"] / 100) * summary["totals"]["trackedValue"]
        ) < 1e-6
    assert entity["runs"] == 2
    assert len(entity["sessions"]["rows"]) == min(6, entity["runs"])
    shown = sum(row["value"] for row in entity["sessions"]["rows"] if row["value"] is not None)
    assert abs(shown - entity["sessions"]["shownTotal"]) < 1e-6
    assert entity["sessions"]["shownTotal"] < entity["value"]
    assert {row["id"] for row in entity["sessions"]["rows"]} <= {"session-a", "session-b"}
    assert entity["opportunity"]["alreadySaved"] is not None
    assert entity["providerLimits"]
    assert entity["isExact"] is True


def test_two_run_model_does_not_pad_to_six_sessions(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="gpt-5.6-sol",
        window_key="1d",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    assert entity["runs"] == 2
    assert len(entity["sessions"]["rows"]) == 2


def test_health_surfaces_ingest_quotas_and_pricing_state(tmp_path: Path) -> None:
    database, _pricing = fixture_database(tmp_path)
    health = aggregate_health(database_path=database, now=NOW)
    assert health["ingest"][0]["source"] == "codex_local"
    assert health["ingest"][0]["status"] == "success"
    assert "opencode:unlisted-model" in health["pricingGaps"]
    assert any(row["providerKey"] == "opencode" for row in health["quotas"])
    assert health["providerVsComputedVariancePct"] is None
    assert health["generatedAt"]


def test_nav_uses_local_midnight_and_excludes_today_from_burn_rate(tmp_path: Path) -> None:
    database = tmp_path / "nav.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    add_event(
        database,
        pricing,
        raw_id="today-event",
        tool="codex",
        model="gpt-5.6-sol",
        session="nav-session",
        occurred=datetime(2026, 8, 30, 20, tzinfo=UTC),
        input_tokens=1_000,
        cached=400,
        writes=0,
        output=100,
    )
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("cursor", "Cursor Pro", 20, "monthly", "2026-08-01"),
        )
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES(?,?,?,?,?)",
            ("codex_local", "2026-08-30T22:58:00Z", "2026-08-30T22:59:00Z", "success", 1),
        )
    result = aggregate_nav(
        database_path=database,
        pricing=pricing,
        timezone=TZ,
        cadence_minutes=15,
        now=NOW,
    )
    assert result["todayUsd"] > 0
    assert result["burnRatePerDay"] is None
    assert result["dayCoverage"] == []
    assert result["lastRefreshAt"] == "2026-08-30T22:59:00Z"
    assert result["cadenceMinutes"] == 15
    assert result["status"] == "live"


def test_nav_error_names_latest_failing_source(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES(?,?,?,?,?,?)",
            (
                "cursor_admin",
                "2026-08-30T22:58:00Z",
                "2026-08-30T22:59:00Z",
                "failed",
                0,
                "HTTP 500",
            ),
        )
    result = aggregate_nav(
        database_path=database,
        pricing=pricing,
        timezone=TZ,
        now=NOW,
    )
    assert result["status"] == "error"
    assert result["failingSource"] == "cursor_admin"


def test_window_variance_changes_records_tokens_buckets_and_shares(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    short = summarize(database, pricing, "15m")
    day = summarize(database, pricing, "1d")
    week = summarize(database, pricing, "1w")
    assert short["window"]["buckets"] == 15
    assert day["window"]["buckets"] == 24
    assert week["window"]["buckets"] == len(week["series"])
    assert week["window"]["buckets"] >= 7
    assert short["totals"]["records"] < day["totals"]["records"] < week["totals"]["records"]
    assert short["totals"]["tokens"] < day["totals"]["tokens"] < week["totals"]["tokens"]
    def share_map(payload: dict) -> dict[str, float]:
        return {tool["key"]: tool["tokens"] / payload["totals"]["tokens"] for tool in payload["tools"] if payload["totals"]["tokens"]}
    assert share_map(short) != share_map(day)
    assert share_map(day) != share_map(week)
    for window in ("mtd", "ytd", "all"):
        payload = summarize(database, pricing, window)
        assert payload["window"]["key"] == window
        assert len(payload["series"]) == payload["window"]["buckets"]


def test_cache_savings_and_waste_share_rate_basis(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    baseline = summarize(database, pricing, "1d", tool="cursor")
    mutated_prices = []
    for price in pricing.prices:
        if price.model_key == "cursor:grok-4.6":
            mutated_prices.append(replace(price, cached_input_per_mtok=Decimal("0.05")))
        else:
            mutated_prices.append(price)
    mutated = PricingEngine(mutated_prices)
    changed = summarize(database, mutated, "1d", tool="cursor")
    baseline_gap = next(item["perDay"] for item in baseline["waste"]["items"] if item["key"] == "cache_gap")
    changed_gap = next(item["perDay"] for item in changed["waste"]["items"] if item["key"] == "cache_gap")
    assert baseline["cacheSavings"] is not None
    assert changed["cacheSavings"] is not None
    assert baseline["cacheSavings"] != changed["cacheSavings"]
    assert baseline_gap != changed_gap


def test_api_equivalent_fields_exclude_subscription_proration(tmp_path: Path) -> None:
    database = tmp_path / "api-equivalent.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    add_event(
        database,
        pricing,
        raw_id="codex-api-eq",
        tool="codex",
        model="gpt-5.6-sol",
        session="api-eq",
        occurred=datetime(2026, 8, 30, 20, tzinfo=UTC),
        input_tokens=1_000,
        cached=400,
        writes=0,
        output=100,
    )
    without_plan = summarize(database, pricing, "1d")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) VALUES(?,?,?,?,?)",
            ("codex", "ChatGPT Pro", 200, "monthly", "2026-08-01"),
        )
    with_plan = summarize(database, pricing, "1d")
    assert without_plan["totals"]["subscriptionUsd"] == 0
    assert with_plan["totals"]["subscriptionUsd"] > 0
    assert with_plan["totals"]["publishedRate"] == without_plan["totals"]["publishedRate"]
    assert with_plan["totals"]["priced"] == without_plan["totals"]["priced"]
    assert (
        with_plan["totals"]["effectiveCostPerMillionTokens"]
        == without_plan["totals"]["effectiveCostPerMillionTokens"]
    )
    assert with_plan["totals"]["effectiveCostPerMillionTokens"] is not None
    assert with_plan["totals"]["trackedValue"] == (
        with_plan["totals"]["priced"]
        + with_plan["totals"]["publishedRate"]
        + with_plan["totals"]["subscriptionUsd"]
    )
    assert with_plan["totals"]["trackedValue"] != (
        with_plan["totals"]["priced"] + with_plan["totals"]["publishedRate"]
    )


def test_quarterly_subscription_uses_amount_over_three(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    result = summarize(database, pricing, "1d")
    zai = next(row for row in result["subscriptions"] if row["cadence"] == "quarterly")
    assert zai["amountUsd"] == 400
    assert abs(zai["monthlyEquivalent"] - 400 / 3) < 1e-9
