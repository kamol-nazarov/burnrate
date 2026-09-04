from __future__ import annotations

import math
import threading
import time as time_module
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from spend_app.db import connect, initialize, upsert_agent_run, upsert_quota, utc_now
from spend_app.limits import (
    _antigravity_limits_uncached,
    _antigravity_active_sessions,
    _cached,
    _claude_desktop_limits_uncached,
    _claude_active_sessions,
    _claude_from_traycer_result,
    _claude_limits_uncached,
    _codex_limits,
    _codex_active_sessions,
    _cursor_limits_uncached,
    _cursor_active_sessions,
    _grok_active_sessions,
    _grok_limits_uncached,
    _openrouter_credits_uncached,
    _traycer_activity,
    _traycer_profile_rate_limits,
    _zai_limits_uncached,
    _zcode_active_sessions,
)

# Persistence convention for the quotas table:
# - available rows carry a real unit ("pct", "credits", "usd") and real values;
# - unavailable rows carry unit "unavailable" with used/allowance/pct NULL and
#   the explicit reason appended to the label after LABEL_REASON_SEPARATOR;
# - source tags preserve undocumented-source labeling ("traycer_profile",
#   "cursor_usage_service", "antigravity_local_rpc", "claude_oauth_usage" are
#   experimental observed interfaces, never official);
# - resets_at is always the real reset timestamp from the source, never invented.

UNAVAILABLE_UNIT = "unavailable"
LABEL_REASON_SEPARATOR = " — "
WEEKLY_MINUTES = 7 * 24 * 60
AGENT_LIVE_STATE = "live"
AGENT_NO_DATA_STATE = "no_data"
LIVE_STATE_ALIASES = frozenset({"live", "running"})
QUOTA_LIMIT_ORDER = {
    "cursor": ("cursor_models", "other_models"),
    "claude-code": ("5h", "weekly"),
    "antigravity": ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"),
    "opencode": ("5h", "weekly"),
    "openrouter": ("balance",),
}
PRESSURE_GREEN = "#63c689"
PRESSURE_BLUE = "#78a8f8"
PRESSURE_AMBER = "#d9a441"
PRESSURE_RED = "#dc6c78"

PROVIDER_SOURCES = {
    "codex": "codex_local_telemetry",
    "claude-code": "traycer_profile",
    "cursor": "cursor_usage_service",
    "grok": "traycer_profile",
    "opencode": "zai_quota_endpoint",
    "openrouter": "openrouter_credits_api",
    "antigravity": "antigravity_local_rpc",
}

REQUIRED_LIMITS: dict[str, tuple[tuple[str, str], ...]] = {
    "codex": (("weekly", "Codex weekly window"),),
    "claude-code": (("5h", "Claude 5-hour window"), ("weekly", "Claude weekly window")),
    "cursor": (("cursor_models", "Cursor Models"), ("other_models", "Other Models")),
    "grok": (("weekly", "Grok Build weekly"),),
    "opencode": (("5h", "Z.AI 5-hour credits"), ("weekly", "Z.AI weekly credits")),
    "openrouter": (("balance", "OpenRouter funds remaining"),),
    "antigravity": (
        ("gemini-weekly", "Antigravity Gemini models · Weekly remaining"),
        ("gemini-5h", "Antigravity Gemini models · Five-hour remaining"),
        ("3p-weekly", "Antigravity Claude/GPT models · Weekly remaining"),
        ("3p-5h", "Antigravity Claude/GPT models · Five-hour remaining"),
    ),
}


@dataclass(frozen=True)
class QuotaSample:
    provider_key: str
    limit_key: str
    label: str
    unit: str
    source: str
    used: float | None = None
    allowance: float | None = None
    pct: float | None = None
    resets_at: str | None = None
    is_payg: bool | None = None
    reason: str | None = None
    # Set when the provider answered 429: seconds the lane must wait (from
    # Retry-After when given). Never persisted; drives the lane back-off.
    throttle_seconds: float | None = None


# Per-lane cadence in seconds as (active, idle). A lane is "active" while its
# tool produced a usage event within QUOTA_ACTIVITY_WINDOW_SECONDS. Local
# lanes cost nothing and refresh every tick; external endpoints publish no
# rate limits, so the active cadence caps them at 120 calls per hour and the
# idle cadence at 12; Cursor's experimental DashboardService stays slow.
LANE_CADENCE: dict[str, tuple[int, int]] = {
    "codex": (15, 15),
    "openrouter": (60, 300),
    "claude-code": (90, 300),  # 40/h: the OAuth usage endpoint 429s near 120/h with a ~1 h lockout (2026-09-02)
    "opencode": (30, 300),
    "grok": (30, 300),
    "cursor": (900, 3600),
    "antigravity": (90, 300),
}
QUOTA_ACTIVITY_WINDOW_SECONDS = 300
THROTTLE_BACKOFF_MIN_SECONDS = 60
THROTTLE_BACKOFF_MAX_SECONDS = 3600  # long enough to honour a full-hour Retry-After
THROTTLED_STATUS = 429


class QuotaLaneScheduler:
    """Decides per lane whether a tick should poll it, with 429 back-off."""

    def __init__(self, cadence: dict[str, tuple[int, int]] | None = None, *, clock=None) -> None:
        self.cadence = dict(cadence or LANE_CADENCE)
        self._clock = clock or time_module.monotonic
        self._next_due: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._lock = threading.Lock()

    def due(self, provider_key: str) -> bool:
        with self._lock:
            return self._clock() >= self._next_due.get(provider_key, float("-inf"))

    def backoff_seconds(self, provider_key: str) -> float:
        with self._lock:
            return self._backoff.get(provider_key, 0.0)

    def record(self, provider_key: str, samples: Sequence[QuotaSample], *, active: bool) -> None:
        active_interval, idle_interval = self.cadence.get(provider_key, (15, 15))
        interval = float(active_interval if active else idle_interval)
        throttles = [s.throttle_seconds for s in samples if s.throttle_seconds is not None]
        with self._lock:
            if throttles:
                previous = self._backoff.get(provider_key, 0.0)
                wait = max(max(throttles), previous * 2, THROTTLE_BACKOFF_MIN_SECONDS)
                wait = min(wait, THROTTLE_BACKOFF_MAX_SECONDS)
                self._backoff[provider_key] = wait
                self._next_due[provider_key] = self._clock() + wait
            else:
                self._backoff.pop(provider_key, None)
                self._next_due[provider_key] = self._clock() + interval


_DEFAULT_LANES = QuotaLaneScheduler()


def lane_is_active(database_path: Path | str, provider_key: str, *, now: datetime | None = None) -> bool:
    """True when the lane's tool produced usage within the activity window."""
    moment = now or datetime.now(UTC)
    since = (moment - timedelta(seconds=QUOTA_ACTIVITY_WINDOW_SECONDS)).isoformat().replace("+00:00", "Z")
    with connect(Path(database_path)) as connection:
        row = connection.execute(
            "SELECT 1 FROM usage_events WHERE occurred_at >= ? AND tool_key = ? LIMIT 1",
            (since, provider_key),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT 1 FROM unpriced_usage_events WHERE occurred_at >= ? AND tool_key = ? LIMIT 1",
                (since, provider_key),
            ).fetchone()
    return row is not None


def _apply_throttle(samples: list[QuotaSample], payload: dict) -> list[QuotaSample]:
    if not isinstance(payload, dict) or payload.get("httpStatus") != THROTTLED_STATUS:
        return samples
    wait = float(payload.get("retryAfterSeconds") or THROTTLE_BACKOFF_MIN_SECONDS)
    throttled = []
    for sample in samples:
        reason = sample.reason or "Quota source is unavailable."
        throttled.append(
            replace(
                sample,
                reason=f"{reason} Provider rate-limited this lane (HTTP 429); polling backs off.",
                throttle_seconds=wait,
            )
        )
    return throttled


@dataclass(frozen=True)
class AgentRunRecord:
    id: str
    name: str
    model_key: str | None
    state: str
    started_at: str
    last_seen_at: str


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _window_reset(window: dict) -> str | None:
    reset = window.get("resetAt")
    return normalize_reset(reset) if reset else None


def normalize_reset(value: object) -> str | None:
    """Keep a provider reset time at whole-second precision.

    Anthropic's OAuth usage endpoint returns ``resets_at`` with a different
    microsecond fraction on every call (server clock noise around a fixed
    minute boundary). Persisting that jitter made every 90-second poll look
    like a changed value, so the dedup never matched and a new quota row was
    written each poll. The reset instant itself is unchanged.
    """
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Round to the nearest second: the observed jitter straddles the second
    # boundary (…:59.9 and …:00.1 for the same reset), so flooring would
    # still flap and would display a :59 reset as the previous minute.
    rounded = (parsed.astimezone(UTC) + timedelta(microseconds=500_000)).replace(microsecond=0)
    return rounded.isoformat().replace("+00:00", "Z")


def _windows_by_key(payload: dict) -> dict[str, dict]:
    return {
        str(window.get("key")): window
        for window in payload.get("windows") or []
        if isinstance(window, dict) and window.get("key")
    }


def _unavailable(
    provider_key: str,
    limit_key: str,
    label: str,
    source: str,
    reason: str | None,
) -> QuotaSample:
    return QuotaSample(
        provider_key=provider_key,
        limit_key=limit_key,
        label=label,
        unit=UNAVAILABLE_UNIT,
        source=source,
        reason=reason or "Quota source is unavailable.",
    )


def unavailable_samples(
    provider_key: str,
    reason: str,
    *,
    source: str | None = None,
) -> list[QuotaSample]:
    tag = source or PROVIDER_SOURCES.get(provider_key, "unavailable")
    return [
        _unavailable(provider_key, limit_key, label, tag, reason)
        for limit_key, label in REQUIRED_LIMITS.get(provider_key, ())
    ]


def claude_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Claude provider quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label, missing_reason in (
        ("5h", "Claude 5-hour window", "Claude quota omitted the 5-hour percentage."),
        ("weekly", "Claude weekly window", "Claude quota omitted the weekly percentage."),
    ):
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "claude-code",
                    limit_key,
                    label,
                    source,
                    detail if not exact else missing_reason,
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="claude-code",
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def grok_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    label = "Grok Build weekly"
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Grok Build provider quota is unavailable.")
    window = _windows_by_key(payload).get("weekly") if exact else None
    used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
    if used_pct is None:
        return [
            _unavailable(
                "grok",
                "weekly",
                label,
                source,
                detail if not exact else "Grok Build quota omitted the weekly percentage.",
            )
        ]
    return [
        QuotaSample(
            provider_key="grok",
            limit_key="weekly",
            label=label,
            unit="pct",
            source=source,
            pct=max(0.0, used_pct),
            resets_at=_window_reset(window),
        )
    ]


def antigravity_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Antigravity local quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["antigravity"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "antigravity",
                    limit_key,
                    label,
                    source,
                    detail if not exact else "Antigravity omitted this quota percentage.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="antigravity",
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def codex_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    label = "Codex weekly window"
    if payload.get("status") != "exact":
        return [
            _unavailable(
                "codex",
                "weekly",
                label,
                source,
                str(payload.get("detail") or "No local rate-limit snapshot was found."),
            )
        ]
    weekly = None
    for window in payload.get("windows") or []:
        minutes = window.get("windowMinutes") if isinstance(window, dict) else None
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and float(minutes) >= WEEKLY_MINUTES:
            weekly = window
            break
    used_pct = _number(weekly.get("usedPct")) if isinstance(weekly, dict) else None
    if used_pct is None:
        return [
            _unavailable(
                "codex",
                "weekly",
                label,
                source,
                "The local Codex snapshot had no weekly rate-limit window.",
            )
        ]
    return [
        QuotaSample(
            provider_key="codex",
            limit_key="weekly",
            label=label,
            unit="pct",
            source=source,
            pct=max(0.0, used_pct),
            resets_at=_window_reset(weekly),
        )
    ]


def cursor_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Cursor usage is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["cursor"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "cursor",
                    limit_key,
                    label,
                    source,
                    detail if not exact else f"Experimental Cursor DashboardService omitted the {label} percentage.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="cursor",
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def zai_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Z.AI quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["opencode"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "opencode",
                    limit_key,
                    label,
                    source,
                    detail if not exact else f"The Z.AI quota endpoint omitted the {label} window.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="opencode",
                limit_key=limit_key,
                label=label,
                unit="credits",
                source=source,
                used=_number(window.get("used")),
                allowance=_number(window.get("limit")),
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def openrouter_quota_samples(
    *,
    collector: Callable[[], dict] | None = None,
) -> list[QuotaSample]:
    payload = (collector or _openrouter_credits_uncached)()
    windows = _windows_by_key(payload)
    balance = windows.get("balance") if payload.get("status") == "exact" else None
    remaining = _number(balance.get("remainingUsd")) if balance else None
    if remaining is None:
        return [
            QuotaSample(
                provider_key="openrouter",
                limit_key="balance",
                label="OpenRouter funds remaining",
                unit=UNAVAILABLE_UNIT,
                source="openrouter_credits_api",
                is_payg=True,
                reason=str(payload.get("detail") or "OpenRouter account balance is unavailable."),
            )
        ]
    return [
        QuotaSample(
            provider_key="openrouter",
            limit_key="balance",
            label="OpenRouter funds remaining",
            unit="usd",
            source="openrouter_credits_api",
            used=remaining,
            is_payg=True,
        )
    ]


def _claude_quota_collector() -> list[QuotaSample]:
    def load() -> tuple[dict, str]:
        try:
            return _claude_desktop_limits_uncached(), "claude_desktop_history"
        except Exception:
            pass
        try:
            return _claude_from_traycer_result(_traycer_profile_rate_limits("claude")), "traycer_profile"
        except Exception:
            return _claude_limits_uncached(), "claude_oauth_usage"

    # The lane scheduler owns the cadence; the short cache only coalesces a
    # poll with a concurrent compatibility /limits read.
    payload, source = _cached("claude_quota_poll", 10, load)
    return _apply_throttle(claude_quota_samples(payload, source=source), payload)


def _grok_quota_collector() -> list[QuotaSample]:
    # Local CLI billing snapshot first (no network); Traycer profile otherwise.
    payload = _cached("grok_quota_poll", 10, _grok_limits_uncached)
    source = str(payload.get("source") or "traycer_profile")
    return _apply_throttle(grok_quota_samples(payload, source=source), payload)


def _collect(name: str, ttl: int, loader: Callable[[], dict], builder, source: str) -> list[QuotaSample]:
    payload = _cached(name, ttl, loader)
    return _apply_throttle(builder(payload, source=source), payload)


def default_quota_collectors(
    database_path: Path | str,
) -> dict[str, Callable[[], list[QuotaSample]]]:
    return {
        "antigravity": lambda: _collect(
            "antigravity_quota_poll",
            30,
            _antigravity_limits_uncached,
            antigravity_quota_samples,
            "antigravity_local_rpc",
        ),
        "codex": lambda: _collect("codex_quota_poll", 10, _codex_limits, codex_quota_samples, "codex_local_telemetry"),
        "claude-code": _claude_quota_collector,
        "cursor": lambda: _collect("cursor_quota_poll", 600, _cursor_limits_uncached, cursor_quota_samples, "cursor_usage_service"),
        "grok": _grok_quota_collector,
        "opencode": lambda: _collect("zai_quota_poll", 10, _zai_limits_uncached, zai_quota_samples, "zai_quota_endpoint"),
        "openrouter": openrouter_quota_samples,
    }


def _persist_fields(sample: QuotaSample) -> tuple:
    label = sample.label
    if sample.reason:
        label = f"{label}{LABEL_REASON_SEPARATOR}{sample.reason}"
    is_payg = None if sample.is_payg is None else int(bool(sample.is_payg))
    return (
        label,
        sample.used,
        sample.allowance,
        sample.unit,
        sample.pct,
        sample.resets_at,
        sample.source,
        is_payg,
    )


def poll_quotas(
    database_path: Path | str,
    *,
    collectors: dict[str, Callable[[], list[QuotaSample]]] | None = None,
    now: Callable[[], str] | None = None,
    lanes: QuotaLaneScheduler | None = None,
    activity: Callable[[str], bool] | None = None,
) -> dict:
    """Poll the lanes that are due on this tick and persist their samples.

    With explicit ``collectors`` (tests, backfills) every collector runs each
    call unless ``lanes`` is given. The scheduled job uses the default lane
    scheduler: local lanes every tick, external lanes on their active/idle
    cadence, and any lane that answered 429 waits out its back-off.
    """
    path = Path(database_path)
    polled_at = (now or utc_now)()
    initialize(path)
    resolved = collectors if collectors is not None else default_quota_collectors(path)
    scheduler = lanes if lanes is not None else (_DEFAULT_LANES if collectors is None else None)
    is_active = activity or (lambda provider: lane_is_active(path, provider))
    written = 0
    skipped = 0
    polled: list[str] = []
    deferred: list[str] = []
    with connect(path) as connection:
        for provider_key in sorted(resolved):
            if scheduler is not None and not scheduler.due(provider_key):
                deferred.append(provider_key)
                continue
            polled.append(provider_key)
            try:
                samples = list(resolved[provider_key]())
            except Exception as exc:
                samples = unavailable_samples(
                    provider_key,
                    f"Quota collection failed ({type(exc).__name__}).",
                )
            if scheduler is not None:
                scheduler.record(provider_key, samples, active=is_active(provider_key))
            present = {sample.limit_key for sample in samples}
            for limit_key, label in REQUIRED_LIMITS.get(provider_key, ()):
                if limit_key not in present:
                    samples.append(
                        _unavailable(
                            provider_key,
                            limit_key,
                            label,
                            PROVIDER_SOURCES.get(provider_key, "unavailable"),
                            "No sample was produced for this limit during the poll.",
                        )
                    )
            for sample in samples:
                fields = _persist_fields(sample)
                latest = connection.execute(
                    "SELECT id, label, used, allowance, unit, pct, resets_at, source, is_payg "
                    "FROM quotas WHERE provider_key=? AND limit_key=? "
                    "ORDER BY polled_at DESC, id DESC LIMIT 1",
                    (sample.provider_key, sample.limit_key),
                ).fetchone()
                if latest is not None and tuple(latest)[1:] == fields:
                    # Same observation: no new history row, but the row now
                    # states the most recent poll that confirmed the value so
                    # "last poll" stays truthful.
                    connection.execute(
                        "UPDATE quotas SET polled_at=? WHERE id=? AND polled_at<?",
                        (polled_at, latest["id"], polled_at),
                    )
                    skipped += 1
                    continue
                upsert_quota(
                    connection,
                    provider_key=sample.provider_key,
                    limit_key=sample.limit_key,
                    label=fields[0],
                    used=sample.used,
                    allowance=sample.allowance,
                    unit=sample.unit,
                    pct=sample.pct,
                    resets_at=sample.resets_at,
                    source=sample.source,
                    polled_at=polled_at,
                    is_payg=sample.is_payg,
                )
                written += 1
    return {
        "polledAt": polled_at,
        "written": written,
        "skipped": skipped,
        "providers": sorted(resolved),
        "polledProviders": polled,
        "deferredProviders": deferred,
    }


def agent_run_records(activity: dict) -> list[AgentRunRecord]:
    records: list[AgentRunRecord] = []
    for agent in activity.get("activeAgents") or []:
        record = _run_record(agent, state=AGENT_LIVE_STATE, last_seen_field="startedAt")
        if record:
            records.append(record)
    for agent in activity.get("unmeteredTurns") or []:
        record = _run_record(agent, state=AGENT_NO_DATA_STATE, last_seen_field="stoppedAt")
        if record:
            records.append(record)
    for session in activity.get("grokSessions") or []:
        record = _grok_run_record(session)
        if record:
            records.append(record)
    for session in activity.get("codexSessions") or []:
        record = _codex_run_record(session)
        if record:
            records.append(record)
    for key, prefix in (
        ("claudeSessions", "claude"),
        ("cursorSessions", "cursor"),
        ("zcodeSessions", "zcode"),
        ("antigravitySessions", "antigravity"),
    ):
        for session in activity.get(key) or []:
            record = _local_run_record(session, prefix=prefix)
            if record:
                records.append(record)
    return records


def _grok_run_record(session: dict) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    if not session_id or not started_at:
        return None
    return AgentRunRecord(
        id=f"grok:{session_id}",
        name=str(session.get("title") or "Grok Build session"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(started_at),
    )


def _codex_run_record(session: dict) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    last_seen_at = session.get("lastSeenAt")
    if not session_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"codex:{session_id}",
        name=str(session.get("title") or "Codex task"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def _local_run_record(session: dict, *, prefix: str) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    last_seen_at = session.get("lastSeenAt")
    if not session_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"{prefix}:{session_id}",
        name=str(session.get("title") or f"{prefix.title()} session"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def _run_record(agent: dict, *, state: str, last_seen_field: str) -> AgentRunRecord | None:
    if not isinstance(agent, dict):
        return None
    chat_id = str(agent.get("chatId") or "").strip()
    started_at = agent.get("startedAt")
    last_seen_at = agent.get(last_seen_field)
    if not chat_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"traycer:{chat_id}",
        name=str(agent.get("title") or "Untitled agent"),
        model_key=str(agent["model"]) if agent.get("model") else None,
        state=state,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def is_live_run(state: str | None) -> bool:
    return str(state or "").strip().lower() in LIVE_STATE_ALIASES


def _clear_unseen_live_runs(connection, seen_ids: set[str]) -> int:
    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        cursor = connection.execute(
            "DELETE FROM agent_runs "
            f"WHERE lower(state) IN ('live','running') AND id NOT IN ({placeholders})",
            tuple(seen_ids),
        )
    else:
        cursor = connection.execute(
            "DELETE FROM agent_runs WHERE lower(state) IN ('live','running')"
        )
    return int(cursor.rowcount or 0)


def default_activity_collector() -> dict:
    activity = _traycer_activity()
    return {
        **activity,
        "grokSessions": _grok_active_sessions(),
        "codexSessions": _codex_active_sessions(),
        "cursorSessions": _cursor_active_sessions(),
        "zcodeSessions": _zcode_active_sessions(),
        "antigravitySessions": _antigravity_active_sessions(),
        "claudeSessions": _claude_active_sessions(),
    }


def poll_activity(
    database_path: Path | str,
    *,
    collector: Callable[[], dict] | None = None,
    now: Callable[[], str] | None = None,
) -> dict:
    path = Path(database_path)
    polled_at = (now or utc_now)()
    initialize(path)
    activity = (collector or default_activity_collector)()
    records = agent_run_records(activity)
    new = 0
    with connect(path) as connection:
        for record in records:
            if upsert_agent_run(
                connection,
                id=record.id,
                name=record.name,
                model_key=record.model_key,
                state=record.state,
                started_at=record.started_at,
                last_seen_at=record.last_seen_at,
            ):
                new += 1
        cleared = _clear_unseen_live_runs(connection, {record.id for record in records})
    return {
        "polledAt": polled_at,
        "seen": len(records),
        "new": new,
        "cleared": cleared,
        "live": sum(1 for record in records if is_live_run(record.state)),
        "noData": sum(1 for record in records if not is_live_run(record.state)),
    }


def pressure_color(pct: float | None) -> str:
    if pct is None:
        return "unavailable"
    if pct < 30:
        return PRESSURE_GREEN
    if pct < 60:
        return PRESSURE_BLUE
    if pct < 85:
        return PRESSURE_AMBER
    return PRESSURE_RED


def order_capacity_rows(rows: Sequence[QuotaSample]) -> list[QuotaSample]:
    grouped: dict[str, list[QuotaSample]] = {}
    for row in rows:
        grouped.setdefault(row.provider_key, []).append(row)
    primary_values: dict[str, float] = {}
    for provider_key, provider_rows in grouped.items():
        order = QUOTA_LIMIT_ORDER.get(provider_key, ())
        primary = next(
            (row for row in provider_rows if order and row.limit_key == order[0]),
            max(provider_rows, key=lambda row: row.pct if row.pct is not None else float("-inf")),
        )
        primary_values[provider_key] = primary.pct if primary.pct is not None else float("-inf")

    def provider_rank(provider_key: str) -> tuple[int, float]:
        if provider_key == "openrouter":
            return (1, 0.0)
        return (0, -primary_values.get(provider_key, float("-inf")))

    def row_key(row: QuotaSample) -> tuple[int, float, int, str]:
        group, provider_primary = provider_rank(row.provider_key)
        order = QUOTA_LIMIT_ORDER.get(row.provider_key, ())
        try:
            limit_rank = order.index(row.limit_key)
        except ValueError:
            limit_rank = len(order) + 100
        return (group, provider_primary, limit_rank, row.limit_key)

    return sorted(rows, key=row_key)


def split_quota_label(label: str) -> tuple[str, str | None]:
    if LABEL_REASON_SEPARATOR in label:
        name, reason = label.split(LABEL_REASON_SEPARATOR, 1)
        return name, reason
    return label, None


def derived_used(
    *,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
) -> float | None:
    if pct is not None and allowance not in (None, 0):
        return (pct / 100.0) * allowance
    return used


def quota_note(
    unit: str,
    *,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
    label: str | None = None,
) -> str | None:
    if unit == UNAVAILABLE_UNIT:
        if label:
            _, reason = split_quota_label(label)
            return reason
        return None
    quantity = derived_used(used=used, allowance=allowance, pct=pct)
    if unit == "usd":
        if quantity is None:
            return None
        if allowance is None:
            return f"${quantity:,.2f} spent"
        return f"${quantity:,.2f} of ${allowance:,.2f} spent"
    if unit == "credits":
        if quantity is not None and allowance is not None:
            return f"{quantity:,.0f} of {allowance:,.0f} credits used"
        if pct is not None:
            return f"{pct:g}% of credits used"
        return None
    if unit == "pct":
        if pct is not None:
            return f"{pct:g}% used"
        return None
    return None
