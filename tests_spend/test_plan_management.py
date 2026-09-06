from datetime import date, datetime, UTC
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from spend_app.db import initialize, connect, _initialize_base
from spend_app.plan_service import apply_mutation, list_plans, PlanError, PlanConflict, effective_terms
from spend_app.subscriptions import materialize_subscription_days, daily_cost
from spend_app.aggregate import _subscription_cost, _range_fingerprint
from spend_app.api import create_app
from tests_spend.test_api import make_settings

TODAY = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def empty_configured_plans(monkeypatch):
    # Shared fixtures remain synthetic even in the private legacy wrapper.
    monkeypatch.setattr("spend_app.subscriptions.SUBSCRIPTION_SEEDS", ())


def values(**changes):
    return dict(name="Example plan", tool_key="codex", amount_usd="100.00", cadence="monthly",
                start_date="2026-09-01", end_date=None, **{}) | changes


def mutate(db, operation, **fields):
    body = dict(operation=operation, request_id=uuid.uuid4().hex, **fields)
    with connect(db) as c:
        c.execute("BEGIN IMMEDIATE")
        return apply_mutation(c, body, TODAY)


def test_effective_history_calendar_cost_and_end_reconciliation(tmp_path):
    db = tmp_path / "plans.db"; initialize(db)
    plan = mutate(db, "add", values=values())
    with connect(db) as c:
        materialize_subscription_days(c, start=TODAY, end=date(2026, 9, 30))
    mutate(db, "schedule", plan_id=plan["id"], expected_version=1,
           effective_date="2026-09-15", values={"amount_usd":"150", "cadence":"monthly"})
    mutate(db, "end", plan_id=plan["id"], expected_version=2, end_date="2026-09-20")
    with connect(db) as c:
        a, b = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        actual, _ = _subscription_cost(c, a, b, "UTC")
        expected = Decimal(100) * 14 / 30 + Decimal(150) * 6 / 30
        assert abs(actual - expected) < Decimal("1e-24")
        daily = c.execute("SELECT date,cost_usd FROM subscription_daily_costs ORDER BY date").fetchall()
        assert len(daily) == 20 and daily[-1]["date"] == "2026-09-20"
        assert abs(sum(Decimal(str(r["cost_usd"])) for r in daily) - expected) < Decimal("1e-12")
        before = [tuple(r) for r in daily]
        materialize_subscription_days(c, start=TODAY, end=date(2026, 9, 30))
        assert before == [tuple(r) for r in c.execute("SELECT date,cost_usd FROM subscription_daily_costs ORDER BY date")]
        terms = list_plans(c, date(2026, 9, 22))["plans"][0]["terms"]
        assert terms[0]["amount_usd"] == "100.0" and terms[0]["end_date"] == "2026-09-14"
        assert all(t["status"] == "ended" for t in terms)


@pytest.mark.parametrize("day,cadence,days", [(date(2024,2,29),"monthly",29),(date(2024,2,29),"quarterly",91),
    (date(2024,12,31),"annual",366),(date(2025,1,1),"annual",365),(date(2026,10,1),"quarterly",92)])
def test_calendar_boundaries(day, cadence, days):
    assert daily_cost("100.01", cadence, day) == Decimal("100.01") / days


def test_multiple_shared_plans_and_dst_day(tmp_path):
    db=tmp_path/"s.db"; initialize(db)
    mutate(db,"add",values=values(tool_key="opencode",start_date="2026-01-01"))
    mutate(db,"add",values=values(tool_key="zcode",start_date="2026-01-01"))
    with connect(db) as c:
        from zoneinfo import ZoneInfo
        zone=ZoneInfo("America/New_York")
        start=datetime(2026,3,8,tzinfo=zone).astimezone(UTC)
        end=datetime(2026,3,9,tzinfo=zone).astimezone(UTC)
        total, by_tool=_subscription_cost(c,start,end,"America/New_York")
        assert abs(total-Decimal(200)/31)<Decimal("1e-24")
        assert set(by_tool)=={"opencode"}  # Two real plans, one shared association.


def test_schedule_then_end_before_scheduled_term_preserves_history(tmp_path):
    db=tmp_path/"s.db"; initialize(db)
    p=mutate(db,"add",values=values())
    mutate(db,"schedule",plan_id=p["id"],expected_version=1,effective_date="2026-10-01",values={"amount_usd":"150","cadence":"annual"})
    mutate(db,"end",plan_id=p["id"],expected_version=2,end_date="2026-09-20")
    with connect(db) as c:
        assert len(list_plans(c,TODAY)["plans"][0]["terms"])==2
        assert len(effective_terms(c))==1
    mutate(db,"end",plan_id=p["id"],expected_version=3,end_date="2026-10-20")
    with connect(db) as c:
        assert len(effective_terms(c))==2


def test_correction_preview_confirmation_conflict_and_retry(tmp_path):
    db=tmp_path/"s.db"; initialize(db)
    p=mutate(db,"add",values=values())
    with connect(db) as c:
        term=list_plans(c,TODAY)["plans"][0]["terms"][0]
        before=_range_fingerprint(c,datetime(2026,9,1,tzinfo=UTC),datetime(2026,10,1,tzinfo=UTC),"all")
    args=dict(plan_id=p["id"],expected_version=1,term_id=term["id"],values=values(amount_usd="90"))
    with pytest.raises(PlanError,match="Preview"):
        mutate(db,"correct",**args)
    preview=mutate(db,"correct",preview=True,**args)
    assert Decimal(preview["after_usd"]) < Decimal(preview["before_usd"])
    body=dict(operation="correct",request_id="repeatable-123",confirmed=True,preview_token=preview["preview_token"],**args)
    with connect(db) as c:
        c.execute("BEGIN IMMEDIATE")
        first=apply_mutation(c,body,TODAY)
    with connect(db) as c:
        assert apply_mutation(c,body,TODAY)==first
        after=_range_fingerprint(c,datetime(2026,9,1,tzinfo=UTC),datetime(2026,10,1,tzinfo=UTC),"all")
        assert after!=before
    with pytest.raises(PlanConflict):
        mutate(db,"end",plan_id=p["id"],expected_version=1,end_date=None)


@pytest.mark.parametrize("amount", ["NaN","Infinity","-1","1000001","1.234",True,None])
def test_invalid_money_rolls_back(tmp_path,amount):
    db=tmp_path/"s.db"; initialize(db)
    with pytest.raises((ValueError,TypeError)):
        mutate(db,"add",values=values(amount_usd=amount))
    with connect(db) as c:
        assert list_plans(c,TODAY)["plans"]==[]


def test_actual_v9_upgrade_backup_repeatability_and_failure(tmp_path,monkeypatch):
    db=tmp_path/"v9.db"
    _initialize_base(db)
    with connect(db) as c:
        c.execute("UPDATE app_meta SET value='9' WHERE key='schema_version'")
        c.execute("INSERT INTO subscriptions(name,tool_key,amount_usd,cadence,start_date,end_date) VALUES('Legacy','codex',100,'monthly','2026-09-01','2026-09-20')")
    import spend_app.db as module
    original=module.backup_database
    monkeypatch.setattr(module,"backup_database",lambda *a: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(RuntimeError,match="backup failed"): initialize(db)
    with sqlite3.connect(db) as c:
        assert "plan_id" not in {r[1] for r in c.execute("PRAGMA table_info(subscriptions)")}
        assert c.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]=="9"
    monkeypatch.setattr(module,"backup_database",original)
    initialize(db); initialize(db)
    with connect(db) as c:
        p=list_plans(c,TODAY)["plans"][0]
        assert p["id"]==1 and p["end_date"]=="2026-09-20" and len(p["terms"])==1
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    assert len(list((tmp_path/"backups").glob("*.db")))==1


def test_current_terms_do_not_reenter_legacy_normalization(tmp_path,monkeypatch):
    db=tmp_path/"current.db"; initialize(db)
    import spend_app.db as module
    monkeypatch.setattr(module,"_initialize_base",lambda p: (_ for _ in ()).throw(AssertionError("legacy normalization called")))
    initialize(db)


def test_api_write_boundaries_and_safe_reads(tmp_path,monkeypatch):
    db=tmp_path/"http.db"
    app=create_app(make_settings(db),enable_scheduler=False)
    client=TestClient(app,base_url="http://localhost")
    headers={"Origin":"http://localhost","X-BURNRATE-Request":"1"}
    body={"operation":"add","request_id":"request-add-123","values":values(name="<img src=x onerror=alert(1)>")}
    assert client.post("/api/subscriptions",json=body).status_code==403
    assert client.post("/api/subscriptions",headers=headers|{"Origin":"http://evil.example"},json=body).status_code==403
    assert client.post("/api/subscriptions",headers=headers|{"Host":"evil.example"},json=body).status_code==403
    assert client.post("/api/subscriptions",headers=headers,data="x").status_code==415
    assert client.post("/api/subscriptions",headers=headers,json=body|{"secret":"no"}).status_code==422
    first=client.post("/api/subscriptions",headers=headers,json=body)
    assert first.status_code==200
    assert client.post("/api/subscriptions",headers=headers,json=body).json()==first.json()
    assert len(client.get("/api/subscriptions").json()["plans"])==1
    assert client.get("/api/subscriptions").json()["plans"][0]["terms"][0]["name"]==body["values"]["name"]
