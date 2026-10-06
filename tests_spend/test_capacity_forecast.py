"""Run-dry pace and one reset alert per quota window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.capacity_forecast import (
    apply_forecasts,
    dismiss_alert,
    evaluate_alerts,
    in_quiet_hours,
    project_pace,
    record_pace_samples,
    save_preferences,
    stale_after_seconds,
    suggestion_line,
)
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_quota, upsert_usage_event
from spend_app.pricing import PricingEngine
from spend_app.aggregate import aggregate_summary
from spend_app.quotas import QuotaSample, poll_quotas

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pace(provider, limit, pct, when, resets):
    return {
        "provider_key": provider,
        "limit_key": limit,
        "pct": pct,
        "sampled_at": iso(when),
        "resets_at": resets,
    }


def project(samples, *, pct, resets, stale_after, now=NOW):
    return project_pace(samples, now=now, resets_at=resets, pct=pct, stale_after=stale_after)


def test_safe_tight_dry_and_flat_rate():
    resets = iso(NOW + timedelta(days=7))
    soon = iso(NOW + timedelta(hours=18))
    flat = [
        pace("codex", "weekly", 40, NOW - timedelta(minutes=40), resets),
        pace("codex", "weekly", 40, NOW - timedelta(minutes=5), resets),
    ]
    assert project(flat, pct=40, resets=resets, stale_after=300)["state"] == "safe"
    assert project(flat, pct=40, resets=resets, stale_after=300)["line"] == "pace → comfortable"

    tight_reset = soon
    tight = [
        pace("codex", "weekly", 89.5, NOW - timedelta(minutes=30), tight_reset),
        pace("codex", "weekly", 90, NOW - timedelta(minutes=1), tight_reset),
    ]
    # Half a point in 29 minutes leaves about 10 hours, inside the last 12 hours of an 18-hour reset.
    tight_forecast = project(tight, pct=90, resets=tight_reset, stale_after=300)
    assert tight_forecast["state"] == "tight"
    assert "before reset" in tight_forecast["line"]

    dry_reset = iso(NOW + timedelta(days=4))
    dry = [
        pace("grok", "weekly", 50, NOW - timedelta(minutes=30), dry_reset),
        pace("grok", "weekly", 60, NOW - timedelta(minutes=1), dry_reset),
    ]
    dry_forecast = project(dry, pct=60, resets=dry_reset, stale_after=300)
    assert dry_forecast["state"] == "dry"
    assert dry_forecast["line"].startswith("pace → dry in ~")
    assert dry_forecast["hoursRemaining"] == pytest.approx(1.9333, rel=0.05)


def test_zero_rate_does_not_divide_by_zero():
    resets = iso(NOW + timedelta(days=2))
    samples = [
        pace("codex", "weekly", 50, NOW - timedelta(minutes=30), resets),
        pace("codex", "weekly", 50, NOW - timedelta(minutes=1), resets),
    ]
    forecast = project(samples, pct=50, resets=resets, stale_after=300)
    assert forecast["state"] == "safe"
    assert forecast["projectedRunDry"] is None


def test_unknown_reset_and_single_sample_have_no_projection():
    assert project_pace([], now=NOW, resets_at=None, pct=90, stale_after=300) is None
    resets = iso(NOW + timedelta(days=2))
    assert project([pace("codex", "weekly", 90, NOW - timedelta(minutes=1), resets)], pct=90, resets=resets, stale_after=300) is None


def test_reset_inside_the_window_drops_the_previous_cycle():
    old = iso(NOW - timedelta(hours=1))
    new = iso(NOW + timedelta(days=5))
    samples = [
        pace("codex", "weekly", 80, NOW - timedelta(minutes=50), old),
        pace("codex", "weekly", 90, NOW - timedelta(minutes=40), old),
        pace("codex", "weekly", 10, NOW - timedelta(minutes=20), new),
        pace("codex", "weekly", 12, NOW - timedelta(minutes=5), new),
    ]
    forecast = project(samples, pct=12, resets=new, stale_after=300)
    assert forecast["burnPctPerHour"] == pytest.approx(8.0, rel=0.02)
    assert forecast["state"] == "dry"


def test_idle_hour_falls_back_to_24h():
    resets = iso(NOW + timedelta(days=7))
    samples = [
        pace("codex", "weekly", 20, NOW - timedelta(hours=20), resets),
        pace("codex", "weekly", 50, NOW - timedelta(minutes=50), resets),
        pace("codex", "weekly", 50, NOW - timedelta(minutes=5), resets),
    ]
    forecast = project(samples, pct=50, resets=resets, stale_after=300)
    assert forecast["state"] == "dry"
    assert forecast["burnPctPerHour"] == pytest.approx(1.5, rel=0.03)


def test_stale_codex_is_five_minutes_and_cursor_is_thirty_when_active():
    assert stale_after_seconds("codex", active=True) == 300
    assert stale_after_seconds("cursor", active=True) == 1800
    resets = iso(NOW + timedelta(days=4))
    codex = [
        pace("codex", "weekly", 70, NOW - timedelta(minutes=10), resets),
        pace("codex", "weekly", 80, NOW - timedelta(minutes=6), resets),
    ]
    assert project(codex, pct=80, resets=resets, stale_after=stale_after_seconds("codex", active=True))["state"] == "stale"
    fresh = [
        pace("codex", "weekly", 70, NOW - timedelta(minutes=8), resets),
        pace("codex", "weekly", 80, NOW - timedelta(minutes=4), resets),
    ]
    assert project(fresh, pct=80, resets=resets, stale_after=300)["state"] != "stale"
    cursor_stale = [
        pace("cursor", "cursor_models", 40, NOW - timedelta(minutes=50), resets),
        pace("cursor", "cursor_models", 55, NOW - timedelta(minutes=31), resets),
    ]
    assert project(cursor_stale, pct=55, resets=resets, stale_after=stale_after_seconds("cursor", active=True))["state"] == "stale"
    cursor_fresh = [
        pace("cursor", "cursor_models", 40, NOW - timedelta(minutes=40), resets),
        pace("cursor", "cursor_models", 55, NOW - timedelta(minutes=20), resets),
    ]
    assert project(cursor_fresh, pct=55, resets=resets, stale_after=1800)["state"] == "dry"


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(datetime(2026, 10, 5, 21, 59, tzinfo=UTC), "UTC", "22:00", "07:00") is False
    assert in_quiet_hours(datetime(2026, 10, 5, 22, 0, tzinfo=UTC), "UTC", "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 10, 5, 3, 0, tzinfo=UTC), "UTC", "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 10, 5, 6, 59, tzinfo=UTC), "UTC", "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 10, 5, 7, 0, tzinfo=UTC), "UTC", "22:00", "07:00") is False


def test_suggestion_names_a_measured_model_or_eases_off():
    resets = iso(NOW + timedelta(days=4))
    assert suggestion_line(
        provider_name="Codex", resets_at=resets, timezone="UTC", destination_name="Codex Sol"
    ) == "Shift heavy work to Codex Sol until Friday."
    assert suggestion_line(
        provider_name="Grok Build", resets_at=resets, timezone="UTC", destination_name=None
    ) == "Ease off Grok Build until Friday."


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "spend.db"
    initialize(database)
    return database


def _quota(connection, *, provider, pct, resets_at, limit="weekly", label="Weekly", polled_at=None):
    upsert_quota(
        connection,
        provider_key=provider,
        limit_key=limit,
        label=label,
        unit="percent",
        source="test",
        polled_at=polled_at or iso(NOW),
        pct=pct,
        resets_at=resets_at,
        is_payg=False,
    )


def _samples(connection, rows):
    connection.execute("DELETE FROM quota_pace_samples")
    for row in rows:
        connection.execute(
            """
            INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["provider_key"], row["limit_key"], row["pct"], row["resets_at"], row["sampled_at"]),
        )


def test_fast_burn_sorts_ahead_of_a_slower_higher_percent(tmp_path: Path):
    database = _database(tmp_path)
    reset = iso(NOW + timedelta(days=7))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=60, resets_at=reset, label="Grok Build weekly")
        _quota(connection, provider="claude-code", pct=89, resets_at=reset, label="Claude weekly window", limit="weekly")
        _samples(connection, [
            pace("grok", "weekly", 50, NOW - timedelta(minutes=30), reset),
            pace("grok", "weekly", 60, NOW - timedelta(minutes=1), reset),
            pace("claude-code", "weekly", 88.5, NOW - timedelta(minutes=30), reset),
            pace("claude-code", "weekly", 89, NOW - timedelta(minutes=1), reset),
        ])
    summary = aggregate_summary(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        window_key="1d",
        tool="all",
        timezone="UTC",
        cache_threshold=0.75,
        now=NOW,
    )
    forecasted = [
        card["providerKey"]
        for card in summary["capacity"]
        if (card.get("forecast") or {}).get("projectedRunDry")
    ]
    assert forecasted[0] == "grok"
    assert forecasted.index("grok") < forecasted.index("claude-code")
    assert summary["capacity"][-1]["providerKey"] == "openrouter"
    grok = next(card for card in summary["capacity"] if card["providerKey"] == "grok")
    assert grok["forecast"]["state"] == "dry"
    assert "capacityForecast" in summary


def test_without_samples_peak_order_keeps_openrouter_last():
    reset = iso(NOW + timedelta(days=3))
    cards = [
        {"providerKey": "grok", "providerName": "Grok Build", "isPayg": False, "peakPct": 60, "rows": [
            {"limitKey": "weekly", "pct": 60, "resetsAt": reset, "isPayg": False}]},
        {"providerKey": "claude-code", "providerName": "Claude Code", "isPayg": False, "peakPct": 89, "rows": [
            {"limitKey": "weekly", "pct": 89, "resetsAt": reset, "isPayg": False}]},
        {"providerKey": "openrouter", "providerName": "OpenRouter", "isPayg": True, "peakPct": None, "rows": [
            {"limitKey": "balance", "pct": None, "resetsAt": None, "isPayg": True}]},
    ]
    apply_forecasts(cards, [], now=NOW, active=set())
    assert [card["providerKey"] for card in cards] == ["claude-code", "grok", "openrouter"]


def test_pace_samples_record_once_a_minute_and_expire(tmp_path: Path):
    database = _database(tmp_path)
    reset = iso(NOW + timedelta(days=4))
    sample = QuotaSample(
        provider_key="grok",
        limit_key="weekly",
        label="Grok Build weekly",
        unit="percent",
        source="test",
        pct=40,
        resets_at=reset,
    )

    def collect():
        return [sample]

    poll_quotas(database, collectors={"grok": collect}, now=lambda: iso(NOW))
    poll_quotas(database, collectors={"grok": collect}, now=lambda: iso(NOW + timedelta(seconds=30)))
    poll_quotas(database, collectors={"grok": collect}, now=lambda: iso(NOW + timedelta(seconds=61)))
    with connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM quota_pace_samples").fetchone()[0]
        quota_rows = connection.execute("SELECT COUNT(*) FROM quotas WHERE provider_key='grok'").fetchone()[0]
    assert count == 2
    assert quota_rows == 1
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at) VALUES (?,?,?,?,?)",
            ("grok", "weekly", 1, reset, iso(NOW - timedelta(hours=49))),
        )
        record_pace_samples(connection, [], iso(NOW + timedelta(hours=2)))
        remaining = connection.execute(
            "SELECT COUNT(*) FROM quota_pace_samples WHERE pct=1"
        ).fetchone()[0]
    assert remaining == 0


def _arm(connection, *, provider, pct, resets_at, start_pct, start, end, label, limit="weekly"):
    _quota(connection, provider=provider, pct=pct, resets_at=resets_at, label=label, limit=limit, polled_at=iso(end))
    _samples(connection, [
        pace(provider, limit, start_pct, start, resets_at),
        pace(provider, limit, pct, end, resets_at),
    ])


def test_threshold_alerts_fire_once_and_respect_projection(tmp_path: Path):
    database = _database(tmp_path)
    near_reset = iso(NOW + timedelta(minutes=10))
    with connect(database) as connection:
        _arm(
            connection, provider="grok", pct=92, resets_at=near_reset, start_pct=88,
            start=NOW - timedelta(minutes=30), end=NOW - timedelta(minutes=1),
            label="Grok Build weekly",
        )
    held = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert held["active"] == []
    assert held["fired"] == {}

    far_reset = iso(NOW + timedelta(days=4))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=90, resets_at=far_reset, label="Grok Build weekly")
        claude_reset = iso(NOW + timedelta(days=2))
        _quota(connection, provider="claude-code", pct=10, resets_at=claude_reset, label="Claude weekly window")
        connection.execute("DELETE FROM quota_pace_samples")
        for row in (
            pace("grok", "weekly", 80, NOW - timedelta(minutes=30), far_reset),
            pace("grok", "weekly", 90, NOW - timedelta(minutes=1), far_reset),
            pace("claude-code", "weekly", 9.5, NOW - timedelta(minutes=30), claude_reset),
            pace("claude-code", "weekly", 10, NOW - timedelta(minutes=1), claude_reset),
        ):
            connection.execute(
                "INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at) VALUES (?,?,?,?,?)",
                (row["provider_key"], row["limit_key"], row["pct"], row["resets_at"], row["sampled_at"]),
            )
    fired = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert len(fired["active"]) == 1
    alert = fired["active"][0]
    assert alert["tier"] == "primary"
    assert alert["suggestion"] == "Shift heavy work to Claude Code until Friday."
    again = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert len(again["fired"]) == 1
    assert again["fired"][alert["id"]]["at"] == fired["fired"][alert["id"]]["at"]
    assert len(again["active"]) == 1


def test_escalation_is_opt_in_and_stale_samples_do_not_alert(tmp_path: Path):
    database = _database(tmp_path)
    resets = iso(NOW + timedelta(days=4))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=98, resets_at=resets, label="Grok Build weekly")
        _samples(connection, [
            pace("grok", "weekly", 88, NOW - timedelta(minutes=30), resets),
            pace("grok", "weekly", 98, NOW - timedelta(minutes=1), resets),
        ])
    primary = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert [item["tier"] for item in primary["active"]] == ["primary"]
    save_preferences(database, {"threshold": 90, "escalate": True, "quietStart": "22:00", "quietEnd": "07:00", "overrides": {}})
    both = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert sorted(item["tier"] for item in both["active"]) == ["escalate", "primary"]

    with connect(database) as connection:
        _samples(connection, [
            pace("grok", "weekly", 90, NOW - timedelta(minutes=20), resets),
            pace("grok", "weekly", 98, NOW - timedelta(minutes=11), resets),
        ])
    stale = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert stale["active"] == []


def test_quiet_hours_hold_unless_run_dry_is_within_two_hours(tmp_path: Path):
    database = _database(tmp_path)
    night = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    resets = iso(datetime(2026, 10, 9, 3, 0, tzinfo=UTC))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=90, resets_at=resets, label="Grok Build weekly", polled_at=iso(night))
        _samples(connection, [
            pace("grok", "weekly", 89, night - timedelta(minutes=56), resets),
            pace("grok", "weekly", 90, night - timedelta(minutes=4), resets),
        ])
    held = evaluate_alerts(database, timezone="UTC", now=night)
    assert held["active"] == []
    assert held["pending"]

    morning = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    with connect(database) as connection:
        _samples(connection, [
            pace("grok", "weekly", 89, morning - timedelta(minutes=56), resets),
            pace("grok", "weekly", 90, morning - timedelta(minutes=4), resets),
        ])
    delivered = evaluate_alerts(database, timezone="UTC", now=morning)
    assert len(delivered["active"]) == 1
    assert delivered["pending"] == {}

    urgent_path = tmp_path / "urgent.db"
    initialize(urgent_path)
    with connect(urgent_path) as connection:
        _quota(connection, provider="grok", pct=90, resets_at=resets, label="Grok Build weekly", polled_at=iso(night))
        _samples(connection, [
            pace("grok", "weekly", 80, night - timedelta(minutes=30), resets),
            pace("grok", "weekly", 90, night - timedelta(minutes=1), resets),
        ])
    urgent = evaluate_alerts(urgent_path, timezone="UTC", now=night)
    assert len(urgent["active"]) == 1


def test_override_raises_the_threshold_and_dismiss_hides_the_banner(tmp_path: Path):
    database = _database(tmp_path)
    resets = iso(NOW + timedelta(days=4))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=92, resets_at=resets, label="Grok Build weekly")
        _samples(connection, [
            pace("grok", "weekly", 80, NOW - timedelta(minutes=30), resets),
            pace("grok", "weekly", 92, NOW - timedelta(minutes=1), resets),
        ])
    save_preferences(database, {"threshold": 90, "escalate": False, "quietStart": "22:00", "quietEnd": "07:00", "overrides": {"grok": 95}})
    skipped = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert skipped["active"] == []
    save_preferences(database, {"threshold": 90, "escalate": False, "quietStart": "22:00", "quietEnd": "07:00", "overrides": {}})
    fired = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert len(fired["active"]) == 1
    hidden = dismiss_alert(database, fired["active"][0]["id"])
    assert hidden["alerts"] == []
    with pytest.raises(ValueError):
        save_preferences(database, {"threshold": 0, "escalate": False, "quietStart": "22:00", "quietEnd": "07:00"})


def test_majority_model_is_named_in_the_suggestion(tmp_path: Path):
    database = _database(tmp_path)
    resets = iso(NOW + timedelta(days=4))
    claude_reset = iso(NOW + timedelta(days=2))
    with connect(database) as connection:
        _quota(connection, provider="grok", pct=90, resets_at=resets, label="Grok Build weekly")
        _quota(connection, provider="claude-code", pct=10, resets_at=claude_reset, label="Claude weekly window")
        for row in (
            pace("grok", "weekly", 80, NOW - timedelta(minutes=30), resets),
            pace("grok", "weekly", 90, NOW - timedelta(minutes=1), resets),
            pace("claude-code", "weekly", 9.5, NOW - timedelta(minutes=30), claude_reset),
            pace("claude-code", "weekly", 10, NOW - timedelta(minutes=1), claude_reset),
        ):
            connection.execute(
                "INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at) VALUES (?,?,?,?,?)",
                (row["provider_key"], row["limit_key"], row["pct"], row["resets_at"], row["sampled_at"]),
            )
        for index in range(3):
            upsert_usage_event(connection, UsageEvent(
                source="claude_local",
                tool_key="claude-code",
                model_key="claude-opus-5",
                occurred_at=iso(NOW - timedelta(hours=1)),
                session_id="s",
                project=None,
                input_tokens=10,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=10,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0.01,
                raw_id=f"claude:{index}",
                ingested_at=iso(NOW),
                is_exact=True,
            ))
    state = evaluate_alerts(database, timezone="UTC", now=NOW)
    assert state["active"][0]["suggestion"] == "Shift heavy work to Claude Opus 5 until Friday."


def test_preferences_endpoint_is_on_the_summary(tmp_path: Path):
    database = tmp_path / "http.db"
    settings = Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        cursor_import_path=tmp_path / "cursor",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    client = TestClient(create_app(settings, enable_scheduler=False))
    saved = client.post("/api/capacity/forecast-preferences", json={
        "threshold": 85,
        "escalate": True,
        "quietStart": "23:00",
        "quietEnd": "06:30",
        "overrides": {"cursor": 88},
    })
    assert saved.status_code == 200
    assert saved.json()["preferences"]["threshold"] == 85
    summary = client.get("/api/spend/summary").json()
    assert summary["capacityForecast"]["preferences"]["overrides"]["cursor"] == 88
    assert summary["capacityForecast"]["preferences"]["escalate"] is True
    rejected = client.post("/api/capacity/forecast-preferences", json={"threshold": 150, "quietStart": "nope"})
    assert rejected.status_code == 400
