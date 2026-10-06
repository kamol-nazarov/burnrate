"""Run-dry pace for capacity lanes, and one reset alert per quota window.

Pace samples are append-only. The quotas table only keeps change-points and
rewrites polled_at, so it cannot reconstruct a burn rate.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from spend_app.db import connect, initialize
from spend_app.timeutil import parse_utc

KEY = "capacity_forecast.v1"
STALE_FLOOR_SECONDS = 300
TIGHT_HOURS = 12
URGENT_HOURS = 2
PRIMARY_TIER = "primary"
ESCALATE_TIER = "escalate"
ESCALATE_AT = 97
SAMPLE_INTERVAL = timedelta(seconds=60)
SAMPLE_RETENTION = timedelta(hours=48)
MIN_SPAN = timedelta(seconds=60)
HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)

_CLOCK = re.compile(r"^(\d{2}):(\d{2})(?::\d{2})?$")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _reset_token(value) -> str | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return _iso(parsed)


def _format_span(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(round(hours * 60)))}m"
    return f"{max(1, int(round(hours)))}h"


def _table_exists(connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def stale_after_seconds(provider_key: str, *, active: bool) -> int:
    from spend_app.quotas import LANE_CADENCE

    active_seconds, idle_seconds = LANE_CADENCE.get(provider_key, (15, 15))
    cadence = active_seconds if active else idle_seconds
    return max(STALE_FLOOR_SECONDS, 2 * int(cadence))


def _points(samples, *, resets_at: str, now: datetime) -> list[tuple[datetime, float]]:
    token = _reset_token(resets_at)
    points = []
    for sample in samples:
        if _reset_token(sample.get("resets_at")) != token:
            continue
        stamp = parse_utc(sample.get("sampled_at"))
        if stamp is None or stamp > now:
            continue
        try:
            pct = float(sample["pct"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((stamp, pct))
    points.sort(key=lambda item: item[0])
    return points


def _rate(points, *, now: datetime, window: timedelta) -> float | None:
    start = now - window
    chosen = [point for point in points if point[0] >= start]
    if len(chosen) < 2:
        return None
    span = chosen[-1][0] - chosen[0][0]
    if span < MIN_SPAN:
        return None
    return (chosen[-1][1] - chosen[0][1]) / (span.total_seconds() / 3600)


def _forecast(
    *,
    state: str,
    projected: datetime | None,
    hours_remaining: float | None,
    reset: datetime | None,
    burn: float | None,
    line: str | None,
) -> dict:
    before = None
    if projected is not None and reset is not None and projected < reset:
        before = (reset - projected).total_seconds() / 3600
    return {
        "state": state,
        "projectedRunDry": _iso(projected) if projected is not None else None,
        "hoursRemaining": None if hours_remaining is None else round(hours_remaining, 4),
        "hoursBeforeReset": None if before is None else round(before, 4),
        "burnPctPerHour": None if burn is None else round(burn, 4),
        "line": line,
    }


def _comfortable() -> dict:
    return _forecast(
        state="safe",
        projected=None,
        hours_remaining=None,
        reset=None,
        burn=0.0,
        line="pace → comfortable",
    )


def _compared(projected: datetime, reset: datetime, *, hours_remaining: float, burn: float | None) -> dict:
    if projected >= reset:
        return _forecast(
            state="safe",
            projected=projected,
            hours_remaining=hours_remaining,
            reset=reset,
            burn=burn,
            line="pace → comfortable",
        )
    before = (reset - projected).total_seconds() / 3600
    state = "tight" if before <= TIGHT_HOURS else "dry"
    when = "pace → dry now" if hours_remaining <= 0 else f"pace → dry in ~{_format_span(hours_remaining)}"
    return _forecast(
        state=state,
        projected=projected,
        hours_remaining=hours_remaining,
        reset=reset,
        burn=burn,
        line=f"{when} ({_format_span(before)} before reset)",
    )


def project_pace(
    samples,
    *,
    now: datetime,
    resets_at: str | None,
    pct: float | None,
    stale_after: int,
) -> dict | None:
    """Project when this window hits 100% from measured quota samples.

    A flat or empty last hour uses the last 24 hours. Fewer than two samples,
    a non-positive rate with no longer window, or a stale newest sample
    produces no run-dry time. A flat fresh window is comfortable.
    """
    if pct is None or not resets_at:
        return None
    reset = parse_utc(resets_at)
    if reset is None or reset <= now:
        return None
    points = _points(samples, resets_at=resets_at, now=now)
    if not points:
        return None
    if now - points[-1][0] > timedelta(seconds=stale_after):
        return _forecast(
            state="stale",
            projected=None,
            hours_remaining=None,
            reset=None,
            burn=None,
            line=None,
        )
    used = float(pct)
    if used >= 100:
        return _compared(now, reset, hours_remaining=0.0, burn=None)
    hour_rate = _rate(points, now=now, window=HOUR)
    day_rate = _rate(points, now=now, window=DAY)
    if hour_rate is not None and hour_rate > 0:
        burn = hour_rate
    elif day_rate is not None and day_rate > 0:
        burn = day_rate
    elif hour_rate is not None or day_rate is not None:
        return _comfortable()
    else:
        return None
    hours_remaining = max(0.0, 100 - used) / burn
    return _compared(now + timedelta(hours=hours_remaining), reset, hours_remaining=hours_remaining, burn=burn)


def _sort_key(card: dict) -> tuple:
    payg = 2 if card.get("providerKey") == "openrouter" else 1 if card.get("isPayg") else 0
    forecast = card.get("forecast") or {}
    projected = forecast.get("projectedRunDry")
    if projected:
        return (payg, 0, projected, 0)
    peak = card.get("peakPct")
    return (payg, 1, "", -(peak if peak is not None else -1))


def apply_forecasts(cards: list[dict], samples, *, now: datetime, active: set[str]) -> None:
    grouped: dict[tuple[str, str], list] = {}
    for sample in samples:
        grouped.setdefault((sample.get("provider_key"), sample.get("limit_key")), []).append(sample)
    for card in cards:
        soonest = None
        comfortable = None
        provider_key = card.get("providerKey")
        stale_after = stale_after_seconds(provider_key, active=provider_key in active)
        for row in card.get("rows") or []:
            row["forecast"] = None
            if card.get("isPayg") or row.get("isPayg") or row.get("pct") is None or not row.get("resetsAt"):
                continue
            forecast = project_pace(
                grouped.get((provider_key, row.get("limitKey")), []),
                now=now,
                resets_at=row.get("resetsAt"),
                pct=row.get("pct"),
                stale_after=stale_after,
            )
            row["forecast"] = forecast
            if not forecast:
                continue
            projected = forecast.get("projectedRunDry")
            if projected and (soonest is None or projected < soonest.get("projectedRunDry")):
                soonest = forecast
            elif forecast.get("state") == "safe" and comfortable is None:
                comfortable = forecast
        card["forecast"] = soonest or comfortable
    cards.sort(key=_sort_key)


def active_provider_keys(connection, now: datetime) -> set[str]:
    since = _iso(now - timedelta(seconds=300))
    keys: set[str] = set()
    for table in ("usage_events", "unpriced_usage_events"):
        if not _table_exists(connection, table):
            continue
        rows = connection.execute(
            f"SELECT DISTINCT tool_key FROM {table} WHERE occurred_at >= ?",
            (since,),
        )
        keys.update(row[0] for row in rows if row[0])
    return keys


def load_pace_samples(connection, now: datetime) -> list[dict]:
    if not _table_exists(connection, "quota_pace_samples"):
        return []
    since = _iso(now - SAMPLE_RETENTION)
    rows = connection.execute(
        """
        SELECT provider_key, limit_key, pct, resets_at, sampled_at
        FROM quota_pace_samples
        WHERE sampled_at >= ?
        ORDER BY sampled_at
        """,
        (since,),
    )
    return [dict(row) for row in rows]


def pace_fingerprint(connection) -> tuple:
    if not _table_exists(connection, "quota_pace_samples"):
        return ()
    row = connection.execute(
        "SELECT COUNT(*), MAX(id), MAX(sampled_at) FROM quota_pace_samples"
    ).fetchone()
    return (row[0], row[1], row[2])


def alert_fingerprint(connection) -> str:
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (KEY,)).fetchone()
    return "" if row is None else row[0]


def record_pace_samples(connection, samples, polled_at: str) -> int:
    """Store at most one numeric sample per window per minute, and drop rows older than 48h."""
    moment = parse_utc(polled_at)
    if moment is None:
        return 0
    if not _table_exists(connection, "quota_pace_samples"):
        return 0
    connection.execute(
        "DELETE FROM quota_pace_samples WHERE sampled_at < ?",
        (_iso(moment - SAMPLE_RETENTION),),
    )
    written = 0
    stamp = _iso(moment)
    for sample in samples:
        if getattr(sample, "pct", None) is None or getattr(sample, "is_payg", None):
            continue
        latest = connection.execute(
            """
            SELECT sampled_at FROM quota_pace_samples
            WHERE provider_key=? AND limit_key=?
            ORDER BY sampled_at DESC, id DESC LIMIT 1
            """,
            (sample.provider_key, sample.limit_key),
        ).fetchone()
        if latest is not None:
            previous = parse_utc(latest["sampled_at"])
            if previous is not None and moment - previous < SAMPLE_INTERVAL:
                continue
        connection.execute(
            """
            INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sample.provider_key, sample.limit_key, float(sample.pct), sample.resets_at, stamp),
        )
        written += 1
    return written


def default_preferences() -> dict:
    return {
        "threshold": 90,
        "escalate": False,
        "quietStart": "22:00",
        "quietEnd": "07:00",
        "overrides": {},
    }


def _threshold(value) -> int:
    number = int(value)
    if not 1 <= number <= 99:
        raise ValueError("Threshold must be from 1 to 99")
    return number


def _clock(value) -> str:
    match = _CLOCK.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("Quiet hours use HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Quiet hours use HH:MM")
    return f"{hour:02d}:{minute:02d}"


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_preferences(body: dict) -> dict:
    overrides = {}
    raw_overrides = body.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ValueError("Overrides must be provider thresholds")
    for key, value in raw_overrides.items():
        name = str(key).strip()
        if not name or value in (None, ""):
            continue
        overrides[name] = _threshold(value)
    return {
        "threshold": _threshold(body.get("threshold", 90)),
        "escalate": _flag(body.get("escalate", False)),
        "quietStart": _clock(body.get("quietStart", "22:00")),
        "quietEnd": _clock(body.get("quietEnd", "07:00")),
        "overrides": overrides,
    }


def _load_preferences(raw) -> dict:
    base = default_preferences()
    if not isinstance(raw, dict):
        return base
    try:
        base["threshold"] = _threshold(raw.get("threshold", base["threshold"]))
        base["escalate"] = _flag(raw.get("escalate", False))
        base["quietStart"] = _clock(raw.get("quietStart", base["quietStart"]))
        base["quietEnd"] = _clock(raw.get("quietEnd", base["quietEnd"]))
    except (TypeError, ValueError):
        return default_preferences()
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        cleaned = {}
        for key, value in overrides.items():
            try:
                cleaned[str(key)] = _threshold(value)
            except (TypeError, ValueError):
                continue
        base["overrides"] = cleaned
    return base


def _blank_state() -> dict:
    return {"preferences": default_preferences(), "fired": {}, "pending": {}, "active": []}


def load_state(connection) -> dict:
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return _blank_state()
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        return _blank_state()
    if not isinstance(parsed, dict):
        return _blank_state()
    state = _blank_state()
    state["preferences"] = _load_preferences(parsed.get("preferences"))
    if isinstance(parsed.get("fired"), dict):
        state["fired"] = parsed["fired"]
    if isinstance(parsed.get("pending"), dict):
        state["pending"] = parsed["pending"]
    if isinstance(parsed.get("active"), list):
        state["active"] = [item for item in parsed["active"] if isinstance(item, dict)]
    return state


def save_state(connection, state: dict) -> None:
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, json.dumps(state, sort_keys=True, separators=(",", ":"))),
    )


def _public_alert(alert: dict) -> dict:
    return {
        key: alert[key]
        for key in (
            "id",
            "providerKey",
            "providerName",
            "limitKey",
            "limitLabel",
            "pct",
            "resetsAt",
            "projectedRunDry",
            "line",
            "suggestion",
            "tier",
            "title",
            "body",
        )
        if key in alert
    }


def read_forecast_view(connection) -> dict:
    state = load_state(connection)
    return {
        "preferences": state["preferences"],
        "alerts": [
            _public_alert(alert)
            for alert in state["active"]
            if not alert.get("dismissedAt")
        ],
    }


def in_quiet_hours(now: datetime, timezone: str, start: str, end: str) -> bool:
    try:
        local = now.astimezone(ZoneInfo(timezone))
        start_minutes = _minutes(start)
        end_minutes = _minutes(end)
    except (TypeError, ValueError):
        return False
    if start_minutes == end_minutes:
        return False
    minutes = local.hour * 60 + local.minute
    if start_minutes < end_minutes:
        return start_minutes <= minutes < end_minutes
    return minutes >= start_minutes or minutes < end_minutes


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in _clock(value).split(":"))
    return hour * 60 + minute


def _tiers(preferences: dict, provider_key: str) -> list[tuple[str, int]]:
    primary = preferences["overrides"].get(provider_key, preferences["threshold"])
    tiers = [(PRIMARY_TIER, int(primary))]
    if preferences.get("escalate") and ESCALATE_AT > int(primary):
        tiers.append((ESCALATE_TIER, ESCALATE_AT))
    return tiers


def _alert_id(provider_key: str, limit_key: str, resets_at: str, tier: str) -> str:
    return "|".join((provider_key, limit_key, _reset_token(resets_at) or "", tier))


def _weekday(resets_at: str, timezone: str) -> str:
    return parse_utc(resets_at).astimezone(ZoneInfo(timezone)).strftime("%A")


def suggestion_line(*, provider_name: str, resets_at: str, timezone: str, destination_name: str | None) -> str:
    weekday = _weekday(resets_at, timezone)
    if destination_name:
        return f"Shift heavy work to {destination_name} until {weekday}."
    return f"Ease off {provider_name} until {weekday}."


def _model_label(model_key: str) -> str:
    from spend_app.aggregate import COVERAGE_TARGETS, display_model

    for _tool, key, name in COVERAGE_TARGETS:
        if key == model_key:
            return name
    return display_model(model_key)


def majority_model_names(connection, now: datetime) -> dict[str, str]:
    """Most-used measured model per tool over the last 24 hours.

    A strict majority is not required. The model is named only when it was
    in ``usage_events`` and it is the unique top count. A tie keeps the
    subscription name rather than picking a model arbitrarily.
    """
    if not _table_exists(connection, "usage_events"):
        return {}
    rows = connection.execute(
        """
        SELECT tool_key, model_key, COUNT(*) AS n
        FROM usage_events
        WHERE occurred_at >= ?
        GROUP BY tool_key, model_key
        """,
        (_iso(now - DAY),),
    ).fetchall()
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        if not row["model_key"]:
            continue
        grouped.setdefault(row["tool_key"], []).append((int(row["n"]), row["model_key"]))
    names = {}
    for tool, pairs in grouped.items():
        top = max(count for count, _key in pairs)
        if top <= 0:
            continue
        leaders = [key for count, key in pairs if count == top]
        if len(leaders) == 1:
            names[tool] = _model_label(leaders[0])
    return names


def _destination_name(cards, provider_key: str, models: dict[str, str]) -> str | None:
    best = None
    best_reset = None
    for card in cards:
        other = card.get("providerKey")
        if other == provider_key or card.get("isPayg"):
            continue
        for row in card.get("rows") or []:
            forecast = row.get("forecast") or {}
            if forecast.get("state") != "safe" or not row.get("resetsAt"):
                continue
            reset = parse_utc(row["resetsAt"])
            if reset is None:
                continue
            if best_reset is None or reset > best_reset:
                best_reset = reset
                best = card
    if best is None:
        return None
    return models.get(best["providerKey"]) or best.get("providerName")


def _until_label(resets_at: str, now: datetime) -> str:
    remaining = (parse_utc(resets_at) - now).total_seconds() / 3600
    if remaining <= 0:
        return "resets now"
    return f"{_format_span(remaining)} to reset"


def _pct_text(pct: float) -> str:
    rounded = round(float(pct), 1)
    if rounded == int(rounded):
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def _urgent(forecast: dict, now: datetime) -> bool:
    projected = parse_utc(forecast.get("projectedRunDry"))
    if projected is None:
        return False
    return (projected - now).total_seconds() <= URGENT_HOURS * 3600


def _should_signal(row: dict, forecast: dict, threshold: int, now: datetime) -> bool:
    if forecast.get("state") not in {"tight", "dry"}:
        return False
    if row.get("pct") is None or float(row["pct"]) < threshold:
        return False
    reset = parse_utc(row.get("resetsAt"))
    projected = parse_utc(forecast.get("projectedRunDry"))
    if reset is None or projected is None or reset <= now:
        return False
    return projected < reset


def _build_alert(card, row, forecast, tier, *, now: datetime, timezone: str, destination: str | None) -> dict:
    provider_name = card.get("providerName") or card.get("providerKey")
    limit_label = row.get("label") or row.get("limitKey")
    line = forecast.get("line") or ""
    suggestion = suggestion_line(
        provider_name=provider_name,
        resets_at=row["resetsAt"],
        timezone=timezone,
        destination_name=destination,
    )
    detail = " · ".join(
        part
        for part in (
            limit_label,
            f"{_pct_text(row['pct'])} used",
            _until_label(row["resetsAt"], now),
            line,
        )
        if part
    )
    title = (
        f"{provider_name} is nearly out"
        if tier == ESCALATE_TIER
        else f"{provider_name} is about to run dry"
    )
    return {
        "id": _alert_id(card["providerKey"], row["limitKey"], row["resetsAt"], tier),
        "providerKey": card["providerKey"],
        "providerName": provider_name,
        "limitKey": row["limitKey"],
        "limitLabel": limit_label,
        "pct": row["pct"],
        "resetsAt": row["resetsAt"],
        "projectedRunDry": forecast.get("projectedRunDry"),
        "line": line,
        "suggestion": suggestion,
        "tier": tier,
        "title": title,
        "body": f"{detail}\n{suggestion}",
    }


def _prune_fired(fired: dict, now: datetime) -> dict:
    kept = {}
    for key, value in fired.items():
        parts = str(key).split("|")
        reset = parse_utc(parts[2]) if len(parts) >= 3 else None
        if reset is not None and now - reset > timedelta(days=14):
            continue
        kept[key] = value
    return kept


def _capacity_cards(connection, now: datetime) -> list[dict]:
    from spend_app.aggregate import _capacity_from_rows, _load_quota_rows

    rows = _load_quota_rows(connection, now=now)
    return _capacity_from_rows(rows, now=now, month_to_date={})


def evaluate_alerts(database_path, *, timezone: str, now: datetime | None = None) -> dict:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    initialize(database_path)
    with connect(database_path) as connection:
        state = load_state(connection)
        cards = _capacity_cards(connection, moment)
        apply_forecasts(
            cards,
            load_pace_samples(connection, moment),
            now=moment,
            active=active_provider_keys(connection, moment),
        )
        models = majority_model_names(connection, moment)
        preferences = state["preferences"]
        quiet = in_quiet_hours(moment, timezone, preferences["quietStart"], preferences["quietEnd"])
        previous = {
            item["id"]: item
            for item in state["active"]
            if isinstance(item, dict) and item.get("id")
        }
        fired = dict(state["fired"])
        pending = dict(state["pending"])
        active = []
        seen = set()
        for card in cards:
            if card.get("isPayg"):
                continue
            destination = _destination_name(cards, card.get("providerKey"), models)
            for row in card.get("rows") or []:
                forecast = row.get("forecast")
                if not isinstance(forecast, dict) or not row.get("resetsAt"):
                    continue
                for tier, threshold in _tiers(preferences, card["providerKey"]):
                    key = _alert_id(card["providerKey"], row["limitKey"], row["resetsAt"], tier)
                    seen.add(key)
                    if not _should_signal(row, forecast, threshold, moment):
                        pending.pop(key, None)
                        continue
                    if key not in fired and quiet and not _urgent(forecast, moment):
                        pending.setdefault(key, {"since": _iso(moment)})
                        continue
                    pending.pop(key, None)
                    if key not in fired:
                        fired[key] = {"at": _iso(moment), "tier": tier}
                    alert = _build_alert(
                        card,
                        row,
                        forecast,
                        tier,
                        now=moment,
                        timezone=timezone,
                        destination=destination,
                    )
                    dismissed = previous.get(key, {}).get("dismissedAt")
                    if dismissed:
                        alert["dismissedAt"] = dismissed
                    active.append(alert)
        for key in list(pending):
            if key not in seen:
                pending.pop(key, None)
        active.sort(key=lambda item: item.get("projectedRunDry") or "")
        state["fired"] = _prune_fired(fired, moment)
        state["pending"] = pending
        state["active"] = active
        save_state(connection, state)
        return state


def save_preferences(database_path, body: dict) -> dict:
    preferences = parse_preferences(body)
    initialize(database_path)
    with connect(database_path) as connection:
        state = load_state(connection)
        state["preferences"] = preferences
        save_state(connection, state)
        return read_forecast_view(connection)


def dismiss_alert(database_path, alert_id: str) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        state = load_state(connection)
        found = False
        for alert in state["active"]:
            if alert.get("id") == alert_id:
                alert["dismissedAt"] = _iso(datetime.now(UTC))
                found = True
        if not found:
            raise ValueError("Unknown alert")
        save_state(connection, state)
        return read_forecast_view(connection)


def forecast_job(database_path, timezone: str) -> None:
    evaluate_alerts(database_path, timezone=timezone)


def register_forecast(scheduler, settings) -> None:
    scheduler.add_job(
        forecast_job,
        "interval",
        seconds=30,
        kwargs={"database_path": settings.database_path, "timezone": settings.timezone},
        id="capacity-forecast",
        coalesce=True,
        max_instances=1,
    )


def forecast_router(settings):
    router = APIRouter()

    @router.post("/api/capacity/forecast-preferences")
    async def update_preferences(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return save_preferences(settings.database_path, body)

    @router.post("/api/capacity/forecast-alerts/dismiss")
    async def dismiss(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        alert_id = body.get("id")
        if not isinstance(alert_id, str) or not alert_id.strip():
            raise ValueError("Missing alert id")
        return dismiss_alert(settings.database_path, alert_id.strip())

    return router
