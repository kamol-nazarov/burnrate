"""Integration access policy (Release A contract 4.4, task A01).

Every credential-bearing or network lane is classified BEFORE any credential
is read or any request is transmitted:

- ``local`` — passive local files/stores and provider-emitted snapshots.
  Always permitted; no consent needed.
- ``configured`` — a dedicated API credential the user explicitly configured
  for this application (admin keys in the BURNRATE environment). Permitted
  when the credential exists; documented interface only.
- ``opt-in-native`` — reuse of another application's native credentials or
  undocumented/private endpoints (Claude OAuth usage endpoint, Cursor
  DashboardService with Cursor's own bearer token, Z.AI quota endpoint with
  OpenCode's stored key). These are DISABLED unless the user sets an explicit
  consent variable, because user consent with BURNRATE is not provider
  permission. Enabling never impersonates a native client: requests identify
  as BURNRATE.

Policy is checked by :func:`authorize` before collectors touch credentials;
a denied lane returns an actionable reason and never performs I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

APP_IDENTITY = "burnrate"


class Lane(str, Enum):
    LOCAL = "local"
    CONFIGURED = "configured"
    OPT_IN_NATIVE = "opt-in-native"


@dataclass(frozen=True)
class LanePolicy:
    key: str
    lane: Lane
    description: str
    allowed_hosts: tuple[str, ...] = ()
    consent_env: str | None = None


# Shipped lane registry. Hosts are exact suffix allowlists; a lane must never
# contact a host outside its list.
LANES: dict[str, LanePolicy] = {
    "codex_local_telemetry": LanePolicy(
        "codex_local_telemetry",
        Lane.LOCAL,
        "Reads rate-limit snapshots from local Codex session logs.",
    ),
    "grok_local_billing": LanePolicy(
        "grok_local_billing",
        Lane.LOCAL,
        "Reads the billing snapshot the Grok CLI writes to its local log.",
    ),
    "traycer_profile": LanePolicy(
        "traycer_profile",
        Lane.LOCAL,
        "Invokes the locally installed Traycer CLI for provider quota snapshots.",
    ),
    "antigravity_quota_rpc": LanePolicy(
        "antigravity_quota_rpc",
        Lane.LOCAL,
        "Reads the quota summary from the local Antigravity language server on "
        "127.0.0.1 using its ephemeral CSRF token; never an external host.",
        allowed_hosts=("127.0.0.1",),
    ),
    "openrouter_credits": LanePolicy(
        "openrouter_credits",
        Lane.CONFIGURED,
        "Official OpenRouter credits endpoint with the dedicated "
        "OPENROUTER_MANAGEMENT_KEY (never an inference key).",
        allowed_hosts=("openrouter.ai",),
    ),
    "openai_admin": LanePolicy(
        "openai_admin",
        Lane.CONFIGURED,
        "Official OpenAI usage/cost APIs with the configured OPENAI_ADMIN_KEY.",
        allowed_hosts=("api.openai.com",),
    ),
    "anthropic_admin": LanePolicy(
        "anthropic_admin",
        Lane.CONFIGURED,
        "Official Anthropic usage/cost APIs with the configured ANTHROPIC_ADMIN_KEY.",
        allowed_hosts=("api.anthropic.com",),
    ),
    "cursor_admin": LanePolicy(
        "cursor_admin",
        Lane.CONFIGURED,
        "Official Cursor Admin API with the configured CURSOR_API_KEY.",
        allowed_hosts=("api.cursor.com",),
    ),
    "claude_oauth_usage": LanePolicy(
        "claude_oauth_usage",
        Lane.OPT_IN_NATIVE,
        "Claude OAuth usage endpoint reusing Claude Code credentials. Off by default: "
        "the OAuth credential belongs to Claude Code; set BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE=1 "
        "only if you accept responsibility for that reuse.",
        allowed_hosts=("api.anthropic.com",),
        consent_env="BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE",
    ),
    "cursor_usage_service": LanePolicy(
        "cursor_usage_service",
        Lane.OPT_IN_NATIVE,
        "Cursor DashboardService reusing Cursor's own session token. Off by default pending "
        "documented permission; prefer CURSOR_API_KEY (cursor_admin) or CSV imports. Set "
        "BURNRATE_ENABLE_CURSOR_USAGE_SERVICE=1 to accept responsibility for this reuse.",
        allowed_hosts=("api2.cursor.sh",),
        consent_env="BURNRATE_ENABLE_CURSOR_USAGE_SERVICE",
    ),
    "zai_quota_endpoint": LanePolicy(
        "zai_quota_endpoint",
        Lane.OPT_IN_NATIVE,
        "Z.AI quota endpoint reusing the API key stored by OpenCode. Off by default: the key "
        "file belongs to OpenCode; set BURNRATE_ENABLE_ZAI_QUOTA=1 only for your own key.",
        allowed_hosts=("api.z.ai",),
        consent_env="BURNRATE_ENABLE_ZAI_QUOTA",
    ),
}

_DISABLED_REASON = (
    "disabled by default — {description} Enable with {env}=1 only if you accept the "
    "provider-terms responsibility for reusing that credential."
)


class PolicyDenied(PermissionError):
    """Raised before any credential read or network transmission."""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip().lower()


def authorize(lane_key: str, *, environ: dict[str, str] | None = None) -> tuple[bool, str | None]:
    """Check a lane against policy WITHOUT performing any I/O.

    Returns ``(allowed, reason)``. ``reason`` is None when allowed and an
    actionable user-facing message otherwise (never an exception class name).
    """
    policy = LANES.get(lane_key)
    if policy is None:
        return False, f"unknown integration lane: {lane_key}"
    if policy.lane is Lane.LOCAL:
        return True, None
    if policy.lane is Lane.CONFIGURED:
        return True, None
    env = policy.consent_env
    if env is None:
        return False, "no consent path is defined for this lane"
    source = environ if environ is not None else os.environ
    consent = (source.get(env, "").strip().lower() if environ is not None else _env(env))
    if consent in {"1", "true", "yes", "on"}:
        return True, None
    return False, _DISABLED_REASON.format(description=policy.description, env=env)


def require(lane_key: str, *, environ: dict[str, str] | None = None) -> None:
    allowed, reason = authorize(lane_key, environ=environ)
    if not allowed:
        raise PolicyDenied(reason or f"integration lane {lane_key} is not permitted")


def assert_allowed_host(lane_key: str, host: str) -> None:
    """Guard the wire: a lane may only talk to its allowlisted hosts."""
    policy = LANES.get(lane_key)
    if policy is None:
        raise PolicyDenied(f"unknown integration lane: {lane_key}")
    if host not in policy.allowed_hosts:
        raise PolicyDenied(
            f"integration lane {lane_key} is restricted to {', '.join(policy.allowed_hosts) or 'no hosts'}"
        )


def identity_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers that identify BURNRATE on every permitted outbound call."""
    return {"User-Agent": f"{APP_IDENTITY}/quota", **(extra or {})}


def enabled_lane_keys(*, environ: dict[str, str] | None = None) -> list[str]:
    return [key for key in LANES if authorize(key, environ=environ)[0]]
