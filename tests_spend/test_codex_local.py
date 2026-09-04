import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.codex_local import ingest, parse_file, reset_file_cache
from spend_app.db import connect
from spend_app.limits import _codex_active_sessions
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def write_session(path: Path, model: str = "gpt-5.6-sol") -> None:
    rows = [
        {
            "timestamp": "2026-08-30T20:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "session-fixture",
                "timestamp": "2026-08-30T20:00:00Z",
                "cwd": r"C:\Dev\ExampleProject",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": "2026-08-30T20:00:01Z",
            "type": "turn_context",
            "payload": {"model": model, "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-08-30T20:00:01.5Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "SECRET_PROMPT_DO_NOT_STORE implement OPT-001",
            },
        },
        {
            "timestamp": "2026-08-30T20:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 400,
                        "cache_write_input_tokens": 100,
                        "output_tokens": 200,
                        "reasoning_output_tokens": 50,
                        "total_tokens": 1200,
                    }
                },
            },
        },
        {
            "timestamp": "2026-08-30T20:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 500,
                        "cached_input_tokens": 250,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 600,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_parser_extracts_only_structured_usage(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    write_session(session_file)
    session, events = parse_file(session_file)
    assert session["id"] == "session-fixture"
    assert session["project"] == "ExampleProject"
    assert len(events) == 2
    assert events[0].cached_input_tokens == 400
    assert events[0].cache_write_tokens == 100
    assert events[0].reasoning_tokens == 50
    assert events[0].raw_id == "codex-local:session-fixture:2026-08-30T20:00:02Z:0"


def test_backfill_is_idempotent_end_to_end(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    database = tmp_path / "spend.db"
    write_session(session_file)
    pricing = PricingEngine.load(ROOT / "pricing")

    first = ingest(database_path=database, pricing=pricing, session_glob=str(session_file))
    second = ingest(database_path=database, pricing=pricing, session_glob=str(session_file))

    assert first["status"] == "success"
    assert first["eventsWritten"] == 2
    assert second["eventsWritten"] == 0
    assert second["filesSkippedUnchanged"] == 1
    with connect(database) as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        row = connection.execute(
            """
            SELECT computed_cost_usd, is_exact, project, session_id, cache_write_tokens
            FROM usage_events ORDER BY occurred_at LIMIT 1
            """
        ).fetchone()
    assert event_count == 2
    assert session_count == 1
    assert round(row[0], 6) == 0.00706
    assert tuple(row[1:]) == (1, "ExampleProject", "session-fixture", 100)


def test_unpriced_model_is_recorded_and_run_fails_loudly(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    database = tmp_path / "spend.db"
    write_session(session_file, model="unpriced-model")
    result = ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(session_file),
    )
    assert result["status"] == "partial"
    assert result["eventsWritten"] == 0
    assert result["unpricedModels"] == ["unpriced-model"]
    with connect(database) as connection:
        gap = connection.execute("SELECT model_key, occurrences FROM pricing_gaps").fetchone()
        unpriced = connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0]
        run_status = connection.execute(
            "SELECT status FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert tuple(gap) == ("unpriced-model", 2)
    assert unpriced == 2
    assert run_status == "partial"


def _stored_text(database: Path) -> str:
    chunks: list[str] = []
    with connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for row in connection.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(value) for value in row if value is not None)
    return "\n".join(chunks)


def test_replay_skips_duplicate_raw_ids_when_file_cache_misses(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    database = tmp_path / "spend.db"
    write_session(session_file)
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_file_cache()
    first = ingest(database_path=database, pricing=pricing, session_glob=str(session_file))
    reset_file_cache()
    second = ingest(database_path=database, pricing=pricing, session_glob=str(session_file))
    assert first["eventsWritten"] == 2
    assert second["eventsWritten"] == 0
    with connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        raw_ids = [
            row[0] for row in connection.execute("SELECT raw_id FROM usage_events")
        ]
    assert count == 2
    assert len(raw_ids) == len(set(raw_ids))


def test_omitted_reasoning_stays_null(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    write_session(session_file)
    payload = json.loads(session_file.read_text(encoding="utf-8").splitlines()[-1])
    usage = payload["payload"]["info"]["last_token_usage"]
    del usage["reasoning_output_tokens"]
    lines = session_file.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps(payload)
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _session, events = parse_file(session_file)
    assert events[0].reasoning_tokens == 50
    assert events[1].reasoning_tokens is None


def test_prompt_text_is_not_persisted(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout.jsonl"
    database = tmp_path / "spend.db"
    write_session(session_file)
    reset_file_cache()
    ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(session_file),
    )
    assert "SECRET_PROMPT_DO_NOT_STORE" in session_file.read_text(encoding="utf-8")
    assert "SECRET_PROMPT_DO_NOT_STORE" not in _stored_text(database)
    assert "C:\\Dev\\ExampleProject" not in _stored_text(database)


def test_stale_archived_or_completed_codex_session_is_not_live(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    history_path = tmp_path / "history.sqlite"
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    current = int(now.timestamp())
    with sqlite3.connect(state_path) as state:
        state.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,title TEXT,model TEXT,updated_at INTEGER,archived INTEGER)"
        )
        state.executemany(
            "INSERT INTO threads VALUES(?,?,?,?,?,?)",
            (
                ("live", "Live Codex task", "fallback", "gpt-5.6-sol", current - 5, 0),
                ("stale", "Stale task", "fallback", "gpt-5.6-terra", current - 7 * 3600, 0),
                ("archived", "Archived task", "fallback", "gpt-5.6-luna", current - 5, 1),
                ("done", "Completed task", "fallback", "gpt-5.6-sol", current - 5, 0),
            ),
        )
    with sqlite3.connect(history_path) as history:
        history.execute(
            "CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER)"
        )
        history.executemany(
            "INSERT INTO thread_turns VALUES(?,?,?,?)",
            (
                ("live", "inProgress", current - 60, None),
                ("stale", "inProgress", current - 8 * 3600, None),
                ("archived", "inProgress", current - 60, None),
                ("done", "completed", current - 60, current - 10),
            ),
        )
    sessions = _codex_active_sessions(state_path, history_path, now=now)
    assert [row["sessionId"] for row in sessions] == ["live"]
