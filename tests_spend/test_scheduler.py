import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.config import (
    ACTIVITY_POLL_SECONDS,
    ADMIN_INGEST_INTERVAL_MINUTES,
    LOCAL_INGEST_INTERVAL_SECONDS,
    QUOTA_POLL_SECONDS,
    Settings,
)
from spend_app.pricing import PricingEngine
from spend_app.providers import PROVIDERS, ProviderSpec
from spend_app.quotas import poll_activity, poll_quotas
from spend_app.scheduler import create_scheduler


ROOT = Path(__file__).resolve().parents[1]

LOCAL_INGEST_JOB_IDS = ("local-ingest",)
INGEST_JOB_IDS = (*LOCAL_INGEST_JOB_IDS, "provider-admin")
QUOTA_JOB_ID = "quota-poll"
ACTIVITY_JOB_ID = "activity"
SCHEDULER_JOB_IDS = (
    "local-ingest",
    "provider-admin",
    "subscriptions",
    "quota-poll",
    "activity",
)
LOCAL_INGEST_ALIASES = (
    "codex",
    "claude",
    "traycer",
    "cursor",
    "opencode",
    "zcode",
    "grok",
    "antigravity",
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        cursor_import_path=tmp_path / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40_000,
    )


def make_scheduler(tmp_path: Path):
    return create_scheduler(make_settings(tmp_path), PricingEngine.load(ROOT / "pricing"))


def test_local_ingest_is_near_real_time_and_admin_jobs_stay_rate_limited(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == set(SCHEDULER_JOB_IDS)
    intervals = {job_id: int(job.trigger.interval.total_seconds()) for job_id, job in jobs.items()}
    for job_id in LOCAL_INGEST_JOB_IDS:
        assert intervals[job_id] == LOCAL_INGEST_INTERVAL_SECONDS
        assert jobs[job_id].misfire_grace_time >= LOCAL_INGEST_INTERVAL_SECONDS
    assert intervals["provider-admin"] == ADMIN_INGEST_INTERVAL_MINUTES * 60
    assert intervals["subscriptions"] == ADMIN_INGEST_INTERVAL_MINUTES * 60
    assert LOCAL_INGEST_INTERVAL_SECONDS == 15
    assert ADMIN_INGEST_INTERVAL_MINUTES == 15
    assert "cursor-csv" not in jobs


def test_quota_and_activity_jobs_are_independent_interval_jobs(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    quota = jobs[QUOTA_JOB_ID]
    activity = jobs[ACTIVITY_JOB_ID]
    assert int(quota.trigger.interval.total_seconds()) == QUOTA_POLL_SECONDS
    assert int(activity.trigger.interval.total_seconds()) == ACTIVITY_POLL_SECONDS
    assert QUOTA_POLL_SECONDS == 15
    assert ACTIVITY_POLL_SECONDS == 4
    assert quota.max_instances == 1
    assert activity.max_instances == 1
    assert quota.coalesce is True
    assert activity.coalesce is True
    assert quota.func is poll_quotas
    assert activity.func is poll_activity
    database = tmp_path / "spend.db"
    assert quota.kwargs == {"database_path": database}
    assert activity.kwargs == {"database_path": database}
    assert quota.id != activity.id
    for job_id in LOCAL_INGEST_JOB_IDS:
        job = jobs[job_id]
        assert job.max_instances == 1
        assert job.coalesce is True
        assert int(job.trigger.interval.total_seconds()) == LOCAL_INGEST_INTERVAL_SECONDS


def test_create_scheduler_does_not_invoke_pollers(tmp_path: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("poller must not run during scheduler construction")

    monkeypatch.setattr("spend_app.scheduler.poll_quotas", boom)
    monkeypatch.setattr("spend_app.scheduler.poll_activity", boom)
    make_scheduler(tmp_path)


def test_local_ingest_job_runs_readers_serially(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def recorder(name):
        def run(**_kwargs):
            calls.append(name)
            return {"status": "success"}

        return run

    for name in LOCAL_INGEST_ALIASES:
        monkeypatch.setattr(
            f"spend_app.scheduler.ingest_{name}_local",
            recorder(name),
        )
    monkeypatch.setattr("spend_app.scheduler.ingest_cursor_usage", recorder("cursor-usage"))
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    jobs["local-ingest"].func()
    assert calls == [
        "codex", "claude", "traycer", "cursor", "cursor-usage", "opencode", "zcode", "grok", "antigravity"
    ]


def test_experimental_provider_failure_is_isolated(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def recorder(name, *, fail=False):
        def run(**_kwargs):
            calls.append(name)
            if fail:
                raise RuntimeError(f"{name} exploded")
            return {"status": "success"}

        return run

    for name in LOCAL_INGEST_ALIASES:
        monkeypatch.setattr(
            f"spend_app.scheduler.ingest_{name}_local",
            recorder(name, fail=(name == "grok")),
        )
    monkeypatch.setattr("spend_app.scheduler.ingest_cursor_usage", recorder("cursor-usage"))

    def boom(**_kwargs):
        calls.append("injected")
        raise RuntimeError("experimental boom")

    extra = ProviderSpec(
        key="requesty_local",
        ingest_import="tests:boom",
        capabilities=frozenset({"usage"}),
        stability="experimental",
        exactness="unavailable",
        enabled_by_default=False,
        ingest=boom,
        kind="local",
    )
    traycer_at = next(index for index, spec in enumerate(PROVIDERS) if spec.key == "traycer_local")
    monkeypatch.setattr(
        "spend_app.providers.PROVIDERS",
        PROVIDERS[: traycer_at + 1] + (extra,) + PROVIDERS[traycer_at + 1 :],
    )
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    jobs["local-ingest"].func()
    assert calls == [
        "codex",
        "claude",
        "traycer",
        "injected",
        "cursor",
        "cursor-usage",
        "opencode",
        "zcode",
        "grok",
        "antigravity",
    ]


def test_official_provider_failure_still_runs_remaining(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def recorder(name, *, fail=False):
        def run(**_kwargs):
            calls.append(name)
            if fail:
                raise RuntimeError(f"{name} exploded")
            return {"status": "success"}

        return run

    for name in LOCAL_INGEST_ALIASES:
        monkeypatch.setattr(
            f"spend_app.scheduler.ingest_{name}_local",
            recorder(name, fail=(name == "claude")),
        )
    monkeypatch.setattr("spend_app.scheduler.ingest_cursor_usage", recorder("cursor-usage"))
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    with pytest.raises(RuntimeError, match="local ingest job"):
        jobs["local-ingest"].func()
    assert calls == [
        "codex",
        "claude",
        "traycer",
        "cursor",
        "cursor-usage",
        "opencode",
        "zcode",
        "grok",
        "antigravity",
    ]


def test_local_ingest_follows_registry_and_keeps_adapter_signatures() -> None:
    from spend_app.adapters import antigravity_local, claude_local, codex_local, grok_local
    from spend_app.providers import CAPABILITIES, EXACTNESS, REGISTRY, STABILITIES

    assert [spec.key for spec in REGISTRY.local_ingest()] == [
        "codex_local",
        "claude_local",
        "traycer_local",
        "cursor_local",
        "cursor_usage_service",
        "opencode_local",
        "zcode_local",
        "grok_local",
        "antigravity_local",
    ]
    assert [spec.key for spec in REGISTRY.admin_ingest()] == [
        "openai_admin",
        "anthropic_admin",
        "cursor_admin",
    ]
    assert REGISTRY.get("cursor_csv") is not None
    assert REGISTRY.get("cursor_csv").kind == "manual"
    assert CAPABILITIES == frozenset({"usage", "quota", "activity", "admin"})
    assert STABILITIES == frozenset({"official", "experimental"})
    assert EXACTNESS == frozenset({"exact", "derived", "partial", "unavailable"})
    for module, key in (
        (codex_local, "codex_local"),
        (claude_local, "claude_local"),
        (grok_local, "grok_local"),
        (antigravity_local, "antigravity_local"),
    ):
        spec = REGISTRY.get(key)
        assert spec is not None
        assert spec.ingest is module.ingest
        assert inspect.signature(spec.ingest) == inspect.signature(module.ingest)


def test_scheduled_poller_jobs_pass_database_path(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_quotas(**kwargs):
        calls.append(("quota", kwargs))
        return {"written": 0}

    def fake_activity(**kwargs):
        calls.append(("activity", kwargs))
        return {"seen": 0}

    monkeypatch.setattr("spend_app.scheduler.poll_quotas", fake_quotas)
    monkeypatch.setattr("spend_app.scheduler.poll_activity", fake_activity)
    scheduler = make_scheduler(tmp_path)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert calls == []
    jobs[QUOTA_JOB_ID].func(**jobs[QUOTA_JOB_ID].kwargs)
    jobs[ACTIVITY_JOB_ID].func(**jobs[ACTIVITY_JOB_ID].kwargs)
    database = tmp_path / "spend.db"
    assert calls == [
        ("quota", {"database_path": database}),
        ("activity", {"database_path": database}),
    ]


def test_disabled_scheduler_does_not_call_pollers(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_quotas(**_kwargs):
        calls.append("quota")
        return {}

    def fake_activity(**_kwargs):
        calls.append("activity")
        return {}

    monkeypatch.setattr("spend_app.scheduler.poll_quotas", fake_quotas)
    monkeypatch.setattr("spend_app.scheduler.poll_activity", fake_activity)
    client = TestClient(create_app(make_settings(tmp_path), enable_scheduler=False))
    assert client.get("/api/spend/summary").status_code == 200
    assert calls == []
