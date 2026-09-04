import json
import os
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.claude_local import ingest, parse_file, reset_file_cache
from spend_app.db import connect
from spend_app.limits import _claude_active_sessions
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def write_fixture(path: Path) -> None:
    message = {
        "id": "msg_fixture",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "SECRET_PROMPT_DO_NOT_STORE reply"}],
        "output_tokens": 100,
        "usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 500,
            "output_tokens": 100,
            "output_tokens_details": {"thinking_tokens": 20},
            "cache_creation": {
                "ephemeral_1h_input_tokens": 500,
                "ephemeral_5m_input_tokens": 0,
            },
        },
    }
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-08-30T20:00:00Z",
            "sessionId": "claude-session",
            "cwd": r"C:\Dev\ExampleProject",
            "uuid": "outer-1",
            "message": message,
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-30T20:00:01Z",
            "sessionId": "claude-session",
            "cwd": r"C:\Dev\ExampleProject",
            "uuid": "outer-2",
            "message": message,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_parser_deduplicates_repeated_assistant_snapshots(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    write_fixture(fixture)
    session, events = parse_file(fixture)
    assert session["id"] == "claude-session"
    assert session["project"] == "ExampleProject"
    assert session["model_key"] == "claude-opus-5"
    assert len(events) == 1
    assert events[0].input_tokens == 1002
    assert events[0].cache_write_tokens == 500
    assert events[0].cache_write_1h_tokens == 500
    assert events[0].raw_id == "claude-local:claude-session:msg_fixture"


def test_ingest_prices_one_hour_writes_and_is_idempotent(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    pricing = PricingEngine.load(ROOT / "pricing")
    first = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    second = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert first["eventsWritten"] == 1
    assert first["duplicatesRemoved"] == 1
    assert second["eventsWritten"] == 0
    assert second["filesSkippedUnchanged"] == 1
    with connect(database) as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        row = connection.execute(
            """
            SELECT cache_write_1h_tokens, computed_cost_usd, is_exact, project,
                   session_id, model_key, raw_id
            FROM usage_events
            """
        ).fetchone()
        session = connection.execute(
            "SELECT session_id, project, model_key, tool_key FROM sessions"
        ).fetchone()
    assert event_count == 1
    assert session_count == 1
    assert row[0] == 500
    assert round(row[1], 6) == 0.00801
    assert tuple(row[2:]) == (
        1,
        "ExampleProject",
        "claude-session",
        "claude-opus-5",
        "claude-local:claude-session:msg_fixture",
    )
    assert tuple(session) == ("claude-session", "ExampleProject", "claude-opus-5", "claude-code")


def test_unpriced_model_is_recorded_and_run_fails_loudly(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    message = {
        "id": "msg_unpriced",
        "model": "unpriced-claude",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
    }
    fixture.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-30T20:00:00Z",
                "sessionId": "claude-unpriced",
                "cwd": r"C:\Dev\ExampleProject",
                "uuid": "outer-unpriced",
                "message": message,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(fixture),
    )
    assert result["status"] == "partial"
    assert result["eventsWritten"] == 0
    assert result["unpricedModels"] == ["unpriced-claude"]
    with connect(database) as connection:
        gap = connection.execute("SELECT model_key, occurrences FROM pricing_gaps").fetchone()
        unpriced = connection.execute(
            "SELECT COUNT(*) FROM unpriced_usage_events"
        ).fetchone()[0]
    assert tuple(gap) == ("unpriced-claude", 1)
    assert unpriced == 1


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
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_file_cache()
    first = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    reset_file_cache()
    second = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert first["eventsWritten"] == 1
    assert second["eventsWritten"] == 0
    with connect(database) as connection:
        raw_ids = [row[0] for row in connection.execute("SELECT raw_id FROM usage_events")]
    assert raw_ids == ["claude-local:claude-session:msg_fixture"]


def test_omitted_thinking_tokens_stay_null(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-30T20:00:00Z",
                "sessionId": "claude-partial",
                "cwd": r"C:\Dev\ExampleProject",
                "uuid": "outer-partial",
                "message": {
                    "id": "msg_partial",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 5,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _session, events = parse_file(fixture)
    assert len(events) == 1
    assert events[0].reasoning_tokens is None
    assert events[0].input_tokens == 10
    assert events[0].output_tokens == 5


def test_prompt_text_is_not_persisted(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    reset_file_cache()
    ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(fixture),
    )
    assert "SECRET_PROMPT_DO_NOT_STORE" in fixture.read_text(encoding="utf-8")
    assert "SECRET_PROMPT_DO_NOT_STORE" not in _stored_text(database)
    assert "C:\\Dev\\ExampleProject" not in _stored_text(database)


def test_idle_terminal_or_expired_claude_session_is_not_live(tmp_path: Path) -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    project = tmp_path / "project"
    project.mkdir()

    def write_session(name: str, events: list[dict], modified_at: float) -> None:
        path = project / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        os.utime(path, (modified_at, modified_at))

    write_session(
        "live-session",
        [
            {"type": "custom-title", "customTitle": "Live Claude task", "sessionId": "live-session"},
            {"type": "user", "timestamp": "2026-09-04T11:59:00Z", "sessionId": "live-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:55Z",
                "sessionId": "live-session",
                "message": {"model": "claude-opus-5", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "complete-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:59:00Z", "sessionId": "complete-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:30Z",
                "sessionId": "complete-session",
                "message": {"model": "claude-opus-5", "stop_reason": "end_turn"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "idle-editor",
        [{"type": "custom-title", "customTitle": "Idle editor", "sessionId": "idle-editor"}],
        now.timestamp(),
    )
    write_session(
        "stale-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:48:00Z", "sessionId": "stale-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:48:30Z",
                "sessionId": "stale-session",
                "message": {"model": "claude-opus-5", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp() - 11 * 60,
    )
    sessions = _claude_active_sessions(tmp_path, now=now)
    assert [row["sessionId"] for row in sessions] == ["live-session"]
