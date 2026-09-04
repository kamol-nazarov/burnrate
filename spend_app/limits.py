"""Provider quota, activity, and credential-bearing HTTP helpers.

Experimental / undocumented interfaces (may change without notice):

- Cursor ``api2.cursor.sh`` DashboardService (usage events and quota)
- Anthropic ``/api/oauth/usage`` (Claude Code OAuth usage)
- Antigravity localhost gRPC-Web language-server RPC
- Traycer CLI ``agent profile-rate-limits`` and ``agent list``

Claude OAuth token refresh writes ``~/.claude/.credentials.json`` and is off
unless ``BURNRATE_CLAUDE_OAUTH_REFRESH=1``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from spend_app.adapters.local_common import sqlite_read_only
from spend_app.adapters.traycer_local import projection_index, settings_candidates


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
TRAYCER_ACTIVITY_TTL_SECONDS = 15
TRAYCER_ACTIVITY_FALLBACK_SECONDS = 90
GROK_ACTIVITY_MAX_AGE_SECONDS = 5 * 60
CODEX_ACTIVITY_MAX_AGE_SECONDS = 6 * 60 * 60
CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
CODEX_HISTORY_DB = Path.home() / ".codex" / "thread_history_1.sqlite"
ZCODE_ACTIVITY_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
CURSOR_ACTIVITY_DB = (
    Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
)
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_ACTIVITY_MAX_AGE_SECONDS = 10 * 60
LOCAL_ACTIVITY_MAX_AGE_SECONDS = 6 * 60 * 60
_TRAYCER_ACTIVITY_SNAPSHOTS: dict[str, tuple[float, set[str] | None]] = {}
_TRAYCER_ACTIVITY_REFRESHING: set[str] = set()
_TRAYCER_ACTIVITY_LOCK = threading.Lock()
# (resolved chat.db path, chat_id) -> (version, updated_at, lifecycle summary).
# The activity poll runs every four seconds; parsing every recent projection
# each time cost ~0.4 s of CPU per poll against a 1 GB Traycer store and
# produced max_instances skips. A projection only changes when its op-log
# position (through_seq) advances; that column precedes the JSON so listing it
# is cheap, whereas reading updated_at walks every overflow page.
_LIFECYCLE_CACHE: dict[tuple[str, str], tuple[object, int, dict | None]] = {}
_CLAUDE_CREDENTIAL_LOCK = threading.Lock()
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
CLAUDE_OAUTH_REFRESH_ENV = "BURNRATE_CLAUDE_OAUTH_REFRESH"
CLAUDE_TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000
CLAUDE_DESKTOP_HISTORY_MAX_AGE_SECONDS = 45 * 60
OPENROUTER_ALLOWED_HOSTS = frozenset({"openrouter.ai"})
CURSOR_DASHBOARD_ALLOWED_HOSTS = frozenset({"api2.cursor.sh"})
CLAUDE_OAUTH_TOKEN_ALLOWED_HOSTS = frozenset({"platform.claude.com"})
CLAUDE_OAUTH_USAGE_ALLOWED_HOSTS = frozenset({"api.anthropic.com"})
ZAI_QUOTA_ALLOWED_HOSTS = frozenset({"api.z.ai"})
ANTIGRAVITY_RPC_ALLOWED_HOSTS = frozenset({"127.0.0.1"})
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"


def reset_activity_cache() -> None:
    _LIFECYCLE_CACHE.clear()


def claude_oauth_refresh_enabled() -> bool:
    """True only when the operator opted into writing Claude credentials."""
    return os.environ.get(CLAUDE_OAUTH_REFRESH_ENV, "").strip() == "1"


def _assert_allowed_https_host(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in allowed_hosts:
        raise RuntimeError("refusing request to a non-allowlisted host")


def _iso_from_seconds(value: int | float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")


def _iso_from_millis(value: int | float | str | None) -> str | None:
    if not value:
        return None
    return _iso_from_seconds(float(value) / 1000)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _antigravity_log_path() -> Path:
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return roaming / "Antigravity" / "logs" / "main.log"


def _antigravity_local_connection(log_path: Path | None = None) -> tuple[str, str]:
    """Return Antigravity's current localhost quota endpoint and CSRF token.

    The token authenticates only the app's ephemeral localhost language-server
    RPC. It is read in memory, never returned by the API, persisted, or logged.
    """
    path = log_path or _antigravity_log_path()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 1_000_000))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError("Antigravity local runtime log is unavailable") from exc
    ports = re.findall(r"Local:\s+https://127\.0\.0\.1:(\d+)/", text)
    tokens = re.findall(r"--csrf_token\s+([^\s]+)", text)
    if not ports or not tokens:
        raise RuntimeError("Antigravity local quota endpoint is unavailable")
    return f"https://127.0.0.1:{ports[-1]}", tokens[-1]


def _grpc_web_json_payload(content: bytes) -> dict:
    offset = 0
    while offset + 5 <= len(content):
        flags = content[offset]
        length = struct.unpack(">I", content[offset + 1 : offset + 5])[0]
        start = offset + 5
        end = start + length
        if end > len(content):
            break
        if flags & 0x80 == 0:
            try:
                payload = json.loads(content[start:end].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Antigravity quota response was malformed") from exc
            if isinstance(payload, dict):
                return payload
        offset = end
    raise RuntimeError("Antigravity quota response contained no data frame")


def _antigravity_from_quota_result(payload: dict) -> dict:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    windows = []
    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or "Antigravity models")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or not bucket.get("bucketId"):
                continue
            remaining = _finite_number(bucket.get("remainingFraction"))
            if remaining is None or not 0 <= remaining <= 1:
                continue
            windows.append(
                {
                    "key": str(bucket["bucketId"]),
                    "label": f"{group_name} · {bucket.get('displayName') or bucket['bucketId']}",
                    "usedPct": (1 - remaining) * 100,
                    "remainingPct": remaining * 100,
                    "resetAt": bucket.get("resetTime"),
                }
            )
    if not windows:
        raise RuntimeError("Antigravity returned no quota windows")
    return {
        "key": "antigravity",
        "name": "Antigravity",
        "plan": "Google AI",
        "status": "exact",
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "windows": windows,
        "detail": "Experimental undocumented Antigravity localhost gRPC-Web quota summary; model groups share 5-hour and weekly limits.",
    }


def _antigravity_limits_uncached() -> dict:
    try:
        base_url, csrf_token = _antigravity_local_connection()
        _assert_allowed_https_host(base_url, ANTIGRAVITY_RPC_ALLOWED_HOSTS)
        request_payload = b"{}"
        frame = b"\x00" + struct.pack(">I", len(request_payload)) + request_payload
        response = httpx.post(
            base_url
            + "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
            content=frame,
            headers={
                "x-codeium-csrf-token": csrf_token,
                "content-type": "application/grpc-web+json",
                "x-grpc-web": "1",
            },
            verify=False,
            timeout=8,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        return _antigravity_from_quota_result(_grpc_web_json_payload(response.content))
    except Exception as exc:
        return {
            "key": "antigravity",
            "name": "Antigravity",
            "plan": "Google AI",
            "status": "error",
            "windows": [],
            "detail": f"Experimental Antigravity localhost gRPC-Web quota lookup failed ({type(exc).__name__}).",
        }


def _antigravity_rpc_json(method: str, payload: dict, *, timeout: float = 8) -> dict:
    base_url, csrf_token = _antigravity_local_connection()
    _assert_allowed_https_host(base_url, ANTIGRAVITY_RPC_ALLOWED_HOSTS)
    request_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    frame = b"\x00" + struct.pack(">I", len(request_payload)) + request_payload
    response = httpx.post(
        base_url + "/exa.language_server_pb.LanguageServerService/" + method,
        content=frame,
        headers={
            "x-codeium-csrf-token": csrf_token,
            "content-type": "application/grpc-web+json",
            "x-grpc-web": "1",
        },
        verify=False,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )
    response.raise_for_status()
    return _grpc_web_json_payload(response.content)


def _openrouter_credits_uncached(api_key: str | None = None) -> dict:
    """Read account credits with a dedicated OpenRouter management key.

    Never reads an inference key (``OPENROUTER_API_KEY``). The request is sent
    only to allowlisted OpenRouter hosts with ``trust_env=False``.
    """
    key = api_key or os.getenv("OPENROUTER_MANAGEMENT_KEY")
    if not key:
        return {
            "key": "openrouter",
            "name": "OpenRouter",
            "plan": "PAYG",
            "status": "unavailable",
            "windows": [],
            "detail": "OPENROUTER_MANAGEMENT_KEY is not configured.",
        }
    try:
        _assert_allowed_https_host(OPENROUTER_CREDITS_URL, OPENROUTER_ALLOWED_HOSTS)
        response = httpx.get(
            OPENROUTER_CREDITS_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        total = float(data["total_credits"])
        usage = float(data["total_usage"])
        remaining = max(0.0, total - usage)
        return {
            "key": "openrouter",
            "name": "OpenRouter",
            "plan": "PAYG",
            "status": "exact",
            "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "windows": [
                {
                    "key": "balance",
                    "remainingUsd": remaining,
                    "totalCreditsUsd": total,
                    "totalUsageUsd": usage,
                }
            ],
            "detail": "OpenRouter account credits from the read-only management endpoint.",
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        return {
            "key": "openrouter",
            "name": "OpenRouter",
            "plan": "PAYG",
            "status": "error",
            "windows": [],
            "detail": f"OpenRouter credits lookup failed ({type(exc).__name__}).",
        }


def _throttle_fields(exc: BaseException) -> dict:
    """HTTP status and Retry-After (seconds) for a failed provider call.

    The quota lane scheduler backs off on 429 and honours Retry-After, so a
    throttled endpoint is never hit again until the provider says so.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return {}
    fields: dict = {"httpStatus": status}
    header = response.headers.get("retry-after") if hasattr(response, "headers") else None
    if header:
        try:
            fields["retryAfterSeconds"] = max(0.0, float(header))
        except ValueError:
            pass
    return fields


def _cached(name: str, ttl_seconds: int, loader: Callable[[], dict]) -> dict:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(name)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[name] = (now, value)
    return value


def _tail_rate_limit(path: Path) -> tuple[float, dict] | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 2_000_000))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload")
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            continue
        timestamp = event.get("timestamp")
        try:
            when = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            when = path.stat().st_mtime
        return when, limits
    return None


def _codex_limits() -> dict:
    candidates = sorted(
        (Path(name) for name in glob.glob(str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl"), recursive=True)),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )[:40]
    latest: tuple[float, dict] | None = None
    for path in candidates:
        row = _tail_rate_limit(path)
        if row and (latest is None or row[0] > latest[0]):
            latest = row
    if latest is None:
        return {
            "key": "codex",
            "name": "Codex",
            "status": "unavailable",
            "detail": "No local rate-limit snapshot was found.",
            "windows": [],
        }
    observed_at, limits = latest
    windows = []
    for key, label in (("primary", "Primary"), ("secondary", "Secondary")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _finite_number(window.get("used_percent"))
        if used is None:
            continue
        minutes = int(window.get("window_minutes") or 0)
        windows.append(
            {
                "key": key,
                "label": f"{label} · {minutes // 1440}d" if minutes >= 1440 else f"{label} · {minutes}m",
                "windowMinutes": minutes,
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": _iso_from_seconds(window.get("resets_at")),
            }
        )
    return {
        "key": "codex",
        "name": "Codex",
        "plan": f"ChatGPT {str(limits.get('plan_type') or 'unknown').title()}",
        "status": "exact",
        "observedAt": _iso_from_seconds(observed_at),
        "windows": windows,
        "detail": "Native Codex rate-limit telemetry.",
        "credits": limits.get("credits"),
    }


def _cursor_access_token() -> str | None:
    database = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not database.is_file():
        return None
    connection = sqlite_read_only(database)
    try:
        row = connection.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def _cursor_limits_uncached() -> dict:
    token = _cursor_access_token()
    if not token:
        return {"key": "cursor", "name": "Cursor", "status": "unavailable", "windows": [], "detail": "Experimental Cursor DashboardService: Cursor is not signed in locally or its account database is unavailable."}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    base = "https://api2.cursor.sh/aiserver.v1.DashboardService/"
    try:
        _assert_allowed_https_host(base, CURSOR_DASHBOARD_ALLOWED_HOSTS)
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            usage_response = client.post(base + "GetCurrentPeriodUsage", headers=headers, json={})
            plan_response = client.post(base + "GetPlanInfo", headers=headers, json={})
            hard_limit_response = client.post(base + "GetHardLimit", headers=headers, json={})
        usage_response.raise_for_status()
        plan_response.raise_for_status()
        hard_limit_response.raise_for_status()
        usage = usage_response.json()
        plan = plan_response.json().get("planInfo") or {}
        hard_limit = hard_limit_response.json()
    except Exception as exc:
        return {"key": "cursor", "name": "Cursor", "status": "error", "windows": [], "detail": f"Experimental Cursor DashboardService lookup failed ({type(exc).__name__}).", **_throttle_fields(exc)}
    pool = usage.get("planUsage") or {}
    limit_cents = int(pool.get("limit") or plan.get("includedAmountCents") or 0)
    used_cents = int(pool.get("totalSpend") or 0)
    remaining_cents = int(pool.get("remaining") or max(0, limit_cents - used_cents))
    return {
        "key": "cursor",
        "name": "Cursor",
        "plan": plan.get("planName") or "Cursor",
        "status": "exact",
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "windows": [
            {
                "key": "included",
                "label": "Included value",
                "usedPct": used_cents / limit_cents * 100 if limit_cents else None,
                "usedUsd": used_cents / 100,
                "limitUsd": limit_cents / 100 if limit_cents else None,
                "remainingUsd": remaining_cents / 100 if limit_cents else None,
                "resetAt": _iso_from_millis(usage.get("billingCycleEnd") or plan.get("billingCycleEnd")),
            },
            {"key": "cursor_models", "label": "Cursor Models", "usedPct": pool.get("autoPercentUsed")},
            {"key": "other_models", "label": "Other Models", "usedPct": pool.get("apiPercentUsed")},
        ],
        "onDemand": {
            "enabled": not bool(hard_limit.get("noUsageBasedAllowed")),
            "limitUsd": hard_limit.get("hardLimit"),
        },
        "detail": "Experimental undocumented Cursor DashboardService (api2.cursor.sh); authenticated read-only usage.",
    }


def _claude_desktop_usage_path() -> Path | None:
    candidates: list[Path] = []
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    candidates.append(roaming / "Claude" / "plan-usage-history.json")
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    packages = local / "Packages"
    candidates.extend(
        package / "LocalCache" / "Roaming" / "Claude" / "plan-usage-history.json"
        for package in packages.glob("Claude_*")
    )
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else None


def _claude_desktop_limits_uncached(
    path: Path | None = None, *, now: datetime | None = None
) -> dict:
    """Newest exact subscription percentages cached by Claude Desktop.

    Claude Desktop writes this compact provider snapshot every 15 minutes.
    It contains only timestamp, organization id, and five-hour/weekly integer
    utilization—no prompts or responses. Reset timestamps are not present and
    therefore remain unavailable rather than inferred.
    """
    history_path = path or _claude_desktop_usage_path()
    if history_path is None:
        raise RuntimeError("Claude Desktop usage history is unavailable")
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Claude Desktop usage history is unreadable") from exc
    samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(samples, list):
        raise RuntimeError("Claude Desktop usage history contained no samples")
    sample = next(
        (
            item
            for item in reversed(samples)
            if isinstance(item, dict)
            and isinstance(item.get("u"), dict)
            and _finite_number(item.get("t")) is not None
        ),
        None,
    )
    if sample is None:
        raise RuntimeError("Claude Desktop usage history contained no samples")
    observed_at = _iso_from_millis(sample.get("t"))
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else None
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if observed is None or moment - observed > timedelta(seconds=CLAUDE_DESKTOP_HISTORY_MAX_AGE_SECONDS):
        raise RuntimeError("Claude Desktop usage history is stale")
    usage = sample["u"]
    windows = []
    for key, label, field in (
        ("5h", "5-hour window", "fh"),
        ("weekly", "Weekly · all models", "sd"),
    ):
        used = _finite_number(usage.get(field))
        if used is None:
            continue
        used = min(100.0, max(0.0, used))
        windows.append(
            {
                "key": key,
                "label": label,
                "usedPct": used,
                "remainingPct": 100 - used,
                "resetAt": None,
            }
        )
    if not windows:
        raise RuntimeError("Claude Desktop usage history omitted utilization")
    return {
        "key": "claude-code",
        "name": "Claude Code",
        "plan": "Claude Max",
        "status": "exact",
        "observedAt": observed_at,
        "windows": windows,
        "detail": (
            "Claude Desktop local provider usage history; exact account percentages "
            "sampled every 15 minutes. Reset timestamps are not present in this source."
        ),
    }


def _claude_credentials(credentials_path: Path) -> tuple[dict, dict]:
    try:
        document = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Claude OAuth credentials are unavailable") from exc
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise RuntimeError("Claude OAuth credentials are unavailable")
    return document, oauth


def _refresh_claude_access_token(credentials_path: Path, *, force: bool = False) -> tuple[str | None, str]:
    """Return a live Claude token, refreshing and rotating it when required.

    Claude Code access tokens expire after a few hours while the refresh token
    remains valid. The refresh response rotates both tokens. A process lock and
    compare-before-replace prevent two BURNRATE polls—or a concurrent Claude
    writer—from overwriting a newer credential file with stale data.

    Refresh and the credential-file write are off unless
    ``BURNRATE_CLAUDE_OAUTH_REFRESH=1``. A still-valid access token is used
    read-only in every build.
    """
    with _CLAUDE_CREDENTIAL_LOCK:
        document, oauth = _claude_credentials(credentials_path)
        subscription = str(oauth.get("subscriptionType") or "Max").title()
        access_token = oauth.get("accessToken")
        expires_at = _finite_number(oauth.get("expiresAt"))
        now_ms = time.time() * 1000
        if (
            not force
            and isinstance(access_token, str)
            and access_token
            and (expires_at is None or expires_at > now_ms + CLAUDE_TOKEN_REFRESH_MARGIN_MS)
        ):
            return access_token, subscription

        if not claude_oauth_refresh_enabled():
            return (
                access_token if isinstance(access_token, str) and access_token else None,
                subscription,
            )

        refresh_token = oauth.get("refreshToken")
        refresh_expires_at = _finite_number(oauth.get("refreshTokenExpiresAt"))
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or (refresh_expires_at is not None and refresh_expires_at <= now_ms)
        ):
            return access_token if isinstance(access_token, str) else None, subscription

        _assert_allowed_https_host(CLAUDE_OAUTH_TOKEN_URL, CLAUDE_OAUTH_TOKEN_ALLOWED_HOSTS)
        response = httpx.post(
            CLAUDE_OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLAUDE_OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/json", "User-Agent": "claude-code/2.1.255"},
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        refreshed = response.json()
        new_access = refreshed.get("access_token")
        new_refresh = refreshed.get("refresh_token")
        expires_in = _finite_number(refreshed.get("expires_in"))
        if not isinstance(new_access, str) or not new_access or expires_in is None or expires_in <= 0:
            raise RuntimeError("Claude OAuth refresh response was incomplete")
        if not isinstance(new_refresh, str) or not new_refresh:
            new_refresh = refresh_token

        # If another Claude process rotated the token while the request was in
        # flight, keep its newer file and use that winner instead of clobbering it.
        latest_document, latest_oauth = _claude_credentials(credentials_path)
        if latest_oauth.get("refreshToken") != refresh_token:
            latest_access = latest_oauth.get("accessToken")
            return (
                latest_access if isinstance(latest_access, str) else None,
                str(latest_oauth.get("subscriptionType") or subscription).title(),
            )

        refreshed_at = time.time() * 1000
        merged = dict(latest_oauth)
        merged.update(
            {
                "accessToken": new_access,
                "refreshToken": new_refresh,
                "expiresAt": int(refreshed_at + expires_in * 1000),
            }
        )
        refresh_expires_in = _finite_number(refreshed.get("refresh_token_expires_in"))
        if refresh_expires_in is not None and refresh_expires_in > 0:
            merged["refreshTokenExpiresAt"] = int(refreshed_at + refresh_expires_in * 1000)
        scope = refreshed.get("scope")
        if isinstance(scope, str) and scope.strip():
            merged["scopes"] = scope.split()
        latest_document["claudeAiOauth"] = merged

        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=credentials_path.parent,
                prefix=credentials_path.name + ".burnrate-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(latest_document, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, credentials_path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
        return new_access, subscription


def _claude_limits_uncached() -> dict:
    credentials_path = Path.home() / ".claude" / ".credentials.json"
    try:
        token, subscription = _refresh_claude_access_token(credentials_path)
    except Exception as exc:
        return {
            "key": "claude-code",
            "name": "Claude Code",
            "plan": "Claude Max",
            "status": "error",
            "windows": [],
            "detail": f"Experimental Claude OAuth refresh failed ({type(exc).__name__}).",
            **_throttle_fields(exc),
        }
    if not token:
        return {"key": "claude-code", "name": "Claude Code", "plan": f"Claude {subscription}", "status": "unavailable", "windows": [], "detail": "Experimental Anthropic /api/oauth/usage credentials are unavailable."}
    try:
        _assert_allowed_https_host(CLAUDE_USAGE_URL, CLAUDE_OAUTH_USAGE_ALLOWED_HOSTS)
        usage_headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.255",
            "Accept": "application/json",
        }
        response = httpx.get(
            CLAUDE_USAGE_URL,
            headers=usage_headers,
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        )
        if response.status_code == 401:
            if not claude_oauth_refresh_enabled():
                return {
                    "key": "claude-code",
                    "name": "Claude Code",
                    "plan": f"Claude {subscription}",
                    "status": "error",
                    "windows": [],
                    "detail": (
                        "Experimental Anthropic /api/oauth/usage rejected the access token. "
                        "Credential refresh is off unless BURNRATE_CLAUDE_OAUTH_REFRESH=1."
                    ),
                    "httpStatus": 401,
                }
            token, subscription = _refresh_claude_access_token(credentials_path, force=True)
            if token:
                usage_headers["Authorization"] = f"Bearer {token}"
                response = httpx.get(
                    CLAUDE_USAGE_URL,
                    headers=usage_headers,
                    timeout=15,
                    trust_env=False,
                    follow_redirects=False,
                )
        response.raise_for_status()
        usage = response.json()
    except Exception as exc:
        return {
            "key": "claude-code",
            "name": "Claude Code",
            "plan": f"Claude {subscription}",
            "status": "error",
            "windows": [],
            "detail": f"Experimental Anthropic /api/oauth/usage lookup failed ({type(exc).__name__}).",
            **_throttle_fields(exc),
        }
    windows = []
    for key, label in (("five_hour", "5-hour window"), ("seven_day", "Weekly window")):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        used = _finite_number(window.get("utilization"))
        if used is None:
            continue
        windows.append(
            {
                "key": "5h" if key == "five_hour" else "weekly",
                "label": label,
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": window.get("resets_at"),
            }
        )
    extra = usage.get("extra_usage") or {}
    return {
        "key": "claude-code",
        "name": "Claude Code",
        "plan": f"Claude {subscription}",
        "status": "exact",
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "windows": windows,
        "extraUsage": {
            "enabled": bool(extra.get("is_enabled")),
            "monthlyLimit": extra.get("monthly_limit"),
            "usedCredits": extra.get("used_credits"),
        },
        "detail": "Experimental undocumented Anthropic /api/oauth/usage snapshot; polled adaptively (90 s while Claude is in use, 5 min idle).",
    }


def _zai_limits_uncached() -> dict:
    token = None
    auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        token = (auth.get("zai-coding-plan") or {}).get("key")
    except (OSError, json.JSONDecodeError):
        pass
    if not token:
        zcode_config = Path.home() / ".zcode" / "v2" / "config.json"
        try:
            providers = json.loads(zcode_config.read_text(encoding="utf-8")).get("provider") or {}
            token = (
                (providers.get("builtin:zai-coding-plan") or {}).get("options") or {}
            ).get("apiKey")
        except (OSError, json.JSONDecodeError, AttributeError):
            token = None
    if not token:
        return {"key": "opencode", "name": "Z.AI Coding Plan", "status": "unavailable", "windows": [], "detail": "No OpenCode or ZCode Coding Plan key was found."}
    try:
        _assert_allowed_https_host(ZAI_QUOTA_URL, ZAI_QUOTA_ALLOWED_HOSTS)
        response = httpx.get(
            ZAI_QUOTA_URL,
            headers={"Authorization": token, "Accept-Language": "en-US,en", "Content-Type": "application/json"},
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        limits = (payload.get("data") or {}).get("limits") or []
    except Exception as exc:
        return {"key": "opencode", "name": "Z.AI Coding Plan", "status": "error", "windows": [], "detail": f"Z.AI quota lookup failed ({type(exc).__name__}).", **_throttle_fields(exc)}
    windows = []
    for index, limit in enumerate(limits):
        number = int(limit.get("number") or 0)
        label = "5-hour credits" if number == 5 else "Weekly credits" if number == 1 else f"Quota {index + 1}"
        maximum = int(limit.get("usage") or 0)
        has_current = limit.get("currentValue") is not None
        used_pct = _finite_number(limit.get("percentage"))
        if not has_current and used_pct is None:
            windows.append(
                {
                    "key": "5h" if number == 5 else "weekly" if number == 1 else str(index),
                    "label": label,
                    "usedPct": None,
                    "used": None,
                    "limit": maximum or None,
                    "remaining": None,
                    "resetAt": _iso_from_millis(limit.get("nextResetTime")),
                }
            )
            continue
        used = int(limit.get("currentValue") or 0)
        if used_pct is None and maximum:
            used_pct = used / maximum * 100
        windows.append(
            {
                "key": "5h" if number == 5 else "weekly" if number == 1 else str(index),
                "label": label,
                "usedPct": used_pct,
                "used": used,
                "limit": maximum,
                "remaining": int(limit.get("remaining") or max(0, maximum - used)),
                "resetAt": _iso_from_millis(limit.get("nextResetTime")),
            }
        )
    weekly_limit = next((window.get("limit") for window in windows if window["key"] == "weekly"), None)
    plan = {140000: "Max", 60000: "Pro", 10000: "Lite"}.get(weekly_limit, "Coding Plan")
    measured = any(window.get("usedPct") is not None or window.get("used") is not None for window in windows)
    return {
        "key": "opencode",
        "name": "Z.AI Coding Plan",
        "plan": plan,
        "status": "exact" if measured else "unavailable",
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "windows": windows,
        "detail": "Official Z.AI Coding Plan quota endpoint shared by OpenCode and ZCode.",
    }


def _grok_from_traycer_result(data: dict) -> dict:
    rate_limits = data.get("rateLimits") if isinstance(data, dict) else None
    if not isinstance(rate_limits, dict) or not rate_limits.get("available"):
        return {
            "key": "grok",
            "name": "Grok Build",
            "status": "unavailable",
            "windows": [],
            "detail": "Experimental Traycer CLI profile-rate-limits did not expose a Grok Build provider quota.",
        }
    period = rate_limits.get("period") or {}
    used = period.get("usedPercent")
    if not isinstance(used, (int, float)):
        return {
            "key": "grok",
            "name": "Grok Build",
            "plan": rate_limits.get("subscriptionTier") or "SuperGrok",
            "status": "unavailable",
            "windows": [],
            "detail": "Experimental Traycer CLI profile-rate-limits omitted the Grok Build weekly percentage.",
        }
    on_demand_cap = float(rate_limits.get("onDemandCap") or 0)
    on_demand_used = float(rate_limits.get("onDemandUsed") or 0)
    used = max(0.0, float(used))
    return {
        "key": "grok",
        "name": "Grok Build",
        "plan": rate_limits.get("subscriptionTier") or "SuperGrok",
        "status": "exact",
        "observedAt": _iso_from_millis(data.get("usageUpdatedAt")),
        "windows": [
            {
                "key": "weekly",
                "label": "Weekly Grok Build",
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": _iso_from_millis(period.get("resetsAt") or rate_limits.get("periodEnd")),
            }
        ],
        "onDemand": {
            "enabled": on_demand_cap > 0,
            "usedUsd": on_demand_used,
            "limitUsd": on_demand_cap if on_demand_cap > 0 else None,
        },
        "prepaidBalanceUsd": float(rate_limits.get("prepaidBalance") or 0),
        "detail": "Experimental Traycer CLI profile-rate-limits; read-only Grok Build provider quota.",
    }


def _claude_from_traycer_result(data: dict) -> dict:
    rate_limits = data.get("rateLimits") if isinstance(data, dict) else None
    if not isinstance(rate_limits, dict) or not rate_limits.get("available"):
        raise RuntimeError("Experimental Traycer CLI profile-rate-limits did not expose a Claude provider quota")
    windows = []
    for key, label, source_key in (
        ("5h", "5-hour window", "fiveHour"),
        ("weekly", "Weekly · all models", "sevenDay"),
    ):
        source = rate_limits.get(source_key)
        if not isinstance(source, dict) or not isinstance(source.get("usedPercent"), (int, float)):
            continue
        used = max(0.0, float(source["usedPercent"]))
        windows.append(
            {
                "key": key,
                "label": label,
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": _iso_from_millis(source.get("resetsAt")),
            }
        )
    for index, source in enumerate(rate_limits.get("modelScoped") or []):
        if not isinstance(source, dict) or not isinstance(source.get("usedPercent"), (int, float)):
            continue
        used = max(0.0, float(source["usedPercent"]))
        windows.append(
            {
                "key": f"model_{index}",
                "label": f"Weekly · {source.get('displayName') or 'model'}",
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": _iso_from_millis(source.get("resetsAt")),
            }
        )
    extra = rate_limits.get("extraUsage") or {}
    return {
        "key": "claude-code",
        "name": "Claude Code",
        "plan": f"Claude {str(rate_limits.get('subscriptionType') or 'Max').title()}",
        "status": "exact",
        "observedAt": _iso_from_millis(data.get("usageUpdatedAt")),
        "windows": windows,
        "extraUsage": {
            "enabled": bool(extra.get("isEnabled")),
            "monthlyLimit": extra.get("monthlyLimit"),
            "usedCredits": extra.get("usedCredits"),
        },
        "detail": "Experimental Traycer CLI profile-rate-limits; read-only Claude provider quota.",
    }


def _traycer_sender() -> tuple[str, str] | None:
    root = Path.home() / ".traycer" / "host" / "epic-state"
    best: tuple[int, str, str] | None = None
    for database in root.glob("*/chat/chat.db"):
        try:
            connection = sqlite_read_only(database)
            try:
                row = connection.execute(
                    "SELECT chat_id,updated_at FROM chat_projection ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            continue
        if row and row[0] and (best is None or int(row[1] or 0) > best[0]):
            best = int(row[1] or 0), database.parents[1].name, str(row[0])
    return (best[1], best[2]) if best else None


TRAYCER_PROFILE_RETRY_SECONDS = 15 * 60
_TRAYCER_PROFILE_BREAKER: dict[str, float] = {"until": 0.0}
_TRAYCER_PROFILE_LOCK = threading.Lock()


def reset_traycer_profile_breaker() -> None:
    with _TRAYCER_PROFILE_LOCK:
        _TRAYCER_PROFILE_BREAKER["until"] = 0.0


def _traycer_profile_rate_limits(harness: str) -> dict:
    # Circuit breaker: while Traycer is unavailable (it is intentionally
    # disabled on this machine) every 90-second poll would otherwise spawn
    # the CLI twice and, when it hangs, block the poll for up to 40 s. After
    # a failure the CLI path is skipped for 15 minutes; the collectors fall
    # through to their other sources immediately, and the path is retried
    # automatically so it recovers as soon as Traycer returns.
    with _TRAYCER_PROFILE_LOCK:
        if time.monotonic() < _TRAYCER_PROFILE_BREAKER["until"]:
            raise RuntimeError("Experimental Traycer CLI profile-rate-limits is paused after a failure")
    try:
        return _traycer_profile_rate_limits_uncached(harness)
    except Exception:
        with _TRAYCER_PROFILE_LOCK:
            _TRAYCER_PROFILE_BREAKER["until"] = time.monotonic() + TRAYCER_PROFILE_RETRY_SECONDS
        raise


def _traycer_profile_rate_limits_uncached(harness: str) -> dict:
    sender = _traycer_sender()
    executable = Path.home() / ".traycer" / "cli" / "bin" / "traycer.exe"
    if sender is None or not executable.is_file():
        raise RuntimeError("Experimental Traycer CLI profile-rate-limits is unavailable")
    epic_id, agent_id = sender
    environment = os.environ.copy()
    environment["TRAYCER_EPIC_ID"] = epic_id
    environment["TRAYCER_AGENT_ID"] = agent_id
    try:
        result = subprocess.run(
            [
                str(executable),
                "--json",
                "--quiet",
                "--no-progress",
                "--no-bootstrap",
                "agent",
                "profile-rate-limits",
                harness,
                "--profile",
                "ambient",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payloads = []
        for line in result.stdout.splitlines():
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        payload = next(
            (
                item.get("data")
                for item in reversed(payloads)
                if item.get("type") == "result" and item.get("status") == "ok"
            ),
            None,
        )
        if payload is None:
            raise RuntimeError("Experimental Traycer CLI profile-rate-limits returned no successful quota result")
        return payload
    except Exception:
        raise


def _claude_limits_via_traycer() -> dict:
    try:
        return _claude_desktop_limits_uncached()
    except Exception:
        pass
    try:
        return _claude_from_traycer_result(_traycer_profile_rate_limits("claude"))
    except Exception:
        return _claude_limits_uncached()


GROK_HOME = Path.home() / ".grok"
GROK_LOG_PATH = GROK_HOME / "logs" / "unified.jsonl"
GROK_ACTIVE_SESSIONS_PATH = GROK_HOME / "active_sessions.json"
GROK_SESSIONS_DIR = GROK_HOME / "sessions"
GROK_BILLING_MESSAGE = "billing: fetched credits config"
GROK_LOCAL_BILLING_SOURCE = "grok_local_billing"
GROK_BILLING_TAIL_BYTES = 1_048_576
_GROK_SESSION_DIRS: dict[str, Path | None] = {}


def _grok_billing_snapshot(path: Path = GROK_LOG_PATH) -> dict | None:
    """Newest ``billing: fetched credits config`` record in the CLI log.

    The grok CLI fetches its credits config every few minutes while it runs
    and logs the result, so the local log is a zero-network quota source.
    Only the tail of the file is read.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - GROK_BILLING_TAIL_BYTES))
            chunk = handle.read()
    except OSError:
        return None
    needle = GROK_BILLING_MESSAGE.encode("utf-8")
    for raw in reversed(chunk.split(b"\n")):
        if needle not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("msg") == GROK_BILLING_MESSAGE and isinstance(record.get("ctx"), dict):
            return record
    return None


def _grok_unavailable(detail: str, plan: str | None = None) -> dict:
    payload = {"key": "grok", "name": "Grok Build", "status": "unavailable", "windows": [], "detail": detail}
    if plan:
        payload["plan"] = plan
    return payload


def _grok_limits_from_log(path: Path = GROK_LOG_PATH, *, now: datetime | None = None) -> dict:
    record = _grok_billing_snapshot(path)
    if record is None:
        return _grok_unavailable("No Grok Build billing snapshot in the local CLI log yet; it appears once Grok Build runs.")
    ctx = record["ctx"]
    config = ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
    plan = str(ctx.get("subscriptionTier") or "SuperGrok")
    observed = str(record.get("ts") or "")
    used = config.get("creditUsagePercent")
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        # A newly reset, untouched weekly period is encoded without
        # creditUsagePercent and with an empty provider history. Grok's own
        # /usage surface renders this state as 0%, so it is an exact zero—not
        # missing telemetry.
        empty_new_period = (
            config.get("historyLen") == 0
            and period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY"
            and bool(period.get("start"))
            and bool(period.get("end"))
        )
        if empty_new_period:
            used = 0.0
        else:
            return _grok_unavailable("The local Grok Build billing snapshot omitted the weekly percentage.", plan)
    reset_raw = period.get("end") or config.get("billingPeriodEnd")
    reset_at: datetime | None = None
    if isinstance(reset_raw, str) and reset_raw:
        try:
            reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
        except ValueError:
            reset_at = None
    current = now or datetime.now(UTC)
    if reset_at is not None and reset_at.tzinfo is not None and reset_at <= current:
        return _grok_unavailable(
            f"The last local Grok Build billing snapshot ({observed}) predates the current weekly period; "
            "it refreshes the next time Grok Build runs.",
            plan,
        )
    on_demand_cap = _finite_number((config.get("onDemandCap") or {}).get("val")) or 0.0
    on_demand_used = _finite_number((config.get("onDemandUsed") or {}).get("val")) or 0.0
    prepaid = _finite_number((config.get("prepaidBalance") or {}).get("val")) or 0.0
    used = max(0.0, float(used))
    return {
        "key": "grok",
        "name": "Grok Build",
        "plan": plan,
        "status": "exact",
        "source": GROK_LOCAL_BILLING_SOURCE,
        "observedAt": observed or None,
        "windows": [
            {
                "key": "weekly",
                "label": "Weekly Grok Build",
                "usedPct": used,
                "remainingPct": max(0.0, 100 - used),
                "resetAt": reset_at.isoformat().replace("+00:00", "Z") if reset_at else None,
            }
        ],
        "onDemand": {
            "enabled": on_demand_cap > 0,
            "usedUsd": on_demand_used,
            "limitUsd": on_demand_cap if on_demand_cap > 0 else None,
        },
        "prepaidBalanceUsd": prepaid,
        "detail": f"Grok Build CLI billing snapshot read from the local log (as of {observed}); no network call.",
    }


def _grok_limits_uncached(log_path: Path | None = None, *, now: datetime | None = None) -> dict:
    local = _grok_limits_from_log(log_path or GROK_LOG_PATH, now=now)
    if local.get("status") == "exact":
        return local
    try:
        payload = _grok_from_traycer_result(_traycer_profile_rate_limits("grok"))
        payload["source"] = "traycer_profile"
        return payload
    except Exception as exc:
        return {
            "key": "grok",
            "name": "Grok Build",
            "status": "error",
            "windows": [],
            "detail": (
                f"{local.get('detail')} Experimental Traycer CLI profile-rate-limits "
                f"Grok Build quota lookup failed ({type(exc).__name__}); "
                "retried every 15 minutes while Traycer is unavailable."
            ),
        }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _grok_session_dir(session_id: str, sessions_dir: Path = GROK_SESSIONS_DIR) -> Path | None:
    key = f"{sessions_dir}|{session_id}"
    if key in _GROK_SESSION_DIRS:
        return _GROK_SESSION_DIRS[key]
    found: Path | None = None
    try:
        for project_dir in sessions_dir.iterdir():
            candidate = project_dir / session_id
            if candidate.is_dir():
                found = candidate
                break
            for parent in project_dir.iterdir() if project_dir.is_dir() else ():
                nested = parent / "subagents" / session_id
                if nested.is_dir():
                    found = nested
                    break
            if found:
                break
    except OSError:
        found = None
    _GROK_SESSION_DIRS[key] = found
    return found


def _grok_active_sessions(
    path: Path = GROK_ACTIVE_SESSIONS_PATH,
    sessions_dir: Path = GROK_SESSIONS_DIR,
    *,
    alive: Callable[[int], bool] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Recently active Grok Build sessions whose owning process is running.

    Grok keeps its interactive shell registered in ``active_sessions.json``
    while it is idle. A live card therefore also requires a recently modified
    file in that session's directory.
    """
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    is_alive = alive or _pid_alive
    cutoff = (now or datetime.now(UTC)).timestamp() - GROK_ACTIVITY_MAX_AGE_SECONDS
    sessions: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("session_id") or "").strip()
        pid = entry.get("pid")
        opened_at = entry.get("opened_at")
        if not session_id or not isinstance(pid, int) or not opened_at or not is_alive(pid):
            continue
        cwd = str(entry.get("cwd") or "")
        title = None
        model = None
        session_dir = _grok_session_dir(session_id, sessions_dir)
        if session_dir is not None:
            try:
                latest_activity = max(
                    (item.stat().st_mtime for item in session_dir.rglob("*") if item.is_file()),
                    default=session_dir.stat().st_mtime,
                )
                if latest_activity < cutoff:
                    continue
                summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
                title = summary.get("session_summary") or None
                model = summary.get("current_model_id") or None
            except (OSError, ValueError):
                pass
        sessions.append(
            {
                "sessionId": session_id,
                "title": str(title or f"Grok Build · {Path(cwd).name or 'session'}"),
                "model": f"supergrok:{str(model or 'grok-4.6').lower()}",
                "startedAt": str(opened_at),
            }
        )
    return sessions


def _codex_active_sessions(
    state_path: Path = CODEX_STATE_DB,
    history_path: Path = CODEX_HISTORY_DB,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Active Codex turns from the desktop app's read-only projection stores."""
    if not state_path.is_file() or not history_path.is_file():
        return []
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = int(current.timestamp()) - CODEX_ACTIVITY_MAX_AGE_SECONDS
    history = None
    state = None
    try:
        history = sqlite_read_only(history_path)
        state = sqlite_read_only(state_path)
        turns = history.execute(
            """
            SELECT thread_id, MIN(started_at) AS started_at
            FROM thread_turns
            WHERE lower(status) IN ('inprogress','in_progress','running')
              AND completed_at IS NULL
            GROUP BY thread_id
            """
        ).fetchall()
        sessions = []
        for turn in turns:
            thread = state.execute(
                """
                SELECT id,name,title,model,updated_at,archived
                FROM threads WHERE id=?
                """,
                (turn[0],),
            ).fetchone()
            if thread is None or thread[5] or int(thread[4] or 0) < cutoff:
                continue
            started_at = _iso_from_seconds(turn[1])
            last_seen_at = _iso_from_seconds(thread[4])
            if not started_at or not last_seen_at:
                continue
            sessions.append(
                {
                    "sessionId": str(thread[0]),
                    "title": str(thread[1] or thread[2] or "Codex task"),
                    "model": str(thread[3] or "codex"),
                    "startedAt": started_at,
                    "lastSeenAt": last_seen_at,
                }
            )
        return sorted(sessions, key=lambda row: row["lastSeenAt"], reverse=True)
    except (OSError, ValueError, sqlite3.Error):
        return []
    finally:
        if history is not None:
            history.close()
        if state is not None:
            state.close()


def _zcode_active_sessions(
    path: Path = ZCODE_ACTIVITY_DB,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Active ZCode turns from its structured, read-only local metrics DB."""
    if not path.is_file():
        return []
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff_ms = int(current.timestamp() * 1000) - LOCAL_ACTIVITY_MAX_AGE_SECONDS * 1000
    connection = None
    try:
        connection = sqlite_read_only(path)
        rows = connection.execute(
            """
            SELECT t.session_id,t.turn_id,t.started_at,s.time_updated,s.title,
                   (SELECT model_id FROM model_usage m
                    WHERE m.session_id=t.session_id AND m.turn_id=t.turn_id
                    ORDER BY CASE WHEN m.status='running' THEN 0 ELSE 1 END,
                             m.started_at DESC LIMIT 1)
            FROM turn_usage t
            JOIN session s ON s.id=t.session_id
            WHERE t.status='running' AND t.completed_at IS NULL
              AND s.time_archived IS NULL AND s.time_updated>=?
            """,
            (cutoff_ms,),
        ).fetchall()
        sessions = []
        for session_id, turn_id, started_at, updated_at, title, model in rows:
            started = _iso_from_millis(started_at)
            last_seen = _iso_from_millis(updated_at)
            if not started or not last_seen:
                continue
            model_key = "zcode:" + (str(model or "unknown").strip().lower() or "unknown")
            sessions.append(
                {
                    "sessionId": f"{session_id}:{turn_id}",
                    "title": str(title or "ZCode session"),
                    "model": model_key,
                    "startedAt": started,
                    "lastSeenAt": last_seen,
                }
            )
        return sorted(sessions, key=lambda row: row["lastSeenAt"], reverse=True)
    except (OSError, ValueError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()


def _cursor_active_sessions(
    path: Path = CURSOR_ACTIVITY_DB,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Actively generating Cursor Composer sessions from read-only local state."""
    if not path.is_file():
        return []
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff_ms = int(current.timestamp() * 1000) - LOCAL_ACTIVITY_MAX_AGE_SECONDS * 1000
    connection = None
    try:
        connection = sqlite_read_only(path)
        rows = connection.execute(
            """
            SELECT h.composerId,h.createdAt,h.lastUpdatedAt,h.value,d.value
            FROM composerHeaders h
            LEFT JOIN cursorDiskKV d ON d.key='composerData:' || h.composerId
            WHERE h.isArchived=0 AND h.lastUpdatedAt>=?
            """,
            (cutoff_ms,),
        ).fetchall()
        sessions = []
        for composer_id, created_at, updated_at, header_raw, data_raw in rows:
            try:
                data = json.loads(data_raw) if data_raw else {}
            except (TypeError, json.JSONDecodeError):
                data = {}
            status = str(data.get("status") or "").strip().lower()
            if status not in {"generating", "running", "inprogress", "in_progress"}:
                continue
            try:
                header = json.loads(header_raw) if header_raw else {}
            except (TypeError, json.JSONDecodeError):
                header = {}
            model_config = data.get("modelConfig") if isinstance(data.get("modelConfig"), dict) else {}
            model = str(model_config.get("modelName") or "unknown").strip().lower() or "unknown"
            started = _iso_from_millis(created_at)
            last_seen = _iso_from_millis(updated_at)
            if not started or not last_seen:
                continue
            sessions.append(
                {
                    "sessionId": str(composer_id),
                    "title": str(data.get("name") or header.get("name") or "Cursor session"),
                    "model": f"cursor:{model}",
                    "startedAt": started,
                    "lastSeenAt": last_seen,
                }
            )
        return sorted(sessions, key=lambda row: row["lastSeenAt"], reverse=True)
    except (OSError, ValueError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()


def _antigravity_active_sessions(*, now: datetime | None = None) -> list[dict]:
    """Busy Antigravity trajectories from its read-only localhost RPC."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current.timestamp() - LOCAL_ACTIVITY_MAX_AGE_SECONDS
    try:
        payload = _antigravity_rpc_json("GetAllCascadeTrajectories", {}, timeout=5)
        raw = payload.get("trajectorySummaries") or {}
        summaries = raw.items() if isinstance(raw, dict) else (
            (str(item.get("trajectoryId") or index), item)
            for index, item in enumerate(raw)
            if isinstance(item, dict)
        )
        sessions = []
        for trajectory_id, summary in summaries:
            if not isinstance(summary, dict):
                continue
            status = str(summary.get("status") or "").upper()
            if status not in {"CASCADE_RUN_STATUS_BUSY", "CASCADE_RUN_STATUS_RUNNING"}:
                continue
            last_seen = str(summary.get("lastModifiedTime") or "")
            try:
                last_seen_epoch = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue
            if last_seen_epoch < cutoff:
                continue
            started = str(summary.get("createdTime") or last_seen)
            title = summary.get("summary")
            model_key = "antigravity"
            try:
                detail = _antigravity_rpc_json(
                    "GetCascadeTrajectory",
                    {"cascadeId": str(trajectory_id), "trajectoryVerbosity": 2},
                    timeout=5,
                )
                trajectory = detail.get("trajectory") if isinstance(detail.get("trajectory"), dict) else detail
                generators = trajectory.get("generatorMetadata") or []
                for generator in reversed(generators):
                    chat = generator.get("chatModel") if isinstance(generator, dict) else None
                    model = chat.get("responseModel") if isinstance(chat, dict) else None
                    if model:
                        model_key = "antigravity:" + str(model).strip().lower()
                        break
            except (OSError, ValueError, httpx.HTTPError, RuntimeError):
                pass
            sessions.append(
                {
                    "sessionId": str(trajectory_id),
                    "title": str(title if isinstance(title, str) and title.strip() else "Antigravity session"),
                    "model": model_key,
                    "startedAt": started,
                    "lastSeenAt": last_seen,
                }
            )
        return sorted(sessions, key=lambda row: row["lastSeenAt"], reverse=True)
    except (OSError, ValueError, httpx.HTTPError, RuntimeError):
        return []


def _claude_active_sessions(
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Active Claude Code turns inferred from terminal versus nonterminal events.

    Only event envelopes in the tail are inspected. Message text, tool input,
    tool output, and attachments are neither read into the result nor stored.
    """
    if not projects_dir.is_dir():
        return []
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current.timestamp() - CLAUDE_ACTIVITY_MAX_AGE_SECONDS
    terminal_reasons = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
    sessions = []
    try:
        candidates = [
            path
            for path in projects_dir.rglob("*.jsonl")
            if path.is_file() and path.stat().st_mtime >= cutoff
        ]
    except OSError:
        return []
    for path in candidates:
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - 1_000_000))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        active = False
        active_started = None
        last_seen = None
        model = None
        slug = None
        title = None
        session_id = path.stem
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "custom-title" and event.get("customTitle"):
                title = str(event["customTitle"])
                continue
            timestamp = event.get("timestamp")
            if timestamp:
                last_seen = str(timestamp)
            slug = event.get("slug") or slug
            session_id = str(event.get("sessionId") or session_id)
            if event_type == "user" and timestamp:
                if not active:
                    active_started = str(timestamp)
                active = True
                continue
            if event_type != "assistant":
                continue
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            model = message.get("model") or model
            stop_reason = message.get("stop_reason")
            if stop_reason in terminal_reasons:
                active = False
                active_started = None
            else:
                active = True
                active_started = active_started or str(timestamp or last_seen or "")
        if not active or not active_started or not last_seen:
            continue
        try:
            last_seen_epoch = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if last_seen_epoch < cutoff:
            continue
        sessions.append(
            {
                "sessionId": session_id,
                "title": title or (str(slug).replace("-", " ").title() if slug else "Claude Code task"),
                "model": str(model or "claude-code"),
                "startedAt": active_started,
                "lastSeenAt": last_seen,
            }
        )
    return sorted(sessions, key=lambda row: row["lastSeenAt"], reverse=True)


def _traycer_active_agents_uncached(epic_id: str, sender_agent_id: str) -> dict:
    executable = Path.home() / ".traycer" / "cli" / "bin" / "traycer.exe"
    if not executable.is_file():
        raise RuntimeError("Experimental Traycer CLI agent list is unavailable")
    environment = os.environ.copy()
    environment["TRAYCER_EPIC_ID"] = epic_id
    environment["TRAYCER_AGENT_ID"] = sender_agent_id
    result = subprocess.run(
        [
            str(executable),
            "--json",
            "--quiet",
            "--no-progress",
            "--no-bootstrap",
            "agent",
            "list",
        ],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    payloads = []
    for line in result.stdout.splitlines():
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    payload = next(
        (
            item.get("data")
            for item in reversed(payloads)
            if item.get("type") == "result" and item.get("status") == "ok"
        ),
        None,
    )
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(agents, list):
        raise RuntimeError("Experimental Traycer CLI agent list returned no successful result")
    return {
        "activeIds": sorted(
            str(agent.get("id"))
            for agent in agents
            if isinstance(agent, dict) and agent.get("active") is True and agent.get("id")
        ),
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _traycer_active_agent_ids(
    epic_id: str,
    sender_agent_id: str,
    *,
    potential_activity: bool,
) -> set[str] | None:
    now = time.monotonic()
    with _TRAYCER_ACTIVITY_LOCK:
        cached = _TRAYCER_ACTIVITY_SNAPSHOTS.get(epic_id)
        cached_ids = cached[1] if cached else None
        expired = cached is None or now - cached[0] >= TRAYCER_ACTIVITY_TTL_SECONDS
        should_refresh = (potential_activity or bool(cached_ids)) and expired
        if should_refresh and epic_id not in _TRAYCER_ACTIVITY_REFRESHING:
            _TRAYCER_ACTIVITY_REFRESHING.add(epic_id)
            refresh_started = now

            def refresh() -> None:
                try:
                    snapshot = _traycer_active_agents_uncached(epic_id, sender_agent_id)
                    value: set[str] | None = set(snapshot.get("activeIds") or [])
                except Exception:
                    value = None
                with _TRAYCER_ACTIVITY_LOCK:
                    _TRAYCER_ACTIVITY_SNAPSHOTS[epic_id] = (refresh_started, value)
                    _TRAYCER_ACTIVITY_REFRESHING.discard(epic_id)

            threading.Thread(
                target=refresh,
                name=f"traycer-activity-{epic_id[:8]}",
                daemon=True,
            ).start()
        if cached:
            return None if cached_ids is None else set(cached_ids)
    return None if potential_activity else set()


def _projection_lifecycle(projection: dict) -> dict | None:
    """Reduce a chat projection to the few fields activity needs.

    This is the expensive part (walking every event of a large JSON
    projection). It depends only on the projection itself, so callers cache it
    by the projection's ``updated_at`` and re-run the cheap liveness decision
    every poll.
    """
    settings = projection.get("settings") if isinstance(projection.get("settings"), dict) else {}
    harness = settings.get("harnessId")
    model = settings.get("model")
    last_lifecycle = None
    last_started_ms = 0
    last_usage_ms = 0
    for event in projection.get("events") or []:
        if not isinstance(event, dict):
            continue
        body = event.get("body")
        if not isinstance(body, dict):
            continue
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        for item in settings_candidates(metadata):
            event_settings = item.get("settings")
            if isinstance(event_settings, dict):
                harness = event_settings.get("harnessId") or harness
                model = event_settings.get("model") or model
        timestamp = int(body.get("timestamp") or 0)
        if body.get("type") in {"turn.started", "turn.completed", "turn.stopped"}:
            last_lifecycle = {
                "type": body.get("type"),
                "timestamp": timestamp,
                "harness": harness,
                "model": model,
            }
            if body.get("type") == "turn.started":
                last_started_ms = max(last_started_ms, timestamp)
        if isinstance(metadata.get("usage"), dict):
            last_usage_ms = max(last_usage_ms, timestamp)
    if not last_lifecycle:
        return None
    lifecycle = projection.get("lifecycle")
    return {
        "title": projection.get("title") or "Untitled agent",
        "parentChatId": projection.get("parentChatId"),
        "lifecycleState": lifecycle.get("state") if isinstance(lifecycle, dict) else None,
        "lastLifecycle": last_lifecycle,
        "lastStartedMs": last_started_ms,
        "lastUsageMs": last_usage_ms,
    }


def _traycer_projection_activity(
    *,
    chat_id: str,
    projection: dict,
    updated_at_ms: int,
    active_ids: set[str] | None,
    now_ms: int,
) -> tuple[dict | None, dict | None]:
    summary = _projection_lifecycle(projection)
    if summary is None:
        return None, None
    return _activity_from_lifecycle(
        chat_id=chat_id,
        summary=summary,
        updated_at_ms=updated_at_ms,
        active_ids=active_ids,
        now_ms=now_ms,
    )


def _activity_from_lifecycle(
    *,
    chat_id: str,
    summary: dict,
    updated_at_ms: int,
    active_ids: set[str] | None,
    now_ms: int,
) -> tuple[dict | None, dict | None]:
    last_lifecycle = summary["lastLifecycle"]
    last_started_ms = int(summary.get("lastStartedMs") or 0)
    last_usage_ms = int(summary.get("lastUsageMs") or 0)
    record = {
        "chatId": str(chat_id),
        "title": summary.get("title") or "Untitled agent",
        "parentChatId": summary.get("parentChatId"),
        "harness": last_lifecycle.get("harness") or "unknown",
        "model": last_lifecycle.get("model") or "unknown",
    }
    active = None
    unmetered = None
    if last_lifecycle["type"] == "turn.started":
        authoritative_live = active_ids is not None and str(chat_id) in active_ids
        lifecycle_state = summary.get("lifecycleState")
        fallback_cutoff_ms = now_ms - TRAYCER_ACTIVITY_FALLBACK_SECONDS * 1000
        fallback_live = (
            active_ids is None
            and lifecycle_state == "active"
            and max(updated_at_ms, int(last_lifecycle.get("timestamp") or 0)) >= fallback_cutoff_ms
        )
        if authoritative_live or fallback_live:
            active = {
                **record,
                "startedAt": _iso_from_millis(last_lifecycle.get("timestamp")),
                "usageStatus": "pending_turn_completion",
            }
    elif (
        last_lifecycle["type"] == "turn.stopped"
        and last_started_ms
        and last_started_ms > last_usage_ms
        and int(last_lifecycle.get("timestamp") or 0) >= now_ms - 30 * 60 * 1000
    ):
        unmetered = {
            **record,
            "startedAt": _iso_from_millis(last_started_ms),
            "stoppedAt": _iso_from_millis(last_lifecycle.get("timestamp")),
            "usageStatus": "stopped_without_usage",
        }
    return active, unmetered


def _traycer_activity() -> dict:
    active: list[dict] = []
    unmetered: list[dict] = []
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    recent_cutoff_ms = now_ms - 6 * 60 * 60 * 1000
    database_glob = str(
        Path.home() / ".traycer" / "host" / "epic-state" / "**" / "chat" / "chat.db"
    )
    for file_name in glob.glob(database_glob, recursive=True):
        connection = sqlite_read_only(Path(file_name))
        cache_prefix = str(Path(file_name).resolve())
        try:
            epic_id = Path(file_name).parents[1].name
            index = projection_index(connection)
            if index is None:
                continue
            projections: list[tuple[str, dict, int]] = []
            potential_activity = False
            sender_id: str | None = None
            sender_stamp = -1
            for chat_id, version in index:
                key = (cache_prefix, chat_id)
                cached = _LIFECYCLE_CACHE.get(key) if version is not None else None
                if cached is not None and cached[0] == version:
                    _version, stamp, summary = cached
                else:
                    fetched = connection.execute(
                        "SELECT updated_at,projection_json FROM chat_projection WHERE chat_id=?",
                        (chat_id,),
                    ).fetchone()
                    stamp = int((fetched[0] if fetched else 0) or 0)
                    try:
                        projection = json.loads(fetched[1]) if fetched and fetched[1] else None
                    except (TypeError, json.JSONDecodeError):
                        projection = None
                    summary = _projection_lifecycle(projection) if isinstance(projection, dict) else None
                    if version is not None:
                        _LIFECYCLE_CACHE[key] = (version, stamp, summary)
                if stamp > sender_stamp:
                    sender_stamp, sender_id = stamp, chat_id
                if summary is None or stamp < recent_cutoff_ms:
                    continue
                projections.append((chat_id, summary, stamp))
                recent_candidate, _ = _activity_from_lifecycle(
                    chat_id=chat_id,
                    summary=summary,
                    updated_at_ms=stamp,
                    active_ids=None,
                    now_ms=now_ms,
                )
                potential_activity = potential_activity or recent_candidate is not None
            present = {(cache_prefix, chat_id) for chat_id, _version in index}
            for key in [key for key in _LIFECYCLE_CACHE if key[0] == cache_prefix and key not in present]:
                _LIFECYCLE_CACHE.pop(key, None)
            active_ids = (
                _traycer_active_agent_ids(
                    epic_id,
                    sender_id,
                    potential_activity=potential_activity,
                )
                if sender_id
                else set()
            )
            for chat_id, summary, updated_at in projections:
                candidate, missing_usage = _activity_from_lifecycle(
                    chat_id=chat_id,
                    summary=summary,
                    updated_at_ms=updated_at,
                    active_ids=active_ids,
                    now_ms=now_ms,
                )
                if candidate:
                    active.append(candidate)
                if missing_usage:
                    unmetered.append(missing_usage)
        finally:
            connection.close()
    return {
        "activeAgents": sorted(active, key=lambda row: row.get("startedAt") or "", reverse=True),
        "unmeteredTurns": sorted(unmetered, key=lambda row: row.get("stoppedAt") or "", reverse=True),
    }


def collect_limits() -> dict:
    loaders = [
        _codex_limits,
        lambda: _cached("claude", 90, _claude_limits_via_traycer),
        lambda: _cached("cursor", 60, _cursor_limits_uncached),
        lambda: _cached("grok", 90, _grok_limits_uncached),
        lambda: _cached("zai", 60, _zai_limits_uncached),
        lambda: _cached("antigravity", 90, _antigravity_limits_uncached),
        lambda: _cached("openrouter", 60, _openrouter_credits_uncached),
    ]
    with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
        providers = list(executor.map(lambda loader: loader(), loaders))
    activity = _traycer_activity()
    return {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "providers": providers,
        **activity,
    }
