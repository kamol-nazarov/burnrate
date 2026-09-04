import sqlite3
from pathlib import Path

from spend_app.config import ROOT, load_settings
from spend_app.db import (
    UsageEvent,
    backup_database,
    connect,
    initialize,
    sync_model_prices,
    upsert_agent_run,
    upsert_quota,
    upsert_usage_event,
)
from spend_app.pricing import PricingEngine


def sample_event() -> UsageEvent:
    return UsageEvent(
        source="codex_local",
        tool_key="codex",
        model_key="gpt-5.6-sol",
        occurred_at="2026-08-30T20:00:00Z",
        session_id="session-1",
        project="northwind",
        input_tokens=1000,
        cached_input_tokens=400,
        cache_write_tokens=100,
        cache_write_1h_tokens=0,
        output_tokens=200,
        reasoning_tokens=50,
        cost_usd=0.012,
        computed_cost_usd=0.00706,
        raw_id="codex-local:session-1:event-1",
        ingested_at="2026-08-30T20:01:00Z",
        is_exact=True,
    )


def test_schema_uses_wal_and_required_tables(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "usage_events",
        "model_prices",
        "subscriptions",
        "sessions",
        "ingest_runs",
        "quotas",
        "agent_runs",
    }.issubset(tables)


def test_usage_events_has_required_is_exact_column(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info('usage_events')")}
    assert "is_exact" in columns
    assert columns["is_exact"][3] == 1


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        upsert_usage_event(connection, sample_event())
    initialize(database)
    initialize(database)
    with connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert version == "9"
    assert count == 1


def test_reingest_is_idempotent_and_updates_without_new_row(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    event = sample_event()
    with connect(database) as connection:
        assert upsert_usage_event(connection, event) is True
        assert upsert_usage_event(connection, event) is False
        count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert count == 1


def test_is_exact_persists_through_reingest(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        upsert_usage_event(connection, sample_event())
        updated = UsageEvent(
            **{
                **sample_event().__dict__,
                "cost_usd": None,
                "is_exact": False,
            }
        )
        assert upsert_usage_event(connection, updated) is False
        row = connection.execute(
            "SELECT cost_usd, is_exact FROM usage_events WHERE raw_id=?",
            (sample_event().raw_id,),
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert count == 1
    assert row[0] is None
    assert row[1] == 0


LEGACY_V7_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_1h_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    cost_usd REAL,
    computed_cost_usd REAL NOT NULL CHECK (computed_cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
    cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'annual')),
    start_date TEXT NOT NULL,
    end_date TEXT
);
CREATE TABLE IF NOT EXISTS subscription_daily_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    tool_key TEXT NOT NULL,
    date TEXT NOT NULL,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);
CREATE TABLE IF NOT EXISTS pricing_gaps (
    model_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    sample_raw_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pricing_gap_events (
    raw_id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


def make_legacy_v7_database(path: Path) -> None:
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.executescript(LEGACY_V7_SQL)
        raw.execute("INSERT INTO app_meta(key, value) VALUES('schema_version', '7')")
        raw.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, raw_id, ingested_at
            ) VALUES('cursor_admin', 'cursor', 'cursor:grok-4.6', '2026-08-30T10:00:00Z', NULL,
                NULL, 1000, 0, 0, 0, 200, NULL, 0.05, 0.0, 'legacy:cursor', '2026-08-30T10:01:00Z')
            """
        )
        raw.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, raw_id, ingested_at
            ) VALUES('codex_local', 'codex', 'gpt-5.6-sol', '2026-08-30T11:00:00Z', NULL,
                NULL, 1000, 0, 0, 0, 200, NULL, NULL, 0.007, 'legacy:codex', '2026-08-30T11:01:00Z')
            """
        )
        raw.execute(
            "INSERT INTO subscriptions(tool_key, name, amount_usd, cadence, start_date) "
            "VALUES('custom-tool', 'Custom Plan', 31, 'monthly', '2026-08-01')"
        )
        raw.execute(
            "INSERT INTO subscription_daily_costs(subscription_id, tool_key, date, cost_usd, raw_id) "
            "VALUES(1, 'custom-tool', '2026-08-01', 1.0, 'subscription:1:2026-08-01')"
        )


def test_v7_database_migrates_with_is_exact_backfill_and_quarterly(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    make_legacy_v7_database(database)
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info('usage_events')")}
        assert "is_exact" in columns
        assert columns["is_exact"][3] == 1
        flags = {
            row[0]: row[1]
            for row in connection.execute("SELECT raw_id, is_exact FROM usage_events")
        }
        assert flags["legacy:cursor"] == 0
        assert flags["legacy:codex"] == 1
        cadences = {
            row[0]: row[1] for row in connection.execute("SELECT tool_key, cadence FROM subscriptions")
        }
        assert cadences == {"custom-tool": "monthly"}
        preserved = connection.execute(
            "SELECT amount_usd FROM subscriptions WHERE tool_key='custom-tool'"
        ).fetchone()[0]
        daily_rows = connection.execute(
            "SELECT COUNT(*) FROM subscription_daily_costs"
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert preserved == 31
    assert daily_rows == 1
    assert version == "9"
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1


def test_raw_id_is_unique(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        upsert_usage_event(connection, sample_event())
        indexes = connection.execute("PRAGMA index_list('usage_events')").fetchall()
    assert any(row[2] for row in indexes)


def test_quota_upsert_is_idempotent_per_poll_and_keeps_unknowns_distinct(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        first = upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="SuperGrok weekly",
            unit="requests",
            source="traycer_local",
            polled_at="2026-08-31T00:00:00Z",
            used=52.0,
            allowance=140.0,
            pct=37.1,
            resets_at="2026-09-02T00:00:00Z",
            is_payg=False,
        )
        second = upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="SuperGrok weekly",
            unit="requests",
            source="traycer_local",
            polled_at="2026-08-31T00:00:00Z",
            used=54.0,
            allowance=140.0,
            pct=38.6,
            resets_at="2026-09-02T00:00:00Z",
            is_payg=False,
        )
        next_poll = upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="SuperGrok weekly",
            unit="requests",
            source="traycer_local",
            polled_at="2026-08-31T01:00:00Z",
            used=None,
            allowance=None,
            pct=None,
            resets_at=None,
            is_payg=None,
        )
        rows = connection.execute(
            "SELECT used, pct, is_payg FROM quotas ORDER BY polled_at"
        ).fetchall()
        count = connection.execute("SELECT COUNT(*) FROM quotas").fetchone()[0]
    assert first is True
    assert second is False
    assert next_poll is True
    assert count == 2
    assert tuple(rows[0]) == (54.0, 38.6, 0)
    assert tuple(rows[1]) == (None, None, None)


def test_agent_run_upsert_is_idempotent_and_tracks_last_seen(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        first = upsert_agent_run(
            connection,
            id="traycer:run-1",
            name="Codex implementation",
            model_key="gpt-5.6-sol",
            state="running",
            started_at="2026-08-30T20:00:00Z",
            last_seen_at="2026-08-30T20:01:00Z",
        )
        second = upsert_agent_run(
            connection,
            id="traycer:run-1",
            name="Codex implementation",
            model_key="gpt-5.6-sol",
            state="complete",
            started_at="2026-08-30T20:00:00Z",
            last_seen_at="2026-08-30T20:05:00Z",
        )
        rows = connection.execute(
            "SELECT id, state, last_seen_at FROM agent_runs"
        ).fetchall()
        count = connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
    assert first is True
    assert second is False
    assert count == 1
    assert tuple(rows[0]) == ("traycer:run-1", "complete", "2026-08-30T20:05:00Z")


STALE_ACTIVITY_SQL = """
CREATE TABLE IF NOT EXISTS quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    allowance REAL,
    used REAL,
    pct REAL,
    resets_at TEXT,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    raw_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    model_key TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);
"""


def test_stale_activity_tables_are_rebuilt_to_required_shape(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    with sqlite3.connect(database) as raw:
        raw.executescript(STALE_ACTIVITY_SQL)
    initialize(database)
    with connect(database) as connection:
        quota_columns = {row[1] for row in connection.execute("PRAGMA table_info('quotas')")}
        agent_columns = {row[1] for row in connection.execute("PRAGMA table_info('agent_runs')")}
        agent_pk = {
            row[1]
            for row in connection.execute("PRAGMA table_info('agent_runs')")
            if row[5]
        }
        upsert_quota(
            connection,
            provider_key="openrouter",
            limit_key="credits",
            label="OpenRouter credits",
            unit="usd",
            source="openrouter",
            polled_at="2026-08-31T00:00:00Z",
            is_payg=True,
        )
    assert {"provider_key", "limit_key", "polled_at", "is_payg"}.issubset(quota_columns)
    assert "raw_id" not in quota_columns
    assert {"id", "name", "model_key", "state", "started_at", "last_seen_at"} == agent_columns
    assert agent_pk == {"id"}


LEGACY_V8_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER,
    cost_usd REAL,
    computed_cost_usd REAL NOT NULL,
    is_exact INTEGER NOT NULL DEFAULT 0,
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'annual')),
    start_date TEXT NOT NULL,
    end_date TEXT
);
CREATE TABLE IF NOT EXISTS quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    label TEXT NOT NULL,
    used REAL,
    allowance REAL,
    unit TEXT NOT NULL,
    pct REAL,
    resets_at TEXT,
    source TEXT NOT NULL,
    is_payg BOOLEAN CHECK (is_payg IS NULL OR is_payg IN (0, 1)),
    polled_at TEXT NOT NULL,
    UNIQUE(provider_key, limit_key, polled_at)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_key TEXT,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
"""


def make_legacy_v8_database(path: Path) -> None:
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.executescript(LEGACY_V8_SQL)
        raw.execute("INSERT INTO app_meta(key, value) VALUES('schema_version', '8')")
        raw.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, is_exact,
                raw_id, ingested_at
            ) VALUES('codex_local', 'codex', 'gpt-5.6-sol', '2026-08-30T11:00:00Z', NULL,
                NULL, 1000, 0, 0, 0, 200, NULL, NULL, 0.007, 1, 'v8:codex', '2026-08-30T11:01:00Z')
            """
        )
        raw.execute(
            "INSERT INTO subscriptions(tool_key, name, amount_usd, cadence, start_date) "
            "VALUES('opencode', 'Z.AI Coding Plan', 400, 'quarterly', '2026-08-01')"
        )
        raw.execute(
            """
            INSERT INTO quotas(
                provider_key, limit_key, label, used, allowance, unit, pct, resets_at,
                source, is_payg, polled_at
            ) VALUES('grok', 'weekly', 'Grok Build weekly', NULL, NULL, 'pct', 26.0,
                '2026-09-02T00:00:00Z', 'traycer_profile', 0, '2026-08-31T12:00:00Z')
            """
        )
        raw.execute(
            "INSERT INTO agent_runs(id, name, model_key, state, started_at, last_seen_at) "
            "VALUES('traycer:v8', 'Wave 4', 'grok-4.6', 'live', '2026-08-31T10:00:00Z', "
            "'2026-08-31T10:04:00Z')"
        )


def test_v8_database_preserves_wal_quota_activity_and_seeds(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v8.db"
    make_legacy_v8_database(database)
    initialize(database)
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()[0]
        usage = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        quota = connection.execute(
            "SELECT pct FROM quotas WHERE provider_key='grok' AND limit_key='weekly'"
        ).fetchone()[0]
        run_state = connection.execute(
            "SELECT state FROM agent_runs WHERE id='traycer:v8'"
        ).fetchone()[0]
        zai = connection.execute(
            "SELECT amount_usd, cadence FROM subscriptions WHERE tool_key='opencode'"
        ).fetchone()
        seed_count = connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == "9"
    assert usage == 1
    assert quota == 26.0
    assert run_state == "live"
    assert tuple(zai) == (400, "quarterly")
    assert seed_count == 1


def test_pricing_files_sync_into_effective_dated_table(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    engine = PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")
    with connect(database) as connection:
        written = sync_model_prices(connection, engine)
        count = connection.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0]
        rows = connection.execute("SELECT model_key, source_url FROM model_prices").fetchall()
    # openai 3 + daybreak 1 + auto-review 1 + anthropic 7 + xai 1 + cursor 4 + zai 2 + openrouter 1 + google 2 revisions
    assert len(engine.prices) == 22
    assert written == 22
    assert count == 22
    assert len({row[0] for row in rows}) == 20
    assert all(str(row[1]).startswith("https://") for row in rows)


def test_backup_database_round_trips_schema_and_row(tmp_path: Path) -> None:
    source = tmp_path / "spend.db"
    dest = tmp_path / "backups" / "spend.db"
    initialize(source)
    with connect(source) as connection:
        assert upsert_usage_event(connection, sample_event()) is True
    assert backup_database(source, dest) == dest
    with connect(dest) as connection:
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()[0]
        row = connection.execute(
            "SELECT raw_id, input_tokens, is_exact FROM usage_events"
        ).fetchone()
        tables = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        subscriptions = connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    assert dest.is_file()
    assert version == "9"
    assert tuple(row) == (sample_event().raw_id, 1000, 1)
    assert {"usage_events", "subscriptions", "app_meta"}.issubset(tables)
    assert integrity == "ok"
    assert subscriptions == 0


def test_installed_defaults_use_localappdata_and_utc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPEND_DATABASE_PATH", raising=False)
    monkeypatch.delenv("BURNRATE_DEV", raising=False)
    monkeypatch.delenv("SPEND_TIMEZONE", raising=False)
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    settings = load_settings(env_path=tmp_path / "missing.env")
    assert settings.database_path == local / "BURNRATE" / "spend.db"
    assert settings.logs_path == local / "BURNRATE" / "logs"
    assert settings.timezone == "UTC"
    assert settings.anthropic_admin_key is None
    assert settings.openai_admin_key is None
    assert settings.cursor_api_key is None


def test_dev_and_override_database_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPEND_TIMEZONE", raising=False)
    override = tmp_path / "custom" / "spend.db"
    monkeypatch.setenv("BURNRATE_DEV", "1")
    monkeypatch.delenv("SPEND_DATABASE_PATH", raising=False)
    dev = load_settings(env_path=tmp_path / "missing.env")
    assert dev.database_path == ROOT / "data" / "spend" / "spend.db"
    assert dev.logs_path == ROOT / "logs"
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(override))
    overridden = load_settings(env_path=tmp_path / "missing.env")
    assert overridden.database_path == override


def test_env_example_has_no_personal_machine_paths() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "users\\kamol" not in lowered
    assert "users/kamol" not in lowered
    assert "ai-telemetry" not in lowered
    assert "america/new_york" not in lowered
    assert "SPEND_TIMEZONE=UTC" in text
    assert "ANTHROPIC_ADMIN_KEY=" in text
    assert "OPENAI_ADMIN_KEY=" in text
    assert "CURSOR_API_KEY=" in text
