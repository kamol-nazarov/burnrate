import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from spend_app.adapters.claude_local import ingest as ingest_claude
from spend_app.adapters.claude_local import reset_file_cache as reset_claude_cache
from spend_app.adapters.codex_local import ingest as ingest_codex
from spend_app.adapters.codex_local import reset_file_cache as reset_codex_cache
from spend_app.adapters.common import CostRow, UsageRow, persist_rows
from spend_app.aggregate import aggregate_entity, aggregate_health, aggregate_nav, aggregate_summary
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription, materialize_subscription_days
from tests_spend.test_claude_local import write_fixture as write_claude_fixture
from tests_spend.test_codex_local import write_session as write_codex_session


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
TZ = "America/New_York"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _usage_row(**overrides) -> UsageRow:
    base = dict(
        source="codex_local",
        tool_key="codex",
        model_key="gpt-5.6-sol",
        occurred_at=datetime(2026, 8, 30, 20, tzinfo=UTC),
        session_id="s01-session",
        project="fixture",
        input_tokens=1000,
        cached_input_tokens=400,
        cache_write_tokens=100,
        cache_write_1h_tokens=0,
        output_tokens=200,
        reasoning_tokens=50,
        cost_usd=None,
        raw_id="codex-local:s01:priced",
    )
    base.update(overrides)
    return UsageRow(**base)


def _counts(connection) -> dict[str, int]:
    tables = (
        "usage_events",
        "unpriced_usage_events",
        "provider_cost_buckets",
        "pricing_gap_events",
        "coverage_gap_events",
        "sessions",
        "subscription_daily_costs",
    )
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
    }


def _money_leaves(payload: object) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            leaf = path.rsplit(".", 1)[-1]
            if leaf in {
                "trackedValue",
                "priced",
                "publishedRate",
                "burnRatePerDay",
                "todayUsd",
                "planCost",
                "value",
                "amountUsd",
                "monthlyEquivalent",
                "shownTotal",
            }:
                found.append((path, float(node)))

    walk(payload, "")
    return found


def test_s01_01_unpriced_tokens_are_staged(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    priced = _usage_row()
    unpriced = _usage_row(
        model_key="unpriced-model",
        raw_id="codex-local:s01:unpriced",
        input_tokens=500,
        cached_input_tokens=100,
        cache_write_tokens=0,
        output_tokens=50,
    )
    result = persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[priced, unpriced],
    )
    assert result["status"] == "partial"
    assert result["eventsWritten"] == 1
    assert result["unpricedEventsWritten"] == 1
    with connect(database) as connection:
        staged = connection.execute(
            "SELECT input_tokens, cached_input_tokens, output_tokens, telemetry_complete "
            "FROM unpriced_usage_events WHERE raw_id=?",
            (unpriced.raw_id,),
        ).fetchone()
        gaps = [
            row[0] for row in connection.execute("SELECT model_key FROM pricing_gaps")
        ]
    assert tuple(staged) == (500, 100, 50, 1)
    assert gaps == ["unpriced-model"]


def test_s01_04_health_treats_partial_as_healthy_with_gap(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=[
            _usage_row(),
            _usage_row(model_key="unpriced-model", raw_id="codex-local:s01:gap"),
        ],
    )
    health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    ingest = next(item for item in health["ingest"] if item["source"] == "codex_local")
    assert ingest["status"] == "partial"
    assert ingest["status"] != "failed"
    assert ingest["lastSuccess"]
    assert "unpriced-model" in health["pricingGaps"]


def test_s01_08_malformed_row_is_quarantined_and_does_not_raise(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    good = _usage_row()
    bad = _usage_row(
        raw_id="codex-local:s01:malformed",
        input_tokens=100,
        cached_input_tokens=150,
    )
    result = persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[good, bad],
    )
    assert result["status"] == "partial"
    assert result["quarantined"] == 1
    assert result["eventsWritten"] == 1
    with connect(database) as connection:
        issue = connection.execute(
            "SELECT issue FROM coverage_gap_events WHERE raw_id=?",
            (bad.raw_id,),
        ).fetchone()[0]
        persisted = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert "cached_input_tokens cannot exceed input_tokens" in issue
    assert persisted == 1


def test_s01_09_second_persist_of_same_rows_writes_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    rows = [
        _usage_row(),
        _usage_row(model_key="unpriced-model", raw_id="codex-local:s01:unpriced"),
    ]
    costs = [
        CostRow(
            source="openai_admin",
            starting_at=datetime(2026, 8, 30, tzinfo=UTC),
            ending_at=datetime(2026, 8, 31, tzinfo=UTC),
            project_id="proj",
            line_item="Model usage",
            model_key=None,
            cost_usd=1.25,
            raw_id="openai-cost:s01",
        )
    ]
    first = persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=rows,
        cost_rows=costs,
    )
    assert first["eventsWritten"] == 1
    with connect(database) as connection:
        before = _counts(connection)
        occurrences = connection.execute(
            "SELECT occurrences FROM pricing_gaps WHERE model_key='unpriced-model'"
        ).fetchone()[0]
    second = persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=rows,
        cost_rows=costs,
    )
    assert second["eventsWritten"] == 0
    assert second["unpricedEventsWritten"] == 0
    assert second["costBucketsWritten"] == 0
    with connect(database) as connection:
        after = _counts(connection)
        after_occ = connection.execute(
            "SELECT occurrences FROM pricing_gaps WHERE model_key='unpriced-model'"
        ).fetchone()[0]
        ingest_runs = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    assert after == before
    assert after_occ == occurrences
    assert ingest_runs == 2


def test_s01_05_price_arrival_promotes_on_v7_migrated_db(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as raw:
        raw.row_factory = sqlite3.Row
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
            """
            INSERT INTO unpriced_usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, unclassified_tokens, telemetry_complete,
                cost_usd, raw_id, ingested_at
            ) VALUES('claude_local','claude-code','claude-opus-5','2026-08-30T20:00:00Z',
                'sess','proj',1002,1000,500,500,100,20,0,1,NULL,'claude-local:sess:msg','2026-08-30T20:01:00Z')
            """
        )
        raw.execute(
            "INSERT INTO pricing_gap_events(raw_id,model_key,source,occurred_at) "
            "VALUES('claude-local:sess:msg','claude-opus-5','claude_local','2026-08-30T20:00:00Z')"
        )
        raw.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,occurrences,sample_raw_id) "
            "VALUES('claude-opus-5','claude_local','2026-08-30T20:00:00Z','2026-08-30T20:00:00Z',1,'claude-local:sess:msg')"
        )
        raw.commit()
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0] == 1
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == "9"
    persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="claude_local",
        usage_rows=[],
    )
    health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM pricing_gap_events").fetchone()[0] == 0
    assert "claude-opus-5" not in health["pricingGaps"]


def test_s01_05_unstaged_gaps_survive_empty_other_source_persist(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
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
    persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=[],
    )
    health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM pricing_gap_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0] == 1
    assert "claude-opus-5" in health["pricingGaps"]


def test_s01_03_priced_calendar_day_is_not_zero_in_series(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="claude_local",
        usage_rows=[
            _usage_row(
                source="claude_local",
                tool_key="claude-code",
                model_key="claude-opus-5",
                occurred_at=datetime(2026, 8, 11, 16, tzinfo=UTC),
                raw_id="claude-local:aug11",
                input_tokens=2030,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=20,
            )
        ],
    )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1mo",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    coverage = {row["date"]: row["status"] for row in summary["navigation"]["dayCoverage"]}
    assert coverage.get("2026-08-11") == "priced"
    labeled = [point for point in summary["series"] if point["label"] == "Aug 11"]
    assert labeled
    assert sum(point["total"] for point in labeled) > 0


def test_s01_06_payload_excludes_future_subscription_days(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[_usage_row()],
    )
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
    before = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1mo",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    nav_before = aggregate_nav(database_path=database, pricing=pricing, timezone=TZ, now=NOW)
    with connect(database) as connection:
        materialize_subscription_days(
            connection, start=today + timedelta(days=1), end=today + timedelta(days=40)
        )
        future_rows = connection.execute(
            "SELECT COUNT(*) FROM subscription_daily_costs WHERE date > ?",
            (today.isoformat(),),
        ).fetchone()[0]
    assert future_rows > 0
    after = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1mo",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    nav_after = aggregate_nav(database_path=database, pricing=pricing, timezone=TZ, now=NOW)
    assert _money_leaves(before) == _money_leaves(after)
    assert nav_before["todayUsd"] == nav_after["todayUsd"]
    assert nav_before["burnRatePerDay"] == nav_after["burnRatePerDay"]
    assert before["projected"]["planCost"] == after["projected"]["planCost"]


def test_s01_07_cursor_free_plan_is_not_money(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    summary = aggregate_summary(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    assert not any(row["toolKey"] == "cursor" for row in summary["subscriptions"])
    for path, value in _money_leaves(summary):
        assert not path.lower().startswith("subscriptions") or "cursor" not in path.lower()
        assert not (path.endswith("amountUsd") and value == 0.0)


def test_s01_15_derived_values_carry_approx_marker(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="cursor_local",
        usage_rows=[
            _usage_row(
                source="cursor_local",
                tool_key="cursor",
                model_key="cursor:grok-4.6",
                raw_id="cursor-local:s01",
            )
        ],
    )
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[_usage_row()],
    )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=NOW,
    )
    cursor = next(model for model in summary["models"] if model["key"] == "cursor:grok-4.6")
    sol = next(model for model in summary["models"] if model["key"] == "gpt-5.6-sol")
    assert cursor["isExact"] is False
    assert cursor["value"] is not None
    assert cursor["valueMarker"] == "≈"
    assert sol["isExact"] is True
    assert sol["valueMarker"] is None


def test_owner_asserted_codex_cards_are_not_invoice_exact(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[
            _usage_row(
                model_key="codex-auto-review",
                raw_id="codex-local:s01:auto-review",
                occurred_at=datetime(2026, 9, 2, 20, tzinfo=UTC),
            ),
            _usage_row(
                model_key="gpt-daybreak-blue-latest",
                raw_id="codex-local:s01:daybreak",
                occurred_at=datetime(2026, 9, 2, 20, tzinfo=UTC),
            ),
        ],
    )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="all",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )
    auto = next(model for model in summary["models"] if model["key"] == "codex-auto-review")
    daybreak = next(model for model in summary["models"] if model["key"] == "gpt-daybreak-blue-latest")
    assert auto["isExact"] is False
    assert auto["valueMarker"] == "≈"
    assert daybreak["isExact"] is False
    assert daybreak["valueMarker"] == "≈"
    assert summary["totals"]["priced"] == 0
    assert summary["totals"]["publishedRate"] > 0


def test_s01_16_legacy_identity_converges(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as raw:
        raw.executescript(
            """
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_key TEXT NOT NULL, name TEXT NOT NULL, amount_usd REAL NOT NULL,
                cadence TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT
            );
            """
        )
        raw.execute("INSERT INTO app_meta(key, value) VALUES('schema_version', '7')")
        raw.execute("INSERT INTO app_meta(key, value) VALUES('subscription_seed_version', '1')")
        rows = (
            ("codex", "Codex - $200 per month", 200.0, "monthly"),
            ("claude-code", "Claude - $200 per month", 200.0, "monthly"),
            ("grok", "SuperGrok - $300 per month", 300.0, "monthly"),
            ("cursor", "Cursor - free", 0.0, "monthly"),
            ("opencode", "Z.AI - $400 for 3 months", 400.0, "quarterly"),
            ("custom-tool", "Custom Plan", 31.0, "monthly"),
        )
        for tool_key, name, amount, cadence in rows:
            raw.execute(
                "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) "
                "VALUES(?,?,?,?, '2026-08-01')",
                (tool_key, name, amount, cadence),
            )
        raw.commit()
    initialize(database)
    with connect(database) as connection:
        identities = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT tool_key, name, amount_usd, cadence FROM subscriptions"
            )
        }
        custom = identities["custom-tool"]
    fresh = tmp_path / "fresh.db"
    initialize(fresh)
    with connect(fresh) as connection:
        canonical = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT tool_key, name, amount_usd, cadence FROM subscriptions"
            )
        }
    assert canonical == {}
    assert identities["codex"] == ("Codex", 200.0, "monthly")
    assert identities["claude-code"] == ("Claude Code", 200.0, "monthly")
    assert identities["grok"] == ("SuperGrok", 300.0, "monthly")
    assert identities["cursor"] == ("Cursor", 0.0, "monthly")
    assert identities["opencode"] == ("Z.AI Coding Plan", 400.0, "quarterly")
    assert custom == ("Custom Plan", 31.0, "monthly")


def test_s01_17_token_totals_are_union(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    persist_rows(
        database_path=database,
        pricing=pricing,
        source="codex_local",
        usage_rows=[
            _usage_row(),
            _usage_row(
                model_key="unpriced-model",
                raw_id="codex-local:s01:unpriced",
                input_tokens=500,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=50,
            ),
        ],
    )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone="UTC",
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 21, tzinfo=UTC),
    )
    entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="tool",
        key="codex",
        window_key="1d",
        timezone="UTC",
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 21, tzinfo=UTC),
    )
    with connect(database) as connection:
        union = connection.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(input_tokens - cached_input_tokens + cached_input_tokens
                    + cache_write_tokens + output_tokens),0) FROM usage_events)
              + (SELECT COALESCE(SUM(input_tokens - cached_input_tokens + cached_input_tokens
                    + cache_write_tokens + output_tokens),0) FROM unpriced_usage_events)
            """
        ).fetchone()[0]
        priced_only = connection.execute(
            "SELECT COALESCE(SUM(input_tokens - cached_input_tokens + cached_input_tokens "
            "+ cache_write_tokens + output_tokens),0) FROM usage_events"
        ).fetchone()[0]
    assert summary["totals"]["tokens"] == union
    assert entity["tokens"] == union
    assert union != priced_only


def test_s01_18_v7_to_v9_preserves_listed_tables(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as raw:
        raw.executescript(
            """
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, tool_key TEXT NOT NULL,
                model_key TEXT NOT NULL, occurred_at TEXT NOT NULL, session_id TEXT, project TEXT,
                input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL,
                reasoning_tokens INTEGER, cost_usd REAL, computed_cost_usd REAL NOT NULL,
                raw_id TEXT NOT NULL UNIQUE, ingested_at TEXT NOT NULL
            );
            CREATE TABLE unpriced_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, tool_key TEXT NOT NULL,
                model_key TEXT NOT NULL, occurred_at TEXT NOT NULL, session_id TEXT, project TEXT,
                input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                cache_write_tokens INTEGER NOT NULL, cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER,
                unclassified_tokens INTEGER NOT NULL DEFAULT 0,
                telemetry_complete INTEGER NOT NULL DEFAULT 1, cost_usd REAL,
                raw_id TEXT NOT NULL UNIQUE, ingested_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, tool_key TEXT NOT NULL, project TEXT,
                started_at TEXT NOT NULL, ended_at TEXT, model_key TEXT NOT NULL
            );
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tool_key TEXT NOT NULL, name TEXT NOT NULL,
                amount_usd REAL NOT NULL, cadence TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT
            );
            CREATE TABLE subscription_daily_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
                tool_key TEXT NOT NULL, date TEXT NOT NULL, cost_usd REAL NOT NULL,
                raw_id TEXT NOT NULL UNIQUE,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
            );
            CREATE TABLE provider_cost_buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, starting_at TEXT NOT NULL,
                ending_at TEXT NOT NULL, project_id TEXT, line_item TEXT, model_key TEXT,
                cost_usd REAL NOT NULL, raw_id TEXT NOT NULL UNIQUE, ingested_at TEXT NOT NULL
            );
            CREATE TABLE pricing_gaps (
                model_key TEXT PRIMARY KEY, source TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
                sample_raw_id TEXT NOT NULL
            );
            CREATE TABLE pricing_gap_events (
                raw_id TEXT PRIMARY KEY, model_key TEXT NOT NULL, source TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            """
        )
        raw.execute("INSERT INTO app_meta(key, value) VALUES('schema_version', '7')")
        raw.execute(
            "INSERT INTO usage_events(source,tool_key,model_key,occurred_at,session_id,project,"
            "input_tokens,cached_input_tokens,cache_write_tokens,cache_write_1h_tokens,output_tokens,"
            "reasoning_tokens,cost_usd,computed_cost_usd,raw_id,ingested_at) "
            "VALUES('codex_local','codex','gpt-5.6-sol','2026-08-30T11:00:00Z',NULL,NULL,"
            "1000,0,0,0,200,NULL,NULL,0.007,'legacy:codex','2026-08-30T11:01:00Z')"
        )
        raw.execute(
            "INSERT INTO unpriced_usage_events(source,tool_key,model_key,occurred_at,session_id,project,"
            "input_tokens,cached_input_tokens,cache_write_tokens,cache_write_1h_tokens,output_tokens,"
            "reasoning_tokens,unclassified_tokens,telemetry_complete,cost_usd,raw_id,ingested_at) "
            "VALUES('opencode_local','opencode','opencode:unlisted','2026-08-30T12:00:00Z',NULL,NULL,"
            "10,0,0,0,1,NULL,0,1,NULL,'legacy:unpriced','2026-08-30T12:01:00Z')"
        )
        raw.execute(
            "INSERT INTO sessions(session_id,tool_key,project,started_at,ended_at,model_key) "
            "VALUES('sess-1','codex','proj','2026-08-30T10:00:00Z','2026-08-30T11:00:00Z','gpt-5.6-sol')"
        )
        raw.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date) "
            "VALUES('custom-tool','Custom Plan',31,'monthly','2026-08-01')"
        )
        raw.execute(
            "INSERT INTO subscription_daily_costs(subscription_id,tool_key,date,cost_usd,raw_id) "
            "VALUES(1,'custom-tool','2026-08-01',1.0,'subscription:1:2026-08-01')"
        )
        raw.execute(
            "INSERT INTO provider_cost_buckets(source,starting_at,ending_at,project_id,line_item,"
            "model_key,cost_usd,raw_id,ingested_at) "
            "VALUES('openai_admin','2026-08-30T00:00:00Z','2026-08-31T00:00:00Z','p','usage',NULL,1.25,"
            "'legacy:bucket','2026-08-30T01:00:00Z')"
        )
        raw.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,occurrences,sample_raw_id) "
            "VALUES('opencode:unlisted','opencode_local','2026-08-30T12:00:00Z','2026-08-30T12:00:00Z',1,'legacy:unpriced')"
        )
        raw.execute(
            "INSERT INTO pricing_gap_events(raw_id,model_key,source,occurred_at) "
            "VALUES('legacy:unpriced','opencode:unlisted','opencode_local','2026-08-30T12:00:00Z')"
        )
        raw.commit()
    before = {
        "usage_events": 1,
        "unpriced_usage_events": 1,
        "sessions": 1,
        "subscription_daily_costs": 1,
        "provider_cost_buckets": 1,
    }
    initialize(database)
    with connect(database) as connection:
        for table, count in before.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert connection.execute("SELECT COUNT(*) FROM subscriptions WHERE tool_key='custom-tool'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pricing_gap_events").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        flags = dict(connection.execute("SELECT raw_id, is_exact FROM usage_events"))
        assert flags["legacy:codex"] == 1
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) "
            "VALUES('codex_local','2026-08-30T22:00:00Z','2026-08-30T22:01:00Z','partial',0)"
        )


def test_s01_19_codex_desktop_originator_filter(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop.jsonl"
    traycer = tmp_path / "traycer.jsonl"
    missing = tmp_path / "missing.jsonl"
    write_codex_session(desktop)
    write_codex_session(traycer)
    text = traycer.read_text(encoding="utf-8").replace("Codex Desktop", "traycer-agents")
    traycer.write_text(text, encoding="utf-8")
    write_codex_session(missing)
    missing.write_text(
        missing.read_text(encoding="utf-8").replace('"originator": "Codex Desktop"', '"originator": null'),
        encoding="utf-8",
    )
    database = tmp_path / "spend.db"
    reset_codex_cache()
    result = ingest_codex(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(tmp_path / "*.jsonl"),
    )
    assert result["filesSkippedOriginator"] == 2
    with connect(database) as connection:
        events = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        projects = {
            row[0] for row in connection.execute("SELECT session_id FROM sessions")
        }
    assert events == 2
    assert sessions == 1
    assert projects == {"session-fixture"}


def test_s01_01_claude_unpriced_is_staged(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    write_claude_fixture(fixture)
    text = fixture.read_text(encoding="utf-8").replace("claude-opus-5", "claude-unlisted")
    fixture.write_text(text, encoding="utf-8")
    database = tmp_path / "spend.db"
    reset_claude_cache()
    result = ingest_claude(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(fixture),
    )
    assert result["status"] == "partial"
    with connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0]
        priced = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert count == 1
    assert priced == 0
