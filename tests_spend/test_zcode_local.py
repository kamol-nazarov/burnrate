import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.zcode_local import ingest, parse_database
from spend_app.db import connect
from spend_app.limits import _zcode_active_sessions
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT)")
        connection.execute(
            """
            CREATE TABLE model_usage(
                id TEXT,session_id TEXT,provider_id TEXT,model_id TEXT,status TEXT,
                completed_at INTEGER,input_tokens INTEGER,output_tokens INTEGER,
                reasoning_tokens INTEGER,cache_creation_input_tokens INTEGER,
                cache_read_input_tokens INTEGER,raw_usage_json TEXT
            )
            """
        )
        connection.execute("INSERT INTO session VALUES('session-1','C:/Dev/ExampleProject')")
        connection.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "usage-1",
                "session-1",
                "builtin:zai-coding-plan",
                "GLM-5.3-Flash",
                "completed",
                1788404281408,
                300,
                20,
                0,
                40,
                100,
                json.dumps({"inputTokens": 300, "cacheReadTokens": 100}),
            ),
        )
        connection.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "usage-2",
                "session-2",
                "custom:api",
                "other",
                "completed",
                1788404281408,
                10,
                2,
                0,
                0,
                0,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "usage-3",
                "session-1",
                "builtin:zai-coding-plan",
                "GLM-5.3-Flash",
                "cancelled",
                1788404281408,
                999,
                999,
                0,
                0,
                0,
                "{}",
            ),
        )


def test_zcode_database_keeps_only_completed_subscription_usage(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    _database(path)
    rows, skipped = parse_database(path)
    assert skipped == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "zcode_local"
    assert row.tool_key == "zcode"
    assert row.model_key == "zcode:glm-5.3-flash"
    assert row.session_id == "session-1"
    assert row.project == "ExampleProject"
    assert row.input_tokens == 260
    assert row.cached_input_tokens == 100
    assert row.cache_write_tokens == 40
    assert row.output_tokens == 20
    assert row.reasoning_tokens is None
    assert row.input_tokens + row.cache_write_tokens + row.output_tokens == 320


def test_zcode_missing_usage_table_is_empty_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path):
        pass
    assert parse_database(path) == ([], 0)


def test_ingest_is_idempotent_and_skips_duplicate_raw_ids(tmp_path: Path) -> None:
    source = tmp_path / "db.sqlite"
    spend = tmp_path / "spend.db"
    _database(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "usage-1",
                "session-1",
                "builtin:zai-coding-plan",
                "GLM-5.3-Flash",
                "completed",
                1788404281408,
                300,
                20,
                0,
                40,
                100,
                json.dumps({"inputTokens": 300, "cacheReadTokens": 100}),
            ),
        )
    pricing = PricingEngine.load(ROOT / "pricing")
    parsed, skipped = parse_database(source)
    assert skipped == 1
    assert len(parsed) == 2
    assert parsed[0].raw_id == parsed[1].raw_id
    first = ingest(database_path=spend, pricing=pricing, source_database=source)
    second = ingest(database_path=spend, pricing=pricing, source_database=source)
    assert first["eventsWritten"] == 1
    assert second["eventsWritten"] == 0
    with connect(spend) as connection:
        rows = list(connection.execute("SELECT raw_id, project FROM usage_events"))
    assert len(rows) == 1
    assert rows[0][1] == "ExampleProject"


def test_prompt_tables_are_not_queried_and_content_is_not_stored(tmp_path: Path) -> None:
    source = tmp_path / "db.sqlite"
    spend = tmp_path / "spend.db"
    _database(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE message(id TEXT, session_id TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO message VALUES('m1','session-1','SECRET_PROMPT_DO_NOT_STORE')"
        )
    ingest(
        database_path=spend,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source_database=source,
    )
    with connect(spend) as connection:
        blobs = [
            str(value)
            for table in (
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in row
            if value is not None
        ]
    assert all("SECRET_PROMPT_DO_NOT_STORE" not in blob for blob in blobs)
    assert all("C:/Dev/ExampleProject" not in blob for blob in blobs)


def test_completed_or_stale_zcode_turn_is_not_live(tmp_path: Path) -> None:
    path = tmp_path / "zcode.sqlite"
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    current_ms = int(now.timestamp() * 1000)
    stale_ms = current_ms - 7 * 3600 * 1000
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session(id TEXT PRIMARY KEY,title TEXT,time_updated INTEGER,time_archived INTEGER);
            CREATE TABLE turn_usage(session_id TEXT,turn_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER);
            CREATE TABLE model_usage(session_id TEXT,turn_id TEXT,model_id TEXT,status TEXT,started_at INTEGER);
            """
        )
        connection.executemany(
            "INSERT INTO session VALUES(?,?,?,?)",
            (
                ("live", "Live ZCode task", current_ms - 1_000, None),
                ("done", "Completed ZCode task", current_ms - 1_000, None),
                ("stale", "Stale ZCode task", stale_ms, None),
            ),
        )
        connection.executemany(
            "INSERT INTO turn_usage VALUES(?,?,?,?,?)",
            (
                ("live", "turn-1", "running", current_ms - 60_000, None),
                ("done", "turn-2", "completed", current_ms - 60_000, current_ms - 10_000),
                ("stale", "turn-3", "running", stale_ms - 60_000, None),
            ),
        )
        connection.executemany(
            "INSERT INTO model_usage VALUES(?,?,?,?,?)",
            (
                ("live", "turn-1", "glm-5.3-flash", "running", current_ms - 59_000),
                ("done", "turn-2", "glm-5.3-flash", "completed", current_ms - 59_000),
                ("stale", "turn-3", "glm-5.3-flash", "running", stale_ms),
            ),
        )
    sessions = _zcode_active_sessions(path, now=now)
    assert [row["sessionId"] for row in sessions] == ["live:turn-1"]
