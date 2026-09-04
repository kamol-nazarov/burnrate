import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.adapters.cursor_local import parse_database as parse_cursor
from spend_app.adapters.local_common import (
    classify_traycer_usage,
    open_text_read_only,
    sqlite_read_only,
)
from spend_app.adapters.opencode_local import ingest as ingest_opencode
from spend_app.adapters.opencode_local import parse_database as parse_opencode
from spend_app.adapters.traycer_local import ingest as ingest_traycer
from spend_app.adapters.traycer_local import parse_database as parse_traycer
from spend_app.aggregate import aggregate_entity, aggregate_summary
from spend_app.db import connect
from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.reference_rates import compute as compute_reference_cost


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests_spend" / "fixtures"


def test_sqlite_read_only_sets_query_only_and_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "provider.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(x INTEGER)")
        connection.execute("INSERT INTO t VALUES(1)")
    readonly = sqlite_read_only(path)
    try:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        write_failed = False
        try:
            readonly.execute("INSERT INTO t VALUES(2)")
            readonly.commit()
        except sqlite3.OperationalError:
            write_failed = True
        assert write_failed
        assert readonly.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        readonly.close()


def test_open_text_read_only_does_not_truncate(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("keep-me\n", encoding="utf-8")
    with open_text_read_only(path) as handle:
        assert handle.read() == "keep-me\n"
    assert path.read_text(encoding="utf-8") == "keep-me\n"


def test_cursor_local_preserves_additive_cache_semantics(tmp_path: Path) -> None:
    database = tmp_path / "cursor.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE runs(
            run_id TEXT,agent_id TEXT,model TEXT,usage_json TEXT,
            finished_at TEXT,updated_at TEXT,created_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
        (
            "run-1",
            "agent-1",
            "grok-4.6",
            json.dumps(
                {
                    "inputTokens": 100,
                    "cacheReadTokens": 900,
                    "cacheWriteTokens": 25,
                    "outputTokens": 50,
                }
            ),
            "2026-08-30T12:00:00Z",
            None,
            None,
        ),
    )
    connection.commit()
    connection.close()

    rows = parse_cursor(database)
    assert len(rows) == 1
    assert rows[0].model_key == "cursor:grok-4.6"
    assert rows[0].input_tokens == 1_000
    assert rows[0].cached_input_tokens == 900
    assert rows[0].cache_write_tokens == 25


def test_cursor_local_tolerates_missing_runs_table(tmp_path: Path) -> None:
    database = tmp_path / "cursor.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE other(x)")
    connection.commit()
    connection.close()

    assert parse_cursor(database) == []


def test_opencode_excludes_traycer_openrouter_mirror(tmp_path: Path) -> None:
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE session(
            id TEXT,project_id TEXT,directory TEXT,path TEXT,model TEXT,cost REAL,
            tokens_input INTEGER,tokens_output INTEGER,tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,tokens_cache_write INTEGER,time_updated INTEGER
        )
        """
    )
    values = (
        "project",
        "C:/repo",
        "C:/repo",
        0.0,
        100,
        10,
        5,
        900,
        0,
        1788100000000,
    )
    connection.execute(
        "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("direct", *values[:3], json.dumps({"id": "glm-5.3-flash", "providerID": "zai-coding-plan"}), *values[3:]),
    )
    connection.execute(
        "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("mirror", *values[:3], json.dumps({"id": "glm-5.3-flash", "providerID": "traycer-openrouter"}), *values[3:]),
    )
    connection.commit()
    connection.close()

    rows = parse_opencode(database)
    assert [row.session_id for row in rows] == ["direct"]
    assert rows[0].model_key == "opencode:glm-5.3-flash"


def test_traycer_local_reads_only_structured_grok_and_openrouter_usage(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE chat_projection(chat_id TEXT,projection_json TEXT)")
    projection = {
        "settings": {"harnessId": "grok", "model": "grok-4.6"},
        "events": [
            {
                "body": {
                    "timestamp": 1788100000000,
                    "metadata": {
                        "usage": {
                            "inputTokens": 1_000,
                            "cacheReadInputTokens": 900,
                            "outputTokens": 50,
                            "totalTokens": 1_050,
                        }
                    },
                }
            },
            {
                "body": {
                    "timestamp": 1788100010000,
                    "metadata": {
                        "item": {
                            "settings": {
                                "harnessId": "openrouter",
                                "model": "openrouter:z-ai/glm-5.3-flash",
                            }
                        },
                        "usage": {
                            "inputTokens": 100,
                            "cacheReadInputTokens": 900,
                            "outputTokens": 10,
                            "totalTokens": 1_010,
                        },
                    },
                }
            },
        ],
    }
    connection.execute("INSERT INTO chat_projection VALUES(?,?)", ("chat-1", json.dumps(projection)))
    connection.commit()
    connection.close()

    rows = parse_traycer(database)
    assert [row.model_key for row in rows] == [
        "supergrok:grok-4.6",
        "openrouter:glm-5.3-flash",
    ]
    assert rows[0].cached_input_tokens == 900
    assert rows[1].input_tokens == 1_000


def test_unpriced_usage_is_idempotent_and_visible_without_fake_spend(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    occurred = datetime(2026, 8, 30, 12, tzinfo=UTC)
    row = UsageRow(
        source="opencode_local",
        tool_key="opencode",
        model_key="opencode:unlisted-model",
        occurred_at=occurred,
        session_id="agent-1",
        project="fixture",
        input_tokens=1_000,
        cached_input_tokens=900,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=50,
        reasoning_tokens=5,
        cost_usd=None,
        raw_id="opencode-fixture-1",
    )
    first = persist_rows(
        database_path=database,
        pricing=pricing,
        source="opencode_local",
        usage_rows=[row],
    )
    second = persist_rows(
        database_path=database,
        pricing=pricing,
        source="opencode_local",
        usage_rows=[row],
    )
    assert first["unpricedEventsWritten"] == 1
    assert second["unpricedEventsWritten"] == 0
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0] == 1

    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone="UTC",
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 13, tzinfo=UTC),
    )
    model = summary["models"][0]
    assert summary["totals"]["trackedValue"] is None
    assert model["name"] == "OpenCode UNLISTED MODEL"
    assert model["value"] is None
    assert model["cachePct"] == 90
    assert model["tokens"] == 1_050
    assert summary["totals"]["tokens"] == 1_050
    assert abs(sum(point["total"] for point in summary["series"]) - 1_050) < 1e-6

    entity = aggregate_entity(
        database_path=database,
        pricing=pricing,
        kind="model",
        key="opencode:unlisted-model",
        window_key="1d",
        timezone="UTC",
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 13, tzinfo=UTC),
    )
    assert entity["value"] is None
    assert entity["cachePct"] == 90
    assert entity["tokens"] == 1_050


def test_published_reference_rates_are_effective_dated() -> None:
    cursor_value, cursor_rate = compute_reference_cost(
        model_key="cursor:grok-4.6",
        occurred_at=datetime(2026, 8, 30, tzinfo=UTC),
        input_tokens=1_000,
        cached_input_tokens=900,
        cache_write_tokens=0,
        output_tokens=100,
    )
    assert cursor_rate is not None
    # Official Cursor rate: fresh 100 @ $2 + cached 900 @ $0.5 + out 100 @ $6 per MTok.
    assert float(cursor_value) == 0.00125
    assert cursor_rate.label == "Cursor published usage rate"
    assert cursor_rate.source_url == "https://cursor.com/docs/models-and-pricing"

    supergrok_value, supergrok_rate = compute_reference_cost(
        model_key="supergrok:grok-4.6",
        occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
        input_tokens=200_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
    )
    assert supergrok_rate is not None
    # xAI long-context tier applies at exactly 200k prompt tokens: $4/MTok.
    assert float(supergrok_value) == 0.8

    # GLM-5.3-Flash is priced from official listings only: Z.AI promo card and
    # the recorded official OpenRouter metadata, both through the promo
    # boundary 2026-09-09T16:00:00Z.
    promo_kwargs = dict(
        occurred_at=datetime(2026, 8, 30, tzinfo=UTC),
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    zai_value, zai_rate = compute_reference_cost(model_key="opencode:glm-5.3-flash", **promo_kwargs)
    assert zai_rate is not None
    assert zai_rate.label == "Z.AI published rate"
    assert zai_rate.source_url == "https://docs.z.ai/guides/overview/pricing.md"
    assert float(zai_value) == 0.325
    router_value, router_rate = compute_reference_cost(
        model_key="openrouter:glm-5.3-flash", **promo_kwargs
    )
    assert router_rate is not None
    assert router_rate.source_url == "https://openrouter.ai/api/v1/models?q=glm-5.3-flash"
    assert float(router_value) == 0.325

    # After the promo boundary: Z.AI strikes to list rates ($0.15 / $0.50),
    # while the OpenRouter promo row must not silently extend.
    after_kwargs = dict(promo_kwargs, occurred_at=datetime(2026, 9, 10, tzinfo=UTC))
    list_value, list_rate = compute_reference_cost(
        model_key="opencode:glm-5.3-flash", **after_kwargs
    )
    assert list_rate is not None
    assert float(list_value) == 0.65
    value, rate = compute_reference_cost(model_key="openrouter:glm-5.3-flash", **after_kwargs)
    assert value is None
    assert rate is None


def _opencode_sessions() -> list[dict]:
    return json.loads((FIXTURES / "opencode_sessions.json").read_text(encoding="utf-8"))["sessions"]


def write_opencode_fixture(path: Path, sessions: list[dict] | None = None) -> Path:
    sessions = _opencode_sessions() if sessions is None else sessions
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE session(
            id TEXT,project_id TEXT,directory TEXT,path TEXT,model TEXT,cost REAL,
            tokens_input INTEGER,tokens_output INTEGER,tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,tokens_cache_write INTEGER,time_updated INTEGER
        )
        """
    )
    for session in sessions:
        connection.execute(
            "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session["id"],
                None,
                session.get("directory"),
                session.get("directory"),
                json.dumps({"id": session["modelID"], "providerID": session["providerID"]}),
                session["cost"],
                session["tokens_input"],
                session["tokens_output"],
                session["tokens_reasoning"],
                session["tokens_cache_read"],
                session["tokens_cache_write"],
                session["time_updated"],
            ),
        )
    connection.commit()
    connection.close()
    return path


def _projection_json(chat: dict) -> str:
    events = []
    for event in chat["events"]:
        metadata: dict = {}
        if "usage" in event:
            metadata["usage"] = event["usage"]
        transition = event.get("settingsTransition")
        if isinstance(transition, dict) and isinstance(transition.get("settings"), dict):
            settings_wrap = {"settings": transition["settings"]}
            if transition.get("shape") == "items":
                metadata["items"] = [settings_wrap]
            else:
                metadata["item"] = settings_wrap
        events.append({"body": {"timestamp": event["timestamp"], "metadata": metadata}})
    return json.dumps({"settings": chat["rootSettings"], "events": events})


def write_traycer_fixture(path: Path) -> Path:
    document = json.loads(
        (FIXTURES / "traycer_chat_projection.json").read_text(encoding="utf-8")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE chat_projection(chat_id TEXT,projection_json TEXT)")
    for chat in document["chats"]:
        connection.execute(
            "INSERT INTO chat_projection VALUES(?,?)",
            (chat["chatId"], _projection_json(chat)),
        )
    connection.commit()
    connection.close()
    return path


def test_opencode_omitted_reasoning_stays_null(tmp_path: Path) -> None:
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE session(
            id TEXT,project_id TEXT,directory TEXT,path TEXT,model TEXT,cost REAL,
            tokens_input INTEGER,tokens_output INTEGER,tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,tokens_cache_write INTEGER,time_updated INTEGER
        )
        """
    )
    connection.execute(
        "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "partial",
            None,
            r"C:\Dev\ExampleProject",
            r"C:\Dev\ExampleProject",
            json.dumps({"id": "glm-5.3-flash", "providerID": "zai-coding-plan"}),
            0.0,
            100,
            10,
            None,
            0,
            0,
            1788100000000,
        ),
    )
    connection.commit()
    connection.close()
    rows = parse_opencode(database)
    assert len(rows) == 1
    assert rows[0].reasoning_tokens is None
    assert rows[0].project == "ExampleProject"
    assert rows[0].telemetry_complete is True


def test_opencode_tolerates_missing_session_table(tmp_path: Path) -> None:
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE other(x)")
    connection.commit()
    connection.close()
    assert parse_opencode(database) == []


def test_opencode_fixture_skips_mirrors_and_is_idempotent(tmp_path: Path) -> None:
    source = write_opencode_fixture(tmp_path / "opencode.db")
    spend = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    sessions = _opencode_sessions()
    expected_direct = [session for session in sessions if session["providerID"] != "traycer-openrouter"]
    expected_mirrors = [session for session in sessions if session["providerID"] == "traycer-openrouter"]
    assert expected_direct
    assert expected_mirrors

    parsed = parse_opencode(source)
    assert {row.session_id for row in parsed} == {session["id"] for session in expected_direct}
    assert all(row.model_key == "opencode:glm-5.3-flash" for row in parsed)
    assert all(row.tool_key == "opencode" for row in parsed)
    assert all(row.cost_usd is None for row in parsed)
    sample = next(row for row in parsed if row.session_id == "rec-ses-009")
    assert sample.input_tokens == 11_059 + 10_496
    assert sample.cached_input_tokens == 10_496
    first_ids = [row.raw_id for row in parsed]
    assert first_ids == [row.raw_id for row in parse_opencode(source)]

    first = ingest_opencode(database_path=spend, pricing=pricing, source_database=source)
    second = ingest_opencode(database_path=spend, pricing=pricing, source_database=source)
    assert first["status"] == "success"
    assert first["eventsWritten"] == len(expected_direct)
    assert first["unpricedModels"] == []
    assert second["eventsWritten"] == 0
    with connect(spend) as connection:
        stored = list(
            connection.execute(
                """
                SELECT session_id, model_key, is_exact, cost_usd, computed_cost_usd,
                       input_tokens, cached_input_tokens, output_tokens, raw_id
                FROM usage_events ORDER BY session_id
                """
            )
        )
        gaps = connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0]
    assert len(stored) == len(expected_direct)
    assert gaps == 0
    assert {row[0] for row in stored} == {session["id"] for session in expected_direct}
    assert all(row[1] == "opencode:glm-5.3-flash" for row in stored)
    assert all(row[2] == 0 for row in stored)
    assert all(row[3] is None for row in stored)
    assert all(row[4] > 0 for row in stored)
    price = pricing.resolve("opencode:glm-5.3-flash", parsed[0].occurred_at)
    assert price.is_exact is False
    assert price.source_url.startswith("https://docs.z.ai/")
    rec009 = next(row for row in stored if row[0] == "rec-ses-009")
    assert rec009[5] == 11_059 + 10_496
    assert rec009[6] == 10_496
    assert rec009[7] == 299
    assert rec009[4] == float(
        pricing.compute(
            model_key="opencode:glm-5.3-flash",
            occurred_at=sample.occurred_at,
            input_tokens=sample.input_tokens,
            cached_input_tokens=sample.cached_input_tokens,
            cache_write_tokens=sample.cache_write_tokens,
            output_tokens=sample.output_tokens,
        )
    )


def test_traycer_fixture_ingests_grok_and_openrouter_only(tmp_path: Path) -> None:
    source = write_traycer_fixture(tmp_path / "epic" / "chat" / "chat.db")
    spend = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    parsed = parse_traycer(source)
    assert {row.tool_key for row in parsed} <= {"grok", "openrouter"}
    assert {row.raw_id for row in parsed} == {row.raw_id for row in parse_traycer(source)}
    grok_rows = [row for row in parsed if row.tool_key == "grok"]
    router_rows = [row for row in parsed if row.tool_key == "openrouter"]
    assert grok_rows
    assert router_rows
    assert all(row.model_key == "supergrok:grok-4.6" for row in grok_rows)
    assert all(row.model_key.startswith("openrouter:") for row in router_rows)
    reported = [row for row in grok_rows if row.cost_usd is not None]
    assert reported
    assert all(row.cost_usd > 0 for row in reported)
    incomplete = [row for row in parsed if not row.telemetry_complete]
    assert incomplete
    assert all(row.unclassified_tokens > 0 for row in incomplete)
    priced_incomplete = []
    for row in incomplete:
        try:
            pricing.resolve(row.model_key, row.occurred_at)
            priced_incomplete.append(row)
        except UnpricedModelError:
            continue
    assert priced_incomplete

    first = ingest_traycer(database_path=spend, pricing=pricing, database_glob=str(source))
    second = ingest_traycer(database_path=spend, pricing=pricing, database_glob=str(source))
    assert first["unclassifiedEvents"] == len(incomplete)
    assert first["coverageGapsWritten"] == len(priced_incomplete)
    assert second["eventsWritten"] == 0
    assert second["coverageGapsWritten"] == 0
    with connect(spend) as connection:
        usage = list(
            connection.execute(
                "SELECT tool_key, model_key, is_exact, cost_usd, raw_id FROM usage_events"
            )
        )
        coverage = list(connection.execute("SELECT raw_id, issue FROM coverage_gap_events"))
        unpriced = list(
            connection.execute(
                """
                SELECT raw_id, telemetry_complete, unclassified_tokens, model_key
                FROM unpriced_usage_events
                """
            )
        )
        usage_ids = {row[4] for row in usage}
        coverage_ids = {row[0] for row in coverage}
    assert usage
    assert all(row[0] in {"grok", "openrouter"} for row in usage)
    assert all(row[2] == 0 for row in usage)
    grok_price = pricing.resolve("supergrok:grok-4.6", grok_rows[0].occurred_at)
    assert grok_price.is_exact is False
    assert grok_price.source_url.startswith("https://docs.x.ai/")
    stored_reported = [row[3] for row in usage if row[3] is not None]
    assert stored_reported
    assert all(value > 0 for value in stored_reported)
    assert {row.raw_id for row in priced_incomplete} <= coverage_ids
    assert {row.raw_id for row in priced_incomplete}.isdisjoint(usage_ids)
    assert all(row[1] == 0 for row in unpriced if row[0] in coverage_ids)
    glm_priced = pricing.resolve(
        "openrouter:glm-5.3-flash",
        next(row.occurred_at for row in router_rows if row.model_key == "openrouter:glm-5.3-flash"),
    )
    assert glm_priced.is_exact is False
    assert "openrouter.ai" in glm_priced.source_url


def test_opencode_and_traycer_do_not_double_count_mirrors(tmp_path: Path) -> None:
    opencode_source = write_opencode_fixture(tmp_path / "opencode.db")
    traycer_source = write_traycer_fixture(tmp_path / "epic" / "chat" / "chat.db")
    spend = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    ingest_opencode(database_path=spend, pricing=pricing, source_database=opencode_source)
    ingest_traycer(database_path=spend, pricing=pricing, database_glob=str(traycer_source))
    with connect(spend) as connection:
        rows = list(connection.execute("SELECT source, tool_key, session_id FROM usage_events"))
    opencode_sessions = {row[2] for row in rows if row[0] == "opencode_local"}
    traycer_sessions = {row[2] for row in rows if row[0] == "traycer_local"}
    assert opencode_sessions
    assert traycer_sessions
    assert opencode_sessions.isdisjoint(traycer_sessions)
    assert "rec-ses-001" not in opencode_sessions
    assert "traycer:rec-chat-11" not in traycer_sessions
    assert all(row[1] == "opencode" for row in rows if row[0] == "opencode_local")
    assert {row[1] for row in rows if row[0] == "traycer_local"} <= {"grok", "openrouter"}


def test_persist_priced_incomplete_does_not_write_zero_spend(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    row = UsageRow(
        source="traycer_local",
        tool_key="grok",
        model_key="supergrok:grok-4.6",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        session_id="traycer:chat-incomplete",
        project="fixture",
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=0,
        reasoning_tokens=None,
        cost_usd=None,
        raw_id="traycer-local:incomplete-1",
        unclassified_tokens=167_790,
        telemetry_complete=False,
    )
    first = persist_rows(
        database_path=database,
        pricing=pricing,
        source="traycer_local",
        usage_rows=[row],
    )
    second = persist_rows(
        database_path=database,
        pricing=pricing,
        source="traycer_local",
        usage_rows=[row],
    )
    assert first["status"] == "success"
    assert first["eventsWritten"] == 0
    assert first["unpricedModels"] == []
    assert first["coverageGapsWritten"] == 1
    assert second["coverageGapsWritten"] == 0
    with connect(database) as connection:
        usage = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        staged = connection.execute(
            """
            SELECT telemetry_complete, unclassified_tokens
            FROM unpriced_usage_events WHERE raw_id=?
            """,
            (row.raw_id,),
        ).fetchone()
        issue = connection.execute(
            "SELECT issue FROM coverage_gap_events WHERE raw_id=?",
            (row.raw_id,),
        ).fetchone()[0]
        gaps = connection.execute("SELECT COUNT(*) FROM pricing_gaps").fetchone()[0]
        priced_zeros = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE computed_cost_usd=0"
        ).fetchone()[0]
    assert usage == 0
    assert tuple(staged) == (0, 167_790)
    assert issue.startswith("Token components are incomplete")
    assert gaps == 0
    assert priced_zeros == 0


def test_classify_traycer_usage_preserves_incomplete_honesty() -> None:
    complete = classify_traycer_usage(
        {
            "inputTokens": 91_377,
            "cacheReadInputTokens": 88_832,
            "outputTokens": 194,
            "totalTokens": 91_571,
            "contextTokens": 91_571,
        }
    )
    assert complete.telemetry_complete is True
    assert complete.input_tokens == 91_377
    assert complete.cached_input_tokens == 88_832
    incomplete = classify_traycer_usage(
        {
            "inputTokens": 165_426,
            "outputTokens": 29,
            "totalTokens": 167_790,
            "contextTokens": 167_790,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
        }
    )
    assert incomplete.telemetry_complete is False
    assert incomplete.input_tokens == 0
    assert incomplete.unclassified_tokens == 167_790
