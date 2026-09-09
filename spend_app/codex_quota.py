"""Main-pool Codex quota metadata. No credentials, usage accounting or probes."""
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

KEY = "codex.quota-observation.v1"
MAX_AGE_SECONDS = 6 * 60 * 60


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def observation(event, now):
    if not isinstance(event, dict):
        return None
    when = timestamp(event.get("timestamp"))
    payload = event.get("payload")
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if when is None or when > now or not isinstance(limits, dict) or limits.get("limit_id") != "codex":
        return None
    # Credit-only updates and missing-ID legacy records cannot establish this pool.
    windows = [w for w in (limits.get("primary"), limits.get("secondary"))
               if isinstance(w, dict) and number(w.get("window_minutes")) and w["window_minutes"] == 10080]
    if len(windows) != 1:
        return None
    w = windows[0]
    if not number(w.get("used_percent")) or not 0 <= w["used_percent"] <= 100 or not number(w.get("resets_at")):
        return None
    try:
        reset = datetime.fromtimestamp(w["resets_at"], UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if reset <= when or (reset - when).total_seconds() > 10080 * 60:
        return None
    # Retain only quota metadata, never transcript fields or credit documents.
    return when.timestamp(), {"limit_id": "codex", "plan_type": limits.get("plan_type") if isinstance(limits.get("plan_type"), str) else None, "primary": {key: w[key] for key in ("window_minutes", "used_percent", "resets_at")}}


def newest(previous, candidate):
    if candidate is None:
        return previous
    if previous and candidate[0] == previous[0] and candidate[1] != previous[1]:
        return candidate[0], {"ambiguous": True}
    return candidate if previous is None or candidate[0] > previous[0] else previous


def freshness_reason(observed_at, resets_at, now):
    observed, reset = timestamp(observed_at), timestamp(resets_at)
    if observed is None or reset is None or observed > now:
        return "Codex quota observation time is unavailable or invalid; wait for fresh main-pool telemetry."
    if reset <= now:
        return "Codex quota window expired; wait for a new main-pool snapshot. No new zero was inferred."
    if (now - observed).total_seconds() > MAX_AGE_SECONDS:
        return "Codex quota snapshot is older than six hours; wait for fresh main-pool telemetry."
    return None


def scope(root):
    return hashlib.sha256(str(Path(root).resolve()).replace("\\", "/").casefold().encode()).hexdigest()


def current_scope():
    from spend_app.providers import default_codex_glob
    return scope(default_codex_glob().replace("\\", "/").removesuffix("/**/*.jsonl"))


def save_metadata(connection, sample, polled_at):
    if sample.provider_key != "codex":
        return
    value = {"poolId": sample.pool_id, "scope": sample.scope,
             "observedAt": sample.observed_at, "polledAt": polled_at,
             "pct": sample.pct, "resetsAt": sample.resets_at}
    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (KEY, json.dumps(value)))


def read_rows(connection, *, now=None, rows=None):
    """Mask unproven/expired Codex rows at read time, including pre-fix rows."""
    rows = [dict(row) for row in (connection.execute("SELECT * FROM quotas") if rows is None else rows)]
    if not any(row['provider_key'] == 'codex' for row in rows):
        return rows
    from spend_app.connections import KEY as CONNECTIONS_KEY
    records = dict(connection.execute("SELECT key,value FROM app_meta WHERE key IN (?,?)", (KEY, CONNECTIONS_KEY)))
    try:
        meta = json.loads(records.get(KEY, "{}"))
        binding = json.loads(records.get(CONNECTIONS_KEY, "{}")).get("bindings", {}).get("codex_local")
        if not isinstance(meta, dict) or (binding is not None and not isinstance(binding, dict)):
            raise ValueError("Invalid quota metadata")
    except (ValueError, TypeError, AttributeError):
        meta, binding = {}, {"enabled": False}
    now = now or datetime.now(UTC)
    try:
        active_scope = current_scope() if not binding else None
    except (OSError, ValueError):
        active_scope = None
    for row in rows:
        if row['provider_key'] != 'codex':
            continue
        reason = None
        if binding:
            reason = "Codex connection is disabled." if not binding.get("enabled") else "Managed Codex location grants usage only; local quota permission is not supported. No default profile was read."
        elif row['pct'] is not None:
            if (meta.get('poolId') != 'codex' or meta.get('scope') != active_scope or active_scope is None
                    or meta.get('pct') != row['pct'] or meta.get('resetsAt') != row['resets_at']
                    or meta.get('polledAt') != row['polled_at']):
                reason = "Main Codex quota identity is unverified; awaiting a new quota poll."
            else:
                row['observed_at'] = meta.get('observedAt')
                reason = freshness_reason(meta.get('observedAt'), row['resets_at'], now)
        if reason:
            row.update(pct=None, used=None, allowance=None, resets_at=None, unit='unavailable', label='Codex weekly window \u2014 ' + reason)
    return rows
