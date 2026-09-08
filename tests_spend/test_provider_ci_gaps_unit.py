"""Compatibility/CI-gap units; storage, filesystem and HTTP are fake boundaries."""
import copy
import io
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_connections_unit import service, body
from test_provider_compat_unit import message
from spend_app import connections as c
from spend_app.connection_paths import MissingLocation, LocationError, normalize
from spend_app.providers import REGISTRY
from spend_app.adapters import opencode_granular as oc, zcode_schema as zc


@pytest.fixture
def execution(service, monkeypatch):
    for name in ('CODEX_HOME','CLAUDE_CONFIG_DIR','GROK_HOME','XDG_DATA_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(c, 'Service', lambda settings: service)
    monkeypatch.setattr(service, 'cleanup', lambda:None)
    monkeypatch.setattr(c, 'default_location', lambda spec, settings:'C:/simulated/'+spec.key)
    monkeypatch.setattr('spend_app.adapters.common.skipped_result', lambda **kw:{'source':kw['source'], 'status':'skipped','eventsSeen':0,'eventsAccepted':0,'reason':kw['reason']})
    return service


def test_absent_default_is_skipped_then_new_source_is_collected(execution):
    spec = REGISTRY.get('codex_local')
    execution.inspector.normalize = Mock(side_effect=[MissingLocation('absent'),'C:/simulated/codex_local'])
    collect = Mock(return_value={'status':'success','eventsSeen':1,'eventsAccepted':1})
    first = c.execute(execution.settings, object(), spec, collect)
    assert first['status']=='skipped' and first['eventsAccepted']==0
    collect.assert_not_called()
    assert c.execute(execution.settings, object(), spec, collect)['eventsAccepted']==1
    assert collect.call_count==1


def test_missing_explicit_environment_is_failure(execution, monkeypatch):
    monkeypatch.setenv('CODEX_HOME','C:/explicit')
    execution.inspector.normalize=Mock(side_effect=MissingLocation('missing'))
    collect=Mock()
    with pytest.raises(LocationError,match='Configured source is missing'):
        c.execute(execution.settings,object(),REGISTRY.get('codex_local'),collect)
    collect.assert_not_called()


def test_moved_binding_needs_attention_and_disabled_stays_disabled(execution):
    execution.mutate(body())
    execution.inspector.normalize=Mock(side_effect=MissingLocation('moved'))
    collect=Mock()
    with pytest.raises(LocationError):
        c.execute(execution.settings,object(),REGISTRY.get('codex_local'),collect)
    assert execution.store.read()['bindings']['codex_local']['state']=='needs_attention'
    execution.mutate(body(revision=1,operation='disable'))
    assert c.execute(execution.settings,object(),REGISTRY.get('codex_local'),collect)['status']=='skipped'
    collect.assert_not_called()


@pytest.mark.parametrize('error',[LocationError('Permission denied'),LocationError('incompatible schema'),OSError('unexpected I/O')])
def test_default_errors_are_not_absence(execution,error):
    execution.inspector.normalize=Mock(side_effect=error)
    with pytest.raises(type(error)):
        c.execute(execution.settings,object(),REGISTRY.get('codex_local'),Mock())


@pytest.mark.parametrize('error,missing',[(FileNotFoundError(),True),(PermissionError(),False),(OSError(),False)])
def test_normalizer_classifies_only_enoent_as_missing(monkeypatch,error,missing):
    monkeypatch.setattr(Path,'resolve',Mock(side_effect=error))
    with pytest.raises(LocationError) as caught:
        normalize('C:/simulated/sessions',REGISTRY.get('codex_local').connection)
    assert isinstance(caught.value,MissingLocation)==missing


def test_missing_default_does_not_stop_next_source(execution,monkeypatch):
    from spend_app import scheduler
    def inspect(path,meta):
        if meta.name=='Codex': raise MissingLocation('absent')
        return path
    execution.inspector.normalize=inspect
    calls=[]
    monkeypatch.setattr(scheduler,'_ingest_callable',lambda spec: lambda **kwargs: calls.append(spec.key) or {'status':'success'})
    monkeypatch.setattr('spend_app.adapters.common.failed_result',Mock(side_effect=AssertionError('absence is not failure')))
    scheduler._run_ingest_specs([(REGISTRY.get('codex_local'),{}),(REGISTRY.get('claude_local'),{})],execution.settings,object())
    assert calls==['claude_local']


BASE={'source':'opencode_local','status':'success','eventsSeen':2,'eventsAccepted':2,'eventsWritten':1,
      'unpricedEventsWritten':0,'coverageGapsWritten':0,'costBucketsSeen':0,'costBucketsWritten':0,
      'quarantined':0,'unpricedModels':[],'issues':[],'files':1}


@pytest.mark.parametrize('partial',[False,True])
def test_cumulative_single_result_is_preserved(monkeypatch,partial):
    child={**copy.deepcopy(BASE),'customMetadata':'retained'}
    if partial: child.update(status='partial',unpricedModels=['opencode:missing'],unpricedEventsWritten=1)
    monkeypatch.setattr(Path,'exists',lambda p:True)
    monkeypatch.setattr(oc,'read_source',lambda p:([],[Path('C:/fake/a.db')],[],['cumulative']))
    monkeypatch.setattr('spend_app.adapters.opencode_local._ingest_cumulative',lambda **kw:child)
    assert oc.ingest(database_path=Path('C:/fake/app.db'),pricing=object(),source_database=Path('C:/fake/a.db'))=={**child,'formats':['cumulative']}


def test_multiple_cumulative_results_keep_partial_pricing_and_counts():
    partial={**BASE,'status':'partial','eventsSeen':3,'eventsAccepted':2,'eventsWritten':0,'unpricedEventsWritten':2,
             'quarantined':1,'unpricedModels':['opencode:x','opencode:x'],'issues':['malformed']}
    assert oc.combine_results([BASE,partial],formats=['cumulative'])=={
        'source':'opencode_local','status':'partial','eventsSeen':5,'eventsAccepted':4,'eventsWritten':1,
        'unpricedEventsWritten':2,'coverageGapsWritten':0,'costBucketsSeen':0,'costBucketsWritten':0,
        'quarantined':1,'files':2,'unpricedModels':['opencode:x'],'issues':['malformed'],'formats':['cumulative']}


def test_failure_and_deferred_evidence_do_not_erase_success():
    failed={**BASE,'status':'failed','eventsSeen':0,'eventsAccepted':0,'eventsWritten':0,'issues':['unreadable']}
    combined=oc.combine_results([failed,BASE])
    assert combined['status']=='partial' and combined['eventsAccepted']==2 and combined['eventsWritten']==1
    deferred={**BASE,'status':'partial','eventsAccepted':0,'eventsWritten':0,'issues':['deferred']}
    result=oc.combine_results([deferred])
    assert result['eventsAccepted']==0 and result['eventsWritten']==0 and result['issues']==['deferred']
    replay={**BASE,'eventsWritten':0}
    assert oc.combine_results([replay])=={**replay,'formats':[]}


def test_mixed_granular_and_cumulative_keeps_both_results(monkeypatch):
    from spend_app.adapters.opencode_schema import parse_message
    monkeypatch.setattr(Path,'exists',lambda p:True)
    monkeypatch.setattr(oc,'read_source',lambda p:([parse_message(message())],[Path('C:/fake/a.db')],[],['v1','cumulative']))
    child={**BASE,'status':'partial','unpricedModels':['opencode:x']}
    monkeypatch.setattr('spend_app.adapters.opencode_local._ingest_cumulative',lambda **kw:child)
    monkeypatch.setattr(oc,'persist_rows',lambda **kw:{**BASE,'eventsSeen':1,'eventsAccepted':1})
    result=oc.ingest(database_path=Path('C:/fake/app.db'),pricing=object(),source_database=Path('C:/fake/a.db'))
    assert result['status']=='partial' and result['unpricedModels']==['opencode:x']
    assert result['eventsSeen']==3 and result['eventsAccepted']==3 and result['eventsWritten']==2


@pytest.mark.parametrize('directory,expected',[(r'C:\work\ExampleProject','ExampleProject'),('/work/项目/','项目'),('',None),(None,None),({},None),('C:\\',None)])
def test_zcode_project_label_does_not_change_tokens(directory,expected):
    row,issue=zc.parse_row(dict(id='r',session_id='s',model_id='glm',provider_id='builtin:zai-coding-plan',completed_at=1760000000000,
        input_tokens=10,output_tokens=5,cache_read_input_tokens=2,cache_creation_input_tokens=3,_project_directory=directory))
    assert row.project==expected and row.input_tokens==7 and row.output_tokens==5 and row.cache_write_tokens==3
    assert issue is None


@pytest.mark.parametrize('session_columns',[{'id','directory'},set(),{'id'}])
def test_zcode_optional_project_schema_has_one_row_per_usage(session_columns):
    calls=[]
    columns=zc.REQUIRED|set(zc.OPTIONAL)
    def execute(sql):
        calls.append(sql)
        if 'table_info("model_usage")' in sql: return [(0,col) for col in columns]
        if 'table_info("session")' in sql: return [(0,col) for col in session_columns]
        return [tuple([None]*(len(zc.REQUIRED)+len(zc.OPTIONAL))+['/work/example' if 'directory' in session_columns else None])]
    rows,_=zc.select_rows(SimpleNamespace(execute=execute))
    assert len(rows)==1
    assert ' JOIN ' not in calls[-1]
    assert ('COUNT(*)=1' in calls[-1])==('directory' in session_columns)
    assert rows[0]['_project_directory']==('/work/example' if 'directory' in session_columns else None)


def test_incomplete_pages_retain_evidence_but_not_false_success():
    from spend_app.adapters.report_pages import fetch,IncompleteReport
    page={'data':[{'start_time':1,'results':[]}],'has_more':True,'next_page':None}
    client=Mock()
    client.get.return_value.json.return_value=page
    with pytest.raises(IncompleteReport) as caught:
        fetch(client,url='https://example.test/report',params={},clock=lambda:0)
    assert caught.value.partial_pages==(page,)
    assert client.get.call_count==1
    assert 'start_time' not in str(caught.value)
    client.get.return_value.json.return_value={**page,'has_more':False}
    assert fetch(client,url='https://example.test/report',params={},clock=lambda:0)==[{**page,'has_more':False}]


def test_codex_contradictory_total_is_unclassified_not_discarded():
    from spend_app.adapters.codex_records import usage_vector
    result=usage_vector(dict(input_tokens=1000,cached_input_tokens=400,cache_write_input_tokens=100,output_tokens=200,reasoning_output_tokens=50,total_tokens=1200))
    assert result['values']==(0,0,0,0) and result['total']==1200 and not result['complete']
    assert result['conflict'] and result['reasoning'] is None


def test_grok_append_reads_only_new_records_and_rotation_resets(monkeypatch):
    from spend_app.adapters import grok_local,grok_records
    import hashlib
    content=[b'']
    def line(msg, **ctx): return (json.dumps(dict(msg=msg,sid='s',ts='2026-09-01T00:00:00Z',ctx=ctx))+'\n').encode()
    content[0]=line('model changed',model='grok-a')+line('shell.turn.inference_done',prompt_tokens=10,cached_prompt_tokens=0,completion_tokens=2,loop_index=1)
    monkeypatch.setattr('spend_app.connection_paths.confined',lambda p:p)
    monkeypatch.setattr('spend_app.connection_paths.file_signature',lambda p:(hashlib.sha256(content[0]).hexdigest(),))
    monkeypatch.setattr(Path,'stat',lambda p:SimpleNamespace(st_size=len(content[0]),st_dev=1,st_ino=2))
    monkeypatch.setattr(Path,'open',lambda *a,**k:io.BytesIO(content[0]))
    reducer=grok_records.reduce_records
    sizes=[]
    def spy(records,state=None): sizes.append(len(records));return reducer(records,state)
    monkeypatch.setattr(grok_records,'reduce_records',spy)
    first,state=grok_local.parse_log(Path('C:/fake/log.jsonl'))
    content[0]+=line('shell.turn.inference_done',prompt_tokens=20,cached_prompt_tokens=0,completion_tokens=3,loop_index=2)
    second,state=grok_local.parse_log(Path('C:/fake/log.jsonl'),state)
    assert sizes==[2,1] and len(first)==len(second)==1
    assert second[0].input_tokens==20 and first[0].raw_id!=second[0].raw_id
    unchanged,state=grok_local.parse_log(Path('C:/fake/log.jsonl'),state)
    assert unchanged==[] and sizes==[2,1]
    content[0]=line('shell.turn.inference_done',prompt_tokens=5,cached_prompt_tokens=0,completion_tokens=1,loop_index=3)
    third,state=grok_local.parse_log(Path('C:/fake/log.jsonl'),state)
    assert third[0].model_key=='supergrok:unknown' and third[0].input_tokens==5


def test_traycer_projection_versions_reparse_only_changed_chat(monkeypatch):
    from spend_app.adapters import traycer_local as adapter
    versions={'a':1,'b':1}
    def projection(chat):
        return json.dumps({'settings':{'harnessId':'grok','model':'grok-a'},'events':[
            {'body':{'timestamp':1760000000000+i,'metadata':{'usage':{'totalTokens':10}}}} for i in range(versions[chat])]})
    def execute(sql,args=()):
        if sql.startswith('SELECT chat_id,through_seq'):
            return SimpleNamespace(fetchall=lambda:list(versions.items()))
        assert sql.startswith('SELECT projection_json')
        return SimpleNamespace(fetchone=lambda:(projection(args[0]),))
    monkeypatch.setattr(adapter,'sqlite_read_only',lambda p:SimpleNamespace(execute=execute,close=lambda:None))
    monkeypatch.setattr(adapter,'_PROJECTION_CACHE',{})
    monkeypatch.setattr(Path,'stat',lambda p:SimpleNamespace(st_dev=1,st_ino=2,st_mtime_ns=sum(versions.values())))
    monkeypatch.setattr(Path,'resolve',lambda p,**kw:p)
    original=adapter.parse_projection
    calls=[]
    def spy(**kwargs):
        calls.append(kwargs['chat_id'])
        return original(**kwargs)
    monkeypatch.setattr(adapter,'parse_projection',spy)
    first=adapter.parse_database(Path('C:/fake/chat.db'))
    assert len(first)==2 and calls==['a','b']
    assert adapter.parse_database(Path('C:/fake/chat.db'))==first
    assert calls==['a','b']
    versions['b']=2
    assert len(adapter.parse_database(Path('C:/fake/chat.db')))==3
    assert calls==['a','b','b']


def test_zcode_unmatched_session_retains_valid_token_row():
    columns=zc.REQUIRED|{'provider_id','completed_at','cache_creation_input_tokens','cache_read_input_tokens'}
    record=dict(id='r',session_id='missing-session',model_id='glm',provider_id='builtin:zai-coding-plan',
                input_tokens=10,output_tokens=5,completed_at=1760000000000,cache_creation_input_tokens=3,cache_read_input_tokens=2)
    def execute(sql):
        if 'table_info("model_usage")' in sql:return [(0,k) for k in columns]
        if 'table_info("session")' in sql:return [(0,'id'),(1,'directory')]
        return [tuple(record.get(k) for k in sorted(zc.REQUIRED)+list(zc.OPTIONAL))+(None,)]
    rows,modern=zc.select_rows(SimpleNamespace(execute=execute))
    assert len(rows)==1
    parsed,issue=zc.parse_row(rows[0],modern)
    assert parsed.project is None and parsed.input_tokens==7 and parsed.output_tokens==5 and parsed.cache_write_tokens==3
    assert issue is None


def test_failed_cycle_still_warms_only_requested_windows(monkeypatch):
    from spend_app import scheduler
    class FakeScheduler:
        def __init__(self,**kwargs):self.jobs={}
        def add_job(self,func,*args,**kwargs):self.jobs[kwargs['id']]=func
    monkeypatch.setattr(scheduler,'BackgroundScheduler',FakeScheduler)
    monkeypatch.setattr(scheduler,'connect',lambda path:nullcontext(object()))
    monkeypatch.setattr(scheduler,'prune_ingest_runs',lambda db:None)
    monkeypatch.setattr(scheduler,'_run_ingest_specs',Mock(side_effect=RuntimeError('one failed source')))
    requested=[]
    monkeypatch.setattr(scheduler,'recently_requested_summaries',lambda:requested)
    monkeypatch.setattr(scheduler,'data_clock',lambda *args:None)
    warmed=[]
    monkeypatch.setattr(scheduler,'aggregate_summary_cached',lambda **kw:warmed.append((kw['window_key'],kw['tool'])))
    settings=SimpleNamespace(database_path=Path('C:/fake/app.db'),local_ingest_interval_seconds=15,admin_ingest_interval_minutes=15,
        quota_poll_seconds=15,activity_poll_seconds=4,timezone='UTC',cache_hit_threshold=.5)
    instance=scheduler.create_scheduler(settings,object())
    with pytest.raises(RuntimeError,match='local ingest'):
        instance.jobs['local-ingest']()
    assert warmed==[]
    requested.extend([('15m','all'),('1w','codex')])
    with pytest.raises(RuntimeError,match='local ingest'):
        instance.jobs['local-ingest']()
    assert warmed==[('15m','all'),('1w','codex')]
