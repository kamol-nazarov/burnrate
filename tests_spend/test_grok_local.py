"""Grok Build (xAI grok CLI) local ingest, quota snapshot, and live sessions.

Usage comes from ``shell.turn.inference_done`` records in the CLI's unified
log; the quota from its ``billing: fetched credits config`` records; live
sessions from ``active_sessions.json``. No network call is involved anywhere.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import spend_app.limits as limits
from spend_app.adapters import grok_local
from spend_app.adapters import traycer_local
from spend_app.adapters.common import UsageRow
from spend_app.aggregate import aggregate_summary
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine
from spend_app.quotas import agent_run_records, grok_quota_samples

ROOT = Path(__file__).resolve().parents[1]
SID = "01a05fb0-9944-79a3-b111-1a1572034fe8"
SUB = "01a05fb3-cf80-7581-a576-13862d2bb4f5"


def _line(ts: str, msg: str, ctx: dict, sid: str | None = SID) -> str:
    return json.dumps({"ts": ts, "src": "shell", "pid": 27876, "lvl": "info", "msg": msg, "ctx": ctx, "sid": sid})


def _turn(ts: str, sid: str, loop: int, prompt: int, cached: int, completion: int, reasoning: int | None = None) -> str:
    ctx = {"loop_index": loop, "model_elapsed_ms": 100, "attempts": 1, "prompt_tokens": prompt,
           "cached_prompt_tokens": cached, "completion_tokens": completion, "tokens_per_sec": 100.0}
    if reasoning is not None:
        ctx["reasoning_tokens"] = reasoning
    return _line(ts, "shell.turn.inference_done", ctx, sid)


def _billing(ts: str, used: float, end: str, tier: str = "SuperGrok Heavy") -> str:
    return _line(ts, "billing: fetched credits config", {
        "config": {"creditUsagePercent": used, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-08-27T03:40:35.074896+00:00", "end": end},
                   "onDemandCap": {"val": 0}, "onDemandUsed": {"val": 0}, "prepaidBalance": {"val": 0}},
        "subscriptionTier": tier}, None)


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_turns_become_rows_with_model_and_project_from_session_lines(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [
        _line("2026-09-02T01:16:44.436Z", "model changed", {"model": "grok-4.6"}),
        _line("2026-09-02T01:16:49.908Z", "session created", {"cwd": "C:\\\\Dev\\\\ExampleProject"}),
        _turn("2026-09-02T01:25:05.000Z", SID, 1, 21598, 3328, 318, 155),
        _turn("2026-09-02T01:24:58.267Z", SUB, 16, 66945, 128, 30, 25),
        _line("2026-09-02T01:26:00.000Z", "shell.tool.exec_done", {"tool": "read_file"}),
        _turn("2026-09-02T01:27:00.000Z", SID, 2, 0, 0, 0),
    ])
    rows, state = grok_local.parse_log(log)
    assert [row.session_id for row in rows] == [SID, SUB], "zero-token turns are skipped"
    first, sub = rows
    assert first.model_key == "supergrok:grok-4.6" and first.tool_key == "grok" and first.source == "grok_local"
    assert first.project == "ExampleProject"
    assert (first.input_tokens, first.cached_input_tokens, first.output_tokens, first.reasoning_tokens) == (21598, 3328, 318, 155)
    assert first.cost_usd is None, "subscription usage carries no metered cost"
    assert sub.model_key == "supergrok:grok-4.6", "a subagent without its own model line inherits the last model seen"
    assert sub.project is None
    assert first.raw_id != sub.raw_id
    assert state["offset"] == log.stat().st_size


def test_turns_without_any_model_line_are_skipped(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [_turn("2026-09-02T01:25:05.000Z", SID, 1, 100, 10, 5)])
    rows, _state = grok_local.parse_log(log)
    assert rows == []


def test_reader_is_incremental_and_restarts_after_truncation(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [_line("2026-09-02T01:00:00Z", "model changed", {"model": "grok-4.6"}), _turn("2026-09-02T01:01:00Z", SID, 1, 100, 10, 5)])
    rows, state = grok_local.parse_log(log)
    assert len(rows) == 1
    with log.open("a", encoding="utf-8") as handle:
        handle.write(_turn("2026-09-02T01:02:00Z", SID, 2, 200, 20, 6) + "\n")
        handle.write('{"ts":"2026-09-02T01:03:00Z","msg":"shell.turn.inference_done","sid":"' + SID)  # partial line
    rows, state = grok_local.parse_log(log, state)
    assert [row.raw_id for row in rows] and len(rows) == 1, "only new complete lines are returned"
    second_offset = state["offset"]
    rows, state = grok_local.parse_log(log, state)
    assert rows == [] and state["offset"] == second_offset, "a partial trailing line waits"
    _write_log(
        log,
        [
            _line("2026-09-02T02:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T02:00:01Z", SID, 1, 50, 0, 5),
        ],
    )  # CLI truncated the log
    rows, state = grok_local.parse_log(log, state)
    assert len(rows) == 1 and rows[0].input_tokens == 50 and rows[0].model_key == "supergrok:grok-4.6"
    assert state["offset"] == log.stat().st_size


def test_ingest_persists_derived_rows_and_tolerates_a_missing_log(tmp_path: Path) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    missing = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    assert missing["files"] == 0 and missing["rows"] == 0
    _write_log(log, [_line("2026-09-02T01:00:00Z", "model changed", {"model": "grok-4.6"}), _turn("2026-09-02T01:01:00Z", SID, 1, 100_000, 50_000, 10_000)])
    first = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    second = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    assert first["rows"] == 1 and second["rows"] == 0
    with connect(database) as connection:
        stored = connection.execute("SELECT tool_key, model_key, cost_usd, computed_cost_usd FROM usage_events").fetchall()
    assert len(stored) == 1
    tool_key, model_key, cost, computed = stored[0]
    assert (tool_key, model_key, cost) == ("grok", "supergrok:grok-4.6", None)
    # xai:grok-4.6 published rates: $2/M input, $0.50/M cached, $6/M output (below the 200k long-context tier).
    assert abs(computed - (0.05 * 2.00 + 0.05 * 0.50 + 0.01 * 6.00)) < 1e-6
    summary = aggregate_summary(database_path=database, pricing=pricing, window_key="all", tool="all", timezone="UTC", cache_threshold=0.5, now=datetime(2026, 9, 2, 2, tzinfo=UTC))
    tool = next(item for item in summary["tools"] if item["key"] == "grok")
    assert tool["tokens"] == 110_000 and tool["isExact"] is False


def test_quota_comes_from_the_newest_local_billing_snapshot(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [
        _billing("2026-09-02T00:10:00Z", 40.0, "2026-09-03T03:40:35.074896+00:00"),
        _turn("2026-09-02T00:11:00Z", SID, 1, 10, 0, 1),
        _billing("2026-09-02T01:16:49.969Z", 53.0, "2026-09-03T03:40:35.074896+00:00"),
    ])
    now = datetime(2026, 9, 2, 1, 30, tzinfo=UTC)
    payload = limits._grok_limits_from_log(log, now=now)
    assert payload["status"] == "exact" and payload["source"] == "grok_local_billing" and payload["plan"] == "SuperGrok Heavy"
    window = payload["windows"][0]
    assert window["usedPct"] == 53.0 and window["resetAt"] == "2026-09-03T03:40:35.074896Z"
    assert "2026-09-02T01:16:49.969Z" in payload["detail"]
    samples = grok_quota_samples(payload, source=payload["source"])
    assert samples[0].pct == 53.0 and samples[0].resets_at == "2026-09-03T03:40:35Z" and samples[0].source == "grok_local_billing"
    stale = limits._grok_limits_from_log(log, now=datetime(2026, 9, 4, tzinfo=UTC))
    assert stale["status"] == "unavailable" and "predates the current weekly period" in stale["detail"]
    assert limits._grok_limits_from_log(tmp_path / "absent.jsonl")["status"] == "unavailable"

    def no_traycer(*_args, **_kwargs):
        raise AssertionError("Traycer fallback must not run when the local snapshot is current")

    monkeypatch.setattr(limits, "_traycer_profile_rate_limits", no_traycer)
    uncached = limits._grok_limits_uncached(log, now=now)
    assert uncached["source"] == "grok_local_billing", "the local snapshot wins over the Traycer path"


def test_live_sessions_need_a_running_pid_and_carry_title_and_model(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(json.dumps({"info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"}, "session_summary": "Demo session", "current_model_id": "grok-4.6"}), encoding="utf-8")
    os.utime(summary, (now.timestamp(), now.timestamp()))
    active = tmp_path / "active_sessions.json"
    active.write_text(json.dumps([
        {"session_id": SID, "pid": 111, "cwd": "C:\\\\Dev\\\\ExampleProject", "opened_at": "2026-09-02T01:16:49.909310900Z"},
        {"session_id": "dead", "pid": 222, "cwd": "C:\\\\Dev\\\\Other", "opened_at": "2026-09-02T01:00:00Z"},
    ]), encoding="utf-8")
    limits._GROK_SESSION_DIRS.clear()
    live = limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now)
    assert live == [{"sessionId": SID, "title": "Demo session", "model": "supergrok:grok-4.6", "startedAt": "2026-09-02T01:16:49.909310900Z"}]
    records = agent_run_records({"activeAgents": [], "unmeteredTurns": [], "grokSessions": live})
    assert [(r.id, r.name, r.model_key, r.state) for r in records] == [(f"grok:{SID}", "Demo session", "supergrok:grok-4.6", "live")]
    stale_stamp = (now - timedelta(minutes=6)).timestamp()
    os.utime(summary, (stale_stamp, stale_stamp))
    assert limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now) == []
    assert limits._grok_active_sessions(tmp_path / "missing.json", sessions) == []


def test_idle_grok_shell_is_not_a_live_session(tmp_path: Path) -> None:
    """A running grok pid with stale session-dir files is an idle shell, not live."""
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(
        json.dumps({"info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"}, "session_summary": "Idle shell", "current_model_id": "grok-4.6"}),
        encoding="utf-8",
    )
    os.utime(summary, ((now - timedelta(minutes=6)).timestamp(),) * 2)
    active = tmp_path / "active_sessions.json"
    active.write_text(
        json.dumps([{"session_id": SID, "pid": 111, "cwd": "C:\\\\Dev\\\\ExampleProject", "opened_at": "2026-09-02T01:16:49Z"}]),
        encoding="utf-8",
    )
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now) == []


def test_coverage_start_is_the_first_log_record(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    assert grok_local.coverage_start(log) is None
    _write_log(log, [_line("2026-09-01T12:10:40.496Z", "shell.tool.exec_done", {}), _turn("2026-09-01T12:10:45Z", SID, 1, 5, 0, 1)])
    assert grok_local.coverage_start(log) == datetime(2026, 9, 1, 12, 10, 40, 496000, tzinfo=UTC)


def test_coverage_start_keeps_persisted_history_after_log_rotation(tmp_path: Path) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    _write_log(
        log,
        [
            _line("2026-09-01T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-01T12:00:01Z", SID, 1, 1000, 0, 100),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    first = grok_local.coverage_start(log, database)
    assert first == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    _write_log(
        log,
        [
            _line("2026-09-02T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T12:00:01Z", SID, 1, 10, 0, 1),
        ],
    )
    assert grok_local.coverage_start(log) == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    rotated = grok_local.coverage_start(log, database)
    assert rotated == datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)
    assert rotated < datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_rotated_grok_log_does_not_reopen_traycer_history(tmp_path: Path, monkeypatch) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    _write_log(
        log,
        [
            _line("2026-09-01T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-01T12:00:01Z", SID, 1, 1000, 0, 100),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    _write_log(
        log,
        [
            _line("2026-09-02T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T12:00:01Z", SID, 1, 10, 0, 1),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    boundary = grok_local.coverage_start(log, database)
    t0 = datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)

    def row(when: datetime) -> UsageRow:
        return UsageRow(
            source="traycer_local",
            tool_key="grok",
            model_key="grok:m",
            occurred_at=when,
            session_id="traycer:c",
            project="p",
            input_tokens=1000,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=100,
            reasoning_tokens=None,
            cost_usd=None,
            raw_id=f"traycer-grok-{when.isoformat()}",
        )

    fake_db = tmp_path / "chat.db"
    fake_db.write_bytes(b"")
    monkeypatch.setattr(traycer_local, "parse_database", lambda _path: [row(t0)])
    seen: list[UsageRow] = []

    def capture(**kwargs):
        seen.extend(kwargs["usage_rows"])
        return {"status": "success"}

    monkeypatch.setattr(traycer_local, "persist_rows", capture)
    result = traycer_local.ingest(
        database_path=database,
        pricing=None,
        database_glob=str(fake_db),
        grok_covered_from=boundary,
    )
    assert result["mirroredGrokEvents"] == 1 and result["eventsSeen"] == 0
    assert seen == []
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="all",
        tool="grok",
        timezone="UTC",
        cache_threshold=0.5,
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert summary["totals"]["tokens"] == 1111


def test_live_session_omits_model_when_current_model_id_is_missing(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"},
                "session_summary": "Demo session",
                "current_model_id": None,
            }
        ),
        encoding="utf-8",
    )
    os.utime(summary, (now.timestamp(), now.timestamp()))
    active = tmp_path / "active_sessions.json"
    active.write_text(
        json.dumps(
            [
                {
                    "session_id": SID,
                    "pid": 111,
                    "cwd": "C:\\\\Dev\\\\ExampleProject",
                    "opened_at": "2026-09-02T01:16:49.909310900Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    limits._GROK_SESSION_DIRS.clear()
    live = limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now)
    assert live == [
        {
            "sessionId": SID,
            "title": "Demo session",
            "model": None,
            "startedAt": "2026-09-02T01:16:49.909310900Z",
        }
    ]
    records = agent_run_records({"activeAgents": [], "unmeteredTurns": [], "grokSessions": live})
    assert records[0].model_key is None


def test_grok_session_dir_rejects_path_escape_and_finds_nested_subagents(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    project = sessions / "proj"
    nested = project / "parent-sid" / "subagents" / SID
    nested.mkdir(parents=True)
    secret = tmp_path / "secret-dir"
    secret.mkdir()
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(SID, sessions) == nested.resolve()
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir("..", sessions) is None
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(r"..\..\secret-dir", sessions) is None
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(str(secret), sessions) is None


def test_traycer_drops_grok_rows_the_cli_log_already_covers(tmp_path: Path, monkeypatch) -> None:
    def row(tool: str, when: datetime) -> UsageRow:
        return UsageRow(source="traycer_local", tool_key=tool, model_key=f"{tool}:m", occurred_at=when, session_id="traycer:c", project="p",
                        input_tokens=10, cached_input_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=1,
                        reasoning_tokens=None, cost_usd=None, raw_id=f"{tool}-{when.isoformat()}")
    boundary = datetime(2026, 9, 1, 12, 10, 40, tzinfo=UTC)
    rows = [row("grok", boundary - timedelta(hours=1)), row("grok", boundary), row("grok", boundary + timedelta(hours=1)), row("openrouter", boundary + timedelta(hours=1))]
    fake_db = tmp_path / "chat.db"
    fake_db.write_bytes(b"")
    monkeypatch.setattr(traycer_local, "parse_database", lambda _path: rows)
    seen: list[UsageRow] = []

    def capture(**kwargs):
        seen.extend(kwargs["usage_rows"])
        return {"status": "success"}

    monkeypatch.setattr(traycer_local, "persist_rows", capture)
    result = traycer_local.ingest(database_path=tmp_path / "spend.db", pricing=None, database_glob=str(fake_db), grok_covered_from=boundary)
    assert result["mirroredGrokEvents"] == 2 and result["eventsSeen"] == 2
    assert [r.raw_id for r in seen] == [rows[0].raw_id, rows[3].raw_id], "history before the log and non-Grok rows stay"
    seen.clear()
    traycer_local.ingest(database_path=tmp_path / "spend.db", pricing=None, database_glob=str(fake_db))
    assert len(seen) == 4, "without a Grok log nothing is dropped"
