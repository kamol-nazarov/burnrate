from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from spend_app.aggregate import aggregate_summary_cached, data_clock, recently_requested_summaries
from spend_app.config import Settings
from spend_app.db import connect, initialize, prune_ingest_runs
from spend_app.pricing import PricingEngine
from spend_app.providers import (
    ProviderSpec,
    ingest_anthropic_admin,
    ingest_antigravity_local,
    ingest_claude_local,
    ingest_codex_local,
    ingest_cursor_admin,
    ingest_cursor_csv,
    ingest_cursor_local,
    ingest_cursor_usage,
    ingest_grok_local,
    ingest_opencode_local,
    ingest_openai_admin,
    ingest_traycer_local,
    ingest_zcode_local,
    iter_admin_ingest,
    iter_local_ingest,
)
from spend_app.quotas import poll_activity, poll_quotas
from spend_app.subscriptions import materialize_subscription_days

# Re-exported so existing tests can monkeypatch scheduler.ingest_* aliases.
# The local/admin loops resolve these at call time via scheduler_alias.
_ = (
    ingest_anthropic_admin,
    ingest_antigravity_local,
    ingest_claude_local,
    ingest_codex_local,
    ingest_cursor_admin,
    ingest_cursor_csv,
    ingest_cursor_local,
    ingest_cursor_usage,
    ingest_grok_local,
    ingest_opencode_local,
    ingest_openai_admin,
    ingest_traycer_local,
    ingest_zcode_local,
)


def _ingest_callable(spec: ProviderSpec):
    alias = spec.scheduler_alias
    if alias:
        fn = globals().get(alias)
        if fn is not None:
            return fn
    return spec.ingest


def _run_ingest_specs(jobs: list[tuple[ProviderSpec, dict]]) -> None:
    errors: list[BaseException] = []
    for spec, kwargs in jobs:
        ingest = _ingest_callable(spec)
        if ingest is None:
            continue
        try:
            ingest(**kwargs)
        except Exception as exc:
            if spec.stability == "experimental":
                continue
            errors.append(exc)
    if errors:
        raise RuntimeError(f"{len(errors)} ingest job(s) failed") from errors[0]


def create_scheduler(settings: Settings, pricing: PricingEngine) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    local_ingest_interval = {
        "seconds": settings.local_ingest_interval_seconds,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": max(5, settings.local_ingest_interval_seconds * 2),
    }

    def local_ingest_jobs() -> None:
        errors: list[BaseException] = []
        try:
            _run_ingest_specs(list(iter_local_ingest(settings, pricing)))
        except Exception as exc:
            errors.append(exc)
        try:
            with connect(settings.database_path) as connection:
                prune_ingest_runs(connection)
        except Exception as exc:
            errors.append(exc)
        warm_summaries()
        if errors:
            raise RuntimeError(f"{len(errors)} local ingest job(s) failed") from errors[0]

    def warm_summaries() -> None:
        # Compute the summaries viewers are currently watching right after the
        # cycle that could have changed them, so their next poll is a memo hit.
        pending = recently_requested_summaries()
        if not pending:
            return
        clock = data_clock(settings.database_path, settings.local_ingest_interval_seconds)
        for window_key, tool in pending:
            try:
                aggregate_summary_cached(
                    database_path=settings.database_path,
                    pricing=pricing,
                    window_key=window_key,
                    tool=tool,
                    timezone=settings.timezone,
                    cache_threshold=settings.cache_hit_threshold,
                    cadence_seconds=settings.local_ingest_interval_seconds,
                    now=clock,
                )
            except Exception:
                continue

    scheduler.add_job(
        local_ingest_jobs,
        "interval",
        **local_ingest_interval,
        id="local-ingest",
        next_run_time=datetime.now(UTC) + timedelta(seconds=1),
    )

    def admin_jobs() -> None:
        end = datetime.now(UTC)
        start = end - timedelta(hours=2)
        _run_ingest_specs(list(iter_admin_ingest(settings, pricing, start=start, end=end)))

    scheduler.add_job(
        admin_jobs,
        "interval",
        minutes=settings.admin_ingest_interval_minutes,
        id="provider-admin",
        coalesce=True,
        max_instances=1,
    )

    def subscription_job() -> None:
        initialize(settings.database_path)
        today = date.today()
        with connect(settings.database_path) as connection:
            materialize_subscription_days(
                connection,
                start=today.replace(day=1),
                end=today + timedelta(days=40),
            )

    scheduler.add_job(
        subscription_job,
        "interval",
        minutes=settings.admin_ingest_interval_minutes,
        id="subscriptions",
        coalesce=True,
        max_instances=1,
    )

    poller_opts = {"coalesce": True, "max_instances": 1}
    scheduler.add_job(
        poll_quotas,
        "interval",
        seconds=settings.quota_poll_seconds,
        kwargs={"database_path": settings.database_path},
        id="quota-poll",
        next_run_time=datetime.now(UTC) + timedelta(seconds=3),
        **poller_opts,
    )
    scheduler.add_job(
        poll_activity,
        "interval",
        seconds=settings.activity_poll_seconds,
        kwargs={"database_path": settings.database_path},
        id="activity",
        next_run_time=datetime.now(UTC) + timedelta(seconds=1),
        **poller_opts,
    )
    return scheduler
