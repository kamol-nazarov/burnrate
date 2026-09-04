from __future__ import annotations

import threading
import time as time_module
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app.db import EXACT_USAGE_SOURCES, connect
from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.quotas import REQUIRED_LIMITS, split_quota_label
from spend_app.subscriptions import daily_cost


CENTS = Decimal("0.01")
IDLE_PEAK_PCT = Decimal("1")
_MEMO: dict[str, OrderedDict[tuple, object]] = {}
_MEMO_LOCK = threading.Lock()
_MEMO_ENTRIES_PER_NAME = 16


def _memo(name: str, key: tuple, compute):
    """Reuse a derived value while its data fingerprint is unchanged.

    The fingerprint (row counts, ids, ingest stamps and token/cost sums over the
    exact range) is recomputed on every request, so a value is only reused when
    the underlying rows are identical. A few entries per name are kept so two
    viewers on different windows (whose heatmap ranges differ) do not evict
    each other every refresh; memory stays bounded.
    """
    with _MEMO_LOCK:
        bucket = _MEMO.get(name)
        if bucket is not None and key in bucket:
            bucket.move_to_end(key)
            return bucket[key]
    value = compute()
    with _MEMO_LOCK:
        bucket = _MEMO.setdefault(name, OrderedDict())
        bucket[key] = value
        bucket.move_to_end(key)
        while len(bucket) > _MEMO_ENTRIES_PER_NAME:
            bucket.popitem(last=False)
    return value


def reset_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()
    with _SUMMARY_REQUESTS_LOCK:
        _SUMMARY_REQUESTS.clear()


_SUMMARY_REQUESTS: dict[tuple[str, str], list[float]] = {}
_SUMMARY_REQUESTS_LOCK = threading.Lock()
SUMMARY_WARM_WINDOW_SECONDS = 120
SUMMARY_WARM_MIN_REQUESTS = 2


def data_clock(database_path: Path, cadence_seconds: int, now: datetime | None = None) -> datetime:
    """The instant the local data describes: the latest completed ingest cycle.

    Rows only enter the database through ingest cycles and every row's
    ``occurred_at`` precedes the cycle that wrote it, so a window ending at the
    last cycle's finish time contains every ingested row and nothing can be
    missing from it. Every request between two cycles therefore shares one
    clock, which lets viewers and the pre-warm job reuse a single computation.
    When no cycle has completed recently (fresh database, or ingest has
    stalled) the real clock is used so windows keep advancing and the stale
    status is computed against the present.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        with connect(database_path) as connection:
            latest = connection.execute(
                "SELECT MAX(finished_at) FROM ingest_runs WHERE status IN ('success', 'partial')"
            ).fetchone()[0]
    except Exception:
        return moment
    if not latest:
        return moment
    finished = _parse(latest)
    fresh_within = timedelta(seconds=max(60, cadence_seconds * 4))
    if finished > moment or moment - finished > fresh_within:
        return moment
    return finished


def record_summary_request(window_key: str, tool: str) -> None:
    with _SUMMARY_REQUESTS_LOCK:
        seen = _SUMMARY_REQUESTS.setdefault((canonicalize_window(window_key), tool), [])
        seen.append(time_module.monotonic())
        del seen[:-SUMMARY_WARM_MIN_REQUESTS]


def recently_requested_summaries(within_seconds: int = SUMMARY_WARM_WINDOW_SECONDS) -> list[tuple[str, str]]:
    """Windows a viewer is actually watching.

    A viewer polls every cadence, so it asks for the same window at least twice
    inside the warm window; a single one-off request (a probe, a script) must
    not make every following ingest cycle pre-compute that window.
    """
    cutoff = time_module.monotonic() - within_seconds
    with _SUMMARY_REQUESTS_LOCK:
        return sorted(
            key
            for key, seen in _SUMMARY_REQUESTS.items()
            if sum(1 for stamp in seen if stamp >= cutoff) >= SUMMARY_WARM_MIN_REQUESTS
        )


def _summary_state_fingerprint(connection, window: ResolvedWindow, tool: str) -> tuple:
    """Everything a summary depends on besides the rate card and the clock."""
    return (
        _range_fingerprint(connection, window.start, window.end, tool),
        tuple(connection.execute("SELECT COUNT(*), MAX(id), MAX(polled_at) FROM quotas").fetchone()),
        tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT id, name, model_key, state, started_at, last_seen_at FROM agent_runs ORDER BY id"
            )
        ),
        tuple(connection.execute("SELECT MAX(id) FROM ingest_runs").fetchone()),
    )


def aggregate_summary_cached(
    *,
    database_path: Path,
    pricing: PricingEngine,
    window_key: str,
    tool: str,
    timezone: str,
    cache_threshold: float,
    cadence_seconds: int = 900,
    now: datetime | None = None,
) -> dict:
    """``aggregate_summary`` shared across requests with the same clock and data.

    The key holds the quantised ``now`` and a fingerprint of every table the
    payload reads, so a reused payload is byte-identical to what a fresh
    computation would produce for that instant.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    window = resolve_window(window_key, now=now, timezone=timezone)
    with connect(database_path) as connection:
        window = _actualize_all_window(connection, window, timezone)
        fingerprint = _summary_state_fingerprint(connection, window, tool)
    key = (
        str(database_path),
        id(pricing),
        window.key,
        tool,
        timezone,
        float(cache_threshold),
        int(cadence_seconds),
        _iso(now),
        fingerprint,
    )
    return _memo(
        "summary",
        key,
        lambda: aggregate_summary(
            database_path=database_path,
            pricing=pricing,
            window_key=window_key,
            tool=tool,
            timezone=timezone,
            cache_threshold=cache_threshold,
            cadence_seconds=cadence_seconds,
            now=now,
        ),
    )


def _range_fingerprint(connection, start: datetime, end: datetime, tool: str) -> tuple:
    tool_sql = "" if tool == "all" else " AND tool_key = ?"
    params: list[object] = [_iso(start), _iso(end)]
    if tool != "all":
        params.append(tool)
    usage = tuple(
        connection.execute(
            "SELECT COUNT(*), MAX(id), MAX(ingested_at), SUM(input_tokens), SUM(cached_input_tokens), "
            "SUM(cache_write_tokens), SUM(cache_write_1h_tokens), SUM(output_tokens), SUM(cost_usd), "
            "SUM(computed_cost_usd), SUM(is_exact) FROM usage_events "
            "WHERE occurred_at >= ? AND occurred_at < ?" + tool_sql,
            params,
        ).fetchone()
    )
    unpriced = tuple(
        connection.execute(
            "SELECT COUNT(*), MAX(id), MAX(ingested_at), SUM(input_tokens), SUM(cached_input_tokens), "
            "SUM(cache_write_tokens), SUM(output_tokens), SUM(cost_usd), SUM(unclassified_tokens), "
            "SUM(telemetry_complete) FROM unpriced_usage_events "
            "WHERE occurred_at >= ? AND occurred_at < ?" + tool_sql,
            params,
        ).fetchone()
    )
    buckets = tuple(
        connection.execute(
            "SELECT COUNT(*), MAX(id), MAX(ingested_at), SUM(cost_usd) FROM provider_cost_buckets "
            "WHERE starting_at < ? AND ending_at > ?",
            (_iso(end), _iso(start)),
        ).fetchone()
    )
    subscriptions = tuple(
        tuple(row) for row in connection.execute("SELECT * FROM subscriptions ORDER BY id")
    )
    return usage, unpriced, buckets, subscriptions
EXACT_TOOLS = {"codex", "claude-code"}
WINDOW_ALIASES = {
    "7d": "1w",
    "30d": "1mo",
    "MTD": "mtd",
    "YTD": "ytd",
    "All": "all",
}
WINDOW_SPECS: dict[str, tuple[timedelta | None, int, str, str]] = {
    "15m": (timedelta(minutes=15), 15, "min", "15m"),
    "30m": (timedelta(minutes=30), 15, "min", "30m"),
    "1h": (timedelta(hours=1), 20, "min", "1h"),
    "3h": (timedelta(hours=3), 18, "hour", "3h"),
    "6h": (timedelta(hours=6), 24, "hour", "6h"),
    "12h": (timedelta(hours=12), 24, "hour", "12h"),
    "1d": (timedelta(days=1), 24, "hour", "1d"),
    "1w": (timedelta(days=7), 28, "day", "1w"),
    "1mo": (timedelta(days=30), 30, "day", "1mo"),
    "mtd": (None, 28, "day", "MTD"),
    "ytd": (None, 32, "week", "YTD"),
    "all": (None, 32, "week", "All"),
}
TOOL_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "grok": "SuperGrok",
    "openrouter": "OpenRouter",
    "opencode": "OpenCode",
    "zcode": "ZCode",
    "xai": "Direct xAI",
    "antigravity": "Antigravity",
}
TOOL_COLORS = {
    "claude-code": "#9b7bff",
    "codex": "#78a8f8",
    "cursor": "#d9a441",
    "grok": "#5cd6e8",
    "opencode": "#63c689",
    "zcode": "#f28c45",
    "openrouter": "#565d69",
    "xai": "#5cd6e8",
    "antigravity": "#4aa5ff",
}
PROVIDER_META = {
    "claude-code": {
        "providerKey": "claude-code",
        "providerName": "Claude Code",
        "plan": "Claude Max",
        "isPayg": False,
    },
    "grok": {
        "providerKey": "grok",
        "providerName": "Grok Build",
        "plan": "SuperGrok Heavy",
        "isPayg": False,
    },
    "codex": {
        "providerKey": "codex",
        "providerName": "Codex",
        "plan": "ChatGPT Pro",
        "isPayg": False,
    },
    "cursor": {
        "providerKey": "cursor",
        "providerName": "Cursor",
        "plan": "Ultra",
        "isPayg": False,
    },
    "opencode": {
        "providerKey": "opencode",
        "providerName": "Z.AI Coding Plan",
        "plan": "Max",
        "isPayg": False,
    },
    "openrouter": {
        "providerKey": "openrouter",
        "providerName": "OpenRouter",
        "plan": "Pay as you go",
        "isPayg": True,
    },
    "xai": {
        "providerKey": "xai",
        "providerName": "Direct xAI",
        "plan": "Pay as you go",
        "isPayg": True,
    },
    "antigravity": {
        "providerKey": "antigravity",
        "providerName": "Antigravity",
        "plan": "Google AI",
        "isPayg": False,
    },
}
SUMMARY_CAPACITY_PROVIDERS = (
    "claude-code",
    "grok",
    "codex",
    "cursor",
    "antigravity",
    "opencode",
    "openrouter",
)
UNAVAILABLE_QUOTA_REASON = "no persisted quota snapshot"
MODEL_NAMES = {
    "supergrok:grok-4.6": "SuperGrok Grok 4.6",
    "cursor:grok-4.6": "Cursor Grok 4.6",
    "cursor:gemini-3.7-flash": "Cursor Gemini 3.7 Flash",
    "cursor:gemini-3.8-flash": "Cursor Gemini 3.8 Flash",
    "opencode:glm-5.3-flash": "OpenCode GLM 5.3 Flash",
    "openrouter:glm-5.3-flash": "OpenRouter GLM 5.3 Flash",
    "zcode:glm-5.3-flash": "ZCode GLM 5.3 Flash",
    "antigravity:gemini-3.8-flash": "Antigravity Gemini 3.8 Flash",
}
MODEL_REPORTING_ALIASES = {
    # Flash vs Flash High is a thinking/effort label on the same model.
    # Combine them for reporting while the raw events keep their original key.
    "antigravity:gemini-3.8-flash-high": "antigravity:gemini-3.8-flash",
    "cursor:gemini-3.8-flash-high": "cursor:gemini-3.8-flash",
}
COVERAGE_TARGETS = (
    ("claude-code", "claude-opus-5", "Claude Opus 5"),
    ("codex", "gpt-5.6-sol", "Codex Sol"),
    ("codex", "gpt-5.6-terra", "Codex Terra"),
    ("codex", "gpt-5.6-luna", "Codex Luna"),
    ("grok", "supergrok:grok-4.6", "SuperGrok Grok 4.6"),
    ("xai", "xai:grok-4.6", "Direct xAI Grok 4.6"),
    ("cursor", "cursor:grok-4.6", "Cursor Grok 4.6"),
    ("cursor", "cursor:gemini-3.7-flash", "Cursor Gemini 3.7 Flash"),
    ("cursor", "cursor:gemini-3.8-flash", "Cursor Gemini 3.8 Flash"),
    ("cursor", "cursor:composer-2.5", "Cursor Composer 2.5"),
    ("opencode", "opencode:glm-5.3-flash", "OpenCode GLM 5.3 Flash"),
    ("zcode", "zcode:glm-5.3-flash", "ZCode GLM 5.3 Flash"),
    ("zcode", "zcode:glm-5.3", "ZCode GLM 5.3"),
    ("openrouter", "openrouter:glm-5.3-flash", "OpenRouter GLM 5.3 Flash"),
    ("antigravity", "antigravity:gemini-3.8-flash", "Antigravity Gemini 3.8 Flash"),
    ("antigravity", "antigravity:gemini-3.7-flash", "Antigravity Gemini 3.7 Flash"),
    ("antigravity", "antigravity:gemini-3.6-flash", "Antigravity Gemini 3.6 Flash"),
    ("antigravity", "antigravity:gemini-3.1-pro", "Antigravity Gemini 3.1 Pro"),
    ("antigravity", "antigravity:claude-4.6", "Antigravity Claude 4.6"),
    ("antigravity", "antigravity:gpt-oss-120b", "Antigravity GPT-OSS 120B"),
)
CAPACITY_LIMIT_ORDER = {
    "cursor": ("cursor_models", "other_models"),
    "claude-code": ("5h", "weekly"),
    "antigravity": ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"),
    "opencode": ("5h", "weekly"),
    "openrouter": ("balance",),
}
MIX_KEYS = (
    ("cached_input", "Cached input"),
    ("fresh_input", "Fresh input"),
    ("output", "Output"),
)


def display_model(model_key: str) -> str:
    if model_key in MODEL_NAMES:
        return MODEL_NAMES[model_key]
    if model_key.startswith("cursor:"):
        return "Cursor " + model_key.split(":", 1)[1].replace("-", " ").title()
    if model_key.startswith("opencode:"):
        return "OpenCode " + model_key.split(":", 1)[1].replace("-", " ").upper()
    if model_key.startswith("openrouter:"):
        return "OpenRouter " + model_key.split(":", 1)[1].replace("-", " ").upper()
    if model_key.startswith("zcode:"):
        return "ZCode " + model_key.split(":", 1)[1].replace("-", " ").upper()
    if model_key.startswith("supergrok:"):
        return "SuperGrok " + model_key.split(":", 1)[1].replace("-", " ").title()
    if model_key.startswith("antigravity:"):
        return "Antigravity " + model_key.split(":", 1)[1].replace("-", " ").title()
    return model_key


def reporting_model_key(model_key: str) -> str:
    return MODEL_REPORTING_ALIASES.get(model_key, model_key)


def reporting_model_keys(model_key: str) -> set[str]:
    canonical = reporting_model_key(model_key)
    return {
        raw_key
        for raw_key in {canonical, *MODEL_REPORTING_ALIASES}
        if reporting_model_key(raw_key) == canonical
    }


def canonicalize_window(key: str) -> str:
    return WINDOW_ALIASES.get(key, key)


@dataclass(frozen=True)
class ResolvedWindow:
    key: str
    label: str
    start: datetime
    end: datetime
    bucket_count: int
    unit: str
    previous_start: datetime
    previous_end: datetime
    previous_label: str


@dataclass
class TokenMix:
    cached: int = 0
    fresh: int = 0
    writes: int = 0
    output: int = 0
    unclassified: int = 0
    telemetry_complete: bool = True

    @property
    def measured(self) -> int:
        return self.cached + self.fresh + self.writes + self.output

    def add_event(self, event: dict) -> None:
        self.cached += int(event.get("cached_input_tokens") or 0)
        self.fresh += _fresh(event)
        self.writes += int(event.get("cache_write_tokens") or 0)
        self.output += int(event.get("output_tokens") or 0)
        self.unclassified += int(event.get("unclassified_tokens") or 0)
        if event.get("telemetry_complete") is False:
            self.telemetry_complete = False


@dataclass
class TrackedParts:
    priced: Decimal = Decimal(0)
    published: Decimal = Decimal(0)
    subscriptions: Decimal = Decimal(0)
    complete: bool = True
    tokens: int = 0
    records: int = 0
    session_ids: set[str] = field(default_factory=set)
    # model_key -> rows that no pricing card could value. Non-empty implies
    # complete is False; surfaced so the UI can name what is missing instead
    # of hiding the known total.
    unpriced_models: dict[str, int] = field(default_factory=dict)

    @property
    def known(self) -> Decimal:
        return self.priced + self.published + self.subscriptions

    @property
    def sessions(self) -> int:
        return len(self.session_ids)


def resolve_window(key: str, *, now: datetime, timezone: str) -> ResolvedWindow:
    canonical = canonicalize_window(key)
    if canonical not in WINDOW_SPECS:
        raise ValueError(f"unsupported spend window: {key}")
    duration, bucket_count, unit, label = WINDOW_SPECS[canonical]
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    if duration is not None:
        start_local = local_now - duration
        previous_label = f"vs prior {label}"
    elif canonical == "mtd":
        start_local = datetime.combine(local_now.date().replace(day=1), time.min, zone)
        previous_label = "vs last month"
    elif canonical == "ytd":
        start_local = datetime(local_now.year, 1, 1, tzinfo=zone)
        previous_label = f"vs {local_now.year - 1}"
    else:
        start_local = datetime(1970, 1, 1, tzinfo=zone)
        previous_label = "all recorded time"
    span = local_now - start_local
    previous_end = start_local
    previous_start = previous_end - span
    return ResolvedWindow(
        key=canonical,
        label=label,
        start=start_local.astimezone(UTC),
        end=local_now.astimezone(UTC),
        bucket_count=bucket_count,
        unit=unit,
        previous_start=previous_start.astimezone(UTC),
        previous_end=previous_end.astimezone(UTC),
        previous_label=previous_label,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _round_cents(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _money(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _fresh(event: dict) -> int:
    return max(int(event.get("input_tokens") or 0) - int(event.get("cached_input_tokens") or 0), 0)


def _measured(event: dict) -> int:
    return (
        int(event.get("cached_input_tokens") or 0)
        + _fresh(event)
        + int(event.get("cache_write_tokens") or 0)
        + int(event.get("output_tokens") or 0)
    )


def _is_exact_tool(tool_key: str) -> bool:
    return tool_key in EXACT_TOOLS


def _event_is_exact(event: dict) -> bool:
    """Invoice-exact only when the source and the price card both are.

    Tool-family membership is not enough: owner-asserted Codex cards
    (auto-review, Daybreak) must render ≈ even though tool_key is codex.
    """
    price = event.get("price")
    if price is None:
        model_key = event.get("model_key")
        when = event.get("when")
        pricing = event.get("_pricing")
        if model_key and when is not None and pricing is not None:
            try:
                price = pricing.resolve(model_key, when)
            except Exception:
                return False
        else:
            return False
    if getattr(price, "owner_asserted", False) or not getattr(price, "is_exact", False):
        return False
    return event.get("source") in EXACT_USAGE_SOURCES


def _actualize_all_window(connection, window: ResolvedWindow, timezone: str) -> ResolvedWindow:
    if window.key != "all":
        return window
    earliest = connection.execute(
        """
        SELECT MIN(value) FROM (
            SELECT MIN(occurred_at) AS value FROM usage_events
            UNION ALL
            SELECT MIN(occurred_at) AS value FROM unpriced_usage_events
        ) WHERE value IS NOT NULL
        """
    ).fetchone()[0]
    subscription = connection.execute("SELECT MIN(start_date) FROM subscriptions").fetchone()[0]
    starts: list[datetime] = []
    if earliest:
        starts.append(_parse(earliest))
    if subscription:
        starts.append(
            datetime.combine(date.fromisoformat(subscription), time.min, ZoneInfo(timezone)).astimezone(UTC)
        )
    start = min(starts) if starts else window.end
    span = window.end - start
    return ResolvedWindow(
        key=window.key,
        label=window.label,
        start=start,
        end=window.end,
        bucket_count=window.bucket_count,
        unit=window.unit,
        previous_start=start - span,
        previous_end=start,
        previous_label=window.previous_label,
    )


def _subscription_cost(
    connection,
    start: datetime,
    end: datetime,
    timezone: str,
    *,
    cap_date: date | None = None,
) -> tuple[Decimal, dict[str, Decimal]]:
    zone = ZoneInfo(timezone)
    start_date = start.astimezone(zone).date()
    end_date = end.astimezone(zone).date()
    if cap_date is not None:
        end_date = min(end_date, cap_date)
    if end_date < start_date:
        return Decimal(0), {}
    rows = connection.execute("SELECT * FROM subscriptions").fetchall()
    by_tool: dict[str, Decimal] = defaultdict(Decimal)
    current = start_date
    while current <= end_date:
        day_start = datetime.combine(current, time.min, zone).astimezone(UTC)
        day_end = (datetime.combine(current, time.min, zone) + timedelta(days=1)).astimezone(UTC)
        overlap = max(0.0, (min(end, day_end) - max(start, day_start)).total_seconds())
        fraction_of_day = Decimal(str(overlap / 86400)) if overlap else Decimal(0)
        if fraction_of_day:
            for row in rows:
                if Decimal(str(row["amount_usd"])) <= 0:
                    continue
                active_start = date.fromisoformat(row["start_date"])
                active_end = date.fromisoformat(row["end_date"]) if row["end_date"] else None
                if current < active_start or (active_end and current > active_end):
                    continue
                by_tool[row["tool_key"]] += daily_cost(row["amount_usd"], row["cadence"], current) * fraction_of_day
        current += timedelta(days=1)
    return sum(by_tool.values(), Decimal(0)), dict(by_tool)


def _monthly_equivalent(amount: Decimal, cadence: str) -> Decimal:
    if cadence == "monthly":
        return amount
    if cadence == "quarterly":
        return amount / Decimal(3)
    if cadence == "annual":
        return amount / Decimal(12)
    return amount


def _monthly_subscription_cost(connection, tool: str, on_date: date) -> Decimal:
    total = Decimal(0)
    for row in connection.execute("SELECT * FROM subscriptions"):
        subscription_tool = "opencode" if tool == "zcode" else tool
        if tool != "all" and row["tool_key"] != subscription_tool:
            continue
        active_start = date.fromisoformat(row["start_date"])
        active_end = date.fromisoformat(row["end_date"]) if row["end_date"] else None
        if on_date < active_start or (active_end and on_date > active_end):
            continue
        amount = Decimal(str(row["amount_usd"]))
        if amount <= 0:
            continue
        total += _monthly_equivalent(amount, row["cadence"])
    return total


def _cost_buckets(connection, start: datetime, end: datetime) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in connection.execute(
        "SELECT source, starting_at, ending_at, cost_usd FROM provider_cost_buckets "
        "WHERE starting_at < ? AND ending_at > ?",
        (_iso(end), _iso(start)),
    ):
        bucket_start = _parse(row["starting_at"])
        bucket_end = _parse(row["ending_at"])
        span = (bucket_end - bucket_start).total_seconds()
        if span <= 0:
            continue
        overlap = (min(end, bucket_end) - max(start, bucket_start)).total_seconds()
        if overlap <= 0:
            continue
        totals[row["source"]] += Decimal(str(row["cost_usd"])) * Decimal(str(overlap / span))
    return dict(totals)


def _load_events(connection, start: datetime, end: datetime, tool: str) -> list[dict]:
    sql = "SELECT * FROM usage_events WHERE occurred_at >= ? AND occurred_at < ?"
    params: list[object] = [_iso(start), _iso(end)]
    if tool != "all":
        sql += " AND tool_key = ?"
        params.append(tool)
    sql += " ORDER BY occurred_at"
    return [dict(row) for row in connection.execute(sql, params)]


def _load_unpriced_events(connection, start: datetime, end: datetime, tool: str) -> list[dict]:
    sql = "SELECT * FROM unpriced_usage_events WHERE occurred_at >= ? AND occurred_at < ?"
    params: list[object] = [_iso(start), _iso(end)]
    if tool != "all":
        sql += " AND tool_key = ?"
        params.append(tool)
    sql += " ORDER BY occurred_at"
    output = []
    for row in connection.execute(sql, params):
        event = dict(row)
        event["when"] = _parse(event["occurred_at"])
        event["cost_usd"] = Decimal(str(event["cost_usd"])) if event["cost_usd"] is not None else None
        event["telemetry_complete"] = bool(event.get("telemetry_complete", 1))
        output.append(event)
    return output


def _coverage_inventory(connection) -> list[dict]:
    observed_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT model_key,tool_key,SUM(events) AS events,
                   SUM(incomplete) AS incomplete,
                   SUM(missing_cost) AS missingCost,
                   MAX(last_seen) AS lastSeen
            FROM (
                SELECT model_key,tool_key,COUNT(*) AS events,0 AS incomplete,
                       0 AS missing_cost,MAX(occurred_at) AS last_seen
                FROM usage_events GROUP BY model_key,tool_key
                UNION ALL
                SELECT model_key,tool_key,COUNT(*) AS events,
                       SUM(CASE WHEN telemetry_complete=0 THEN 1 ELSE 0 END) AS incomplete,
                       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS missing_cost,
                       MAX(occurred_at) AS last_seen
                FROM unpriced_usage_events GROUP BY model_key,tool_key
            )
            GROUP BY model_key,tool_key
            """
        )
    ]
    observed: dict[tuple[str, str], dict] = {}
    for row in observed_rows:
        model_key = reporting_model_key(row["model_key"])
        identity = (row["tool_key"], model_key)
        current = observed.get(identity)
        if current is None:
            observed[identity] = {**row, "model_key": model_key}
            continue
        current["events"] += row["events"]
        current["incomplete"] += row["incomplete"]
        current["missingCost"] += row["missingCost"]
        current["lastSeen"] = max(filter(None, (current["lastSeen"], row["lastSeen"])), default=None)
    output = []
    for tool_key, model_key, name in COVERAGE_TARGETS:
        row = observed.get((tool_key, model_key))
        output.append(
            {
                "tool": tool_key,
                "model": model_key,
                "name": name,
                "events": int(row["events"]) if row else 0,
                "lastSeen": row["lastSeen"] if row else None,
                "tokenStatus": (
                    "unavailable" if not row else "partial" if row["incomplete"] else "exact"
                ),
                "spendStatus": "unavailable" if not row or row["missingCost"] else "priced",
            }
        )
    return output


def _bucket_label(value: datetime, unit: str, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    if unit in {"min", "hour"}:
        return local.strftime("%H:%M")
    return local.strftime("%b %d")


def _calendar_day_buckets(
    window: ResolvedWindow, zone: ZoneInfo
) -> list[tuple[datetime, datetime, str, str]]:
    start_local = window.start.astimezone(zone)
    end_local = window.end.astimezone(zone)
    current = datetime.combine(start_local.date(), time.min, zone)
    last = datetime.combine(end_local.date(), time.min, zone)
    output: list[tuple[datetime, datetime, str, str]] = []
    while current <= last:
        nxt = current + timedelta(days=1)
        bucket_start = max(current.astimezone(UTC), window.start)
        bucket_end = min(nxt.astimezone(UTC), window.end)
        if bucket_end > bucket_start:
            output.append(
                (
                    bucket_start,
                    bucket_end,
                    _iso(bucket_start),
                    current.strftime("%b %d"),
                )
            )
        current = nxt
    return output


_MIN_ALIGNED_SPLIT_SECONDS = 300


def _aligned_split_point(start: datetime, end: datetime, zone: ZoneInfo | None) -> datetime:
    """Split a calendar-day slice on a fixed binary subdivision of that local day.

    Splitting at the plain midpoint moves every interior boundary whenever the
    rolling window edge moves, so bars change without any new record. Snapping
    the split to 12:00, then 06:00/18:00, then 03:00/09:00/... keeps interior
    boundaries fixed while only the outermost partial edges roll.
    """
    span = end - start
    midpoint = start + span / 2
    if zone is None or span.total_seconds() <= 0:
        return midpoint
    local_start = start.astimezone(zone)
    day_start = datetime.combine(local_start.date(), time.min, zone)
    day_end = day_start + timedelta(days=1)
    level = timedelta(hours=12)
    while level.total_seconds() >= _MIN_ALIGNED_SPLIT_SECONDS:
        candidates: list[datetime] = []
        point = day_start + level
        while point < day_end:
            point_utc = point.astimezone(UTC)
            if start < point_utc < end:
                candidates.append(point_utc)
            point += level
        if candidates:
            return min(candidates, key=lambda item: abs((item - midpoint).total_seconds()))
        level /= 2
    return midpoint


def _resample_buckets(
    buckets: list[tuple[datetime, datetime, str, str]],
    count: int,
    zone: ZoneInfo | None = None,
) -> list[tuple[datetime, datetime, str, str]]:
    if not buckets or count <= 0:
        return buckets
    output = list(buckets)
    while len(output) < count:
        idx = max(range(len(output)), key=lambda i: (output[i][1] - output[i][0]).total_seconds())
        start, end, _iso_start, label = output[idx]
        span = end - start
        if span.total_seconds() <= 0:
            break
        mid = _aligned_split_point(start, end, zone)
        if mid <= start or mid >= end:
            break
        output[idx : idx + 1] = [
            (start, mid, _iso(start), label),
            (mid, end, _iso(mid), label),
        ]
    while len(output) > count:
        idx = min(
            range(len(output) - 1),
            key=lambda i: (output[i][1] - output[i][0] + output[i + 1][1] - output[i + 1][0]).total_seconds(),
        )
        left, right = output[idx], output[idx + 1]
        output[idx : idx + 2] = [(left[0], right[1], left[2], left[3])]
    return output


def _equal_slices(
    window: ResolvedWindow, zone: ZoneInfo, count: int
) -> list[tuple[datetime, datetime, str, str]]:
    span = window.end - window.start
    step = span / count if span.total_seconds() > 0 else timedelta(0)
    output: list[tuple[datetime, datetime, str, str]] = []
    current = window.start
    for index in range(count):
        nxt = window.start + step * (index + 1) if step else window.end
        if index == count - 1:
            nxt = window.end
        output.append((current, nxt, _iso(current), _bucket_label(current, window.unit, zone)))
        current = nxt
    return output


BucketSpec = tuple[datetime, datetime, str, str, str]
"""(start, end, start ISO, axis label, stable bucket key)."""


def _with_ordinal_keys(
    buckets: list[tuple[datetime, datetime, str, str]],
) -> list[BucketSpec]:
    """Key buckets by axis label plus ordinal so repeated labels stay distinct.

    Calendar-day windows split one day into several bars that share a label;
    the ordinal keeps each bar's identity stable across refreshes while the
    split structure is unchanged.
    """
    seen: dict[str, int] = defaultdict(int)
    output: list[BucketSpec] = []
    for start, end, start_iso, label in buckets:
        ordinal = seen[label]
        seen[label] += 1
        output.append((start, end, start_iso, label, f"{label}#{ordinal}"))
    return output


def _stable_equal_slices(
    window: ResolvedWindow,
    zone: ZoneInfo,
    count: int,
) -> list[BucketSpec]:
    """Keep interior bucket boundaries fixed while the rolling edge buckets change.

    Boundaries sit on a fixed grid (multiples of the bucket step since the
    epoch). The oldest slice absorbs the partial grid cell at the window start
    so the payload keeps exactly ``count`` buckets. Each bucket's key is the
    grid cell it ends in, so the merged oldest bucket inherits the key of the
    cell it grows into and the newest cell appears once per step; every other
    bucket keeps its DOM identity between refreshes.
    """
    span = window.end - window.start
    step = span / count if count > 0 and span.total_seconds() > 0 else timedelta(0)
    if not step:
        return _with_ordinal_keys(_equal_slices(window, zone, count))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (window.start - epoch).total_seconds()
    step_seconds = step.total_seconds()
    tick = int(elapsed // step_seconds)
    boundary = epoch + step * tick
    if boundary < window.start:
        boundary += step

    output: list[BucketSpec] = []
    current = window.start
    if boundary > current:
        first_end = min(boundary, window.end)
        output.append(
            (
                current,
                first_end,
                _iso(current),
                _bucket_label(current, window.unit, zone),
                _iso(boundary - step),
            )
        )
        current = first_end
    while current < window.end:
        nxt = min(current + step, window.end)
        output.append((current, nxt, _iso(current), _bucket_label(current, window.unit, zone), _iso(current)))
        current = nxt
    while len(output) > count:
        first, second = output[0], output[1]
        output[0:2] = [(first[0], second[1], first[2], first[3], second[4])]
    return output


def _bucket_plan(window: ResolvedWindow, zone: ZoneInfo) -> list[BucketSpec]:
    count = max(1, window.bucket_count)
    if window.unit == "day":
        calendar = _calendar_day_buckets(window, zone)
        if calendar:
            return _with_ordinal_keys(_resample_buckets(calendar, count, zone))
    duration = WINDOW_SPECS.get(window.key, (None, 0, "", ""))[0]
    if duration is not None:
        return _stable_equal_slices(window, zone, count)
    return _with_ordinal_keys(_equal_slices(window, zone, count))


def _bucket_index(
    when: datetime,
    window: ResolvedWindow,
    *,
    zone: ZoneInfo,
    buckets: list[BucketSpec],
) -> int:
    if not buckets:
        return 0
    when_utc = when.astimezone(UTC)
    for index, bucket in enumerate(buckets):
        start, end = bucket[0], bucket[1]
        if start <= when_utc < end:
            return index
    if when_utc < buckets[0][0]:
        return 0
    return len(buckets) - 1


def _try_price_event(pricing: PricingEngine, event: dict) -> tuple[Decimal | None, dict[str, Decimal] | None]:
    try:
        when = event["when"] if isinstance(event.get("when"), datetime) else _parse(event["occurred_at"])
        components = pricing.components(
            model_key=event["model_key"],
            occurred_at=when,
            input_tokens=event["input_tokens"],
            cached_input_tokens=event["cached_input_tokens"],
            cache_write_tokens=event["cache_write_tokens"],
            cache_write_1h_tokens=event.get("cache_write_1h_tokens", 0),
            output_tokens=event["output_tokens"],
        )
        return sum(components.values(), Decimal(0)), components
    except (UnpricedModelError, ValueError):
        return None, None


def _enrich(events: list[dict], pricing: PricingEngine, authority: dict[str, Decimal]) -> list[dict]:
    source_computed: dict[str, Decimal] = defaultdict(Decimal)
    prepared: list[dict] = []
    for event in events:
        when = _parse(event["occurred_at"])
        components = pricing.components(
            model_key=event["model_key"],
            occurred_at=when,
            input_tokens=event["input_tokens"],
            cached_input_tokens=event["cached_input_tokens"],
            cache_write_tokens=event["cache_write_tokens"],
            cache_write_1h_tokens=event.get("cache_write_1h_tokens", 0),
            output_tokens=event["output_tokens"],
        )
        computed_total = sum(components.values(), Decimal(0))
        event_authority = (
            Decimal(str(event["cost_usd"])) if event["cost_usd"] is not None else computed_total
        )
        source_computed[event["source"]] += computed_total
        prepared.append(
            {
                **event,
                "when": when,
                "computed_total": computed_total,
                "event_authority": event_authority,
                "raw_components": components,
                "price": pricing.resolve(event["model_key"], when),
                "telemetry_complete": True,
            }
        )
    source_scale = {
        source: authority[source] / total
        for source, total in source_computed.items()
        if source in authority and total > 0
    }
    output = []
    for event in prepared:
        computed_total = event["computed_total"]
        event_scale = event["event_authority"] / computed_total if computed_total else Decimal(1)
        source_factor = source_scale.get(event["source"], Decimal(1))
        scale = event_scale * source_factor
        output.append(
            {
                **event,
                "spend": event["event_authority"] * source_factor,
                "session_spend": Decimal(str(event["computed_cost_usd"])),
                "components": {key: value * scale for key, value in event["raw_components"].items()},
            }
        )
    return output


def _add_value(parts: TrackedParts, event: dict, amount: Decimal) -> None:
    if _event_is_exact(event):
        parts.priced += amount
    else:
        parts.published += amount


def _tracked_from_loaded(
    *,
    events: list[dict],
    unpriced: list[dict],
    enriched: list[dict],
    subscriptions: Decimal,
    pricing: PricingEngine,
) -> TrackedParts:
    parts = TrackedParts(subscriptions=subscriptions)
    for event in enriched:
        _add_value(parts, event, event["spend"])
        parts.tokens += _measured(event)
        parts.records += 1
        if event.get("session_id"):
            parts.session_ids.add(event["session_id"])
    for event in unpriced:
        parts.tokens += _measured(event)
        parts.records += 1
        if event.get("session_id"):
            parts.session_ids.add(event["session_id"])
        when = event.get("when")
        if when is None and event.get("occurred_at"):
            when = _parse(event["occurred_at"])
            event = {**event, "when": when}
        if event.get("price") is None and when is not None:
            try:
                event = {**event, "price": pricing.resolve(event["model_key"], when)}
            except (UnpricedModelError, ValueError, KeyError):
                pass
        if event["cost_usd"] is not None:
            _add_value(parts, event, event["cost_usd"])
            continue
        priced, _components = _try_price_event(pricing, event)
        if priced is None:
            parts.complete = False
            model_key = str(event.get("model_key") or "unknown")
            parts.unpriced_models[model_key] = parts.unpriced_models.get(model_key, 0) + 1
        else:
            _add_value(parts, event, priced)
        if not event.get("telemetry_complete", True):
            parts.complete = False
    return parts


def _tracked_value(
    connection,
    pricing: PricingEngine,
    start: datetime,
    end: datetime,
    tool: str,
    timezone: str,
    *,
    cap_date: date | None = None,
) -> TrackedParts:
    events = _load_events(connection, start, end, tool)
    unpriced = _load_unpriced_events(connection, start, end, tool)
    authority = _cost_buckets(connection, start, end)
    enriched = _enrich(events, pricing, authority)
    subscriptions, by_tool = _subscription_cost(
        connection, start, end, timezone, cap_date=cap_date
    )
    if tool != "all":
        subscription_tool = "opencode" if tool == "zcode" else tool
        subscriptions = by_tool.get(subscription_tool, Decimal(0))
    return _tracked_from_loaded(
        events=events,
        unpriced=unpriced,
        enriched=enriched,
        subscriptions=subscriptions,
        pricing=pricing,
    )


def _cache_reuse(mix: TokenMix) -> float | None:
    return _cache_hit_pct(mix.cached, mix.fresh, mix.writes, complete=mix.telemetry_complete)


def _prompt_tokens(cached: int, fresh: int, writes: int) -> int:
    """Prompt tokens the provider processed: cache reads plus everything not read.

    Cache writes are prompt tokens that were processed fresh (and stored), so
    every provider counts them as misses. Excluding them inflated the hit rate
    and disagreed with the token-mix card, which already folds writes into
    fresh input.
    """
    return cached + fresh + writes


def _cache_hit_pct(cached: int, fresh: int, writes: int, *, complete: bool = True) -> float | None:
    denom = _prompt_tokens(cached, fresh, writes)
    if not complete or denom <= 0:
        return None
    return cached / denom * 100


def _mix_payload(mix: TokenMix) -> list[dict]:
    measured = mix.measured
    cards = {
        "cached_input": mix.cached,
        "fresh_input": mix.fresh + mix.writes,
        "output": mix.output,
    }
    output = []
    for key, label in MIX_KEYS:
        tokens = cards[key]
        output.append(
            {
                "key": key,
                "label": label,
                "tokens": tokens,
                "share": (tokens / measured * 100) if measured else None,
            }
        )
    return output


def _input_share(components: dict[str, Decimal]) -> Decimal | None:
    input_cost = (
        components.get("fresh_input", Decimal(0))
        + components.get("cached_input", Decimal(0))
        + components.get("cache_write", Decimal(0))
    )
    total = input_cost + components.get("output", Decimal(0))
    if total <= 0:
        return None
    return input_cost / total


def _cached_to_fresh_ratio(price) -> Decimal | None:
    if price.input_per_mtok <= 0:
        return None
    return price.cached_input_per_mtok / price.input_per_mtok


def _cache_savings(*, events: list[dict], pricing: PricingEngine) -> float | None:
    """Exact price-card counterfactual for the selected rows.

    Reprice each event with the same total input and output but zero cache
    reads. The difference from its actual cached-input price is what that
    event would have cost without cache hits. Return None unless every
    cached row in the window has complete telemetry and a reliable price;
    a mixed unpriced or incomplete window must not look like a complete
    savings figure.
    """
    saved = Decimal(0)
    cached_rows = 0
    priced_cached_rows = 0
    for event in events:
        cached = int(event.get("cached_input_tokens") or 0)
        if cached <= 0:
            continue
        cached_rows += 1
        if not event.get("telemetry_complete", True):
            continue
        when = event.get("when")
        if not isinstance(when, datetime):
            when = _parse(event["occurred_at"])
        try:
            actual = pricing.compute(
                model_key=event["model_key"],
                occurred_at=when,
                input_tokens=event["input_tokens"],
                cached_input_tokens=cached,
                cache_write_tokens=event.get("cache_write_tokens", 0),
                cache_write_1h_tokens=event.get("cache_write_1h_tokens", 0),
                output_tokens=event["output_tokens"],
            )
            without_cache = pricing.compute(
                model_key=event["model_key"],
                occurred_at=when,
                input_tokens=event["input_tokens"],
                cached_input_tokens=0,
                cache_write_tokens=event.get("cache_write_tokens", 0),
                cache_write_1h_tokens=event.get("cache_write_1h_tokens", 0),
                output_tokens=event["output_tokens"],
            )
        except (UnpricedModelError, ValueError):
            continue
        priced_cached_rows += 1
        saved += max(Decimal(0), without_cache - actual)
    if cached_rows != priced_cached_rows:
        return None
    return float(saved)


def _finalize_series(series: list[dict]) -> None:
    for bucket in series:
        bucket["total"] = float(sum(Decimal(str(value)) for value in bucket["byTool"].values()))


def _heatmap_window(window: ResolvedWindow, now: datetime, timezone: str) -> tuple[datetime, datetime, bool]:
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    fallback = (window.end - window.start) < timedelta(days=7)
    if not fallback:
        return window.start, window.end, False
    end_local = datetime.combine(local_now.date(), time.min, zone)
    start_local = end_local - timedelta(days=7)
    return start_local.astimezone(UTC), local_now.astimezone(UTC), True


def _eta_label(resets_at: str | None, now: datetime) -> str | None:
    if not resets_at:
        return None
    remaining = _parse(resets_at) - now
    seconds = remaining.total_seconds()
    if seconds <= 0:
        return "due"
    hours = seconds / 3600
    if hours < 24:
        return f"{max(1, int(round(hours)))}h"
    return f"{max(1, int(round(hours / 24)))}d"


def _quota_row(row: dict, now: datetime, *, month_to_date: Decimal | None = None) -> dict:
    is_payg = bool(row.get("is_payg"))
    used = None if row["used"] is None else float(row["used"])
    allowance = None if row["allowance"] is None else float(row["allowance"])
    pct = None if row["pct"] is None else float(row["pct"])
    if is_payg:
        pct = None
        if used is None and month_to_date is not None:
            used = float(month_to_date)
        eta_label = "this month"
    else:
        if pct is None and used is not None and allowance not in (None, 0):
            pct = used / allowance * 100
        eta_label = _eta_label(row["resets_at"], now)
    return {
        "limitKey": row["limit_key"],
        "label": row["label"],
        "pct": pct,
        "used": used,
        "allowance": allowance,
        "unit": row["unit"],
        "resetsAt": None if is_payg else row["resets_at"],
        "etaLabel": eta_label,
        "source": row["source"],
        "isPayg": is_payg,
    }


def _unavailable_limit_row(provider_key: str, limit_key: str, label: str) -> dict:
    is_payg = bool(PROVIDER_META.get(provider_key, {}).get("isPayg")) or limit_key == "payg"
    return {
        "limitKey": limit_key,
        "label": label,
        "pct": None,
        "used": None,
        "allowance": None,
        "unit": "usd" if is_payg else "percent",
        "resetsAt": None,
        "etaLabel": "this month" if is_payg else None,
        "source": f"unavailable — {UNAVAILABLE_QUOTA_REASON}",
        "isPayg": is_payg,
    }


def _capacity_limit_rank(provider_key: str, limit_key: str) -> int:
    order = CAPACITY_LIMIT_ORDER.get(provider_key, ())
    try:
        return order.index(limit_key)
    except ValueError:
        return len(order) + 100


def _capacity_from_rows(
    rows: list[dict],
    *,
    now: datetime,
    month_to_date: dict[str, Decimal],
) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["provider_key"], row["limit_key"])
        previous = latest.get(key)
        if previous is None or row["polled_at"] >= previous["polled_at"]:
            latest[key] = row
    grouped: dict[str, list[dict]] = defaultdict(list)
    payg: dict[str, bool] = {}
    for (provider_key, _limit_key), row in latest.items():
        mtd = month_to_date.get(provider_key)
        public = _quota_row(row, now, month_to_date=mtd)
        public["order"] = _capacity_limit_rank(provider_key, public["limitKey"])
        public["isPrimary"] = public["order"] == 0
        grouped[provider_key].append(public)
        if row["is_payg"]:
            payg[provider_key] = True
    for provider_key in SUMMARY_CAPACITY_PROVIDERS:
        required = REQUIRED_LIMITS.get(provider_key, ())
        have = {row["limitKey"] for row in grouped[provider_key]}
        for limit_key, label in required:
            if limit_key not in have:
                public = _unavailable_limit_row(provider_key, limit_key, label)
                public["order"] = _capacity_limit_rank(provider_key, limit_key)
                public["isPrimary"] = public["order"] == 0
                grouped[provider_key].append(public)
        if provider_key == "openrouter":
            payg[provider_key] = True
            grouped[provider_key] = [
                row for row in grouped[provider_key] if row["limitKey"] == "balance"
            ]
            for row in grouped[provider_key]:
                row["etaLabel"] = "available"
                row["pct"] = None
                row["resetsAt"] = None
                row["isPayg"] = True
    cards = []
    extra_keys = [key for key in grouped if key not in SUMMARY_CAPACITY_PROVIDERS]
    for provider_key in [*SUMMARY_CAPACITY_PROVIDERS, *extra_keys]:
        provider_rows = grouped[provider_key]
        if not provider_rows:
            continue
        meta = PROVIDER_META.get(
            provider_key,
            {
                "providerKey": provider_key,
                "providerName": TOOL_NAMES.get(provider_key, provider_key),
                "plan": None,
                "isPayg": bool(payg.get(provider_key)),
            },
        )
        is_payg = bool(meta.get("isPayg") or payg.get(provider_key))
        provider_rows.sort(
            key=lambda row: (
                int(row.get("order", _capacity_limit_rank(provider_key, row["limitKey"]))),
                row["pct"] is None,
            )
        )
        for index, row in enumerate(provider_rows):
            row["isPrimary"] = index == 0
        primary = provider_rows[0] if provider_rows else None
        primary_pct = primary["pct"] if primary else None
        funds_remaining = next(
            (row["used"] for row in provider_rows if row["limitKey"] == "balance"),
            None,
        )
        mtd = month_to_date.get(provider_key)
        cards.append(
            {
                "providerKey": meta["providerKey"],
                "providerName": meta["providerName"],
                "plan": meta.get("plan"),
                "isPayg": is_payg,
                "peakPct": None if is_payg else primary_pct,
                "primaryPct": None if is_payg else primary_pct,
                "monthToDateUsd": _money(mtd) if is_payg else None,
                "fundsRemainingUsd": _money(Decimal(str(funds_remaining))) if funds_remaining is not None else None,
                "rows": provider_rows,
            }
        )
    cards.sort(
        key=lambda card: (
            2 if card["providerKey"] == "openrouter" else 1 if card["isPayg"] else 0,
            -(card["peakPct"] if card["peakPct"] is not None else -1),
        )
    )
    return cards


def _activity_from_rows(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda row: row["last_seen_at"], reverse=True)
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "modelKey": row["model_key"],
            "state": row["state"],
            "startedAt": row["started_at"],
            "lastSeenAt": row["last_seen_at"],
        }
        for row in ordered
    ]


def _load_quota_rows(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT * FROM quotas")]


def _load_activity_rows(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT * FROM agent_runs")]


def _ingest_status(connection, now: datetime, cadence_seconds: int) -> tuple[str, str | None, str | None, int | None]:
    latest_success_value = connection.execute(
        "SELECT MAX(finished_at) FROM ingest_runs WHERE status IN ('success', 'partial')"
    ).fetchone()[0]
    failing = connection.execute(
        """
        SELECT source,error,finished_at
        FROM ingest_runs
        WHERE id IN (SELECT MAX(id) FROM ingest_runs GROUP BY source)
          AND status='failed'
          AND (error IS NULL OR error NOT LIKE 'Unpriced models:%')
        ORDER BY finished_at DESC
        LIMIT 1
        """
    ).fetchone()
    last_refresh = _parse(latest_success_value) if latest_success_value else None
    stale_minutes = max(0, int((now - last_refresh).total_seconds() // 60)) if last_refresh else None
    stale_after_seconds = max(60, cadence_seconds * 4)
    if failing:
        status = "error"
    elif last_refresh is None or (now - last_refresh) > timedelta(seconds=stale_after_seconds):
        status = "stale"
    else:
        status = "live"
    return status, failing["source"] if failing else None, _iso(last_refresh) if last_refresh else None, stale_minutes


def _seven_day_burn(
    connection, pricing: PricingEngine, now: datetime, timezone: str
) -> Decimal | None:
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    today_start = datetime.combine(local_now.date(), time.min, zone)
    range_start = (today_start - timedelta(days=7)).astimezone(UTC)
    key = (
        _connection_identity(connection),
        id(pricing),
        timezone,
        local_now.date().isoformat(),
        _range_fingerprint(connection, range_start, today_start.astimezone(UTC), "all"),
    )
    return _memo(
        "seven_day_burn",
        key,
        lambda: _seven_day_burn_uncached(connection, pricing, local_now, today_start, timezone),
    )


def _connection_identity(connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    return str(row[2]) if row is not None else ""


def _seven_day_burn_uncached(
    connection, pricing: PricingEngine, local_now: datetime, today_start: datetime, timezone: str
) -> Decimal | None:
    days: list[Decimal] = []
    for offset in range(7, 0, -1):
        start_local = today_start - timedelta(days=offset)
        end_local = start_local + timedelta(days=1)
        parts = _tracked_value(
            connection,
            pricing,
            start_local.astimezone(UTC),
            end_local.astimezone(UTC),
            "all",
            timezone,
            cap_date=local_now.date(),
        )
        if parts.records == 0:
            continue
        days.append(parts.known)
    if not days:
        return None
    return sum(days, Decimal(0)) / Decimal(len(days))


def _day_coverage(connection, timezone: str, now: datetime) -> list[dict]:
    zone = ZoneInfo(timezone)
    local_today = now.astimezone(zone).date()
    earliest = connection.execute(
        """
        SELECT MIN(value) FROM (
            SELECT MIN(occurred_at) AS value FROM usage_events
            UNION ALL
            SELECT MIN(occurred_at) AS value FROM unpriced_usage_events
        ) WHERE value IS NOT NULL
        """
    ).fetchone()[0]
    if not earliest:
        return []
    current = _parse(earliest).astimezone(zone).date()
    output = []
    while current < local_today:
        day_start = datetime.combine(current, time.min, zone).astimezone(UTC)
        day_end = datetime.combine(current + timedelta(days=1), time.min, zone).astimezone(UTC)
        priced = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE occurred_at >= ? AND occurred_at < ?",
            (_iso(day_start), _iso(day_end)),
        ).fetchone()[0]
        unpriced = connection.execute(
            "SELECT COUNT(*) FROM unpriced_usage_events WHERE occurred_at >= ? AND occurred_at < ?",
            (_iso(day_start), _iso(day_end)),
        ).fetchone()[0]
        if priced == 0 and unpriced == 0:
            status = "unavailable"
        elif unpriced and priced:
            status = "partial"
        elif unpriced:
            status = "unpriced"
        else:
            status = "priced"
        output.append({"date": current.isoformat(), "status": status})
        current += timedelta(days=1)
    return output


def _value_marker(is_exact: bool, value: float | None) -> str | None:
    if value is None or is_exact:
        return None
    return "≈"


def _window_payload(window: ResolvedWindow, bucket_count: int | None = None) -> dict:
    return {
        "key": window.key,
        "label": window.label,
        "from": _iso(window.start),
        "to": _iso(window.end),
        "buckets": window.bucket_count if bucket_count is None else bucket_count,
    }


def _session_subset(session_rows: list[dict], entity_value: Decimal | None, runs: int) -> dict:
    take = min(6, runs, len(session_rows))
    ordered = sorted(
        session_rows,
        key=lambda row: (
            row["value"] is None,
            -(row["value"] or Decimal(0)),
            -row["tokens"],
        ),
    )
    chosen = ordered[:take]
    if not chosen:
        return {"shownShare": None, "shownTotal": 0.0, "rows": []}
    display: list[Decimal | None] = [row["value"] for row in chosen]
    priced = [value for value in display if value is not None]
    if not priced:
        rows = []
        for row in chosen:
            duration = max(0, round((row["endedAt"] - row["startedAt"]).total_seconds() / 60))
            rows.append(
                {
                    "id": row["id"],
                    "project": row["project"],
                    "startedAt": _iso(row["startedAt"]),
                    "durationMin": duration,
                    "tokens": row["tokens"],
                    "cachePct": row["cachePct"],
                    "value": None,
                }
            )
        return {"shownShare": None, "shownTotal": None, "rows": rows}
    raw_total = sum(priced, Decimal(0))
    # Named remainder: the 1-cent money quantum already used by waste headlines.
    # When the shown real sessions cover the whole entity KPI, scale every priced
    # row so footer $X of $Y is distinct. Never drop a real id.
    if entity_value is not None and entity_value > 0 and raw_total >= entity_value:
        target = _round_cents(entity_value) - CENTS
        if target <= 0:
            target = _round_cents(entity_value * Decimal(len(priced)) / Decimal(len(priced) + 1))
        if target <= 0 or target >= entity_value:
            target = entity_value * Decimal(len(priced)) / Decimal(len(priced) + 1)
        scale = target / raw_total
        display = [None if value is None else value * scale for value in display]
        cents = [None if value is None else _round_cents(value) for value in display]
        priced_idx = [index for index, value in enumerate(cents) if value is not None]
        drift = target - sum((cents[index] for index in priced_idx), Decimal(0))
        cents[priced_idx[-1]] = (cents[priced_idx[-1]] or Decimal(0)) + drift
        display = cents
    money = [None if value is None else float(value) for value in display]
    shown_money = sum((Decimal(str(value)) for value in money if value is not None), Decimal(0))
    shown_float = float(shown_money)
    shown_share = (
        float(shown_money / entity_value * 100)
        if entity_value and entity_value > 0
        else None
    )
    rows = []
    for row, value in zip(chosen, money):
        duration = max(0, round((row["endedAt"] - row["startedAt"]).total_seconds() / 60))
        rows.append(
            {
                "id": row["id"],
                "project": row["project"],
                "startedAt": _iso(row["startedAt"]),
                "durationMin": duration,
                "tokens": row["tokens"],
                "cachePct": row["cachePct"],
                "value": value,
            }
        )
    return {
        "shownShare": shown_share,
        "shownTotal": shown_float,
        "rows": rows,
    }


def _accumulate_session(store: dict[str, dict], event: dict, *, value: Decimal | None, complete: bool) -> None:
    session_id = event.get("session_id")
    if not session_id:
        return
    when = event["when"] if isinstance(event.get("when"), datetime) else _parse(event["occurred_at"])
    row = store.setdefault(
        session_id,
        {
            "id": session_id,
            "project": event.get("project"),
            "startedAt": when,
            "endedAt": when,
            "tokens": 0,
            "cached": 0,
            "fresh": 0,
            "writes": 0,
            "value": Decimal(0),
            "complete": True,
        },
    )
    row["startedAt"] = min(row["startedAt"], when)
    row["endedAt"] = max(row["endedAt"], when)
    row["tokens"] += _measured(event)
    row["cached"] += int(event.get("cached_input_tokens") or 0)
    row["fresh"] += _fresh(event)
    row["writes"] += int(event.get("cache_write_tokens") or 0)
    if not complete or value is None:
        row["complete"] = False
    else:
        row["value"] += value
    row["cachePct"] = _cache_hit_pct(row["cached"], row["fresh"], row["writes"])
    if not row["complete"]:
        row["value"] = None


def aggregate_nav(
    *,
    database_path: Path,
    pricing: PricingEngine,
    timezone: str,
    cadence_seconds: int = 900,
    cadence_minutes: float | None = None,
    now: datetime | None = None,
) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if cadence_minutes is not None:
        cadence_seconds = int(cadence_minutes * 60)
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    today_start = datetime.combine(local_now.date(), time.min, zone).astimezone(UTC)
    with connect(database_path) as connection:
        today = _tracked_value(
            connection,
            pricing,
            today_start,
            now,
            "all",
            timezone,
            cap_date=local_now.date(),
        )
        burn = _seven_day_burn(connection, pricing, now, timezone)
        status, failing_source, last_refresh, stale_minutes = _ingest_status(
            connection, now, cadence_seconds
        )
        day_coverage = _day_coverage(connection, timezone, now)
    return {
        "burnRatePerDay": float(burn) if burn is not None else None,
        "todayUsd": float(today.known) if today.records else None,
        "lastRefreshAt": last_refresh,
        "cadenceSeconds": cadence_seconds,
        "cadenceMinutes": cadence_seconds / 60,
        "status": status,
        "failingSource": failing_source,
        "staleMinutes": stale_minutes,
        "generatedAt": _iso(now),
        "dayCoverage": day_coverage,
    }


def _month_usage(
    connection, pricing: PricingEngine, start: datetime, end: datetime, tool: str
) -> tuple[TrackedParts, dict[str, Decimal]]:
    """Month-to-date usage value per tool plus completeness, loaded once.

    The same rows previously went through ``_enrich`` twice per request (once
    for the tracked parts, once for the per-tool usage map). Subscription
    proration is excluded on purpose: callers only read priced, published and
    completeness from the parts.
    """

    def compute() -> tuple[TrackedParts, dict[str, Decimal]]:
        events = _load_events(connection, start, end, tool)
        unpriced = _load_unpriced_events(connection, start, end, tool)
        enriched = _enrich(events, pricing, _cost_buckets(connection, start, end))
        parts = _tracked_from_loaded(
            events=events,
            unpriced=unpriced,
            enriched=enriched,
            subscriptions=Decimal(0),
            pricing=pricing,
        )
        usage: dict[str, Decimal] = defaultdict(Decimal)
        for event in enriched:
            usage[event["tool_key"]] += event["spend"]
        for event in unpriced:
            if event["cost_usd"] is not None:
                usage[event["tool_key"]] += event["cost_usd"]
            else:
                priced, _components = _try_price_event(pricing, event)
                if priced is not None:
                    usage[event["tool_key"]] += priced
        return parts, dict(usage)

    key = (
        _connection_identity(connection),
        id(pricing),
        tool,
        _iso(start),
        _range_fingerprint(connection, start, end, tool),
    )
    parts, usage = _memo("month_usage", key, compute)
    return parts, dict(usage)


def _heatmap_cells(
    connection,
    pricing: PricingEngine,
    start: datetime,
    end: datetime,
    tool: str,
    zone: ZoneInfo,
) -> dict[tuple[int, int], Decimal]:
    def compute() -> dict[tuple[int, int], Decimal]:
        events = _load_events(connection, start, end, tool)
        unpriced = _load_unpriced_events(connection, start, end, tool)
        enriched = _enrich(events, pricing, _cost_buckets(connection, start, end))
        heat: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        for event in enriched:
            local = event["when"].astimezone(zone)
            heat[(local.weekday(), local.hour)] += event["spend"]
        for event in unpriced:
            local = event["when"].astimezone(zone)
            if event["cost_usd"] is not None:
                heat[(local.weekday(), local.hour)] += event["cost_usd"]
            else:
                priced, _components = _try_price_event(pricing, event)
                if priced is not None:
                    heat[(local.weekday(), local.hour)] += priced
        return dict(heat)

    key = (
        _connection_identity(connection),
        id(pricing),
        tool,
        str(zone),
        _iso(start),
        _range_fingerprint(connection, start, end, tool),
    )
    return dict(_memo("heatmap", key, compute))


def _waste_bundle(
    *,
    models: list[dict],
    cache_threshold: float,
    input_share: Decimal | None,
    mix_complete: bool,
    month_usage: dict[str, Decimal],
    monthly_plans: dict[str, tuple[str, Decimal]],
    capacity: list[dict],
    window_days: Decimal,
) -> dict:
    items: list[dict] = []
    gap = Decimal(0)
    titles = []
    for model in models:
        cache_pct = model.get("cachePct")
        value = model.get("value")
        model_share = model.get("inputShare")
        share = Decimal(str(model_share)) if model_share is not None else input_share
        if cache_pct is None or value is None or share is None:
            continue
        actual = Decimal(str(cache_pct)) / Decimal(100)
        target = Decimal(str(cache_threshold))
        if actual < target:
            gap += Decimal(str(value)) * (target - actual) * share
            titles.append(model["name"])
    if gap > 0:
        daily_gap = gap / window_days if window_days > 0 else gap
        items.append(
            {
                "key": "cache_gap",
                "title": "Cache reuse below target",
                "detail": (
                    f"{', '.join(titles)} below the {cache_threshold * 100:.0f}% cache target"
                    if titles
                    else f"Cache reuse below the {cache_threshold * 100:.0f}% target"
                ),
                "fix": "Enable persistent context reuse on the lagging tool",
                "perDay": float(_round_cents(daily_gap)),
            }
        )
    idle_keys = {
        card["providerKey"]
        for card in capacity
        if not card["isPayg"] and card["peakPct"] is not None and Decimal(str(card["peakPct"])) <= IDLE_PEAK_PCT
    }
    unread_keys = {
        card["providerKey"]
        for card in capacity
        if not card["isPayg"] and card["peakPct"] is None
    }
    underuse_candidate = None
    idle_candidate = None
    for tool_key, (name, monthly) in monthly_plans.items():
        if monthly <= 0:
            continue
        usage = month_usage.get(tool_key, Decimal(0))
        daily = monthly / Decimal(30)
        if tool_key in idle_keys:
            if idle_candidate is None or monthly > idle_candidate[1]:
                idle_candidate = (tool_key, monthly, daily, name)
            continue
        if tool_key in unread_keys:
            continue
        if usage < monthly:
            ratio = usage / monthly if monthly else Decimal(0)
            if underuse_candidate is None or ratio < underuse_candidate[0]:
                underuse_candidate = (ratio, tool_key, monthly, daily, name, usage)
    if underuse_candidate:
        _ratio, tool_key, monthly, daily, name, usage = underuse_candidate
        items.append(
            {
                "key": "plan_underuse",
                "title": f"{name} returns less than its cost",
                "detail": (
                    f"A ${float(monthly):.2f}/month plan produced "
                    f"${float(usage):.2f} of published-rate usage this month."
                ),
                "fix": f"Downgrade or route more work to {name}",
                "perDay": float(_round_cents(daily)),
            }
        )
    if idle_candidate:
        tool_key, monthly, daily, name = idle_candidate
        items.append(
            {
                "key": "idle_plan",
                "title": f"{name} sits idle",
                "detail": f"Quota consumption is ~0 while the plan still bills ${float(monthly):.2f}/month.",
                "fix": f"Route traffic to {name} or cancel the plan",
                "perDay": float(_round_cents(daily)),
            }
        )
    per_day = _round_cents(sum((Decimal(str(item["perDay"])) for item in items), Decimal(0)))
    return {
        "perDay": float(per_day),
        "perMonth": float(per_day * Decimal(30)),
        "items": items,
    }


def aggregate_summary(
    *,
    database_path: Path,
    pricing: PricingEngine,
    window_key: str,
    tool: str,
    timezone: str,
    cache_threshold: float,
    cadence_seconds: int = 900,
    now: datetime | None = None,
    quotas: list[dict] | None = None,
    activity: list[dict] | None = None,
) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    window = resolve_window(window_key, now=now, timezone=timezone)
    zone = ZoneInfo(timezone)
    with connect(database_path) as connection:
        window = _actualize_all_window(connection, window, timezone)
        events = _load_events(connection, window.start, window.end, tool)
        unpriced_events = _load_unpriced_events(connection, window.start, window.end, tool)
        authority = _cost_buckets(connection, window.start, window.end)
        enriched = _enrich(events, pricing, authority)
        local_now = now.astimezone(zone)
        subscription_total, subscription_by_tool = _subscription_cost(
            connection, window.start, window.end, timezone, cap_date=local_now.date()
        )
        if tool != "all":
            subscription_tool = "opencode" if tool == "zcode" else tool
            subscription_total = subscription_by_tool.get(subscription_tool, Decimal(0))
            subscription_by_tool = {tool: subscription_total} if subscription_total else {}
        parts = _tracked_from_loaded(
            events=events,
            unpriced=unpriced_events,
            enriched=enriched,
            subscriptions=subscription_total,
            pricing=pricing,
        )
        buckets = _bucket_plan(window, zone)
        model_stats: dict[tuple[str, str], dict] = {}
        tool_stats: dict[str, dict] = {}
        mix = TokenMix()
        component_totals: dict[str, Decimal] = defaultdict(Decimal)
        ratio_weight = Decimal(0)
        ratio_acc = Decimal(0)
        api_equivalent_value = Decimal(0)
        api_equivalent_tokens = 0
        api_equivalent_complete = True

        def ensure_model(model_key: str, tool_key: str) -> dict:
            model_key = reporting_model_key(model_key)
            return model_stats.setdefault(
                (model_key, tool_key),
                {
                    "key": model_key,
                    "name": display_model(model_key),
                    "toolKey": tool_key,
                    "value": Decimal(0),
                    "tokens": 0,
                    "cached": 0,
                    "fresh": 0,
                    "writes": 0,
                    "runs": set(),
                    "complete": True,
                    "telemetry": True,
                    "inputCost": Decimal(0),
                    "outputCost": Decimal(0),
                    "exactEvents": 0,
                    "derivedEvents": 0,
                },
            )

        def ensure_tool(tool_key: str) -> dict:
            return tool_stats.setdefault(
                tool_key,
                {
                    "key": tool_key,
                    "name": TOOL_NAMES.get(tool_key, tool_key),
                    "value": Decimal(0),
                    "tokens": 0,
                    "cached": 0,
                    "fresh": 0,
                    "writes": 0,
                    "complete": True,
                    "telemetry": True,
                    "series": [0] * len(buckets),
                    "exactEvents": 0,
                    "derivedEvents": 0,
                },
            )

        for event in enriched:
            model = ensure_model(event["model_key"], event["tool_key"])
            tool_stat = ensure_tool(event["tool_key"])
            measured = _measured(event)
            api_equivalent_value += event["computed_total"]
            api_equivalent_tokens += measured
            model["value"] += event["spend"]
            model["tokens"] += measured
            model["cached"] += event["cached_input_tokens"]
            model["fresh"] += _fresh(event)
            model["writes"] += int(event.get("cache_write_tokens") or 0)
            if event.get("session_id"):
                model["runs"].add(event["session_id"])
            tool_stat["value"] += event["spend"]
            tool_stat["tokens"] += measured
            tool_stat["cached"] += event["cached_input_tokens"]
            tool_stat["fresh"] += _fresh(event)
            tool_stat["writes"] += int(event.get("cache_write_tokens") or 0)
            index = _bucket_index(event["when"], window, zone=zone, buckets=buckets)
            tool_stat["series"][index] += measured
            mix.add_event(event)
            model["inputCost"] += (
                event["components"]["fresh_input"]
                + event["components"]["cached_input"]
                + event["components"]["cache_write"]
            )
            model["outputCost"] += event["components"]["output"]
            for key, amount in event["components"].items():
                component_totals[key] += amount
            if _event_is_exact(event):
                model["exactEvents"] += 1
                tool_stat["exactEvents"] += 1
            else:
                model["derivedEvents"] += 1
                tool_stat["derivedEvents"] += 1
            ratio = _cached_to_fresh_ratio(event["price"])
            weight = Decimal(event["cached_input_tokens"] + _fresh(event))
            if ratio is not None and weight > 0:
                ratio_acc += ratio * weight
                ratio_weight += weight

        for event in unpriced_events:
            model = ensure_model(event["model_key"], event["tool_key"])
            tool_stat = ensure_tool(event["tool_key"])
            measured = _measured(event)
            published_price, published_components = _try_price_event(pricing, event)
            if published_price is None:
                api_equivalent_complete = False
            else:
                api_equivalent_value += published_price
                api_equivalent_tokens += measured
            if not event["telemetry_complete"]:
                api_equivalent_complete = False
            model["tokens"] += measured
            model["cached"] += event["cached_input_tokens"]
            model["fresh"] += _fresh(event)
            model["writes"] += int(event.get("cache_write_tokens") or 0)
            model["telemetry"] = model["telemetry"] and event["telemetry_complete"]
            tool_stat["tokens"] += measured
            tool_stat["cached"] += event["cached_input_tokens"]
            tool_stat["fresh"] += _fresh(event)
            tool_stat["writes"] += int(event.get("cache_write_tokens") or 0)
            tool_stat["telemetry"] = tool_stat["telemetry"] and event["telemetry_complete"]
            if event.get("session_id"):
                model["runs"].add(event["session_id"])
            if event["cost_usd"] is not None:
                model["value"] += event["cost_usd"]
                tool_stat["value"] += event["cost_usd"]
            else:
                priced, components = published_price, published_components
                if priced is None:
                    model["complete"] = False
                    tool_stat["complete"] = False
                else:
                    model["value"] += priced
                    tool_stat["value"] += priced
                    if components:
                        model["inputCost"] += (
                            components["fresh_input"]
                            + components["cached_input"]
                            + components["cache_write"]
                        )
                        model["outputCost"] += components["output"]
                        for key, amount in components.items():
                            component_totals[key] += amount
            index = _bucket_index(event["when"], window, zone=zone, buckets=buckets)
            tool_stat["series"][index] += measured
            mix.add_event(event)

        for tool_key, amount in subscription_by_tool.items():
            ensure_tool(tool_key)

        local_now = now.astimezone(zone)
        month_start = datetime(local_now.year, local_now.month, 1, tzinfo=zone).astimezone(UTC)
        month_parts, month_usage = _month_usage(connection, pricing, month_start, now, tool)
        # OpenCode and ZCode use different plan keys but the provider returns
        # identical quota counters: they consume one shared Z.AI subscription.
        # Attribute both harnesses' published-rate value to that single plan.
        month_usage = dict(month_usage)
        month_usage["opencode"] = month_usage.get("opencode", Decimal(0)) + month_usage.get(
            "zcode", Decimal(0)
        )
        monthly_plans: dict[str, tuple[str, Decimal]] = {}
        subscription_rows = []
        for row in connection.execute("SELECT * FROM subscriptions"):
            amount = Decimal(str(row["amount_usd"]))
            monthly = _monthly_equivalent(amount, row["cadence"])
            note = None
            if row["cadence"] == "quarterly":
                note = f"${float(amount):.2f} quarterly; ${float(monthly):.2f}/mo equivalent"
            elif row["cadence"] == "annual":
                note = f"${float(amount):.2f} annual; ${float(monthly):.2f}/mo equivalent"
            zero = amount <= 0
            subscription_rows.append(
                {
                    "name": row["name"],
                    "toolKey": row["tool_key"],
                    "amountUsd": None if zero else float(amount),
                    "cadence": row["cadence"],
                    "note": None if zero else note,
                    "monthlyEquivalent": None if zero else float(monthly),
                    "planState": "no paid plan" if zero else "paid",
                }
            )
            if zero:
                continue
            active_start = date.fromisoformat(row["start_date"])
            active_end = date.fromisoformat(row["end_date"]) if row["end_date"] else None
            if local_now.date() < active_start or (active_end and local_now.date() > active_end):
                continue
            monthly_plans[row["tool_key"]] = (row["name"], monthly)

        quota_rows = quotas if quotas is not None else _load_quota_rows(connection)
        activity_rows = activity if activity is not None else _load_activity_rows(connection)
        capacity = _capacity_from_rows(quota_rows, now=now, month_to_date=month_usage)
        activity_payload = _activity_from_rows(activity_rows)
        status, failing_source, _last_refresh, _stale = _ingest_status(
            connection, now, cadence_seconds
        )
        today_start = datetime.combine(local_now.date(), time.min, zone).astimezone(UTC)
        today = _tracked_value(
            connection,
            pricing,
            today_start,
            now,
            "all",
            timezone,
            cap_date=local_now.date(),
        )
        burn = _seven_day_burn(connection, pricing, now, timezone)
        day_coverage = _day_coverage(connection, timezone, now)
        heat_start, heat_end, heat_fallback = _heatmap_window(window, now, timezone)
        heat = _heatmap_cells(connection, pricing, heat_start, heat_end, tool, zone)
        heatmap = [
            {"weekday": weekday, "hour": hour, "value": float(value)}
            for (weekday, hour), value in sorted(heat.items())
        ]

        series = [
            {"bucketStart": start_iso, "bucketKey": key, "label": label, "total": 0.0, "byTool": {}}
            for _start, _end, start_iso, label, key in buckets
        ]
        for tool_key, stat in tool_stats.items():
            for index, tokens in enumerate(stat["series"]):
                series[index]["byTool"][tool_key] = float(tokens)
        _finalize_series(series)

        tracked_value = parts.known if parts.complete else None
        effective_cost_per_million = (
            api_equivalent_value / Decimal(api_equivalent_tokens) * Decimal(1_000_000)
            if api_equivalent_tokens
            else None
        )
        effective_cost_coverage = (
            Decimal(api_equivalent_tokens) / Decimal(parts.tokens) * Decimal(100)
            if parts.tokens
            else None
        )
        input_share = _input_share(component_totals)
        ratio = (ratio_acc / ratio_weight) if ratio_weight > 0 else None
        models = []
        waste_models = []
        for stat in model_stats.values():
            cache_pct = _cache_hit_pct(
                stat["cached"], stat["fresh"], stat["writes"], complete=stat["telemetry"]
            )
            input_cost = stat["inputCost"]
            output_cost = stat["outputCost"]
            model_share = (
                float(input_cost / (input_cost + output_cost))
                if input_cost + output_cost
                else None
            )
            value = _money(stat["value"]) if stat["complete"] else None
            exact = stat["exactEvents"] > 0 and stat["derivedEvents"] == 0
            public = {
                "key": stat["key"],
                "name": stat["name"],
                "toolKey": stat["toolKey"],
                "value": value,
                "tokens": stat["tokens"],
                "cachePct": cache_pct,
                "runs": len(stat["runs"]),
                "isExact": exact,
                "valueMarker": _value_marker(exact, value),
            }
            models.append(public)
            waste_models.append({**public, "inputShare": model_share})
        models.sort(key=lambda row: (row["value"] is None, -(row["value"] or 0), -row["tokens"]))
        tools = []
        for stat in tool_stats.values():
            cache_pct = _cache_hit_pct(
                stat["cached"], stat["fresh"], stat["writes"], complete=stat["telemetry"]
            )
            value = _money(stat["value"]) if stat["complete"] else None
            exact = stat["exactEvents"] > 0 and stat["derivedEvents"] == 0
            tools.append(
                {
                    "key": stat["key"],
                    "name": stat["name"],
                    "tokens": stat["tokens"],
                    "value": value,
                    "cachePct": cache_pct,
                    "isExact": exact,
                    "valueMarker": _value_marker(exact, value),
                    "share": (
                        float(Decimal(str(value)) / tracked_value * 100)
                        if value is not None and tracked_value
                        else None
                    ),
                }
            )
        tools.sort(key=lambda row: (row["value"] is None, -(row["value"] or 0), -row["tokens"]))
        window_days = Decimal(str(max((window.end - window.start).total_seconds() / 86400, 1 / 24)))
        waste = _waste_bundle(
            models=waste_models,
            cache_threshold=cache_threshold,
            input_share=input_share,
            mix_complete=mix.telemetry_complete,
            month_usage=month_usage,
            monthly_plans=monthly_plans,
            capacity=capacity,
            window_days=window_days,
        )
        plan_cost = _monthly_subscription_cost(connection, tool, local_now.date())
        usage_month = month_parts.priced + month_parts.published
        projected_value = usage_month if month_parts.complete else None
        projected = {
            "value": _money(projected_value),
            "planCost": float(plan_cost),
            "multiple": float(projected_value / plan_cost) if projected_value is not None and plan_cost else None,
            "method": (
                "Month-to-date published-rate equivalent of usage versus configured "
                "monthly plan cost. Not a bill."
            ),
        }
        mean_session = (
            float(tracked_value / Decimal(parts.sessions))
            if tracked_value is not None and parts.sessions
            else None
        )
        return {
            "window": _window_payload(window, len(buckets)),
            "generatedAt": _iso(now),
            "cadenceSeconds": cadence_seconds,
            "cadenceMinutes": cadence_seconds / 60,
            "status": status,
            "failingSource": failing_source,
            "navigation": {
                "burnRatePerDay": float(burn) if burn is not None else None,
                "todayUsd": float(today.known) if today.records else None,
                "dayCoverage": day_coverage,
            },
            "coverage": {
                "exactProviders": ["Codex / OpenAI", "Claude Code / Anthropic"],
                "derivedProviders": [
                    "Cursor",
                    "Z.AI Coding Plan / OpenCode / ZCode",
                    "SuperGrok / Grok Build",
                    "OpenRouter",
                ],
                "note": "Exact providers use invoice or native cost; derived providers use official published rates and render ≈.",
            },
            "totals": {
                "trackedValue": _money(tracked_value),
                "trackedValueMarker": (
                    "≈" if tracked_value is not None and parts.published > 0 else None
                ),
                "priced": float(parts.priced),
                # Token-card published-rate equivalent only. Subscription
                # proration is subscriptionUsd, never this field.
                "publishedRate": float(parts.published),
                "subscriptionUsd": float(parts.subscriptions),
                "effectiveCostPerMillionTokens": (
                    _money(effective_cost_per_million)
                    if api_equivalent_complete
                    else None
                ),
                "knownEffectiveCostPerMillionTokens": _money(effective_cost_per_million),
                "effectiveCostPricedTokens": api_equivalent_tokens,
                "effectiveCostCoveragePct": _money(effective_cost_coverage),
                "effectiveCostComplete": api_equivalent_complete,
                # Sum of every row that could be valued, even when trackedValue
                # is withheld because some rows are unpriced. Null only when
                # the window has no rows at all.
                "knownValue": _money(parts.known) if parts.records else None,
                "unpricedModels": [
                    {"modelKey": model_key, "records": count}
                    for model_key, count in sorted(parts.unpriced_models.items())
                ],
                "tokens": parts.tokens,
                "records": parts.records,
                "sessions": parts.sessions,
                "meanSessionValue": mean_session,
                "cacheReusePct": _cache_reuse(mix),
            },
            "capacity": capacity,
            "activity": activity_payload,
            "waste": waste,
            "cacheSavings": _cache_savings(
                events=[*events, *unpriced_events],
                pricing=pricing,
            ),
            "projected": projected,
            "mix": _mix_payload(mix),
            "series": series,
            "tools": tools,
            "models": models,
            "subscriptions": subscription_rows,
            "heatmap": heatmap,
            "heatmapFallback": heat_fallback,
        }


def aggregate_entity(
    *,
    database_path: Path,
    pricing: PricingEngine,
    kind: str,
    key: str,
    window_key: str,
    timezone: str,
    cache_threshold: float,
    now: datetime | None = None,
    quotas: list[dict] | None = None,
) -> dict:
    if kind not in {"model", "tool"}:
        raise ValueError("kind must be model or tool")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    window = resolve_window(window_key, now=now, timezone=timezone)
    zone = ZoneInfo(timezone)
    with connect(database_path) as connection:
        window = _actualize_all_window(connection, window, timezone)
        tool_filter = key if kind == "tool" else "all"
        events = _load_events(connection, window.start, window.end, tool_filter)
        unpriced_events = _load_unpriced_events(connection, window.start, window.end, tool_filter)
        if kind == "model":
            model_keys = reporting_model_keys(key)
            events = [event for event in events if event["model_key"] in model_keys]
            unpriced_events = [event for event in unpriced_events if event["model_key"] in model_keys]
        authority = _cost_buckets(connection, window.start, window.end)
        enriched = _enrich(events, pricing, authority)
        cap_date = now.astimezone(zone).date()
        subscriptions = Decimal(0)
        if kind == "tool":
            _total, by_tool = _subscription_cost(
                connection, window.start, window.end, timezone, cap_date=cap_date
            )
            subscriptions = by_tool.get(key, Decimal(0))
        parts = _tracked_from_loaded(
            events=events,
            unpriced=unpriced_events,
            enriched=enriched,
            subscriptions=subscriptions,
            pricing=pricing,
        )
        global_parts = _tracked_value(
            connection,
            pricing,
            window.start,
            window.end,
            "all",
            timezone,
            cap_date=cap_date,
        )
        mix = TokenMix()
        component_totals: dict[str, Decimal] = defaultdict(Decimal)
        ratio_weight = Decimal(0)
        ratio_acc = Decimal(0)
        buckets = _bucket_plan(window, zone)
        token_series = [0] * len(buckets)
        session_store: dict[str, dict] = {}
        entity_tool = key if kind == "tool" else None
        for event in enriched:
            mix.add_event(event)
            token_series[_bucket_index(event["when"], window, zone=zone, buckets=buckets)] += _measured(event)
            for comp_key, amount in event["components"].items():
                component_totals[comp_key] += amount
            ratio = _cached_to_fresh_ratio(event["price"])
            weight = Decimal(event["cached_input_tokens"] + _fresh(event))
            if ratio is not None and weight > 0:
                ratio_acc += ratio * weight
                ratio_weight += weight
            entity_tool = entity_tool or event["tool_key"]
            _accumulate_session(session_store, event, value=event["spend"], complete=True)
        for event in unpriced_events:
            mix.add_event(event)
            token_series[_bucket_index(event["when"], window, zone=zone, buckets=buckets)] += _measured(event)
            entity_tool = entity_tool or event["tool_key"]
            if event["cost_usd"] is not None:
                _accumulate_session(session_store, event, value=event["cost_usd"], complete=True)
            else:
                priced, components = _try_price_event(pricing, event)
                if priced is None:
                    _accumulate_session(session_store, event, value=None, complete=False)
                else:
                    if components:
                        for comp_key, amount in components.items():
                            component_totals[comp_key] += amount
                    _accumulate_session(session_store, event, value=priced, complete=True)
        entity_value = parts.known if parts.complete else None
        global_value = global_parts.known if global_parts.complete else None
        input_share = _input_share(component_totals)
        ratio = (ratio_acc / ratio_weight) if ratio_weight > 0 else None
        already_saved = _cache_savings(
            events=[*events, *unpriced_events],
            pricing=pricing,
        )
        cache_pct = _cache_reuse(mix)
        opportunity_amount = None
        opportunity_kind = None
        opportunity_detail = "Cost optimization is unavailable until this usage has complete priced token components."
        opportunity_fix = "Wait for priced telemetry before acting on cache opportunity."
        if entity_value is not None and cache_pct is not None and input_share is not None:
            actual = Decimal(str(cache_pct)) / Decimal(100)
            target = Decimal(str(cache_threshold))
            if actual < target:
                opportunity_kind = "cache_gap"
                opportunity_amount = float(entity_value * (target - actual) * input_share)
                opportunity_detail = (
                    f"Cache reuse is {cache_pct:.1f}% versus the {cache_threshold * 100:.0f}% target."
                )
                opportunity_fix = f"Raise cache reuse toward {cache_threshold * 100:.0f}% for this {kind}."
            else:
                opportunity_kind = "healthy"
                opportunity_amount = 0.0
                opportunity_detail = "Cache reuse is at or above the target."
                opportunity_fix = "Review output length and model routing."
        runs = len(session_store)
        series = [
            {
                "bucketStart": start_iso,
                "bucketKey": key,
                "label": label,
                "tokens": float(token_series[index]),
            }
            for index, (_start, _end, start_iso, label, key) in enumerate(buckets)
        ]
        quota_rows = quotas if quotas is not None else _load_quota_rows(connection)
        capacity = _capacity_from_rows(
            quota_rows,
            now=now,
            month_to_date={entity_tool or "": parts.published + parts.priced},
        )
        provider_key = entity_tool or key
        provider_card = next((card for card in capacity if card["providerKey"] == provider_key), None)
        if provider_card is None:
            meta = PROVIDER_META.get(
                provider_key,
                {
                    "providerKey": provider_key,
                    "providerName": TOOL_NAMES.get(provider_key, provider_key),
                    "plan": None,
                    "isPayg": False,
                },
            )
            provider_limits = []
            plan = meta.get("plan")
            provider_name = meta["providerName"]
            is_payg = bool(meta.get("isPayg"))
        else:
            provider_limits = provider_card["rows"]
            plan = provider_card["plan"]
            provider_name = provider_card["providerName"]
            is_payg = provider_card["isPayg"]
        name = display_model(reporting_model_key(key)) if kind == "model" else TOOL_NAMES.get(key, key)
        value_float = _money(entity_value)
        share_of_tracked = (
            float(entity_value / global_value * 100)
            if entity_value is not None and global_value
            else None
        )
        share_of_tokens = (
            parts.tokens / global_parts.tokens * 100 if global_parts.tokens else None
        )
        return {
            "window": _window_payload(window, len(buckets)),
            "generatedAt": _iso(now),
            "name": name,
            "kind": kind,
            "providerKey": provider_key,
            "providerName": provider_name,
            "plan": plan,
            "isExact": bool(enriched)
            and not unpriced_events
            and all(_event_is_exact(event) for event in enriched),
            "valueMarker": _value_marker(
                bool(enriched)
                and not unpriced_events
                and all(_event_is_exact(event) for event in enriched),
                value_float,
            ),
            "color": TOOL_COLORS.get(provider_key, "#78a8f8"),
            "value": value_float,
            "shareOfTrackedValue": share_of_tracked,
            "tokens": parts.tokens,
            "shareOfTokens": share_of_tokens,
            "cachePct": cache_pct,
            "outputTokens": mix.output,
            "runs": runs,
            "valuePerRun": (
                float(entity_value / Decimal(runs)) if entity_value is not None and runs else None
            ),
            "tokensPerRun": (parts.tokens / runs) if runs else None,
            "series": series,
            "mix": _mix_payload(mix),
            "opportunity": {
                "kind": opportunity_kind,
                "amount": opportunity_amount,
                "detail": opportunity_detail,
                "fix": opportunity_fix,
                "alreadySaved": already_saved,
            },
            "providerLimits": provider_limits,
            "sessions": _session_subset(list(session_store.values()), entity_value, runs),
            "isPayg": is_payg,
        }


def _health_state_fingerprint(connection) -> tuple:
    """Everything /health reads: ingest runs, events, quotas, gaps, buckets."""
    return (
        tuple(connection.execute("SELECT COUNT(*), MAX(id), MAX(ingested_at) FROM usage_events").fetchone()),
        tuple(connection.execute("SELECT COUNT(*), MAX(id), MAX(ingested_at) FROM unpriced_usage_events").fetchone()),
        tuple(connection.execute("SELECT COUNT(*), MAX(id) FROM ingest_runs").fetchone()),
        tuple(connection.execute("SELECT COUNT(*), MAX(id), MAX(polled_at) FROM quotas").fetchone()),
        tuple(tuple(row) for row in connection.execute("SELECT model_key, occurrences FROM pricing_gaps ORDER BY model_key")),
        tuple(connection.execute("SELECT COUNT(*), MAX(id), COALESCE(SUM(cost_usd), 0) FROM provider_cost_buckets").fetchone()),
    )


def aggregate_health_cached(
    *,
    database_path: Path,
    now: datetime | None = None,
    timezone: str = "UTC",
) -> dict:
    """``aggregate_health`` shared across requests with the same clock and data.

    /health is polled alongside every summary and cost ~50 ms (day coverage
    walks every day since the first event); between two ingest cycles nothing
    it reads can change, so the payload is reused per (clock, fingerprint).
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    with connect(database_path) as connection:
        fingerprint = _health_state_fingerprint(connection)
    key = (str(database_path), timezone, _iso(now), fingerprint)
    return _memo(
        "health",
        key,
        lambda: aggregate_health(database_path=database_path, now=now, timezone=timezone),
    )


def aggregate_health(
    *,
    database_path: Path,
    now: datetime | None = None,
    timezone: str = "UTC",
) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    since = _iso(now - timedelta(hours=24))
    with connect(database_path) as connection:
        sources = [row[0] for row in connection.execute("SELECT DISTINCT source FROM ingest_runs")]
        ingest = []
        for source in sorted(sources):
            latest = connection.execute(
                "SELECT finished_at,status,error FROM ingest_runs WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
            count = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM usage_events WHERE source=? AND occurred_at>=?) +
                    (SELECT COUNT(*) FROM unpriced_usage_events WHERE source=? AND occurred_at>=?)
                """,
                (source, since, source, since),
            ).fetchone()[0]
            status = latest["status"] if latest else "never"
            error = latest["error"] if latest else None
            last_success = (
                latest["finished_at"]
                if latest and latest["status"] in {"success", "partial"}
                else None
            )
            if status == "skipped" and error and "not configured" in error:
                status = "unavailable"
                error = "unavailable — credential missing"
            ingest.append(
                {
                    "source": source,
                    "lastSuccess": last_success,
                    "status": status,
                    "eventsLast24h": count,
                    "error": error,
                }
            )
        gaps = [row[0] for row in connection.execute("SELECT model_key FROM pricing_gaps ORDER BY model_key")]
        quota_rows = _load_quota_rows(connection)
        latest_quota: dict[str, dict] = {}
        for row in quota_rows:
            previous = latest_quota.get(row["provider_key"])
            if previous is None or row["polled_at"] >= previous["polled_at"]:
                latest_quota[row["provider_key"]] = row
        quotas = []
        seen_providers = set(latest_quota)
        for provider_key, row in sorted(latest_quota.items()):
            unavailable = row["used"] is None and row["pct"] is None and not row["is_payg"]
            _name, label_reason = split_quota_label(str(row["label"] or ""))
            quotas.append(
                {
                    "providerKey": provider_key,
                    "lastPoll": row["polled_at"],
                    "status": "unavailable" if unavailable else "success",
                    "reason": (label_reason or "quota fields unread") if unavailable else None,
                }
            )
        for provider_key, meta in PROVIDER_META.items():
            if provider_key in seen_providers:
                continue
            quotas.append(
                {
                    "providerKey": provider_key,
                    "lastPoll": None,
                    "status": "payg" if meta.get("isPayg") else "unavailable",
                    "reason": None if meta.get("isPayg") else "no persisted quota snapshot",
                }
            )
        provider_total = Decimal(
            str(connection.execute("SELECT COALESCE(SUM(cost_usd),0) FROM provider_cost_buckets").fetchone()[0])
        )
        computed_total = Decimal(
            str(connection.execute("SELECT COALESCE(SUM(computed_cost_usd),0) FROM usage_events").fetchone()[0])
        )
        bucket_count = connection.execute("SELECT COUNT(*) FROM provider_cost_buckets").fetchone()[0]
        variance = (
            float((provider_total - computed_total) / provider_total * 100)
            if provider_total and bucket_count
            else None
        )
        coverage_inventory = _coverage_inventory(connection)
        day_coverage = _day_coverage(connection, timezone, now)
    return {
        "generatedAt": _iso(now),
        "ingest": ingest,
        "quotas": quotas,
        "pricingGaps": gaps,
        "providerVsComputedVariancePct": variance,
        "coverageInventory": coverage_inventory,
        "dayCoverage": day_coverage,
    }
