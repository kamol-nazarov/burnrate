"""R1-R5 regressions: production reducers and an in-memory persistence boundary."""
import copy
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from test_provider_compat_unit import FakeEventDB, codex_meta, codex_usage, claude_message, message
from spend_app.adapters.event_identity import reconcile
from spend_app.adapters import codex_records, claude_records, opencode_schema
from spend_app.source_health import SourceHealth


def save(db, rows):
    for row in rows:
        db.events[row.raw_id] = {**asdict(row), "occurred_at": row.occurred_at.isoformat()}


def total(db):
    return sum(r['input_tokens'] + r['output_tokens'] + r['cache_write_tokens'] + r.get('unclassified_tokens', 0) for r in db.events.values())


def codex(cumulative=False, request='request-1'):
    counts = dict(input_tokens=100, output_tokens=50, cached_input_tokens=0, total_tokens=150)
    event = codex_usage(1, last=counts, total=counts if cumulative else None)
    event['payload']['info']['request_id'] = request
    return codex_records.reduce_records([codex_meta(), {'type':'turn_context','payload':{'model':'gpt-test'}}, event], SourceHealth(), 's')[1]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_r1_request_counter_transition(reverse, batch):
    variants = [codex(), codex(True)]
    if reverse:
        variants.reverse()
    db = FakeEventDB()
    for group in [sum(variants, [])] if batch else variants:
        save(db, reconcile(db, group))
        db = copy.deepcopy(db)  # reconstruct persistent state, no runtime cache
    save(db, reconcile(db, sum(variants, [])))
    assert len(db.events) == 1
    assert total(db) == 150


def claude(usage, second=1):
    return claude_records.reduce_records([claude_message(second, usage=usage)], SourceHealth(), 's')[1]


COMPLETE = dict(input_tokens=100, cache_read_input_tokens=20, cache_creation_input_tokens=50, output_tokens=30,
                cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 50})


@pytest.mark.parametrize('reverse', [False, True])
def test_r3_separate_partial_files_share_known_components(reverse):
    observations = claude(COMPLETE) + claude({'output_tokens': 40}, 2)
    db = FakeEventDB()
    save(db, reconcile(db, list(reversed(observations)) if reverse else observations))
    assert total(db) == 210
    assert next(iter(db.events.values()))['telemetry_complete']


def test_r5_omitted_one_hour_duration_survives_restart():
    db = FakeEventDB()
    save(db, reconcile(db, claude(COMPLETE)))
    db = copy.deepcopy(db)
    save(db, reconcile(db, claude({'output_tokens': 40}, 2)))
    row = next(iter(db.events.values()))
    assert row['cache_write_tokens'] == row['cache_write_1h_tokens'] == 50
    assert total(db) == 210


class OpenCodeDB(FakeEventDB):
    def execute(self, sql, params=()):
        if sql.startswith('SELECT provider_id'):
            return [('provider-a',)]
        if sql.startswith('SELECT e.*'):
            return [r for k,r in self.events.items() if k.startswith('coarse')] if 'unpriced' not in sql else []
        if sql.startswith('INSERT OR REPLACE INTO coverage_gap_events'):
            return SimpleNamespace(fetchone=lambda:None)
        return super().execute(sql, params)


def test_r2_reverse_import_preserves_actual_message_start():
    from spend_app.adapters.opencode_granular import reconcile_messages, reconcile_cumulative
    start = datetime(2026, 9, 1, 10, tzinfo=UTC)
    payload = message(input=100, output=50, cache=0, write=0, time={'created': int(start.timestamp()*1000), 'completed': int(start.timestamp()*1000)+600000})
    fine = opencode_schema.parse_message(payload)
    db = OpenCodeDB()
    save(db, reconcile_messages(db, [fine], []))
    db = copy.deepcopy(db)
    coarse = replace(fine.row, raw_id='coarse', occurred_at=start.replace(minute=5), input_tokens=100, output_tokens=0)
    save(db, reconcile_cumulative(db, [coarse], [SimpleNamespace(session_id='s1', provider_id='provider-a')]))
    assert total(db) == 150


def test_r2_forward_direction_matches_reverse_and_replay():
    from spend_app.adapters.opencode_granular import reconcile_messages
    start = datetime(2026,9,1,10,tzinfo=UTC)
    fine = opencode_schema.parse_message(message(input=100,output=50,cache=0,write=0,time={'created':int(start.timestamp()*1000),'completed':int(start.timestamp()*1000)+600000}))
    coarse = replace(fine.row,raw_id='coarse',occurred_at=start.replace(minute=5),input_tokens=100,output_tokens=0)
    db = OpenCodeDB([coarse])
    save(db,reconcile_messages(db,[fine],[]))
    assert total(db)==150 and 'coarse' not in db.events
    db = copy.deepcopy(db)
    save(db,reconcile_messages(db,[fine],[]))
    assert total(db)==150


@pytest.mark.parametrize('alias', ['totalTokens', 'total_tokens'])
def test_r4_transcript_conserves_trustworthy_total(alias):
    from spend_app.adapters.zcode_transcript import reduce_records
    rows, issues = reduce_records([dict(role='assistant', sessionId='s', timestamp='2026-09-01T10:00:00Z',
        usage=dict(input=10, output=5, cache_read=2, cache_write=3, reasoning=1, **{alias:15}))])
    assert len(rows) == 1
    row = rows[0]
    assert row.input_tokens + row.output_tokens + row.cache_write_tokens + row.unclassified_tokens == 15
    assert not row.telemetry_complete and row.unclassified_tokens == 15
    assert issues == ['zcode_transcript_component_attribution_unproven']


def test_r1_distinct_request_ids_and_subsequent_counter_increment():
    db = FakeEventDB()
    save(db, reconcile(db, codex() + codex(request='request-2')))
    assert len(db.events) == 2 and total(db) == 300
    counts = dict(input_tokens=100, output_tokens=50, cached_input_tokens=0, total_tokens=150)
    next_counts = dict(input_tokens=110, output_tokens=55, cached_input_tokens=0, total_tokens=165)
    event1 = codex_usage(1, total=counts, last=counts)
    event1['payload']['info']['request_id'] = 'request-1'
    event2 = codex_usage(2, total=next_counts, last=dict(input_tokens=10, output_tokens=5, cached_input_tokens=0, total_tokens=15))
    event2['payload']['info']['request_id'] = 'request-2'
    observations = codex_records.reduce_records([codex_meta(), event1, event2], SourceHealth(), 's')[1]
    db = FakeEventDB()
    save(db, reconcile(db, observations))
    assert len(db.events) == 2 and total(db) == 165
    save(db, reconcile(db, codex()))
    assert len(db.events) == 2 and total(db) == 165


def test_r1_prior_counter_ledger_and_030_alias_are_retained():
    obs = codex(True)[0]
    counter_id = dict(obs.components)['request_equivalent_counter']
    old_alias = next(alias for alias in obs.aliases if alias.startswith('codex-local:'))
    old = replace(obs, row=replace(obs.row, raw_id=counter_id), components=tuple((k,v) for k,v in obs.components if k!='request_equivalent_counter'))
    db = FakeEventDB([replace(old.row,raw_id=old_alias)])
    save(db,reconcile(db,[old]))
    assert list(db.events)==[old_alias]
    save(db,reconcile(db,[obs]))
    assert list(db.events)==[old_alias] and total(db)==150
    db=copy.deepcopy(db)
    save(db,reconcile(db,[old]))  # a replay without the request field uses proven ledger equivalence
    save(db,reconcile(db,codex()))
    assert list(db.events)==[old_alias] and total(db)==150


def test_r1_repeated_explicit_request_does_not_raise_counter_baseline():
    counts=dict(input_tokens=100,output_tokens=50,cached_input_tokens=0,total_tokens=150)
    first=codex_usage(1,last=counts)
    first['payload']['info']['request_id']='request-1'
    cumulative=codex_usage(2,total=counts)
    later=codex_usage(3,total=dict(input_tokens=110,output_tokens=55,cached_input_tokens=0,total_tokens=165))
    observations=codex_records.reduce_records([codex_meta(),first,copy.deepcopy(first),cumulative,later],SourceHealth(),'s')[1]
    db=FakeEventDB()
    save(db,reconcile(db,observations))
    assert total(db)==165 and len(db.events)==2


@pytest.mark.parametrize('reverse', [False, True])
def test_r3_same_file_chronology_and_replay(reverse):
    records = [claude_message(1, usage=COMPLETE), claude_message(2, usage={'output_tokens':40})]
    if reverse:
        records.reverse()
    observations = claude_records.reduce_records(records, SourceHealth(), 's')[1]
    db = FakeEventDB()
    save(db, reconcile(db, observations))
    assert total(db) == 210
    db = copy.deepcopy(db)
    save(db, reconcile(db, claude(COMPLETE)))
    assert total(db) == 210
    save(db, reconcile(db, claude({'output_tokens':0}, 3)))
    assert total(db) == 170


def test_r3_equal_field_authority_conflict_is_explicit_until_new_revision():
    db = FakeEventDB()
    issues = []
    save(db, reconcile(db, claude(COMPLETE) + claude({**COMPLETE, 'output_tokens':40}), issues))
    assert not next(iter(db.events.values()))['telemetry_complete']
    assert 'claude_equal_authority_field_conflict' in issues
    db = copy.deepcopy(db)
    save(db, reconcile(db, claude(COMPLETE)))
    assert not next(iter(db.events.values()))['telemetry_complete']
    save(db, reconcile(db, claude({'output_tokens':20}, 2)))
    assert next(iter(db.events.values()))['telemetry_complete']
    assert total(db) == 190


def test_r3_carried_fields_do_not_acquire_partial_fragment_revision():
    file_a = claude_records.reduce_records([claude_message(1, usage=COMPLETE), claude_message(3, usage={'output_tokens':40})], SourceHealth(), 's')[1]
    file_b = claude({'input_tokens':80}, 2)
    db = FakeEventDB()
    save(db, reconcile(db, file_a + file_b))
    assert total(db) == 190  # 80 fresh +20 read +50 write +40 output


def test_r5_explicit_zero_and_conflicting_duration_are_distinct():
    db = FakeEventDB()
    save(db, reconcile(db, claude(COMPLETE)))
    save(db, reconcile(db, claude({'cache_creation':{'ephemeral_5m_input_tokens':50, 'ephemeral_1h_input_tokens':0}}, 2)))
    row = next(iter(db.events.values()))
    assert row['cache_write_1h_tokens'] == 0 and row['cache_write_tokens'] == 50
    assert row['telemetry_complete'] and total(db) == 200
    issues = []
    save(db, reconcile(db, claude({'cache_creation':{'ephemeral_1h_input_tokens':70}}, 3), issues))
    assert not next(iter(db.events.values()))['telemetry_complete']
    assert total(db) == 200
    assert 'claude_cache_duration_conflict' in issues


def test_r2_missing_old_start_defers_checkpoint_without_guessing():
    from spend_app.adapters.opencode_granular import reconcile_cumulative
    fine = opencode_schema.parse_message(message(input=100, output=50, cache=0, write=0))
    db = OpenCodeDB([fine.row])
    from spend_app.adapters.common import stable_id
    key = 'opencode.granular.v1:' + stable_id('s1', 'provider-a')
    db.meta[key] = json.dumps({'revisions':{fine.row.raw_id:[1,1,0]}, 'allocations':{}})
    coarse = replace(fine.row, raw_id='coarse', occurred_at=fine.row.occurred_at.replace(year=2024), input_tokens=100, output_tokens=0)
    issues, deferred = [], set()
    rows = reconcile_cumulative(db, [coarse], [SimpleNamespace(session_id='s1',provider_id='provider-a')], issues, deferred)
    assert rows == [] and total(db) == 150
    assert deferred == {('s1','provider-a')}
    assert issues == ['opencode_start_evidence_missing_overlap_unresolved']


def test_r2_proven_later_work_does_not_consume_checkpoint():
    from spend_app.adapters.opencode_granular import reconcile_messages, reconcile_cumulative
    start = datetime(2026,9,1,10,6,tzinfo=UTC)
    fine = opencode_schema.parse_message(message(input=100,output=50,cache=0,write=0,time={'created':int(start.timestamp()*1000),'completed':int(start.timestamp()*1000)+240000}))
    db = OpenCodeDB()
    save(db,reconcile_messages(db,[fine],[]))
    coarse = replace(fine.row,raw_id='coarse',occurred_at=start.replace(minute=5),input_tokens=100,output_tokens=0)
    save(db,reconcile_cumulative(db,[coarse],[SimpleNamespace(session_id='s1',provider_id='provider-a')]))
    assert total(db) == 250  # independent earlier checkpoint and later request


@pytest.mark.parametrize('components,expected,complete', [
    (dict(input=10,output=5,cache_read=2,cache_write=3,reasoning=1,total_tokens=21),21,True),
    (dict(input=10,output=5,cache_read=None,cache_write=3,reasoning=1,total_tokens=15),15,False),
    (dict(input='bad',output=5,totalTokens=15),15,False),
    (dict(total_tokens=15),15,False),
])
def test_r4_additive_and_missing_components(components, expected, complete):
    from spend_app.adapters.zcode_transcript import reduce_records
    rows, issues = reduce_records([dict(role='assistant',sessionId='s',timestamp='2026-09-01T10:00:00Z',usage=components)])
    row = rows[0]
    assert row.input_tokens + row.output_tokens + row.cache_write_tokens + row.unclassified_tokens == expected
    assert row.telemetry_complete == complete


def test_r2_deferred_checkpoint_does_not_advance_logical_or_path_progress(monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    from unittest.mock import Mock
    from spend_app.adapters import opencode_local as adapter
    from spend_app.adapters.common import stable_id
    stamp = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    fine = opencode_schema.parse_message(message(input=100, output=50, cache=0, write=0,
        time={'created':int(stamp.replace(minute=0).timestamp()*1000), 'completed':int(stamp.replace(minute=10).timestamp()*1000)}))
    db = OpenCodeDB([fine.row])
    ledger = 'opencode.granular.v1:' + stable_id('s1', 'provider-a')
    db.meta[ledger] = json.dumps({'revisions':{fine.row.raw_id:[1,1,0]}, 'allocations':{}})
    snapshot = adapter.SessionSnapshot('s1', 'provider-a', 'model-a', None, 100, 0, 0, 0, None, None, stamp)
    monkeypatch.setattr(Path, 'resolve', lambda path, **kwargs:path)
    monkeypatch.setattr(Path, 'is_file', lambda path:True)
    monkeypatch.setattr(adapter, 'initialize', Mock())
    monkeypatch.setattr(adapter, 'connect', lambda path:nullcontext(db))
    monkeypatch.setattr(adapter, 'read_session_snapshots', lambda path:[snapshot])
    monkeypatch.setattr(adapter, 'progress_for_location', lambda *args:None)
    progress = Mock()
    monkeypatch.setattr(adapter, 'write_opencode_progress', progress)
    monkeypatch.setattr(adapter, 'record_coverage_gap', Mock())
    def persist(**kwargs):
        rows = kwargs['prepare'](db, kwargs['usage_rows'])
        save(db, rows)
        kwargs['finalize'](db)
        return {'eventsAccepted':len(rows), 'status':'partial' if kwargs['issues'] else 'success'}
    monkeypatch.setattr(adapter, 'persist_rows', persist)
    result = adapter._ingest_cumulative(database_path=Path('C:/fake/app.db'), pricing=object(), source_database=Path('C:/fake/source.db'))
    assert result['eventsAccepted'] == 0
    progress.assert_not_called()
    state = json.loads(db.meta[ledger])
    state['starts'] = {fine.row.raw_id:fine.started_at.isoformat()}
    db.meta[ledger] = json.dumps(state)
    result = adapter._ingest_cumulative(database_path=Path('C:/fake/app.db'), pricing=object(), source_database=Path('C:/fake/source.db'))
    assert result['status'] == 'success' and total(db) == 150
    assert progress.call_count == 2  # logical and location ledgers advance together


@pytest.mark.parametrize('reverse', [False, True])
def test_r3_partial_and_complete_across_reconstructed_imports(reverse):
    groups = [claude(COMPLETE), claude({'output_tokens':40},2)]
    if reverse:
        groups.reverse()
    db = FakeEventDB()
    for group in groups:
        save(db, reconcile(db, group))
        db = copy.deepcopy(db)
    assert total(db) == 210 and len(db.events) == 1
    assert next(iter(db.events.values()))['cache_write_1h_tokens'] == 50


def test_r1_unproven_cumulative_prefix_does_not_duplicate_explicit_request():
    counts = dict(input_tokens=100,output_tokens=50,cached_input_tokens=0,total_tokens=150)
    event = codex_usage(1,last=counts,total=dict(input_tokens=200,output_tokens=100,cached_input_tokens=0,total_tokens=300))
    event['payload']['info']['request_id']='request-1'
    health = SourceHealth()
    ambiguous = codex_records.reduce_records([codex_meta(),event],health,'s')[1]
    db = FakeEventDB()
    save(db,reconcile(db,codex()+ambiguous))
    assert total(db)==150 and len(db.events)==1
    assert health.partial
