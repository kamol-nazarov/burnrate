"""Provider compatibility units. All storage, filesystem and HTTP are fakes."""
import copy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spend_app.adapters import opencode_schema as schema
from spend_app.adapters.opencode_local import SessionSnapshot, plan_rows
from spend_app.adapters.usage_repair import coarse_remainder
from spend_app.connection_paths import matches_parts


def test_grok_process_generation_and_unknown_model_keep_real_tokens():
    from spend_app.adapters.grok_records import reduce_records
    def event(msg, sid="s", pid=1, **ctx):
        return {"msg": msg, "sid": sid, "pid": pid, "ts": "2026-09-01T00:00:00Z", "ctx": ctx}
    rows, _, issues = reduce_records([
        event("model changed", model="grok-a"),
        event("shell.turn.inference_done", prompt_tokens=10, cached_prompt_tokens=2, completion_tokens=4, reasoning_tokens=1, loop_index=1),
        event("AuthManager::new"),
        event("shell.turn.inference_done", prompt_tokens=20, cached_prompt_tokens=3, completion_tokens=5, loop_index=2),
        event("model changed", sid="other", model="grok-b"),
        event("shell.turn.inference_done", sid="child", prompt_tokens=30, cached_prompt_tokens=0, completion_tokens=6, loop_index=3),
    ])
    assert len(rows) == 3
    assert sum(r.input_tokens for r in rows) == 60
    assert sum(r.output_tokens for r in rows) == 15
    assert [r.model_key for r in rows] == ["supergrok:grok-a", "supergrok:unknown", "supergrok:unknown"]
    assert "historical_model_unavailable" in issues


@pytest.mark.parametrize("total,expected", [(15, (7, 5, 2, 3)), (21, (12, 6, 2, 3))])
def test_zcode_evidenced_component_conventions(total, expected):
    from spend_app.adapters.zcode_schema import parse_row
    row, issue = parse_row(dict(id="r", session_id="s", provider_id="builtin:zai-coding-plan", model_id="glm",
                               completed_at=1760000000000, input_tokens=10, output_tokens=5,
                               cache_read_input_tokens=2, cache_creation_input_tokens=3,
                               reasoning_tokens=1, computed_total_tokens=total), True)
    assert issue is None
    assert (row.input_tokens, row.output_tokens, row.cached_input_tokens, row.cache_write_tokens) == expected


def test_zcode_unknown_convention_is_unsplit_not_estimated():
    from spend_app.adapters.zcode_schema import parse_row
    row, issue = parse_row(dict(id="r", session_id="s", completed_at=1760000000000,
                               input_tokens=10, output_tokens=5, computed_total_tokens=99), True)
    assert row.unclassified_tokens == 99
    assert row.input_tokens == row.output_tokens == 0
    assert not row.telemetry_complete
    assert issue == "unknown_zcode_token_convention"


def test_antigravity_cache_preserves_rpc_id_and_disjoint_tokens():
    from spend_app.adapters.antigravity_cache import reduce_records
    from spend_app.adapters.common import stable_id
    record = dict(type="usage", sessionId="s", timestamp=1760000000000, responseId="response",
                  modelId="model", input=12, output=4, cacheRead=2, cacheWrite=3, reasoning=1)
    rows, issues = reduce_records([record, copy.deepcopy(record)])
    assert not issues
    assert len(rows) == 1
    assert rows[0].raw_id == stable_id("antigravity-local", "response")
    assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].cache_write_tokens) == (14, 4, 3)
    record.pop("responseId")
    assert reduce_records([record])[0] == []
    assert reduce_records([record])[1] == ["antigravity_response_identity_or_time_unavailable"]


def test_cursor_export_included_reference_value_is_not_charge():
    from spend_app.adapters.cursor_export import parse_export
    event = dict(timestamp="1760000000000", conversationId="c", model="m",
                 tokenUsage=dict(inputTokens=10, outputTokens=4, cacheReadTokens=2, totalCents=99))
    rows, issues = parse_export(dict(accountId="a", usageEventsDisplay=[event, copy.deepcopy(event)]))
    assert len(rows) == 2
    assert rows[0].raw_id != rows[1].raw_id
    assert sum(row.input_tokens for row in rows) == 24
    assert all(row.cost_usd is None and not row.telemetry_complete for row in rows)
    assert not issues
    other = parse_export(dict(accountId="b", usageEventsDisplay=[event]))[0][0]
    assert other.raw_id != rows[0].raw_id


def test_reporting_cursor_failure_is_not_success_and_no_retry():
    from spend_app.adapters.report_pages import fetch, IncompleteReport
    client = Mock()
    client.get.return_value.json.return_value = {"data": [], "has_more": True, "next_page": "repeat"}
    with pytest.raises(IncompleteReport, match="repeated"):
        fetch(client, url="https://fixed.example/report", params={}, clock=lambda: 0)
    assert client.get.call_count == 2
    assert client.get.call_args.kwargs["timeout"] == 10


def test_reporting_deadline_prevents_request():
    from spend_app.adapters.report_pages import fetch, IncompleteReport
    client = Mock()
    clock = iter([0, 21])
    with pytest.raises(IncompleteReport, match="deadline"):
        fetch(client, url="https://fixed.example/report", params={}, clock=lambda: next(clock))
    client.get.assert_not_called()


def test_zcode_transcript_has_no_text_estimate_or_current_model_guess():
    from spend_app.adapters.zcode_transcript import reduce_records
    base = dict(role="assistant", sessionId="s", timestamp="2026-09-01T00:00:00Z", content="not retained")
    assert reduce_records([base])[0] == []
    rows, issues = reduce_records([{**base, "usage": {"totalTokens": 17}}])
    assert not issues
    assert rows[0].unclassified_tokens == 17 and rows[0].model_key == "zcode:unknown"
    assert rows[0].input_tokens == rows[0].output_tokens == 0


def test_traycer_copies_have_logical_identity_and_legacy_aliases(monkeypatch):
    import json
    from pathlib import Path
    from spend_app.adapters.traycer_local import parse_projection
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    projection = json.dumps({"settings": {"harnessId": "grok", "model": "grok-a"}, "events": [
        {"body": {"timestamp": 1760000000000, "metadata": {"usage": {"totalTokens": 12}}}}]})
    a = parse_projection(path=Path("C:/approved/a/chat.db"), chat_id="chat", projection_json=projection, observations=True)
    b = parse_projection(path=Path("C:/approved/b/chat.db"), chat_id="chat", projection_json=projection, observations=True)
    assert len(a) == len(b) == 1
    assert a[0].row.raw_id == b[0].row.raw_id
    assert a[0].aliases != b[0].aliases


class FakeEventDB:
    def __init__(self, records=()):
        from dataclasses import asdict
        self.meta = {}
        self.events = {row.raw_id: {**asdict(row), "occurred_at": row.occurred_at.isoformat()} for row in records}

    def execute(self, sql, params=()):
        import json
        if sql.startswith("SELECT value FROM app_meta"):
            value = self.meta.get(params[0])
            return SimpleNamespace(fetchone=lambda: (value,) if value is not None else None)
        if sql.startswith("SELECT 1 FROM"):
            found = any(row["source"] == params[0] and row["tool_key"] == params[1] for row in self.events.values())
            return SimpleNamespace(fetchone=lambda: (1,) if found else None)
        if sql.startswith("INSERT INTO app_meta"):
            self.meta[params[0]] = params[1]
        elif sql.startswith("SELECT * FROM"):
            if "raw_id=?" in sql:
                return SimpleNamespace(fetchone=lambda: self.events.get(params[0]) if "unpriced" not in sql else None)
            return [value for value in self.events.values() if value["source"] == params[0] and value["session_id"] == params[1]] if "unpriced" not in sql else []
        elif sql.startswith("DELETE FROM"):
            self.events.pop(params[0], None)
        else:
            raise AssertionError(sql)
        return SimpleNamespace(fetchone=lambda: None)


def test_exact_alias_upgrade_keeps_030_id_after_reconstruction():
    from spend_app.adapters.event_identity import Observation, reconcile
    row = schema.parse_message(message()).row
    row = replace(row, source="codex_local", raw_id="canonical")
    db = FakeEventDB([replace(row, raw_id="old-030-id")])
    first = reconcile(db, [Observation(row, ("old-030-id",), 2, "codex")])
    assert [r.raw_id for r in first] == ["old-030-id"]
    reconstructed = FakeEventDB()
    reconstructed.meta, reconstructed.events = copy.deepcopy(db.meta), copy.deepcopy(db.events)
    assert reconcile(reconstructed, [Observation(replace(row, input_tokens=30), (), 3, "codex")])[0].raw_id == "old-030-id"
    issues = []
    assert reconcile(reconstructed, [Observation(row, (), 1, "codex")], issues) == []
    assert issues == ["older_usage_revision_ignored"]


def test_grok_native_unsplit_remainder_reconciles_unified_import_order():
    from spend_app.adapters.grok_native import signal_row, reconcile
    from spend_app.adapters.common import stable_id
    coarse = signal_row({"totalTokens": 100}, "s", datetime(2026, 9, 2, tzinfo=UTC))
    fine = replace(schema.parse_message(message()).row, source="grok_local", tool_key="grok", session_id="s",
                   occurred_at=datetime(2026, 9, 1, tzinfo=UTC), input_tokens=50, output_tokens=10,
                   cached_input_tokens=0, cache_write_tokens=0, raw_id=stable_id("grok-local", "s", "turn"))
    db = FakeEventDB()
    first = reconcile(db, [coarse], [])
    assert first[0].unclassified_tokens == 100
    db.events = FakeEventDB(first).events
    second = reconcile(db, [fine], [])
    assert sum(r.unclassified_tokens for r in second) == 40
    assert sum(r.input_tokens + r.output_tokens + r.unclassified_tokens for r in second) == 100
    reverse_db = FakeEventDB([fine])
    reverse = reconcile(reverse_db, [coarse], [])
    assert sum(r.input_tokens + r.output_tokens + r.unclassified_tokens for r in reverse) == 100


def test_statusline_allowlist_expiry_scope_and_missing_windows():
    from spend_app.claude_statusline import reduce_payload, collect_snapshot
    snapshot = reduce_payload({"prompt": "secret", "context_window": {"used_percentage": 99}, "rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": 2000}, "seven_day": {"used_percentage": 101, "resets_at": 3000}}}, scope="profile", now=1000)
    assert set(snapshot) == {"version", "scope", "observed", "windows"}
    assert list(snapshot["windows"]) == ["five_hour"]
    assert collect_snapshot(snapshot, scope="profile", now=1100)["windows"][0]["usedPct"] == 42
    assert collect_snapshot(snapshot, scope="other", now=1100)["status"] == "unavailable"
    assert collect_snapshot(snapshot, scope="profile", now=2100)["status"] == "unavailable"
    assert reduce_payload({"context_window": {"used_percentage": 99}}, scope="p", now=1000)["windows"] == {}


def test_statusline_policy_is_explicit_and_not_native_oauth():
    from spend_app.integration_policy import authorize
    assert not authorize("claude_statusline_snapshot", environ={})[0]
    assert authorize("claude_statusline_snapshot", environ={"BURNRATE_ENABLE_CLAUDE_STATUSLINE": "1"})[0]
    assert not authorize("claude_oauth_usage", environ={"BURNRATE_ENABLE_CLAUDE_STATUSLINE": "1"})[0]


def test_retry_after_is_not_shortened_to_backoff_cap():
    from spend_app.quotas import QuotaLaneScheduler, QuotaSample
    clock = [0]
    scheduler = QuotaLaneScheduler(clock=lambda: clock[0])
    scheduler.record("p", [QuotaSample("p", "w", "label", "pct", "fake", throttle_seconds=7200)], active=True)
    clock[0] = 3601
    assert not scheduler.due("p")
    clock[0] = 7200
    assert scheduler.due("p")


def test_ingest_rows_aliases_and_progress_share_transaction_on_failure(monkeypatch):
    from contextlib import contextmanager
    from spend_app.adapters import common
    db = FakeEventDB()
    @contextmanager
    def transaction(_):
        before = copy.deepcopy((db.events, db.meta))
        try:
            yield db
        except Exception:
            db.events, db.meta = before
            raise
    monkeypatch.setattr(common, "connect", transaction)
    for name in ("initialize", "sync_model_prices", "resolve_pricing_gaps", "record_coverage_gap"):
        monkeypatch.setattr(common, name, Mock())
    monkeypatch.setattr(common, "promote_priced_unpriced_events", lambda *_: 0)
    run = SimpleNamespace(events_written=0, finish=Mock())
    monkeypatch.setattr(common.IngestRun, "start", lambda *_: run)
    def upsert(_db, event):
        db.events[event.raw_id] = event
        return True
    monkeypatch.setattr(common, "upsert_unpriced_event", upsert)
    row = replace(schema.parse_message(message()).row, telemetry_complete=False)
    def prepare(_db, rows):
        db.meta["alias"] = "owned"
        return rows
    def finalize(_db):
        db.meta["cursor"] = "advanced"
        assert len(db.events) == 1
        raise OSError("fake commit boundary failure")
    with pytest.raises(OSError):
        common.persist_rows(database_path="fake", pricing=Mock(), source=row.source,
                            usage_rows=[row], prepare=prepare, finalize=finalize)
    assert db.events == {} and db.meta == {}
    assert run.finish.call_args.kwargs["status"] == "failed"


def test_reporting_window_revisits_seven_days_with_fixed_hour_boundaries():
    from spend_app.adapters.report_pages import reporting_window
    start, end = reporting_window(datetime(2026, 9, 8, 15, 42, tzinfo=UTC))
    assert start == datetime(2026, 9, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 8, 15, tzinfo=UTC)


def test_new_claude_partial_fragment_does_not_erase_stored_complete_components():
    from spend_app.adapters.event_identity import Observation, reconcile
    row = replace(schema.parse_message(message()).row, source="claude_local", raw_id="claude-message:one")
    db = FakeEventDB([row])
    partial = replace(row, input_tokens=0, cached_input_tokens=0, cache_write_tokens=0, output_tokens=3, telemetry_complete=False)
    result = reconcile(db, [Observation(partial, (), 4, "claude_message", components=(("output_tokens", 3),))])
    assert result[0].input_tokens == 12 and result[0].cache_write_tokens == 3
    assert result[0].output_tokens == 3 and result[0].telemetry_complete


def test_cursor_real_id_and_scope_reconcile_exports_and_preserve_old_id():
    from spend_app.adapters.cursor_admin import parse_events
    from spend_app.adapters.cursor_export import parse_export
    from spend_app.adapters.event_identity import reconcile
    event = dict(id="request", timestamp="1760000000000", userEmail="one@example.test", model="m", conversationId="c", chargedCents=20,
                 tokenUsage=dict(inputTokens=10, outputTokens=4, cacheReadTokens=2, cacheWriteTokens=0))
    admin = parse_events({"usageEvents": [event]}, observations=True, revision=2000000000)[0]
    export = parse_export({"usageEventsDisplay": [event]}, observations=True)[0][0]
    assert admin.row.raw_id == export.row.raw_id
    other = parse_events({"usageEvents": [{**event, "userEmail": "two@example.test"}]})[0]
    assert other.raw_id != admin.row.raw_id
    db = FakeEventDB([replace(admin.row, raw_id="cursor-admin:request")])
    saved = reconcile(db, [admin])
    assert saved[0].raw_id == "cursor-admin:request"
    db.events = FakeEventDB(saved).events
    copied = reconcile(db, [export])
    assert len(copied) == 1 and copied[0].raw_id == saved[0].raw_id
    assert copied[0].source == "cursor_admin" and copied[0].cost_usd == 0.2


def test_cursor_native_csv_columns_and_legacy_identity_mapping(monkeypatch):
    import io
    from pathlib import Path
    from spend_app.adapters.cursor_csv import parse_csv
    from spend_app.adapters.common import stable_id
    text = "Date,Model,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Cost,Cost to you\n2026-09-01T00:00:00Z,m,3,10,2,4,99,0.20\n"
    monkeypatch.setattr("spend_app.connection_paths.confined", lambda path: path)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.StringIO(text))
    rows = parse_csv(Path("C:/fake/usage.csv"), observations=True)
    row = rows[0].row
    assert (row.input_tokens, row.cache_write_tokens, row.output_tokens, row.cost_usd) == (12, 3, 4, 0.2)
    assert row.telemetry_complete
    expected_old = stable_id("cursor-csv", row.occurred_at.isoformat(), "", "cursor:m", "", 0, 2, 0, 4, 99.0)
    assert rows[0].aliases == (expected_old,)


def test_granular_before_cumulative_does_not_layer_another_baseline():
    import json
    from spend_app.adapters.common import stable_id
    from spend_app.adapters.opencode_granular import reconcile_cumulative
    fine = schema.parse_message(message(input=40, output=0, cache=0, write=0))
    db = FakeEventDB([fine.row])
    db.meta["opencode.granular.v1:" + stable_id("s1", "provider-a")] = json.dumps({"revisions": {fine.row.raw_id: [1, 1, 0]}, "allocations": {}})
    coarse = replace(fine.row, input_tokens=100, raw_id="coarse", occurred_at=datetime(2026, 9, 2, tzinfo=UTC))
    residual = reconcile_cumulative(db, [coarse], [SimpleNamespace(session_id="s1", provider_id="provider-a")])
    assert len(residual) == 1 and residual[0].input_tokens == 60


def test_ambiguous_counter_fragment_cannot_inflate_existing_history():
    from spend_app.adapters.event_identity import Observation, reconcile
    base = replace(schema.parse_message(message()).row, source="codex_local", input_tokens=100, output_tokens=0, cache_write_tokens=0)
    db = FakeEventDB()
    one = Observation(replace(base, raw_id="counter-100"), (), 1, "cumulative_delta", components=(("counter_scope", "s"), ("counter_start", 0), ("counter_end", 100)))
    two = Observation(replace(base, input_tokens=50, raw_id="counter-150"), (), 2, "cumulative_delta", components=(("counter_scope", "s"), ("counter_start", 100), ("counter_end", 150)))
    saved = reconcile(db, [one, two])
    assert sum(row.input_tokens for row in saved) == 150
    db.events = FakeEventDB(saved).events
    fragment = replace(two, row=replace(two.row, input_tokens=150), components=(("counter_scope", "s"), ("counter_start", 0), ("counter_end", 150)))
    issues = []
    assert reconcile(db, [fragment], issues) == []
    assert issues == ["codex_overlapping_counter_fragment_requires_complete_history"]


def test_snapshot_atomic_replacement_failure_cleans_only_owned_temp(monkeypatch):
    import io
    from pathlib import Path
    from spend_app import claude_statusline as bridge
    class Stream(io.StringIO):
        name = "C:/fake/snapshots/.claude-statusline-owned.tmp"
        def fileno(self):
            return 123
        def __exit__(self, *_):
            return False
    stream = Stream()
    monkeypatch.setattr(Path, "is_symlink", lambda _: False)
    monkeypatch.setattr(Path, "is_junction", lambda _: False)
    monkeypatch.setattr(Path, "mkdir", Mock())
    unlink = Mock()
    monkeypatch.setattr(Path, "unlink", lambda path, **kwargs: unlink(str(path)))
    monkeypatch.setattr(bridge.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: stream)
    monkeypatch.setattr(bridge.os, "fsync", Mock())
    replace_file = Mock(side_effect=OSError("fake atomic replacement failure"))
    monkeypatch.setattr(bridge.os, "replace", replace_file)
    target = Path("C:/fake/snapshots/claude-statusline-existing.json")
    with pytest.raises(OSError):
        bridge.atomic_write(target, {"version": 1, "windows": {}})
    assert replace_file.call_args.args[1] == target
    assert unlink.call_count == 1 and "owned.tmp" in unlink.call_args.args[0]
    assert "existing.json" not in unlink.call_args.args[0]


def test_malformed_large_timestamp_isolated_from_usage_neighbors():
    from spend_app.adapters.local_common import parse_millis
    from spend_app.adapters.cursor_admin import parse_events
    assert parse_millis(10**40) is None
    base = dict(model="m", tokenUsage=dict(inputTokens=1, outputTokens=2, cacheReadTokens=0, cacheWriteTokens=0))
    rows = parse_events({"usageEvents": [{**base, "timestamp": 10**40}, {**base, "timestamp": "1760000000000"}]})
    assert len(rows) == 1 and rows[0].input_tokens == 1 and rows[0].output_tokens == 2


def test_copied_unchanged_grok_signal_does_not_move_history_forward():
    from spend_app.adapters.grok_native import signal_row, reconcile
    from spend_app.adapters.common import stable_id
    stamp = datetime(2026, 9, 1, tzinfo=UTC)
    coarse = signal_row({"totalTokens": 100}, "s", stamp)
    db = FakeEventDB()
    saved = reconcile(db, [coarse], [])
    db.events = FakeEventDB(saved).events
    later = replace(schema.parse_message(message()).row, source="grok_local", tool_key="grok", session_id="s",
                    raw_id=stable_id("grok-local", "new"), input_tokens=20, output_tokens=0, cached_input_tokens=0, cache_write_tokens=0,
                    occurred_at=datetime(2026, 9, 2, tzinfo=UTC))
    copied = replace(coarse, occurred_at=datetime(2026, 9, 3, tzinfo=UTC))
    result = reconcile(db, [copied, later], [])
    assert sum(row.input_tokens + row.output_tokens + row.unclassified_tokens for row in result) == 120
    assert next(row for row in result if row.raw_id == coarse.raw_id).occurred_at == stamp


def test_cursor_pagination_contradiction_is_visible():
    from spend_app.adapters.cursor_admin import _has_next_page
    with pytest.raises(ValueError, match="disagree"):
        _has_next_page({"pagination": {"hasNextPage": True, "numPages": 1}}, 1)


def test_native_grok_total_does_not_layer_over_unmapped_traycer_history():
    from spend_app.adapters.grok_native import signal_row, reconcile
    old = replace(schema.parse_message(message()).row, source="traycer_local", tool_key="grok", raw_id="traycer-old")
    db = FakeEventDB([old])
    signal = signal_row({"totalTokens": 100}, "new-session", datetime(2026, 9, 2, tzinfo=UTC))
    issues = []
    assert reconcile(db, [signal], issues) == []
    assert "traycer-old" in db.events
    assert issues == ["grok_native_total_traycer_history_scope_unproven"]


def test_complete_codex_chain_refines_stored_coarse_interval_without_inflation():
    from spend_app.adapters.event_identity import Observation, reconcile
    base = replace(schema.parse_message(message()).row, source="codex_local", raw_id="counter-150", input_tokens=150,
                   output_tokens=0, cache_write_tokens=0, reasoning_tokens=None)
    def observation(row, start, end, revision):
        return Observation(row, (), revision, "cumulative_delta", components=(("counter_scope", "s"), ("counter_start", start), ("counter_end", end)))
    db = FakeEventDB()
    coarse = observation(base, 0, 150, 10)
    original = reconcile(db, [coarse])
    db.events = FakeEventDB(original).events
    first = observation(replace(base, raw_id="counter-100", input_tokens=100), 0, 100, 1)
    last = observation(replace(base, input_tokens=50), 100, 150, 2)
    refined = reconcile(db, [first, last])
    assert sum(row.input_tokens for row in refined) == 150
    assert len(refined) == 2
    assert next(row for row in refined if row.raw_id == "counter-150").input_tokens == 50
    db.events = FakeEventDB(refined).events
    issues = []
    assert reconcile(db, [coarse], issues) == []
    assert "codex_overlapping_counter_fragment_requires_complete_history" in issues


def message(id="m1", *, v2=False, input=10, output=5, cache=2, write=3, when=1760000000000, **extra):
    row = {"id": id, "sessionID": "s1", "role": "assistant", "modelID": "model-a", "providerID": "provider-a",
           "tokens": {"input": input, "output": output, "reasoning": 1, "cache": {"read": cache, "write": write}},
           "time": {"created": when, "completed": when + 1000}}
    if v2:
        row.pop("role")
        row["model"] = {"id": row.pop("modelID"), "providerID": row.pop("providerID")}
    row.update(extra)
    return row


@pytest.mark.parametrize("columns,expected", [
    ({"message": {"id", "session_id", "data"}}, ("v1",)),
    ({"session_message": {"id", "session_id", "type", "data"}}, ("v2",)),
    ({"session_message": {"id", "session_id", "role", "data"}}, ("v2",)),
    ({"session_message": {"id", "session_id", "type", "data"}, "message": {"id", "session_id", "data"}}, ("v2", "v1")),
    ({"session": schema.LEGACY_COLUMNS}, ("cumulative",)),
])
def test_opencode_schema_selection(columns, expected):
    assert schema.recognize(columns) == expected


def test_opencode_unsupported_is_not_empty_success():
    with pytest.raises(schema.UnsupportedSchema, match="incompatible"):
        schema.recognize({"session": {"id", "title"}})


@pytest.mark.parametrize("format", ["v1", "v2", "json"])
def test_opencode_message_tokens_and_models(format):
    parsed = schema.parse_message(message(v2=format == "v2"), role="assistant", format=format)
    assert parsed.row.input_tokens == 12  # 10 fresh + 2 cached
    assert parsed.row.cache_write_tokens == 3
    assert parsed.row.output_tokens == 5  # reasoning 1 is already part of output
    assert parsed.row.reasoning_tokens == 1
    assert parsed.row.model_key == "opencode:model-a"
    assert parsed.row.telemetry_complete is True
    assert parsed.row.occurred_at == datetime.fromtimestamp(1760000001, UTC)


def test_opencode_copy_identity_distinct_equal_requests_and_decrease():
    first = schema.parse_message(message(), format="v1")
    copied = schema.parse_message(message(v2=True), role="assistant", format="v2")
    other = schema.parse_message(message("m2"), format="json")
    revised = schema.parse_message(message(input=4), format="v1", updated_at=1760000002000)
    values, _ = schema.merge_messages([first, copied, other, revised])
    assert len(values) == 2
    assert sum(value.row.input_tokens for value in values) == 18  # (4+2) + (10+2)
    assert first.row.raw_id == copied.row.raw_id == revised.row.raw_id
    assert other.row.raw_id != first.row.raw_id


def test_opencode_total_only_and_optional_metadata():
    partial = message(tokens={"total": 17}, path=["malformed", "metadata"])
    parsed = schema.parse_message(partial)
    assert parsed.row.input_tokens == 0
    assert parsed.row.unclassified_tokens == 17
    assert not parsed.row.telemetry_complete
    assert parsed.row.project is None
    assert schema.parse_message(message(role="user")) is None
    assert schema.parse_message(message(providerID="traycer-openrouter")) is None


def test_coarse_to_granular_preserves_remainder_and_later_corrections():
    fine = schema.parse_message(message(input=40, cache=0, write=0, output=0))
    coarse = replace(fine.row, raw_id="old-0.3-baseline", input_tokens=100, occurred_at=fine.row.occurred_at)
    residual, allocation = coarse_remainder(coarse, [fine], {})
    assert residual.input_tokens == 60
    assert residual.raw_id == coarse.raw_id
    repeat, allocation = coarse_remainder(residual, [fine], allocation)
    assert repeat.input_tokens == 60
    corrected = replace(fine, row=replace(fine.row, input_tokens=30))
    repeat, allocation = coarse_remainder(repeat, [corrected], allocation)
    assert repeat.input_tokens + corrected.row.input_tokens == 90
    second = schema.parse_message(message("m2", input=60, cache=0, write=0, output=0))
    exhausted, _ = coarse_remainder(repeat, [corrected, second], allocation)
    assert exhausted.input_tokens == 0
    assert corrected.row.input_tokens + second.row.input_tokens == 90


def test_one_message_can_cover_baseline_and_delta_without_double_allocation():
    fine = schema.parse_message(message(input=150, cache=0, write=0, output=0))
    baseline = replace(fine.row, raw_id="baseline", input_tokens=100)
    delta = replace(fine.row, raw_id="delta", input_tokens=50)
    left, allocated = coarse_remainder(baseline, [fine], {})
    right, allocated = coarse_remainder(delta, [fine], allocated)
    assert left.input_tokens == right.input_tokens == 0
    assert allocated[fine.row.raw_id]["used"]["input_tokens"] == 150


def test_cumulative_fallback_repeat_and_ambiguous_decrease_do_not_remint():
    first = SessionSnapshot("s", "p", "m", None, 100, 0, 0, 10, None, None, datetime(2026, 9, 1, tzinfo=UTC))
    rows, state, _ = plan_rows([first], {})
    assert sum(row.input_tokens for row in rows) == 100
    assert plan_rows([first], state)[0] == []
    decreased = replace(first, input_tokens=40, observed_at=datetime(2026, 9, 2, tzinfo=UTC))
    new, progress, issues = plan_rows([decreased], state)
    assert new == [] and progress == {}
    assert issues == [("", "opencode_cumulative_decrease_requires_granular_evidence")]


def test_known_subroots_do_not_match_sibling_profiles():
    assert matches_parts(["storage", "message", "s", "m.json"], ["storage", "message", "**", "*.json"])
    assert not matches_parts(["other-profile", "opencode.db"], ["opencode*.db"])
    assert matches_parts(["opencode-next.db"], ["opencode*.db"])
    assert not matches_parts(["archive", "m.jsonl"], ["sessions", "**", "*.jsonl"])


def codex_meta(**extra):
    return {"type": "session_meta", "timestamp": "2026-09-01T00:00:00Z", "payload": {"id": "s1", "originator": "codex_cli_rs", **extra}}


def codex_usage(second, total=None, last=None):
    return {"type": "event_msg", "timestamp": f"2026-09-01T00:00:{second:02}Z", "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last}}}


def usage(inp, out=2, cache=0):
    return {"input_tokens": inp, "cached_input_tokens": cache, "output_tokens": out, "total_tokens": inp + out}


def reduce_codex(records):
    from spend_app.adapters.codex_records import reduce_records
    from spend_app.source_health import SourceHealth
    health = SourceHealth()
    session, events = reduce_records(records, health, "fallback")
    return session, events, health


def test_codex_cli_cumulative_last_mix_and_repeated_snapshots():
    records = [codex_meta(), {"type": "turn_context", "payload": {"model": "gpt-test"}},
               codex_usage(1, usage(10), usage(10)), codex_usage(2, usage(10), usage(10)),
               codex_usage(3, usage(15, 3), usage(5, 1))]
    session, events, health = reduce_codex(records)
    assert session["supported_origin"]
    assert len(events) == 2
    assert sum(event.row.input_tokens for event in events) == 15
    assert sum(event.row.output_tokens for event in events) == 3
    assert len(events[0].aliases) == 2  # both old 0.3.0 repeated rows reconcile
    assert not health.partial


def test_codex_last_only_then_cumulative_does_not_double_first_request():
    _, events, _ = reduce_codex([codex_meta(originator="Codex Desktop"), {"type": "turn_context", "payload": {"model": "gpt-test"}}, codex_usage(1, last=usage(10)), codex_usage(2, total=usage(10)), codex_usage(3, total=usage(15, 3))])
    assert len(events) == 2
    assert sum(event.row.input_tokens for event in events) == 15


def test_codex_unknown_originators_and_traycer_remain_excluded():
    for origin in ("traycer-agents", "unrecognized-client"):
        session, events, _ = reduce_codex([codex_meta(originator=origin, source="cli"), codex_usage(1, last=usage(10))])
        assert not session["supported_origin"]
        assert events == []


def test_codex_total_only_does_not_invent_input_components():
    _, events, _ = reduce_codex([codex_meta(), codex_usage(1, total={"total_tokens": 17})])
    assert len(events) == 1
    assert events[0].row.input_tokens == 0
    assert events[0].row.unclassified_tokens == 17
    assert not events[0].row.telemetry_complete
    assert events[0].row.model_key == "unknown"


def test_codex_distinct_equal_requests_survive():
    _, events, _ = reduce_codex([codex_meta(), {"type": "turn_context", "payload": {"model": "gpt-test"}}, codex_usage(1, last=usage(10)), codex_usage(1, last=usage(10))])
    assert len(events) == 2
    assert events[0].row.raw_id != events[1].row.raw_id


def test_codex_reset_and_out_of_order_snapshot():
    _, events, health = reduce_codex([codex_meta(), {"type": "turn_context", "payload": {"model": "gpt-test"}}, codex_usage(1, usage(100, 10), usage(100, 10)), codex_usage(3, usage(110, 11), usage(10, 1)), codex_usage(2, usage(105, 10), usage(5, 0)), codex_usage(4, usage(5, 1), usage(5, 1))])
    assert sum(event.row.input_tokens for event in events) == 115
    assert health.partial


def test_codex_inherited_fork_then_same_millisecond_real_child_work():
    child = "019e5c03-1e99-7000-8000-0000000000ff"
    turn = "019e5c03-1e99-7000-8000-000000000001"
    _, events, _ = reduce_codex([codex_meta(id=child, forked_from_id="parent"), {"type": "turn_context", "payload": {"model": "old-model", "turn_id": "old"}}, codex_usage(1, usage(300, 30), usage(300, 30)), {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}, {"type": "turn_context", "payload": {"model": "child-model", "turn_id": turn}}, codex_usage(3, usage(320, 32), usage(20, 2))])
    assert len(events) == 1
    assert events[0].row.session_id == child
    assert events[0].row.input_tokens == 20
    assert events[0].row.output_tokens == 2
    assert events[0].row.model_key == "child-model"


def test_connection_decode_rejects_corruption_instead_of_defaulting():
    import json
    from spend_app.connections import decode, empty_state
    for raw in ("", "[]", json.dumps({**empty_state(), "bindings": []}), json.dumps({**empty_state(), "bindings": {"codex_local": {"revision": 1}}})):
        with pytest.raises(ValueError):
            decode(raw)


def claude_message(second=1, *, message_id="m1", request_id="r1", usage=None, **extra):
    data = {"type": "assistant", "sessionId": "s1", "requestId": request_id, "timestamp": f"2026-09-01T00:00:{second:02}Z", "message": {"id": message_id, "model": "claude-model", "usage": usage or {"input_tokens": 10, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3, "output_tokens": 5}}}
    data.update(extra)
    return data


def reduce_claude(records):
    from spend_app.adapters.claude_records import reduce_records
    from spend_app.source_health import SourceHealth
    health = SourceHealth()
    _, events = reduce_records(records, health, "fallback")
    return events, health


def test_claude_partial_to_complete_and_authoritative_decrease():
    first = claude_message(1, usage={"input_tokens": 10, "output_tokens": 0})
    completed = claude_message(2)
    corrected = claude_message(3, usage={"output_tokens": 4})
    events, _ = reduce_claude([first, completed, corrected])
    assert len(events) == 1
    assert events[0].row.input_tokens == 12
    assert events[0].row.output_tokens == 4
    assert events[0].row.cache_write_tokens == 3
    assert events[0].row.telemetry_complete
    assert events[0].row.occurred_at == datetime(2026, 9, 1, 0, 0, 1, tzinfo=UTC)
    assert events[0].aliases == ("claude-local:s1:m1",)


def test_claude_distinct_requests_and_subagent_copies():
    events, _ = reduce_claude([claude_message(request_id="a"), claude_message(request_id="b"), claude_message(request_id="a", sessionId="subagent-copy")])
    assert len(events) == 2
    assert sum(event.row.input_tokens for event in events) == 24
    assert set(events[0].aliases) == {"claude-local:s1:m1", "claude-local:subagent-copy:m1"}


def test_claude_cache_duration_and_malformed_optional_fields():
    record = claude_message(usage={"input_tokens": 10, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 100, "cache_creation": {"ephemeral_1h_input_tokens": 20}, "output_tokens": 5, "output_tokens_details": ["malformed"]}, cwd=["invalid"])
    events, _ = reduce_claude([record])
    assert events[0].row.cache_write_tokens == 100
    assert events[0].row.cache_write_1h_tokens == 20
    assert events[0].row.reasoning_tokens is None
    assert events[0].row.project is None
    assert events[0].row.input_tokens == 12


def test_claude_older_copy_does_not_overwrite_new_usage():
    events, health = reduce_claude([claude_message(3, usage={"input_tokens": 10, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3, "output_tokens": 7}), claude_message(1)])
    assert events[0].row.output_tokens == 7
    assert health.partial


def test_transcript_permission_patterns_preserve_narrow_roots(monkeypatch):
    from pathlib import Path
    from spend_app.connection_paths import transcript_patterns
    monkeypatch.setattr(Path, "is_symlink", lambda path: False)
    monkeypatch.setattr(Path, "is_junction", lambda path: False)
    monkeypatch.setattr(Path, "is_dir", lambda path: False)
    assert transcript_patterns("codex_local", "C:/user/.codex") == ["sessions/**/*.jsonl", "archived_sessions/**/*.jsonl"]
    assert transcript_patterns("codex_local", "C:/user/.codex/sessions") == ["**/*.jsonl"]
    assert transcript_patterns("claude_local", "C:/user/.claude") == ["projects/**/*.jsonl", "transcripts/**/*.jsonl"]
    assert transcript_patterns("claude_local", "C:/user/.claude/projects") == ["**/*.jsonl"]
