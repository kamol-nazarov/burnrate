from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime

from spend_app import limits
from spend_app.adapters import PROVIDERS as ADAPTER_PROVIDERS
from spend_app.adapters import REGISTRY as ADAPTER_REGISTRY
from spend_app.limits import (
    _antigravity_active_sessions,
    _codex_active_sessions,
    _claude_active_sessions,
    _cursor_active_sessions,
    _zcode_active_sessions,
)
from spend_app.providers import (
    CAPABILITIES,
    EXACTNESS,
    PROVIDERS,
    REGISTRY,
    STABILITIES,
    capability_reports,
)
from spend_app.quotas import agent_run_records


def test_codex_activity_uses_in_progress_turns_and_rejects_stale_or_archived(tmp_path) -> None:
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
                ("current", "Current Codex task", "fallback", "gpt-5.6-sol", current - 5, 0),
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
                ("current", "inProgress", current - 60, None),
                ("stale", "inProgress", current - 8 * 3600, None),
                ("archived", "inProgress", current - 60, None),
                ("done", "completed", current - 60, current - 10),
            ),
        )

    sessions = _codex_active_sessions(state_path, history_path, now=now)
    assert sessions == [
        {
            "sessionId": "current",
            "title": "Current Codex task",
            "model": "gpt-5.6-sol",
            "startedAt": "2026-09-04T11:59:00Z",
            "lastSeenAt": "2026-09-04T11:59:55Z",
        }
    ]
    records = agent_run_records({"codexSessions": sessions})
    assert [(row.id, row.name, row.model_key, row.state) for row in records] == [
        ("codex:current", "Current Codex task", "gpt-5.6-sol", "live")
    ]


def test_zcode_activity_uses_running_turn_status(tmp_path) -> None:
    path = tmp_path / "zcode.sqlite"
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    current_ms = int(now.timestamp() * 1000)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session(id TEXT PRIMARY KEY,title TEXT,time_updated INTEGER,time_archived INTEGER);
            CREATE TABLE turn_usage(session_id TEXT,turn_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER);
            CREATE TABLE model_usage(session_id TEXT,turn_id TEXT,model_id TEXT,status TEXT,started_at INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO session VALUES(?,?,?,NULL)", ("session-1", "Live ZCode task", current_ms - 1_000)
        )
        connection.execute(
            "INSERT INTO turn_usage VALUES(?,?,?,?,NULL)",
            ("session-1", "turn-1", "running", current_ms - 60_000),
        )
        connection.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?)",
            ("session-1", "turn-1", "glm-5.3-flash", "running", current_ms - 59_000),
        )

    assert _zcode_active_sessions(path, now=now) == [
        {
            "sessionId": "session-1:turn-1",
            "title": "Live ZCode task",
            "model": "zcode:glm-5.3-flash",
            "startedAt": "2026-09-04T11:59:00Z",
            "lastSeenAt": "2026-09-04T11:59:59Z",
        }
    ]


def test_cursor_activity_requires_generating_status(tmp_path) -> None:
    path = tmp_path / "cursor.sqlite"
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    current_ms = int(now.timestamp() * 1000)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE composerHeaders(
                composerId TEXT PRIMARY KEY,createdAt INTEGER,lastUpdatedAt INTEGER,
                isArchived INTEGER,value TEXT
            );
            CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value BLOB);
            """
        )
        for session_id, status, model in (
            ("live", "generating", "gemini-3.8-flash"),
            ("done", "completed", "grok-4.6"),
        ):
            connection.execute(
                "INSERT INTO composerHeaders VALUES(?,?,?,?,?)",
                (session_id, current_ms - 60_000, current_ms - 1_000, 0, "{}"),
            )
            connection.execute(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                (
                    f"composerData:{session_id}",
                    json.dumps(
                        {"status": status, "name": "Cursor task", "modelConfig": {"modelName": model}}
                    ),
                ),
            )

    assert _cursor_active_sessions(path, now=now) == [
        {
            "sessionId": "live",
            "title": "Cursor task",
            "model": "cursor:gemini-3.8-flash",
            "startedAt": "2026-09-04T11:59:00Z",
            "lastSeenAt": "2026-09-04T11:59:59Z",
        }
    ]


def test_antigravity_activity_requires_busy_status_and_reads_model(monkeypatch) -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)

    def rpc(method, payload, **_kwargs):
        if method == "GetAllCascadeTrajectories":
            return {
                "trajectorySummaries": {
                    "busy": {
                        "status": "CASCADE_RUN_STATUS_BUSY",
                        "summary": "Live Antigravity task",
                        "createdTime": "2026-09-04T11:58:00Z",
                        "lastModifiedTime": "2026-09-04T11:59:59Z",
                    },
                    "idle": {
                        "status": "CASCADE_RUN_STATUS_IDLE",
                        "lastModifiedTime": "2026-09-04T11:59:59Z",
                    },
                }
            }
        assert payload["cascadeId"] == "busy"
        return {
            "trajectory": {
                "generatorMetadata": [
                    {"chatModel": {"responseModel": "gemini-3.8-flash"}}
                ]
            }
        }

    monkeypatch.setattr(limits, "_antigravity_rpc_json", rpc)
    assert _antigravity_active_sessions(now=now) == [
        {
            "sessionId": "busy",
            "title": "Live Antigravity task",
            "model": "antigravity:gemini-3.8-flash",
            "startedAt": "2026-09-04T11:58:00Z",
            "lastSeenAt": "2026-09-04T11:59:59Z",
        }
    ]


def test_local_provider_sessions_become_live_agent_records() -> None:
    base = {
        "sessionId": "session",
        "title": "Task",
        "startedAt": "2026-09-04T11:59:00Z",
        "lastSeenAt": "2026-09-04T11:59:59Z",
    }
    records = agent_run_records(
        {
            "claudeSessions": [{**base, "model": "claude-fable-5-1"}],
            "cursorSessions": [{**base, "model": "cursor:grok-4.6"}],
            "zcodeSessions": [{**base, "model": "zcode:glm-5.3-flash"}],
            "antigravitySessions": [{**base, "model": "antigravity:gemini-3.8-flash"}],
        }
    )
    assert {(row.id, row.model_key, row.state) for row in records} == {
        ("claude:session", "claude-fable-5-1", "live"),
        ("cursor:session", "cursor:grok-4.6", "live"),
        ("zcode:session", "zcode:glm-5.3-flash", "live"),
        ("antigravity:session", "antigravity:gemini-3.8-flash", "live"),
    }


def test_claude_activity_requires_an_unfinished_recent_turn(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    project = tmp_path / "project"
    project.mkdir()

    def write_session(name, events, modified_at):
        path = project / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        os.utime(path, (modified_at, modified_at))

    write_session(
        "live-session",
        [
            {"type": "custom-title", "customTitle": "Live Claude task", "sessionId": "live-session"},
            {
                "type": "user",
                "timestamp": "2026-09-04T11:59:00Z",
                "sessionId": "live-session",
                "slug": "fallback-slug",
            },
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:55Z",
                "sessionId": "live-session",
                "message": {"model": "claude-fable-5-1", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "complete-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:59:00Z"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:30Z",
                "message": {"model": "claude-opus-5", "stop_reason": "end_turn"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "stale-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:48:00Z"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:48:30Z",
                "message": {"model": "claude-opus-5", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp() - 11 * 60,
    )

    assert _claude_active_sessions(tmp_path, now=now) == [
        {
            "sessionId": "live-session",
            "title": "Live Claude task",
            "model": "claude-fable-5-1",
            "startedAt": "2026-09-04T11:59:00Z",
            "lastSeenAt": "2026-09-04T11:59:55Z",
        }
    ]


def test_provider_capability_vocabulary_is_declared() -> None:
    assert CAPABILITIES == frozenset({"usage", "quota", "activity", "admin"})
    assert STABILITIES == frozenset({"official", "experimental"})
    assert EXACTNESS == frozenset({"exact", "derived", "partial", "unavailable"})
    reports = capability_reports()
    assert reports
    seen_caps: set[str] = set()
    seen_exact: set[str] = set()
    seen_stability: set[str] = set()
    for spec, report in zip(PROVIDERS, reports, strict=True):
        assert report["key"] == spec.key
        seen_stability.add(report["stability"])
        seen_exact.add(report["exactness"])
        assert set(report["capabilities"]) == CAPABILITIES
        for capability, exactness in report["capabilities"].items():
            seen_caps.add(capability)
            assert exactness in EXACTNESS
            if capability not in spec.capabilities:
                assert exactness == "unavailable"
    assert seen_caps == CAPABILITIES
    assert seen_stability == STABILITIES
    assert {"exact", "derived", "partial", "unavailable"} <= seen_exact
    assert ADAPTER_PROVIDERS is PROVIDERS
    assert ADAPTER_REGISTRY is REGISTRY


def test_activity_capable_providers_are_registered() -> None:
    activity = {spec.key for spec in PROVIDERS if "activity" in spec.capabilities}
    assert activity >= {
        "codex_local",
        "claude_local",
        "cursor_local",
        "zcode_local",
        "grok_local",
        "antigravity_local",
        "traycer_local",
    }
    assert REGISTRY.get("xai") is not None
    assert REGISTRY.get("xai").exactness_for("usage") == "unavailable"
    assert REGISTRY.get("xai").exactness_for("activity") == "unavailable"
    assert REGISTRY.get("codex_local").ingest_import == "spend_app.adapters.codex_local:ingest"
    from spend_app.adapters import codex_local

    assert REGISTRY.get("codex_local").ingest is codex_local.ingest
