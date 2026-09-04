"""Adaptive quota polling: per-lane cadence, activity awareness, 429 back-off.

The quota job ticks every 15 s. Local lanes refresh on every tick; external
lanes poll every 30 s only while their tool is in use and every 5 minutes
idle; any 429 puts the lane into exponential back-off that honours
Retry-After. Hourly call counts are bounded regardless of tick rate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.quotas import (
    LANE_CADENCE,
    THROTTLE_BACKOFF_MAX_SECONDS,
    THROTTLE_BACKOFF_MIN_SECONDS,
    QuotaLaneScheduler,
    QuotaSample,
    _apply_throttle,
    claude_quota_samples,
    lane_is_active,
    poll_quotas,
)


def _sample(provider: str, limit: str = "weekly", pct: float = 10.0) -> QuotaSample:
    return QuotaSample(provider_key=provider, limit_key=limit, label=limit, unit="pct", source="fixture", pct=pct)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_lane_scheduler_follows_active_and_idle_cadence() -> None:
    clock = FakeClock()
    lanes = QuotaLaneScheduler(clock=clock)
    assert lanes.due("claude-code"), "a never-polled lane is due immediately"
    lanes.record("claude-code", [_sample("claude-code")], active=True)
    assert not lanes.due("claude-code")
    clock.now += 89
    assert not lanes.due("claude-code")
    clock.now += 1
    assert lanes.due("claude-code"), "the Claude lane polls every 90 s while active (40/h, proven safe)"
    lanes.record("claude-code", [_sample("claude-code")], active=False)
    clock.now += 299
    assert not lanes.due("claude-code")
    clock.now += 1
    assert lanes.due("claude-code"), "idle lanes poll every 5 minutes"
    lanes.record("opencode", [_sample("opencode")], active=True)
    clock.now += 29
    assert not lanes.due("opencode")
    clock.now += 1
    assert lanes.due("opencode"), "Z.AI polls every 30 s while active"
    lanes.record("codex", [_sample("codex")], active=False)
    clock.now += 15
    assert lanes.due("codex"), "local lanes refresh every tick"


def test_429_backs_off_exponentially_honours_retry_after_and_caps() -> None:
    clock = FakeClock()
    lanes = QuotaLaneScheduler(clock=clock)
    throttled = [QuotaSample("opencode", "weekly", "w", "unavailable", "fixture", throttle_seconds=45.0)]
    lanes.record("opencode", throttled, active=True)
    assert lanes.backoff_seconds("opencode") == THROTTLE_BACKOFF_MIN_SECONDS, "Retry-After below the floor is raised to the floor"
    clock.now += 59
    assert not lanes.due("opencode")
    clock.now += 1
    assert lanes.due("opencode")
    lanes.record("opencode", throttled, active=True)
    assert lanes.backoff_seconds("opencode") == 120, "repeated throttling doubles the wait"
    clock.now += 120
    lanes.record("opencode", [QuotaSample("opencode", "weekly", "w", "unavailable", "fixture", throttle_seconds=900.0)], active=True)
    assert lanes.backoff_seconds("opencode") == 900, "a larger Retry-After wins over doubling"
    clock.now += 900
    for _ in range(5):
        lanes.record("opencode", throttled, active=True)
        clock.now += lanes.backoff_seconds("opencode")
    assert lanes.backoff_seconds("opencode") == THROTTLE_BACKOFF_MAX_SECONDS
    lanes.record("opencode", [_sample("opencode")], active=True)
    assert lanes.backoff_seconds("opencode") == 0.0, "a successful poll clears the back-off"
    clock.now += 30
    assert lanes.due("opencode")


def test_throttled_payload_marks_samples_without_inventing_values() -> None:
    payload = {"status": "error", "detail": "Claude usage lookup failed (HTTPStatusError).", "httpStatus": 429, "retryAfterSeconds": 120.0, "windows": []}
    samples = _apply_throttle(claude_quota_samples(payload, source="claude_oauth_usage"), payload)
    assert len(samples) == 2
    for sample in samples:
        assert sample.unit == "unavailable" and sample.pct is None
        assert sample.throttle_seconds == 120.0
        assert "HTTP 429" in sample.reason and "backs off" in sample.reason
    untouched = _apply_throttle(claude_quota_samples({"status": "error", "detail": "x", "httpStatus": 503}, source="s"), {"httpStatus": 503})
    assert all(sample.throttle_seconds is None for sample in untouched)


def test_lane_activity_comes_from_recent_usage_events(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    now = datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
    assert lane_is_active(database, "claude-code", now=now) is False
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="claude_local", tool_key="claude-code", model_key="claude-opus-5",
                occurred_at=(now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                session_id="s", project="p", input_tokens=10, cached_input_tokens=0,
                cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=1, reasoning_tokens=None,
                cost_usd=None, computed_cost_usd=0.0, raw_id="r1", ingested_at="2026-09-01T21:58:30Z",
            ),
        )
    assert lane_is_active(database, "claude-code", now=now) is True
    assert lane_is_active(database, "claude-code", now=now + timedelta(minutes=10)) is False
    assert lane_is_active(database, "opencode", now=now) is False


def test_poll_only_touches_lanes_that_are_due(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    clock = FakeClock()
    lanes = QuotaLaneScheduler(clock=clock)
    calls = {"claude-code": 0, "codex": 0}
    collectors = {
        "claude-code": lambda: (calls.__setitem__("claude-code", calls["claude-code"] + 1), [_sample("claude-code", pct=1.0)])[1],
        "codex": lambda: (calls.__setitem__("codex", calls["codex"] + 1), [_sample("codex", pct=2.0)])[1],
    }
    first = poll_quotas(database, collectors=collectors, now=lambda: "2026-09-01T22:00:00Z", lanes=lanes, activity=lambda _p: False)
    assert first["polledProviders"] == ["claude-code", "codex"]
    clock.now += 15
    second = poll_quotas(database, collectors=collectors, now=lambda: "2026-09-01T22:00:15Z", lanes=lanes, activity=lambda _p: False)
    assert second["polledProviders"] == ["codex"] and second["deferredProviders"] == ["claude-code"]
    assert calls == {"claude-code": 1, "codex": 2}
    with connect(database) as connection:
        rows = {row[0]: row[1] for row in connection.execute("SELECT provider_key, polled_at FROM quotas")}
    assert rows["claude-code"] == "2026-09-01T22:00:00Z", "a deferred lane's row is untouched"
    assert rows["codex"] == "2026-09-01T22:00:15Z"


def test_hourly_external_calls_are_bounded_even_when_continuously_active() -> None:
    clock = FakeClock()
    lanes = QuotaLaneScheduler(clock=clock)
    calls = {provider: 0 for provider in LANE_CADENCE}
    for _tick in range(3600 // 15):
        for provider in LANE_CADENCE:
            if lanes.due(provider):
                calls[provider] += 1
                lanes.record(provider, [_sample(provider)], active=True)
        clock.now += 15
    assert calls["claude-code"] <= 40 and calls["opencode"] <= 120 and calls["grok"] <= 120
    assert calls["cursor"] <= 4
    assert calls["codex"] == 240 and calls["openrouter"] == 60
    idle = {provider: 0 for provider in LANE_CADENCE}
    for _tick in range(3600 // 15):
        for provider in LANE_CADENCE:
            if lanes.due(provider):
                idle[provider] += 1
                lanes.record(provider, [_sample(provider)], active=False)
        clock.now += 15
    assert idle["claude-code"] <= 12 and idle["opencode"] <= 12 and idle["cursor"] <= 1
    assert idle["openrouter"] <= 12
