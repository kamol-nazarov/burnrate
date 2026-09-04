"""Provider-connection audit gates (see docs/PROVIDER_CONNECTIONS.md).

Every provider reader must persist what the source reports (no invented
allowances), refresh as soon as its cadence allows, and never call an external
endpoint more often than its cache window; the Traycer CLI path must not be
spawned on every poll while Traycer is unavailable.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx

import spend_app.limits as limits
from spend_app.config import ACTIVITY_POLL_SECONDS, QUOTA_POLL_SECONDS
from spend_app.db import initialize
from spend_app.quotas import REQUIRED_LIMITS, cursor_quota_samples, default_quota_collectors


def test_cursor_included_value_is_read_but_not_persisted_by_contract() -> None:
    """The build-prompt limit inventory lists only the Cursor model pools.

    The usage service also reports the included plan value (dollars used
    against the plan's included amount). It is read by the compatibility
    /limits reader but deliberately not persisted; enabling it is a product
    decision recorded in docs/PROVIDER_CONNECTIONS.md.
    """
    assert REQUIRED_LIMITS["cursor"] == (("cursor_models", "Cursor Models"), ("other_models", "Other Models"))
    payload = {
        "status": "exact",
        "windows": [
            {"key": "included", "label": "Included value", "usedPct": 87.3, "usedUsd": 349.27, "limitUsd": 400.0},
            {"key": "cursor_models", "label": "Cursor Models", "usedPct": 8.7},
            {"key": "other_models", "label": "Other Models", "usedPct": 17.5},
        ],
    }
    keys = [sample.limit_key for sample in cursor_quota_samples(payload, source="cursor_usage_service")]
    assert keys == ["cursor_models", "other_models"]


def test_traycer_profile_breaker_skips_the_cli_after_a_failure(tmp_path: Path, monkeypatch) -> None:
    limits.reset_traycer_profile_breaker()
    executable = tmp_path / ".traycer" / "cli" / "bin" / "traycer.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    monkeypatch.setattr("spend_app.limits._traycer_sender", lambda: ("epic-1", "sender-1"))
    calls = []

    def failing_run(command, **_kwargs):
        calls.append(command[-3])
        return SimpleNamespace(stdout='{"type":"result","status":"error"}\n')

    monkeypatch.setattr("spend_app.limits.subprocess.run", failing_run)
    clock = {"now": 1000.0}
    monkeypatch.setattr("spend_app.limits.time.monotonic", lambda: clock["now"])

    for _ in range(2):
        try:
            limits._traycer_profile_rate_limits("grok")
        except RuntimeError:
            pass
    try:
        limits._traycer_profile_rate_limits("claude")
    except RuntimeError as exc:
        assert "paused" in str(exc)
    assert calls == ["grok"], "after one failure the CLI is not spawned again"
    clock["now"] += limits.TRAYCER_PROFILE_RETRY_SECONDS + 1
    try:
        limits._traycer_profile_rate_limits("grok")
    except RuntimeError:
        pass
    assert calls == ["grok", "grok"], "the path is retried once the pause expires"
    limits.reset_traycer_profile_breaker()


def test_grok_and_claude_collectors_fall_through_immediately_while_paused(monkeypatch) -> None:
    limits.reset_traycer_profile_breaker()
    clock = {"now": 5000.0}
    monkeypatch.setattr("spend_app.limits.time.monotonic", lambda: clock["now"])
    with limits._TRAYCER_PROFILE_LOCK:
        limits._TRAYCER_PROFILE_BREAKER["until"] = clock["now"] + 600

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("CLI spawned while the breaker is open")

    monkeypatch.setattr("spend_app.limits.subprocess.run", must_not_run)
    monkeypatch.setattr(
        "spend_app.limits._claude_desktop_limits_uncached",
        lambda: (_ for _ in ()).throw(RuntimeError("desktop unavailable")),
    )
    grok = limits._grok_limits_uncached(Path("Z:/no-such-grok-log.jsonl"))
    assert grok["status"] == "error"
    assert "retried every 15 minutes" in grok["detail"]
    monkeypatch.setattr("spend_app.limits._claude_limits_uncached", lambda: {"key": "claude-code", "status": "exact", "windows": []})
    assert limits._claude_limits_via_traycer()["status"] == "exact"
    limits.reset_traycer_profile_breaker()


def test_external_quota_endpoints_are_called_at_most_once_per_cache_window(tmp_path: Path, monkeypatch) -> None:
    limits._CACHE.clear()
    counts = {"claude": 0, "zai": 0, "cursor": 0, "codex": 0}

    def counting(name, payload):
        def loader():
            counts[name] += 1
            return payload
        return loader

    exact = {"status": "exact", "windows": []}
    monkeypatch.setattr(
        "spend_app.quotas._claude_desktop_limits_uncached",
        lambda: (_ for _ in ()).throw(RuntimeError("desktop unavailable")),
    )
    # quotas.py binds the readers by name at import time, so the seams live there.
    monkeypatch.setattr("spend_app.quotas._claude_limits_uncached", counting("claude", exact))
    monkeypatch.setattr("spend_app.quotas._traycer_profile_rate_limits", lambda _h: (_ for _ in ()).throw(RuntimeError("paused")))
    monkeypatch.setattr("spend_app.quotas._zai_limits_uncached", counting("zai", exact))
    monkeypatch.setattr("spend_app.quotas._cursor_limits_uncached", counting("cursor", exact))
    monkeypatch.setattr("spend_app.quotas._codex_limits", counting("codex", {"status": "unavailable", "detail": "none", "windows": []}))
    database = tmp_path / "spend.db"
    initialize(database)
    collectors = default_quota_collectors(database)
    for _ in range(3):
        for collector in collectors.values():
            list(collector())
    assert counts == {"claude": 1, "zai": 1, "cursor": 1, "codex": 1}, "three polls inside the cache windows hit each source once"
    limits._CACHE.clear()


def test_poll_cadences_match_the_documented_rate_table() -> None:
    from spend_app.quotas import LANE_CADENCE, QUOTA_ACTIVITY_WINDOW_SECONDS

    assert QUOTA_POLL_SECONDS == 15
    assert ACTIVITY_POLL_SECONDS == 4
    assert QUOTA_ACTIVITY_WINDOW_SECONDS == 300
    # (active, idle) seconds; external lanes never exceed 120 calls per hour.
    assert LANE_CADENCE == {
        "codex": (15, 15),
        "openrouter": (60, 300),
        "claude-code": (90, 300),
        "opencode": (30, 300),
        "grok": (30, 300),
        "cursor": (900, 3600),
        "antigravity": (90, 300),
    }
    for provider, (active, idle) in LANE_CADENCE.items():
        if provider == "codex":
            continue
        assert 3600 / active <= 120, provider
        assert 3600 / idle <= 12, provider


def _write_expired_claude_credentials(tmp_path: Path) -> Path:
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps(
            {
                "unrelated": {"keep": True},
                "claudeAiOauth": {
                    "accessToken": "expired-access",
                    "refreshToken": "valid-refresh",
                    "expiresAt": 1,
                    "refreshTokenExpiresAt": 4_000_000_000_000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                },
            }
        ),
        encoding="utf-8",
    )
    return credentials


def test_claude_oauth_refresh_is_off_by_default_and_does_not_write_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("BURNRATE_CLAUDE_OAUTH_REFRESH", raising=False)
    credentials = _write_expired_claude_credentials(tmp_path)
    original = credentials.read_text(encoding="utf-8")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    posts: list[str] = []

    def fake_post(url, **_kwargs):
        posts.append(url)
        raise AssertionError("default public build must not refresh Claude OAuth")

    def fake_get(url, **kwargs):
        assert kwargs.get("trust_env") is False
        assert kwargs["headers"]["Authorization"] == "Bearer expired-access"
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "five_hour": {"utilization": 7, "resets_at": "2026-09-02T23:00:00Z"},
                "seven_day": {"utilization": 42, "resets_at": "2026-09-09T20:00:00Z"},
            },
        )

    monkeypatch.setattr("spend_app.limits.httpx.post", fake_post)
    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = limits._claude_limits_uncached()
    assert posts == []
    assert credentials.read_text(encoding="utf-8") == original
    assert result["status"] == "exact"
    assert [window["usedPct"] for window in result["windows"]] == [7, 42]
    assert "experimental" in result["detail"].lower()


def test_claude_oauth_refresh_writes_credentials_only_when_opted_in(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BURNRATE_CLAUDE_OAUTH_REFRESH", "1")
    credentials = _write_expired_claude_credentials(tmp_path)
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_post(url, **kwargs):
        assert url == limits.CLAUDE_OAUTH_TOKEN_URL
        assert kwargs.get("trust_env") is False
        assert kwargs["json"]["refresh_token"] == "valid-refresh"
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "access_token": "fresh-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 28_800,
            },
        )

    def fake_get(url, **kwargs):
        assert kwargs.get("trust_env") is False
        assert kwargs["headers"]["Authorization"] == "Bearer fresh-access"
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "five_hour": {"utilization": 7, "resets_at": "2026-09-02T23:00:00Z"},
                "seven_day": {"utilization": 42, "resets_at": "2026-09-09T20:00:00Z"},
            },
        )

    monkeypatch.setattr("spend_app.limits.httpx.post", fake_post)
    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = limits._claude_limits_uncached()
    saved = json.loads(credentials.read_text(encoding="utf-8"))
    assert result["status"] == "exact"
    assert saved["unrelated"] == {"keep": True}
    assert saved["claudeAiOauth"]["accessToken"] == "fresh-access"
    assert saved["claudeAiOauth"]["refreshToken"] == "rotated-refresh"


def test_undocumented_interfaces_are_labeled_experimental() -> None:
    from spend_app.adapters import antigravity_local, cursor_usage, traycer_local

    assert "experimental" in (cursor_usage.__doc__ or "").lower()
    assert "api2.cursor.sh" in (cursor_usage.__doc__ or "")
    assert "experimental" in (antigravity_local.__doc__ or "").lower()
    assert "grpc-web" in (antigravity_local.__doc__ or "").lower()
    assert "experimental" in (traycer_local.__doc__ or "").lower()
    assert "profile-rate-limits" in (traycer_local.__doc__ or "")
    assert "agent list" in (traycer_local.__doc__ or "")
    assert "experimental" in (limits.__doc__ or "").lower()
    assert "/api/oauth/usage" in (limits.__doc__ or "")
    assert "BURNRATE_CLAUDE_OAUTH_REFRESH" in (limits.__doc__ or "")


def test_cursor_quota_client_disables_proxy_env(tmp_path: Path, monkeypatch) -> None:
    database = (
        tmp_path
        / "AppData"
        / "Roaming"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES('cursorAuth/accessToken', 'fixture-token')"
        )
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    observed: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, headers=None, json=None):
            request = httpx.Request("POST", url)
            if url.endswith("GetCurrentPeriodUsage"):
                payload = {"planUsage": {"autoPercentUsed": 1.0, "apiPercentUsed": 2.0}}
            elif url.endswith("GetPlanInfo"):
                payload = {"planInfo": {"planName": "Pro"}}
            else:
                payload = {"noUsageBasedAllowed": True}
            return httpx.Response(200, request=request, json=payload)

    monkeypatch.setattr("spend_app.limits.httpx.Client", FakeClient)
    result = limits._cursor_limits_uncached()
    assert observed.get("trust_env") is False
    assert "experimental" in result["detail"].lower()
    assert "api2.cursor.sh" in result["detail"]


def test_claude_oauth_401_does_not_refresh_when_opt_in_is_unset(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BURNRATE_CLAUDE_OAUTH_REFRESH", "0")
    credentials = _write_expired_claude_credentials(tmp_path)
    original = credentials.read_text(encoding="utf-8")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    posts: list[str] = []

    def fake_post(url, **_kwargs):
        posts.append(url)
        raise AssertionError("BURNRATE_CLAUDE_OAUTH_REFRESH=0 must not write credentials")

    def fake_get(url, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(401, request=request, json={"error": "unauthorized"})

    monkeypatch.setattr("spend_app.limits.httpx.post", fake_post)
    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = limits._claude_limits_uncached()
    assert posts == []
    assert credentials.read_text(encoding="utf-8") == original
    assert result["status"] == "error"
    assert result.get("httpStatus") == 401
    assert "BURNRATE_CLAUDE_OAUTH_REFRESH=1" in result["detail"]
