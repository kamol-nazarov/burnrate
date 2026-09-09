import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

NEW_YORK = ZoneInfo("America/New_York")

from tests_spend.attention_fakes import MemoryRepository, Prices, NOW, SCOPE, Service, KEY, set_quota, attempt, gap, denied
from spend_app import attention_rules as rules
from spend_app.attention_store import Conflict, quota_fact, register
from spend_app.attention_api import attention_router

@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setattr("sqlite3.connect", denied)
    monkeypatch.setattr("pathlib.Path.home", denied)
    monkeypatch.setattr("pathlib.Path.open", denied)
    monkeypatch.setattr("subprocess.Popen", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("spend_app.codex_quota.current_scope", lambda:SCOPE)
    monkeypatch.setattr("spend_app.providers.default_codex_glob", denied)
    monkeypatch.setattr("spend_app.connection_secrets.Vault.backend", denied)

def world():
    repo = MemoryRepository()
    clock = [NOW]
    prices = Prices()
    return repo, clock, prices, Service(repo, prices, "America/New_York", lambda:clock[0])

def only(service, family="quota"):
    return next(x for x in service.read()["current"] if x["family"] == family)

def action(service, operation, item=None, **values):
    body = {"operation":operation, "requestId":str(uuid4()), "expectedRevision":service.read()["revision"], **values}
    if item:body["id"] = item["id"]
    return service.update(body), body

def test_repeated_quota_reconstruction_and_ack():
    repo, clock, prices, service = world()
    set_quota(repo,82)
    assert service.evaluate()
    original = only(service)
    action(service,"acknowledge",original)
    service = Service(repo,prices,"America/New_York",lambda:clock[0])
    for _ in range(3):assert service.evaluate()
    item = only(service)
    assert item["id"] == original["id"] and item["acknowledged"]
    assert item["observedAt"] == original["observedAt"] and service.read()["badgeCount"] == 0

def test_tiers_decreases_and_snooze_survive_escalation():
    repo, clock, prices, service = world()
    seen = []
    for index,pct in enumerate((79,80,94,95,0,82)):
        clock[0] = NOW + timedelta(seconds=index)
        set_quota(repo,pct,clock[0]);assert service.evaluate()
        if pct==79:assert service.read()["badgeCount"]==0;continue
        item=only(service);seen.append(item["id"])
        assert item["evidence"]["usedPercent"]==pct
        if pct==80:action(service,"acknowledge",item)
        if pct==94:assert item["acknowledged"];action(service,"snooze",item,hours=1)
        if pct==95:assert item["severity"]==2 and item["snoozed"] and not item["needsAttention"]
        if pct==0:assert item["severity"]==0
    assert len(set(seen))==1

def test_dip_and_new_window_are_not_poll_recurrence():
    repo,clock,_,service=world()
    for i,pct in enumerate((81,79,82)):
        clock[0]=NOW+timedelta(seconds=i);set_quota(repo,pct,clock[0]);service.evaluate()
        if i==0:first=only(service);action(service,"acknowledge",first)
    assert only(service)["id"]==first["id"] and only(service)["acknowledged"]
    clock[0]=NOW+timedelta(days=7,seconds=1)
    set_quota(repo,96,clock[0],reset="2026-09-24T12:00:00Z");service.evaluate()
    assert only(service)["id"]!=first["id"] and service.read()["history"][0]["reasonCode"]=="window_expired"
    assert "Currently" in only(service)["reason"]

@pytest.mark.parametrize("proof",[{"poolId":"alternate"},{"poolId":None},{"scope":None}])
def test_unknown_or_alternate_provenance_never_mixes_main(proof):
    repo,_,_,service=world();set_quota(repo,0,**proof);service.evaluate()
    assert service.read()["badgeCount"]==0
    set_quota(repo,54);service.evaluate();assert service.read()["badgeCount"]==0

def test_future_observation_cannot_poison_later_valid_evidence():
    repo,clock,_,service=world();set_quota(repo,96,NOW+timedelta(days=1));service.evaluate()
    assert service.read()['badgeCount']==0
    clock[0]+=timedelta(seconds=1);set_quota(repo,82,clock[0]);service.evaluate()
    assert service.read()['badgeCount']==1

def test_remaining_meaning_and_ineligible_balances():
    raw={"provider_key":"example", "limit_key":"weekly", "pct":18, "unit":"pct", "resets_at":"2026-09-17T12:00:00Z"}
    proof={"poolId":"main", "scope":"approved", "observedAt":rules.iso(NOW), "meaning":"remaining"}
    assert quota_fact(raw,proof,NOW)["evidence"]["usedPercent"]==82
    assert quota_fact({**raw,"unit":"usd"},proof,NOW)["eligibility"]!="eligible"
    assert quota_fact(raw,{**proof,"meaning":None},NOW)["eligibility"]!="eligible"

def test_stale_missing_reads_and_lag_do_not_resolve_or_escalate():
    repo,clock,_,service=world();set_quota(repo,82);service.evaluate();item=only(service)
    clock[0]+=timedelta(hours=7);service.evaluate()
    assert only(service)["id"]==item["id"] and only(service)["status"]=="awaiting" and service.read()["badgeCount"]==0
    repo.fail_reads.add("FROM quotas");service.evaluate()
    assert service.read()["evaluation"]["status"]=="error" and only(service)["id"]==item["id"]
    repo.fail_reads.clear();clock[0]=NOW;service.evaluate();clock[0]+=timedelta(seconds=91)
    assert service.read()["evaluation"]["status"]=="stale"

def test_snooze_elapsed_hours_retry_and_expiry():
    repo,clock,_,service=world();set_quota(repo,96);service.evaluate()
    _,body=action(service,"snooze",only(service),hours=1);until=only(service)["snoozedUntil"]
    clock[0]+=timedelta(minutes=30);service.update(body)
    assert only(service)["snoozedUntil"]==until
    clock[0]+=timedelta(minutes=31);set_quota(repo,96,clock[0]);service.evaluate()
    assert only(service)["needsAttention"]
    action(service,"snooze",only(service),hours=24);action(service,"unsnooze",only(service))
    assert only(service)["needsAttention"]

def test_snooze_uses_elapsed_time_across_daylight_saving_transition():
    from datetime import datetime
    from spend_app.attention_store import fact
    now=datetime(2026,11,1,0,30,tzinfo=NEW_YORK)
    f=fact('source','cursor_local','legacy',now,rules.iso(now),eligibility='eligible',condition='breach',reasonCode='source_failed')
    state=rules.reduce(rules.initial(),[f],now)
    item=state['current'][f['key']]
    changed=rules.mutate(state,{'operation':'snooze','id':item['id'],'hours':24},now)
    assert (rules.stamp(changed['current'][f['key']]['snoozedUntil'])-now.astimezone(rules.UTC)).total_seconds()==86400

def test_reset_correction_does_not_mint_duplicate():
    repo,clock,_,service=world();set_quota(repo,82);service.evaluate();first=only(service)
    clock[0]+=timedelta(seconds=1);set_quota(repo,96,clock[0],reset="2026-09-17T12:01:00Z");service.evaluate()
    assert only(service)["id"]==first["id"] and only(service)["reasonCode"]=="window_ambiguous" and service.read()["badgeCount"]==0

def test_sources_failure_zero_row_recovery_and_recurrence():
    repo,clock,_,service=world();repo.attempts=[attempt(),attempt("codex_local","success",reason=None)]
    service.evaluate();first=only(service,"source");assert first["evidence"]["source"]=="cursor_local"
    clock[0]+=timedelta(seconds=10);repo.attempts=[attempt(status="success",when=clock[0],reason=None)]
    service.evaluate();assert not service.read()["current"] and service.read()["history"][0]["reasonCode"]=="recovered"
    clock[0]+=timedelta(seconds=10);repo.attempts=[attempt(when=clock[0])];service.evaluate()
    assert only(service,"source")["id"]!=first["id"]

def test_optional_defaults_and_cadence_do_not_alert():
    repo,_,_,service=world();repo.meta["connections.v1"]["legacy"]=[]
    repo.attempts=[attempt(),attempt("codex_local","skipped",reason="adaptive cadence")];service.evaluate()
    assert service.read()["badgeCount"]==0

def test_proven_quota_and_previously_monitored_default_do_not_require_migration_marker():
    repo,_,_,service=world();repo.meta['connections.v1']['legacy']=[]
    set_quota(repo,82);repo.attempts=[attempt('codex_local')]
    repo.successes=[{'source':'codex_local','last_success_at':'2026-09-10T11:00:00Z'}]
    service.evaluate()
    assert {item['family'] for item in service.read()['current']}=={'quota','source'}

def test_user_mutation_cannot_hide_evaluator_failure():
    repo,_,_,service=world();set_quota(repo,82);service.evaluate()
    repo.meta[KEY+'.error']={'reasonCode':'persistence_failed'}
    response,_=action(service,'acknowledge',only(service))
    assert response['evaluation']['status']=='error' and response['badgeLabel']=='Attention !'

def test_binding_revision_and_disabled_are_not_recovery():
    repo,clock,_,service=world()
    b={"enabled":True,"revision":1,"state":"needs_attention","lastVerification":"2026-09-10T11:00:00Z"}
    repo.meta["connections.v1"]["bindings"]["cursor_local"]=b;repo.attempts=[attempt()];service.evaluate();first=only(service,"source")
    b=repo.meta["connections.v1"]["bindings"]["cursor_local"]
    b.update(revision=2,state="waiting_activity",importRevision=1,lastImport=rules.iso(NOW+timedelta(seconds=3)))
    clock[0]+=timedelta(seconds=3);service.evaluate()
    assert not service.read()["current"] and service.read()["history"][0]["reasonCode"]=="source_rebound"

def test_managed_partial_import_has_same_revision_and_zero_new_rows():
    repo,clock,_,service=world()
    repo.meta['connections.v1']['bindings']['cursor_local']={'enabled':True,'revision':4,'state':'needs_attention','lastVerification':'2026-09-10T11:00:00Z'}
    repo.attempts=[attempt()];service.evaluate();assert only(service,'source')['severity']==1
    clock[0]+=timedelta(seconds=1)
    repo.meta['connections.v1']['bindings']['cursor_local'].update(importRevision=4,lastImport=rules.iso(clock[0]),state='waiting_activity')
    repo.attempts=[attempt(status='partial',when=clock[0],reason='missing model pricing')];service.evaluate()
    assert not service.read()['current'] and service.read()['history'][0]['reasonCode']=='recovered'

def test_disable_closes_applicability_even_with_old_attempt():
    repo,clock,_,service=world();repo.attempts=[attempt()];service.evaluate()
    repo.meta['connections.v1']['bindings']['cursor_local']={'enabled':False,'revision':2}
    repo.attempts=[attempt(when=NOW-timedelta(hours=1))];service.evaluate()
    assert service.read()['history'][0]['reasonCode']=='source_disabled'

def test_missing_configuration_and_quota_provenance_are_not_rebind_or_recovery():
    repo,_,_,service=world();set_quota(repo,96);service.evaluate();original=only(service)
    del repo.meta['codex.quota-observation.v1'];service.evaluate()
    assert only(service)['id']==original['id'] and not service.read()['history']
    del repo.meta['connections.v1'];service.evaluate()
    assert service.read()['evaluation']['status']=='error' and only(service)['status']=='awaiting'

@pytest.mark.parametrize('reason,code',[('incompatible schema','source_schema'),('permission denied','source_permission'),('authentication failed','source_auth')])
def test_failure_types_remain_distinct(reason,code):
    repo,_,_,service=world();repo.attempts=[attempt(reason=reason)];service.evaluate()
    assert only(service,'source')['reasonCode']==code

def test_pricing_repeat_domains_and_incomplete_records():
    repo,clock,_,service=world();repo.gaps=[gap(),gap(2,"another:missing"),gap(3,"incomplete",telemetry_complete=0)]
    service.evaluate();assert len(service.read()["current"])==2
    first=only(service,"pricing");action(service,"acknowledge",first)
    repo.gaps.append(gap(4));clock[0]+=timedelta(seconds=1);service.evaluate()
    same=next(x for x in service.read()["current"] if x["id"]==first["id"])
    assert same["acknowledged"] and same["evidence"]["records"]==2 and same["evidence"]["tokens"]==2200

def test_pricing_restoration_future_rate_failure_and_removal():
    repo,clock,prices,service=world();repo.gaps=[gap()];service.evaluate()
    prices.restored_from=NOW;prices.prices=("future",);clock[0]+=timedelta(seconds=1);service.evaluate()
    assert service.read()["current"]
    repo.fail_reads.add("FROM unpriced_usage_events");service.evaluate();assert only(service,"pricing")["status"]=="awaiting"
    repo.fail_reads.clear();prices.restored_from=NOW-timedelta(days=30);prices.prices=("historical",);clock[0]+=timedelta(seconds=1);service.evaluate()
    assert not service.read()["current"] and service.read()["history"][0]["reasonCode"]=="pricing_restored"

def test_pricing_recurrence_and_removed_evidence():
    repo,clock,prices,service=world();repo.gaps=[gap()];service.evaluate();first=only(service,'pricing')
    prices.prices=('restored',);prices.restored_from=NOW-timedelta(days=30);clock[0]+=timedelta(seconds=1);service.evaluate()
    assert not service.read()['current']
    prices.prices=('missing-again',);prices.restored_from=None;clock[0]+=timedelta(seconds=1);service.evaluate()
    assert only(service,'pricing')['id']!=first['id']
    repo.gaps=[];clock[0]+=timedelta(seconds=1);service.evaluate()
    assert service.read()['history'][0]['reasonCode']=='evidence_removed'

def test_pricing_batches_cache_canonical_counts_and_do_not_drop_unresolved():
    repo,clock,_,service=world();repo.gaps=[gap(i) for i in range(1,502)];service.evaluate()
    assert only(service,'pricing')['evidence']['records']==500 and not service.read()['evaluation']['pricingComplete']
    action(service,'acknowledge',only(service,'pricing'));clock[0]+=timedelta(seconds=30);service.evaluate()
    assert only(service,'pricing')['evidence']['records']==501 and only(service,'pricing')['acknowledged']
    count=len([q for q,_,_ in repo.statements if 'LEFT JOIN coverage_gap_events' in q])
    clock[0]+=timedelta(seconds=30);service.evaluate()
    assert len([q for q,_,_ in repo.statements if 'LEFT JOIN coverage_gap_events' in q])==count
    repo.gaps.append(gap(502));clock[0]+=timedelta(seconds=30);service.evaluate()
    assert only(service,'pricing')['evidence']['records']==502
    assert next(params[0] for q,params,_ in reversed(repo.statements) if 'LEFT JOIN coverage_gap_events' in q)==501

def test_changing_ingest_during_pricing_sweep_cannot_prove_recovery():
    repo,clock,prices,service=world();repo.gaps=[gap()];service.evaluate();original=only(service,'pricing')
    prices.prices=('restored',);prices.restored_from=NOW-timedelta(days=30)
    repo.gaps=[gap(i) for i in range(1,502)];clock[0]+=timedelta(seconds=1);service.evaluate()
    repo.attempts=[attempt('codex_local','success',when=clock[0],reason=None)]
    clock[0]+=timedelta(seconds=1);service.evaluate()
    assert only(service,'pricing')['id']==original['id'] and only(service,'pricing')['status']=='awaiting'
    assert not service.read()['history'] and not service.read()['evaluation']['pricingComplete']

def test_pricing_groups_sources_but_not_distinct_domains_and_excludes_identity():
    repo,_,_,service=world();repo.gaps=[gap(),gap(2,source='cursor_local'),gap(3,'other:missing'),gap(4,coverage_issue='identity_conflict'),gap(5,source='openai_admin')];service.evaluate()
    items=service.read()['current'];assert len(items)==2
    common=next(i for i in items if i['evidence']['model']=='vendor:missing')
    assert common['evidence']['records']==2 and common['evidence']['sources']==['codex_local','cursor_local']

def test_partial_rate_domain_counts_only_affected_events():
    repo,_,prices,service=world();prices.restored_from=NOW-timedelta(days=2)
    repo.gaps=[gap(),gap(2,occurred_at=rules.iso(NOW-timedelta(days=1)))];service.evaluate()
    item=only(service,'pricing');assert item['evidence']['records']==1 and item['evidence']['tokens']==1100

def test_used_only_binding_cannot_borrow_quota_permissions():
    repo,_,_,service=world();set_quota(repo,96)
    repo.meta['connections.v1']['bindings']['codex_local']={'enabled':True,'revision':1,'capabilities':['usage']};service.evaluate()
    assert not service.read()['badgeCount']

def test_read_projection_never_writes_and_unexpected_scope_access_fails(monkeypatch):
    repo,_,_,service=world();set_quota(repo,96);service.evaluate();before=copy.deepcopy(repo.meta)
    service.read();assert repo.meta==before
    monkeypatch.setattr('spend_app.codex_quota.current_scope',denied);service.evaluate()
    assert service.read()['evaluation']['status']=='error' and service.read()['badgeCount']==0

def test_atomic_race_conflicts_retries_and_failed_commit():
    repo,clock,prices,service=world();set_quota(repo,82);service.evaluate()
    before=copy.deepcopy(repo.meta);repo.fail_commit=True
    with pytest.raises(RuntimeError):action(service,"snooze",only(service),hours=1)
    assert repo.meta==before and not service.evaluate() and repo.meta==before
    repo.fail_commit=False
    stale=service.read()["revision"]
    repo.before_write=lambda:action(service,"acknowledge",only(service))
    service.evaluate();assert only(service)["acknowledged"]
    with pytest.raises(Conflict):service.update({"operation":"acknowledge","id":only(service)["id"],"expectedRevision":stale,"requestId":str(uuid4())})
    other=Service(MemoryRepository(),prices,"UTC",lambda:clock[0]);assert other.read()["badgeCount"]==0

def test_persistence_error_is_visible_without_overwriting_incident_progress(monkeypatch):
    repo,_,_,service=world();set_quota(repo,82);service.evaluate();before=copy.deepcopy(repo.meta[KEY])
    monkeypatch.setattr('spend_app.attention_store.MAX_STATE_BYTES',1)
    assert not service.evaluate() and repo.meta[KEY]==before
    assert service.read()['evaluation']['status']=='error' and service.read()['badgeCount']==0

def test_request_identity_cannot_be_reused_for_another_mutation():
    repo,_,_,service=world();set_quota(repo,82);service.evaluate()
    _,body=action(service,'snooze',only(service),hours=1)
    with pytest.raises(Conflict):service.update({**body,'hours':24})

def test_replayed_failure_does_not_recur_after_history_pruning():
    state=rules.initial()
    from spend_app.attention_store import fact
    remembered=None
    for i in range(102):
        when=NOW+timedelta(seconds=i*2)
        f=fact('source','same','legacy',when,rules.iso(when),eligibility='eligible',condition='breach',reasonCode='source_failed')
        state=rules.reduce(state,[f],when)
        if i==0:remembered=f
        state=rules.reduce(state,[{**f,'condition':'clear','reasonCode':'recovered','observedAt':rules.iso(when+timedelta(seconds=1))}],when+timedelta(seconds=1))
    assert len(state['history'])==100
    state=rules.reduce(state,[remembered],NOW+timedelta(minutes=10))
    assert not state['current'] and state['watermarks'][remembered['key']]['serial']==102

def test_preferences_validation_disable_and_reenable_old_snapshot():
    repo,_,_,service=world();set_quota(repo,82);service.evaluate()
    prefs=dict(rules.DEFAULTS,quota=False);action(service,"preferences",preferences=prefs)
    assert service.read()["history"][0]["reasonCode"]=="rule_disabled"
    action(service,"preferences",preferences=dict(rules.DEFAULTS));service.evaluate()
    assert service.read()["badgeCount"]==0
    for lower,higher in ((0,95),(95,80),(80,101),(True,95)):
        with pytest.raises(ValueError):action(service,"preferences",preferences=dict(rules.DEFAULTS,lower=lower,higher=higher))

def test_history_pruning_retains_replay_watermarks_and_unresolved():
    state=rules.initial()
    for i in range(110):
        at=NOW+timedelta(seconds=i*2)
        from spend_app.attention_store import fact
        f=fact("source",str(i),"legacy",at,rules.iso(at),eligibility="eligible",condition="breach",reasonCode="source_failed")
        state=rules.reduce(state,[f],at)
        state=rules.reduce(state,[{**f,"observedAt":rules.iso(at+timedelta(seconds=1)),"condition":"clear","reasonCode":"recovered"}],at+timedelta(seconds=1))
    assert len(state["history"])==100 and len(state["watermarks"])==110
    replay=rules.reduce(state,[f],NOW+timedelta(minutes=5));assert not replay["current"]

def test_fake_scheduler_registration_never_starts_collectors():
    jobs=[]
    fake=SimpleNamespace(add_job=lambda *a,**k:jobs.append((a,k)))
    register(fake,SimpleNamespace(database_path=Path("C:/fake/attention.db"),timezone="UTC"),Prices())
    assert len(jobs)==1 and jobs[0][1]=={"seconds":30,"id":"attention","coalesce":True,"max_instances":1}

def test_existing_scheduler_uses_fake_registration_only(monkeypatch):
    import ast
    import io
    from datetime import datetime, UTC
    jobs=[]
    fake=SimpleNamespace(add_job=lambda *a,**k:jobs.append((a,k)),start=denied)
    with io.open(Path(__file__).parents[1]/'spend_app/scheduler.py',encoding='utf-8') as file:
        tree=ast.parse(file.read())
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='create_scheduler')
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),function],type_ignores=[])
    namespace={'BackgroundScheduler':lambda **_:fake,'datetime':datetime,'UTC':UTC,'timedelta':timedelta,'poll_quotas':denied,'poll_activity':denied}
    exec(compile(ast.fix_missing_locations(module),'scheduler-registration','exec'),namespace)
    settings=SimpleNamespace(database_path=Path('C:/fake/attention.db'),timezone='UTC',local_ingest_interval_seconds=15,admin_ingest_interval_minutes=15,quota_poll_seconds=15,activity_poll_seconds=4)
    assert namespace['create_scheduler'](settings,Prices()) is fake
    attention=[job for job in jobs if job[1]['id']=='attention']
    assert len(attention)==1 and len(jobs)==6
    assert attention[0][1]['coalesce'] and attention[0][1]['max_instances']==1

def drive(coroutine):
    try:coroutine.send(None)
    except StopIteration as done:return done.value
    raise AssertionError("Unexpected I/O await in fake request")

def test_api_boundaries_without_application_or_transport(monkeypatch):
    from starlette.requests import Request
    repo,_,_,service=world()
    async def inline(fn,*args):return fn(*args)
    monkeypatch.setattr("spend_app.attention_api.run_in_threadpool",inline)
    routes=attention_router(SimpleNamespace(),None,service=service,token="fixture-token").routes
    read,write=routes[0].endpoint,routes[1].endpoint
    def request(body=b'{}',auth=True,origin="http://localhost",extra=()):
        headers=[(b'host',b'localhost'),(b'origin',origin.encode()),(b'content-type',b'application/json'),(b'x-burnrate-request',b'1'),*extra]
        if auth:headers.append((b'authorization',b'Bearer fixture-token'))
        async def receive():return {"type":"http.request","body":body,"more_body":False}
        return Request({"type":"http","path":"/api/attention","scheme":"http","method":"POST","headers":headers},receive)
    assert read(request(auth=False)).status_code==401
    assert drive(write(request(auth=False))).status_code==401
    assert drive(write(request(origin="https://evil.test"))).status_code==403
    assert drive(write(request(b'x'*17000))).status_code==422
    assert drive(write(request(b'{"operation":"preferences"}'))).status_code==422
    before=copy.deepcopy(repo.meta);assert read(request()).status_code==200 and repo.meta==before

def response_fixture():
    repo,clock,_,service=world();set_quota(repo,82);repo.attempts=[attempt()];repo.gaps=[gap()]
    with patch("spend_app.codex_quota.current_scope",lambda:SCOPE):
        service.evaluate();action(service,"acknowledge",only(service));action(service,"snooze",only(service,"pricing"),hours=1)
        clock[0]+=timedelta(seconds=10);repo.attempts=[attempt(status="success",when=clock[0],reason=None)];service.evaluate()
        clock[0]+=timedelta(seconds=10);repo.attempts=[attempt(when=clock[0])];service.evaluate()
        return service.read()

def test_shared_renderer_fixture():
    # Path reads are allowed only for this checked-in synthetic fixture.
    import io
    with io.open(Path(__file__).parent/'fixtures/attention_response.json',encoding='utf-8') as file:
        assert response_fixture()==json.load(file)
