"""Regression gates for the 15-second live-cadence audit.

Each test pins one confirmed defect: unstable or colliding chart bucket keys,
cache-hit denominators that ignored cache writes, quota history bloat from
sub-second reset jitter, unbounded ingest-run history, needless schema
rewrites and row rewrites on every cycle, full re-parsing of the Traycer
store on every poll, missing telemetry stored as measured zero, and the
unpriced Claude Fable 5.1 lane.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app import aggregate, limits
from spend_app.adapters import claude_local, codex_local, cursor_local, opencode_local, traycer_local
from spend_app.adapters.common import UsageRow, persist_rows, promote_priced_unpriced_events
from spend_app.aggregate import _bucket_plan, aggregate_entity, aggregate_health, resolve_window
from spend_app.db import (
    INGEST_RUN_KEEP_PER_SOURCE,
    UsageEvent,
    connect,
    initialize,
    prune_ingest_runs,
    upsert_quota,
    upsert_usage_event,
)
from spend_app.pricing import PricingEngine
from spend_app.quotas import grok_quota_samples, normalize_reset, poll_quotas
from tests_spend.test_aggregation import NOW, TZ, fixture_database, summarize


ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo(TZ)


def _keys(buckets):
    return [bucket[4] for bucket in buckets]


# --------------------------------------------------------------------------
# Chart buckets
# --------------------------------------------------------------------------


def test_rolling_window_bucket_keys_are_unique_and_stable_between_refreshes() -> None:
    # Window is [19:22:31, 19:37:31): the oldest grid cell (19:22) is partial.
    first_now = datetime(2026, 9, 1, 19, 37, 31, tzinfo=UTC)
    first = _bucket_plan(resolve_window("15m", now=first_now, timezone=TZ), ZONE)
    second = _bucket_plan(resolve_window("15m", now=first_now + timedelta(seconds=20), timezone=TZ), ZONE)
    assert len(first) == len(second) == 15
    assert len(set(_keys(first))) == 15
    # Within one grid step every column keeps its key, including the merged
    # oldest bucket and the growing newest bucket.
    assert _keys(first) == _keys(second)
    # Keys are grid cells: the newest bucket is keyed by the cell it started in.
    assert first[-1][4] == "2026-09-01T19:37:00Z"
    assert first[-1][0] == datetime(2026, 9, 1, 19, 37, tzinfo=UTC)
    assert second[-1][1] == first_now + timedelta(seconds=20)
    # The merged oldest bucket spans the partial cell plus the next full cell
    # and carries that next cell's key, so it survives the next refresh.
    assert first[0][0] == first_now - timedelta(minutes=15)
    assert first[0][1] == datetime(2026, 9, 1, 19, 24, tzinfo=UTC)
    assert first[0][4] == "2026-09-01T19:23:00Z"
    assert first[0][3] == first[0][0].astimezone(ZONE).strftime("%H:%M")


def test_rolling_window_changes_exactly_one_column_per_grid_step() -> None:
    first_now = datetime(2026, 9, 1, 19, 37, 31, tzinfo=UTC)
    first = set(_keys(_bucket_plan(resolve_window("15m", now=first_now, timezone=TZ), ZONE)))
    later = set(_keys(_bucket_plan(resolve_window("15m", now=first_now + timedelta(seconds=60), timezone=TZ), ZONE)))
    assert first - later == {"2026-09-01T19:23:00Z"}
    assert later - first == {"2026-09-01T19:38:00Z"}


def test_calendar_windows_split_on_a_fixed_day_grid_with_distinct_keys() -> None:
    now = datetime(2026, 9, 1, 19, 56, 57, tzinfo=UTC)
    first = _bucket_plan(resolve_window("1w", now=now, timezone=TZ), ZONE)
    second = _bucket_plan(resolve_window("1w", now=now + timedelta(seconds=15), timezone=TZ), ZONE)
    assert len(first) == len(second) == 28
    labels = [bucket[3] for bucket in first]
    assert len(set(labels)) < len(labels), "calendar windows repeat axis labels"
    assert len(set(_keys(first))) == 28
    assert _keys(first) == _keys(second)
    # Interior boundaries never move with the rolling edge and sit on a
    # sub-day grid of the local calendar day rather than on drifting midpoints.
    assert [bucket[0] for bucket in first[1:]] == [bucket[0] for bucket in second[1:]]
    for bucket in first[1:]:
        local = bucket[0].astimezone(ZONE)
        assert local.second == 0 and local.minute % 5 == 0, local
    for window in ("1mo", "mtd"):
        plan = _bucket_plan(resolve_window(window, now=now, timezone=TZ), ZONE)
        assert len(set(_keys(plan))) == len(plan)


def test_summary_and_entity_series_carry_unique_bucket_keys(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    for window in ("15m", "1d", "1w"):
        result = summarize(database, pricing, window)
        keys = [bucket["bucketKey"] for bucket in result["series"]]
        assert len(set(keys)) == len(keys) == result["window"]["buckets"]
        entity = aggregate_entity(
            database_path=database,
            pricing=pricing,
            kind="model",
            key="gpt-5.6-sol",
            window_key=window,
            timezone=TZ,
            cache_threshold=0.75,
            now=NOW,
        )
        assert [bucket["bucketKey"] for bucket in entity["series"]] == keys


# --------------------------------------------------------------------------
# Cache-hit denominator
# --------------------------------------------------------------------------


def _prompt_sums(database: Path, start: datetime, end: datetime, model: str | None = None) -> tuple[int, int, int]:
    sql = (
        "SELECT SUM(cached_input_tokens), SUM(input_tokens - cached_input_tokens), SUM(cache_write_tokens) "
        "FROM usage_events WHERE occurred_at >= ? AND occurred_at < ?"
    )
    params: list[object] = [start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")]
    if model:
        sql += " AND model_key = ?"
        params.append(model)
    with connect(database) as connection:
        cached, fresh, writes = connection.execute(sql, params).fetchone()
        if model is None:
            extra = connection.execute(
                "SELECT SUM(cached_input_tokens), SUM(input_tokens - cached_input_tokens), SUM(cache_write_tokens) "
                "FROM unpriced_usage_events WHERE occurred_at >= ? AND occurred_at < ?",
                params,
            ).fetchone()
            cached += extra[0] or 0
            fresh += extra[1] or 0
            writes += extra[2] or 0
    return int(cached), int(fresh), int(writes)


def test_cache_reuse_counts_cache_writes_as_prompt_misses(tmp_path: Path) -> None:
    database, pricing = fixture_database(tmp_path)
    window = resolve_window("1d", now=NOW, timezone=TZ)
    result = summarize(database, pricing, "1d")
    cached, fresh, writes = _prompt_sums(database, window.start, window.end)
    assert writes > 0, "fixture must carry cache writes for this gate to bite"
    expected = cached / (cached + fresh + writes) * 100
    assert abs(result["totals"]["cacheReusePct"] - expected) < 1e-9
    # The KPI percentage and the token-mix card now share one denominator:
    # the mix folds writes into fresh input, and so does the hit rate.
    mix = {item["key"]: item["tokens"] for item in result["mix"]}
    assert abs(result["totals"]["cacheReusePct"] - mix["cached_input"] / (mix["cached_input"] + mix["fresh_input"]) * 100) < 1e-9
    sol = next(model for model in result["models"] if model["key"] == "gpt-5.6-sol")
    m_cached, m_fresh, m_writes = _prompt_sums(database, window.start, window.end, "gpt-5.6-sol")
    assert abs(sol["cachePct"] - m_cached / (m_cached + m_fresh + m_writes) * 100) < 1e-9
    codex = next(tool for tool in result["tools"] if tool["key"] == "codex")
    assert abs(codex["cachePct"] - sol["cachePct"]) < 1e-9
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
    assert abs(entity["cachePct"] - sol["cachePct"]) < 1e-9
    session_a = next(row for row in entity["sessions"]["rows"] if row["id"] == "session-a")
    assert abs(session_a["cachePct"] - 20_000 / (20_000 + 80_000 + 10_000) * 100) < 1e-9


# --------------------------------------------------------------------------
# Quota polling
# --------------------------------------------------------------------------


def test_reset_timestamps_persist_at_second_precision() -> None:
    assert normalize_reset("2026-09-02T00:50:00.169266+00:00") == "2026-09-02T00:50:00Z"
    assert normalize_reset("2026-09-02T00:50:00.044345+00:00") == "2026-09-02T00:50:00Z"
    # The same reset was also observed just before the boundary; it must not
    # flap to the previous second (which rendered as the previous minute).
    assert normalize_reset("2026-09-02T00:49:59.912345+00:00") == "2026-09-02T00:50:00Z"
    assert normalize_reset("2026-09-07T02:25:37Z") == "2026-09-07T02:25:37Z"
    assert normalize_reset("not a timestamp") == "not a timestamp"
    assert normalize_reset(None) is None


def test_sub_second_reset_jitter_does_not_write_a_new_quota_row(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)

    def payload(reset: str) -> dict:
        return {
            "status": "exact",
            "windows": [{"key": "weekly", "usedPct": 52.0, "resetAt": reset}],
        }

    first = poll_quotas(
        database,
        collectors={"grok": lambda: grok_quota_samples(payload("2026-09-03T03:40:35.574000+00:00"), source="traycer_profile")},
        now=lambda: "2026-09-01T18:31:47Z",
    )
    second = poll_quotas(
        database,
        collectors={"grok": lambda: grok_quota_samples(payload("2026-09-03T03:40:35.913511+00:00"), source="traycer_profile")},
        now=lambda: "2026-09-01T18:33:17Z",
    )
    assert first["written"] == 1
    assert second["written"] == 0 and second["skipped"] == 1
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT resets_at, polled_at FROM quotas WHERE provider_key='grok'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("2026-09-03T03:40:36Z", "2026-09-01T18:33:17Z")]


def test_health_quota_reason_is_the_persisted_failure_not_a_generic_label(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="Grok Build weekly — Traycer Grok Build quota lookup failed (RuntimeError).",
            unit="unavailable",
            source="traycer_profile",
            polled_at="2026-09-01T18:50:51Z",
        )
    health = aggregate_health(database_path=database, now=NOW, timezone=TZ)
    grok = next(row for row in health["quotas"] if row["providerKey"] == "grok")
    assert grok["status"] == "unavailable"
    assert grok["reason"] == "Traycer Grok Build quota lookup failed (RuntimeError)."


# --------------------------------------------------------------------------
# Database growth and contention
# --------------------------------------------------------------------------


def test_prune_ingest_runs_bounds_history_but_keeps_latest_status(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")  # noqa: E731
    with connect(database) as connection:
        for index in range(INGEST_RUN_KEEP_PER_SOURCE + 300):
            started = now - timedelta(days=2) + timedelta(seconds=15 * index)
            connection.execute(
                "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES(?,?,?,?,?)",
                ("codex_local", stamp(started), stamp(started + timedelta(seconds=1)), "success", 0),
            )
        for index in range(5):
            started = now - timedelta(days=3) + timedelta(minutes=15 * index)
            connection.execute(
                "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES(?,?,?,?,?,?)",
                ("openai_admin", stamp(started), stamp(started), "skipped", 0, "OPENAI_ADMIN_KEY is not configured"),
            )
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,status,events_written) VALUES(?,?,?,?)",
            ("cursor_local", stamp(now - timedelta(days=5)), "running", 0),
        )
        deleted = prune_ingest_runs(connection, now=stamp(now))
        per_source = {
            row[0]: row[1]
            for row in connection.execute("SELECT source, COUNT(*) FROM ingest_runs GROUP BY source")
        }
        newest = connection.execute(
            "SELECT MAX(started_at) FROM ingest_runs WHERE source='codex_local'"
        ).fetchone()[0]
    assert deleted == 300
    assert per_source == {"codex_local": INGEST_RUN_KEEP_PER_SOURCE, "openai_admin": 5, "cursor_local": 1}
    assert newest == stamp(now - timedelta(days=2) + timedelta(seconds=15 * (INGEST_RUN_KEEP_PER_SOURCE + 299)))
    health = aggregate_health(database_path=database, now=now, timezone=TZ)
    assert {row["source"]: row["status"] for row in health["ingest"]}["openai_admin"] == "unavailable"


def test_initialize_on_a_current_database_writes_nothing(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    observer = sqlite3.connect(database)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        observer.execute("SELECT COUNT(*) FROM app_meta").fetchone()
        initialize(database)
        initialize(database)
        observer.execute("SELECT COUNT(*) FROM app_meta").fetchone()
        after = observer.execute("PRAGMA data_version").fetchone()[0]
    finally:
        observer.close()
    assert before == after
    with connect(database) as connection:
        assert connection.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0] == "9"


def test_identical_reingest_does_not_rewrite_the_row(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    event = UsageEvent(
        source="opencode_local",
        tool_key="opencode",
        model_key="opencode:glm-5.3-flash",
        occurred_at="2026-09-01T12:00:00Z",
        session_id="s1",
        project="p",
        input_tokens=1000,
        cached_input_tokens=800,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=50,
        reasoning_tokens=None,
        cost_usd=None,
        computed_cost_usd=0.001,
        raw_id="opencode-local:s1",
        ingested_at="2026-09-01T12:00:05Z",
        is_exact=False,
    )
    with connect(database) as connection:
        assert upsert_usage_event(connection, event) is True
    observer = sqlite3.connect(database)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        with connect(database) as connection:
            same = UsageEvent(**{**event.__dict__, "ingested_at": "2026-09-01T12:00:20Z"})
            assert upsert_usage_event(connection, same) is False
        observer.execute("SELECT COUNT(*) FROM usage_events").fetchone()
        unchanged = observer.execute("PRAGMA data_version").fetchone()[0]
        with connect(database) as connection:
            grown = UsageEvent(**{**event.__dict__, "output_tokens": 75, "ingested_at": "2026-09-01T12:00:35Z"})
            assert upsert_usage_event(connection, grown) is False
        observer.execute("SELECT COUNT(*) FROM usage_events").fetchone()
        changed = observer.execute("PRAGMA data_version").fetchone()[0]
    finally:
        observer.close()
    assert unchanged == before
    assert changed != before
    with connect(database) as connection:
        row = connection.execute("SELECT output_tokens, ingested_at FROM usage_events").fetchone()
    assert tuple(row) == (75, "2026-09-01T12:00:35Z")


# --------------------------------------------------------------------------
# Traycer parse caches
# --------------------------------------------------------------------------


def _projection(events: list[dict], *, title: str = "Agent") -> str:
    return json.dumps(
        {
            "title": title,
            "lifecycle": {"state": "active"},
            "settings": {"harnessId": "grok", "model": "grok-4.6"},
            "events": [{"body": body} for body in events],
        }
    )


def _usage_event(timestamp: int) -> dict:
    return {
        "type": "turn.completed",
        "timestamp": timestamp,
        "metadata": {
            "usage": {
                "inputTokens": 1000,
                "outputTokens": 100,
                "cacheReadInputTokens": 600,
                "cacheCreationInputTokens": 0,
                "totalTokens": 1100,
            }
        },
    }


def _write_versioned_store(path: Path, chats: dict[str, tuple[int, int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS chat_projection("
        "chat_id TEXT PRIMARY KEY, through_seq INTEGER NOT NULL, projection_json TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    connection.execute("DELETE FROM chat_projection")
    for chat_id, (seq, updated_at, projection_json) in chats.items():
        connection.execute(
            "INSERT INTO chat_projection VALUES(?,?,?,?,?)",
            (chat_id, seq, projection_json, updated_at, updated_at),
        )
    connection.commit()
    connection.close()


def test_traycer_ingest_reparses_only_projections_whose_sequence_advanced(tmp_path: Path, monkeypatch) -> None:
    traycer_local.reset_projection_cache()
    store = tmp_path / "epic" / "chat" / "chat.db"
    _write_versioned_store(
        store,
        {
            "chat-a": (5, 1788_300_000_000, _projection([_usage_event(1788_300_000_000)])),
            "chat-b": (7, 1788_300_100_000, _projection([_usage_event(1788_300_100_000)])),
        },
    )
    parsed: list[str] = []
    original = traycer_local.parse_projection

    def counting(*, path, chat_id, projection_json):
        parsed.append(chat_id)
        return original(path=path, chat_id=chat_id, projection_json=projection_json)

    monkeypatch.setattr(traycer_local, "parse_projection", counting)
    first = traycer_local.parse_database(store)
    assert sorted(parsed) == ["chat-a", "chat-b"]
    second = traycer_local.parse_database(store)
    assert parsed == ["chat-a", "chat-b"], "unchanged projections must not be re-parsed"
    assert second == first
    _write_versioned_store(
        store,
        {
            "chat-a": (5, 1788_300_000_000, _projection([_usage_event(1788_300_000_000)])),
            "chat-b": (8, 1788_300_200_000, _projection([_usage_event(1788_300_100_000), _usage_event(1788_300_200_000)])),
        },
    )
    third = traycer_local.parse_database(store)
    assert parsed == ["chat-a", "chat-b", "chat-b"]
    assert len(third) == len(first) + 1
    traycer_local.reset_projection_cache()


def test_traycer_activity_poll_reuses_lifecycle_summaries(tmp_path: Path, monkeypatch) -> None:
    limits.reset_activity_cache()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    store = tmp_path / ".traycer" / "host" / "epic-state" / "epic-1" / "chat" / "chat.db"
    completed = {"type": "turn.completed", "timestamp": now_ms - 5_000, "metadata": {}}
    _write_versioned_store(store, {"chat-1": (3, now_ms - 5_000, _projection([completed], title="Done"))})
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    calls = []
    original = limits._projection_lifecycle

    def counting(projection):
        calls.append(1)
        return original(projection)

    monkeypatch.setattr(limits, "_projection_lifecycle", counting)
    first = limits._traycer_activity()
    second = limits._traycer_activity()
    assert first == second == {"activeAgents": [], "unmeteredTurns": []}
    assert len(calls) == 1, "an unchanged projection is parsed once, not on every 4-second poll"
    limits.reset_activity_cache()


# --------------------------------------------------------------------------
# Missing telemetry stays NULL
# --------------------------------------------------------------------------


def test_reasoning_detail_absent_from_source_is_stored_as_null(tmp_path: Path) -> None:
    session_dir = tmp_path / "projects" / "demo"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "session.jsonl"
    base = {
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 100,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-09-01T10:00:00Z", "sessionId": "s", "cwd": "C:/x", "message": {"id": "m1", "model": "claude-opus-5", "usage": base}}),
                json.dumps({"timestamp": "2026-09-01T10:00:05Z", "sessionId": "s", "cwd": "C:/x", "message": {"id": "m2", "model": "claude-opus-5", "usage": {**base, "output_tokens_details": {"thinking_tokens": 0}}}}),
            ]
        ),
        encoding="utf-8",
    )
    _session, events = claude_local.parse_file(transcript)
    assert [event.reasoning_tokens for event in events] == [None, 0]

    cursor_db = tmp_path / "projects" / "demo" / "sdk-agent-store" / "x" / "index.db"
    cursor_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(cursor_db)
    connection.execute(
        "CREATE TABLE runs(run_id TEXT, agent_id TEXT, model TEXT, usage_json TEXT, finished_at TEXT, updated_at TEXT, created_at TEXT)"
    )
    connection.execute(
        "INSERT INTO runs VALUES('r1','a1','grok-4.6',?, '2026-09-01T10:00:00Z', NULL, NULL)",
        (json.dumps({"inputTokens": 10, "cacheReadTokens": 5, "cacheWriteTokens": 0, "outputTokens": 2}),),
    )
    connection.execute(
        "INSERT INTO runs VALUES('r2','a1','grok-4.6',?, '2026-09-01T10:01:00Z', NULL, NULL)",
        (json.dumps({"inputTokens": 10, "cacheReadTokens": 5, "cacheWriteTokens": 0, "outputTokens": 2, "reasoningTokens": 0}),),
    )
    connection.commit()
    connection.close()
    rows = {row.raw_id: row for row in cursor_local.parse_database(cursor_db)}
    assert sorted(row.reasoning_tokens for row in rows.values() if row.reasoning_tokens is not None) == [0]
    assert any(row.reasoning_tokens is None for row in rows.values())

    opencode_db = tmp_path / "opencode.db"
    connection = sqlite3.connect(opencode_db)
    connection.execute(
        "CREATE TABLE session(id TEXT, project_id TEXT, directory TEXT, path TEXT, model TEXT, cost REAL,"
        " tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER,"
        " tokens_cache_write INTEGER, time_updated INTEGER)"
    )
    model = json.dumps({"providerID": "zai-coding-plan", "id": "glm-5.3-flash"})
    connection.execute("INSERT INTO session VALUES('s1','p','C:/x','C:/x',?,0.0,10,2,NULL,5,0,1788300000000)", (model,))
    connection.execute("INSERT INTO session VALUES('s2','p','C:/x','C:/x',?,0.0,10,2,0,5,0,1788300000000)", (model,))
    connection.commit()
    connection.close()
    opencode_rows = {row.session_id: row for row in opencode_local.parse_database(opencode_db)}
    assert opencode_rows["s1"].reasoning_tokens is None
    assert opencode_rows["s2"].reasoning_tokens == 0

    codex_file = tmp_path / "rollout.jsonl"
    usage = {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2, "total_tokens": 12}
    codex_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "timestamp": "2026-09-01T10:00:00Z", "payload": {"id": "c1", "timestamp": "2026-09-01T10:00:00Z", "originator": "Codex Desktop"}}),
                json.dumps({"type": "turn_context", "timestamp": "2026-09-01T10:00:01Z", "payload": {"model": "gpt-5.6-sol"}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-09-01T10:00:02Z", "payload": {"type": "token_count", "info": {"last_token_usage": usage}}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-09-01T10:00:03Z", "payload": {"type": "token_count", "info": {"last_token_usage": {**usage, "reasoning_output_tokens": 0}}}}),
            ]
        ),
        encoding="utf-8",
    )
    _session, codex_events = codex_local.parse_file(codex_file)
    assert [event.reasoning_tokens for event in codex_events] == [None, 0]


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_claude_fable_5_1_is_priced_from_the_official_card_and_promotes(tmp_path: Path) -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    when = datetime(2026, 9, 1, 19, 50, 41, tzinfo=UTC)
    price = pricing.resolve("claude-fable-5-1", when)
    assert price.is_exact is True
    assert price.source_url.startswith("https://platform.claude.com/")
    assert (price.input_per_mtok, price.cached_input_per_mtok, price.output_per_mtok) == (
        Decimal("10.00"),
        Decimal("0.25"),
        Decimal("50.00"),
    )
    database = tmp_path / "spend.db"
    initialize(database)
    row = UsageRow(
        source="claude_local",
        tool_key="claude-code",
        model_key="claude-fable-5-1",
        occurred_at=when,
        session_id="s",
        project="p",
        input_tokens=39_302,
        cached_input_tokens=39_300,
        cache_write_tokens=22_799,
        cache_write_1h_tokens=22_799,
        output_tokens=167,
        reasoning_tokens=34,
        cost_usd=None,
        raw_id="claude-local:s:msg_1",
    )
    result = persist_rows(database_path=database, pricing=pricing, source="claude_local", usage_rows=[row])
    assert result["status"] == "success"
    assert result["unpricedModels"] == []
    with connect(database) as connection:
        computed = connection.execute("SELECT computed_cost_usd, is_exact FROM usage_events").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0] == 0
    expected = Decimal(2) / Decimal(1_000_000) * Decimal(10) + Decimal(39_300) / Decimal(1_000_000) * Decimal("0.25")
    expected += Decimal(22_799) / Decimal(1_000_000) * Decimal(20) + Decimal(167) / Decimal(1_000_000) * Decimal(50)
    assert abs(Decimal(str(computed[0])) - expected) < Decimal("1e-9")
    assert computed[1] == 1


def test_pricing_index_resolves_exactly_like_a_linear_scan() -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    probes = [datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)]
    keys = {price.model_key for price in pricing.prices} | {alias for price in pricing.prices for alias in price.aliases}
    for key in sorted(keys):
        for when in probes:
            linear = [
                price
                for price in pricing.prices
                if (key == price.model_key or key in price.aliases)
                and price.effective_from <= when
                and (price.effective_to is None or when < price.effective_to)
            ]
            if not linear:
                try:
                    pricing.resolve(key, when)
                except LookupError:
                    continue
                raise AssertionError(f"{key} resolved without a candidate at {when}")
            assert pricing.resolve(key, when) == max(linear, key=lambda price: price.effective_from)


# --------------------------------------------------------------------------
# Memoised derived values
# --------------------------------------------------------------------------


def test_memoised_month_and_burn_values_refresh_when_rows_change(tmp_path: Path) -> None:
    aggregate.reset_memo()
    database, pricing = fixture_database(tmp_path)
    first = summarize(database, pricing, "1d")
    again = summarize(database, pricing, "1d")
    assert again["projected"] == first["projected"]
    assert again["navigation"]["burnRatePerDay"] == first["navigation"]["burnRatePerDay"]
    from tests_spend.test_aggregation import add_event

    add_event(
        database,
        pricing,
        raw_id="codex-late",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-late",
        occurred=NOW - timedelta(hours=20),
        input_tokens=50_000,
        cached=10_000,
        writes=0,
        output=4_000,
    )
    changed = summarize(database, pricing, "1d")
    assert changed["navigation"]["burnRatePerDay"] != first["navigation"]["burnRatePerDay"]
    assert changed["heatmap"] != first["heatmap"]
    zone = ZoneInfo(TZ)
    month_start = datetime(NOW.astimezone(zone).year, NOW.astimezone(zone).month, 1, tzinfo=zone).astimezone(UTC)
    with connect(database) as connection:
        _parts, usage = aggregate._month_usage(connection, pricing, month_start, NOW, "all")
        _parts, again_usage = aggregate._month_usage(connection, pricing, month_start, NOW, "all")
        assert usage == again_usage
        add_event(
            database,
            pricing,
            raw_id="codex-late-2",
            tool="codex",
            model="gpt-5.6-sol",
            session="session-late",
            occurred=NOW - timedelta(hours=19),
            input_tokens=50_000,
            cached=10_000,
            writes=0,
            output=4_000,
        )
    with connect(database) as connection:
        _parts, grown = aggregate._month_usage(connection, pricing, month_start, NOW, "all")
    assert grown["codex"] > usage["codex"]
    aggregate.reset_memo()
