"""Codex quota units: fake source files, clock, persistence and collector gates."""
import copy
import io
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from spend_app import codex_quota as cq, limits, quotas as q, connections as c

NOW = datetime(2026, 9, 8, 21, 30, tzinfo=UTC)

class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW

@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(limits, 'datetime', Clock)
    monkeypatch.setattr(q, 'datetime', Clock)
    monkeypatch.setattr(cq, 'datetime', Clock)
    monkeypatch.setattr(cq, 'scope', lambda root: str(root))
    monkeypatch.setattr(cq, 'current_scope', lambda: 'approved')


def event(pct=54, pool='codex', age=30, slot='primary', **window):
    return {'timestamp': (NOW-timedelta(seconds=age)).isoformat(), 'payload': {'rate_limits': {
        'limit_id': pool, slot: {'used_percent': pct, 'window_minutes': 10080,
        'resets_at': (NOW+timedelta(days=5)).timestamp(), **window}}}}

class File:
    def __init__(self, events):
        self.raw = ('\n'.join(json.dumps(e) for e in events)).encode()
    def stat(self):
        return SimpleNamespace(st_size=len(self.raw), st_mtime=9999999999)
    def open(self, mode):
        assert mode == 'rb'
        return io.BytesIO(self.raw)


def collector(monkeypatch, files):
    from spend_app import connection_paths as cp, providers
    monkeypatch.setattr(providers, 'default_codex_glob', lambda: 'C:/approved/sessions/**/*.jsonl')
    monkeypatch.setattr(Path, 'resolve', lambda self, **kw: self)
    def sources(root, pattern, budget):
        assert str(root).replace('\\','/') == 'C:/approved/sessions'
        assert pattern == '**/*.jsonl' and budget == 5000
        return files
    monkeypatch.setattr(cp, 'source_files', sources)
    monkeypatch.setattr(cp, 'confined', lambda path, root: path)
    return limits._codex_limits()

@pytest.mark.parametrize('reverse',[False, True])
def test_main_before_alternate_same_file(monkeypatch, reverse):
    records = [event(),event(0,'codex_bengalfox',age=10)]
    if reverse: records.reverse()
    result=collector(monkeypatch,[File(records)])
    assert result['poolId']=='codex' and result['windows'][0]['usedPct']==54

@pytest.mark.parametrize('reverse',[False,True])
def test_across_files(monkeypatch,reverse):
    files=[File([event()]),File([event(0,'codex_bengalfox',age=10)])]
    if reverse:files.reverse()
    assert collector(monkeypatch,files)['windows'][0]['usedPct']==54


def test_actual_local_pool_pattern(monkeypatch):
    result=collector(monkeypatch,[File([event(37),event(0,'codex_bengalfox',age=10)])])
    assert result['windows'][0]['usedPct']==37

@pytest.mark.parametrize('pool',[None,'codex_bengalfox','unknown'])
def test_no_pool_guessing(monkeypatch,pool):
    result=collector(monkeypatch,[File([event(0,pool)])])
    assert result['status']=='unavailable' and result['windows']==[]

@pytest.mark.parametrize('slot',['primary','secondary'])
def test_fresh_zero_and_weekly_position(monkeypatch,slot):
    result=collector(monkeypatch,[File([event(),event(0,age=1,slot=slot)])])
    sample=q.codex_quota_samples(result,source='codex_local_telemetry')[0]
    assert sample.pct==0 and sample.pool_id=='codex'
    assert sample.observed_at==(NOW-timedelta(seconds=1)).isoformat().replace('+00:00','Z')

@pytest.mark.parametrize('patch',[
    {'timestamp':'invalid'},{'timestamp':'2026-09-08T21:00:00'},
    {'timestamp':(NOW+timedelta(seconds=1)).isoformat()},
    {'payload':{'rate_limits':{'limit_id':'codex','credits':{'balance':99}}}},
    {'payload':None},
])
def test_malformed_updates_do_not_erase_valid_snapshot(patch):
    bad=event(0,age=1);bad.update(patch)
    row=limits._tail_rate_limit(File([event(),bad]),now=NOW)
    assert row[1]['primary']['used_percent']==54

@pytest.mark.parametrize('window',[{'window_minutes':300},{'window_minutes':20000},
    {'window_minutes':None},{'used_percent':True},{'used_percent':-1},{'used_percent':101},
    {'used_percent':float('nan')},{'resets_at':None},{'resets_at':1e100},{'resets_at':10**400}])
def test_invalid_window_unavailable(monkeypatch,window):
    assert collector(monkeypatch,[File([event(**window)])])['status']=='unavailable'


def test_expired_and_touched_source(monkeypatch):
    old=event(age=8*3600,resets_at=(NOW-timedelta(seconds=1)).timestamp())
    result=collector(monkeypatch,[File([old])])
    assert result['windows']==[] and 'expired' in result['detail']
    old=event(age=7*3600)
    result=collector(monkeypatch,[File([old])])
    assert result['windows']==[] and 'six hours' in result['detail']


def test_scan_budget_failure(monkeypatch):
    from spend_app.connection_paths import SampleLimit
    class Files:
        def __iter__(self):raise SampleLimit('budget')
    assert 'budget' in collector(monkeypatch,Files())['detail']


def test_permission_failure():
    file=File([]);file.open=Mock(side_effect=PermissionError())
    assert limits._tail_rate_limit(file,now=NOW) is None


class Row(dict):
    def __iter__(self):return iter(self.values())

class DB:
    def __init__(self):self.rows=[];self.meta={};self.history=('untouched token event',)
    def execute(self,sql,args=()):
        # Any accounting write or non-quota operation fails this fake contract.
        if sql.startswith('INSERT INTO app_meta'):
            self.meta[args[0]]=args[1];return None
        if sql.startswith('SELECT key,value FROM app_meta'):
            return [(k,v) for k,v in self.meta.items() if k in args]
        if sql=='SELECT * FROM quotas':return copy.deepcopy(self.rows)
        if sql.startswith('SELECT id, label'):
            rows=[r for r in self.rows if r['provider_key']==args[0] and r['limit_key']==args[1]]
            row=rows[-1] if rows else None
            keys=('id','label','used','allowance','unit','pct','resets_at','source','is_payg')
            return SimpleNamespace(fetchone=lambda:Row((k,row[k]) for k in keys) if row else None)
        if sql.startswith('UPDATE quotas SET polled_at'):
            for row in self.rows:
                if row['id']==args[1]:row['polled_at']=args[0]
            return None
        raise AssertionError(sql)


def sample():
    return q.QuotaSample('codex','weekly','Codex weekly window','pct','codex_local_telemetry',
        pct=54,resets_at=(NOW+timedelta(days=5)).isoformat(),observed_at=NOW.isoformat(),pool_id='codex',scope='approved')


def test_repeated_poll_durable_age_and_history(monkeypatch):
    db=DB()
    monkeypatch.setattr(q,'initialize',lambda p:None)
    monkeypatch.setattr(q,'connect',lambda p:nullcontext(db))
    monkeypatch.setattr(c,'lock_for',lambda p:nullcontext())
    monkeypatch.setattr(q,'upsert_quota',lambda conn,**row:conn.rows.append(dict(id=len(conn.rows)+1,**row)))
    for seconds in (0,30):
        q.poll_quotas('fake',collectors={'codex':lambda:[sample()]},now=lambda:(NOW+timedelta(seconds=seconds)).isoformat())
    assert len(db.rows)==1
    assert json.loads(db.meta[cq.KEY])['observedAt']==NOW.isoformat()
    reconstructed=DB();reconstructed.rows=copy.deepcopy(db.rows);reconstructed.meta=json.loads(json.dumps(db.meta))
    assert cq.read_rows(reconstructed,now=NOW+timedelta(seconds=30))[0]['pct']==54
    assert cq.read_rows(reconstructed,now=NOW+timedelta(hours=7))[0]['pct'] is None
    assert db.history==('untouched token event',)

@pytest.mark.parametrize('binding',[{'enabled':False,'revision':2},{'enabled':True,'revision':3,'location':'B'}])
def test_read_hides_old_binding_quota(binding):
    db=DB();s=sample();cq.save_metadata(db,s,NOW.isoformat())
    db.rows=[dict(id=1,provider_key='codex',limit_key='weekly',pct=54,used=None,allowance=None,
        resets_at=s.resets_at,polled_at=NOW.isoformat(),unit='pct',label=s.label,source=s.source)]
    db.meta[c.KEY]=json.dumps({'bindings':{'codex_local':binding}})
    assert cq.read_rows(db,now=NOW)[0]['pct'] is None


def test_no_hidden_default_read_on_rebind(monkeypatch):
    from spend_app import config
    state={'bindings':{'codex_local':{'enabled':True,'revision':1,'location':'A'}}}
    monkeypatch.setattr(config,'load_settings',lambda:config.Settings(Path('fake'),Path('pricing'),Path('imports'),None,None,None,'UTC',0.5,1000))
    monkeypatch.setattr(c,'lock_for',lambda p:nullcontext())
    monkeypatch.setattr(c,'Store',lambda p:SimpleNamespace(read=lambda:state))
    reader=Mock(side_effect=AssertionError('hidden default read'))
    monkeypatch.setattr(q,'_codex_limits',reader)
    collectors=q.default_quota_collectors('fake')
    for enabled,revision,location in [(True,1,'A'),(False,2,'A'),(True,3,'B')]:
        state['bindings']['codex_local'].update(enabled=enabled,revision=revision,location=location)
        result=collectors['codex']()[0]
        assert result.pct is None and 'usage only' in result.reason
    reader.assert_not_called()


def test_external_scope_change_hides_previous_reading(monkeypatch):
    db=DB();s=sample();cq.save_metadata(db,s,NOW.isoformat())
    db.rows=[dict(id=1,provider_key='codex',pct=54,resets_at=s.resets_at,polled_at=NOW.isoformat())]
    monkeypatch.setattr(cq,'current_scope',lambda:'replacement')
    assert cq.read_rows(db,now=NOW)[0]['pct'] is None

@pytest.mark.parametrize('reverse',[False,True])
def test_conflicting_equal_timestamps_are_not_guessed(monkeypatch,reverse):
    files=[File([event(54)]),File([event(0)])]
    if reverse:files.reverse()
    assert collector(monkeypatch,files)['status']=='unavailable'


def test_registry_override_controls_quota_source(monkeypatch):
    from spend_app import connection_paths as cp
    monkeypatch.setenv('CODEX_HOME','C:/Approved Root/Unicode-é')
    monkeypatch.setattr(Path,'resolve',lambda self,**kw:self)
    seen=[]
    monkeypatch.setattr(cp,'source_files',lambda root,*a,**kw: seen.append(str(root).replace('\\','/')) or [])
    limits._codex_limits()
    assert seen==['C:/Approved Root/Unicode-é/sessions']


def test_read_api_masks_legacy_zero_without_deleting_it(monkeypatch):
    db=DB()
    db.rows=[dict(id=1,provider_key='codex',limit_key='weekly',pct=0,used=None,allowance=None,
        resets_at=(NOW+timedelta(days=5)).isoformat(),polled_at=NOW.isoformat(),unit='pct',label='Codex weekly window',source='codex_local_telemetry')]
    monkeypatch.setattr(limits,'connect',lambda p:nullcontext(db))
    result=limits.snapshot_limits(Path('fake'))['providers'][0]
    assert result['status']=='unavailable' and result['windows']==[]
    assert db.rows[0]['pct']==0


def test_current_zero_is_exact_in_api(monkeypatch):
    from dataclasses import replace
    db=DB();s=replace(sample(),pct=0);cq.save_metadata(db,s,NOW.isoformat())
    db.rows=[dict(id=1,provider_key='codex',limit_key='weekly',pct=0,used=None,allowance=None,
        resets_at=s.resets_at,polled_at=NOW.isoformat(),unit='pct',label=s.label,source=s.source)]
    monkeypatch.setattr(limits,'connect',lambda p:nullcontext(db))
    result=limits.snapshot_limits(Path('fake'))['providers'][0]
    assert result['status']=='exact' and result['windows'][0]['usedPct']==0
    assert result['windows'][0]['observedAt']==NOW.isoformat()


def test_source_read_is_capped_at_forty_files(monkeypatch):
    files=[File([event()]) for _ in range(41)]
    files[-1].open=Mock(side_effect=AssertionError('outside budget'))
    assert collector(monkeypatch,files)['status']=='exact'
    files[-1].open.assert_not_called()


def test_newer_unambiguous_reading_supersedes_old_conflict(monkeypatch):
    result=collector(monkeypatch,[File([event(54),event(0),event(30,age=1)])])
    assert result['windows'][0]['usedPct']==30


def test_other_provider_rows_and_writes_unchanged():
    db=DB()
    db.rows=[dict(id=1,provider_key='grok',pct=34)]
    before=copy.deepcopy(db.rows)
    cq.save_metadata(db,q.QuotaSample('grok','weekly','Grok','pct','fixture',pct=34),NOW.isoformat())
    assert cq.read_rows(db,now=NOW)==before and not db.meta


def test_expiry_read_does_not_reinvent_reset():
    db=DB();s=sample();cq.save_metadata(db,s,NOW.isoformat())
    db.rows=[dict(id=1,provider_key='codex',pct=54,resets_at=s.resets_at,polled_at=NOW.isoformat())]
    result=cq.read_rows(db,now=NOW+timedelta(days=6))[0]
    assert result['pct'] is None and result['resets_at'] is None and 'expired' in result['label']
    assert db.rows[0]['resets_at']==s.resets_at


def test_capacity_uses_same_current_observation(monkeypatch):
    from spend_app.aggregate import _load_quota_rows, _capacity_from_rows
    db=DB();s=sample();cq.save_metadata(db,s,NOW.isoformat())
    db.rows=[dict(id=1,provider_key='codex',limit_key='weekly',pct=54,used=None,allowance=None,
        resets_at=s.resets_at,polled_at=NOW.isoformat(),unit='pct',label=s.label,source=s.source,is_payg=None)]
    rows=_load_quota_rows(db,now=NOW)
    card=next(c for c in _capacity_from_rows(rows,now=NOW,month_to_date={}) if c['providerKey']=='codex')
    assert card['primaryPct']==54 and card['rows'][0]['observedAt']==NOW.isoformat()
    rows=_load_quota_rows(db,now=NOW+timedelta(hours=7))
    card=next(c for c in _capacity_from_rows(rows,now=NOW,month_to_date={}) if c['providerKey']=='codex')
    assert card['primaryPct'] is None
