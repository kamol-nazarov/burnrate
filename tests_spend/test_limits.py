import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import spend_app.limits as limits

from spend_app.limits import (
    _claude_from_traycer_result,
    _claude_desktop_limits_uncached,
    _claude_limits_uncached,
    _codex_limits,
    _cursor_limits_uncached,
    _finite_number,
    _grok_from_traycer_result,
    _grok_limits_from_log,
    _tail_rate_limit,
    _traycer_active_agent_ids,
    _traycer_active_agents_uncached,
    _traycer_projection_activity,
    _zai_limits_uncached,
)


def test_claude_desktop_history_is_exact_and_does_not_invent_resets(tmp_path: Path) -> None:
    path = tmp_path / "plan-usage-history.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "samples": [
                    {"t": 1788377520833, "org": "fixture", "u": {"fh": 60, "sd": 1}},
                    {"t": 1788378420832, "org": "fixture", "u": {"fh": 1, "sd": 1}},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = _claude_desktop_limits_uncached(
        path,
        now=limits.datetime.fromtimestamp(1788378420832 / 1000, tz=limits.UTC),
    )
    assert result["status"] == "exact"
    assert [window["usedPct"] for window in result["windows"]] == [1, 1]
    assert all(window["resetAt"] is None for window in result["windows"])
    assert result["observedAt"] == "2026-09-02T19:47:00.832000Z"


def test_codex_rate_limit_tail_parser(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"timestamp": "2026-08-31T12:00:00Z", "payload": {"type": "other"}})
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-08-31T12:01:00Z",
                "payload": {
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {
                            "used_percent": 11,
                            "window_minutes": 10080,
                            "resets_at": 1788747938,
                        },
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    row = _tail_rate_limit(path)
    assert row is not None
    assert row[1]["plan_type"] == "pro"
    assert row[1]["primary"]["used_percent"] == 11


def test_traycer_grok_quota_is_kept_distinct_from_grok_bot() -> None:
    result = _grok_from_traycer_result(
        {
            "rateLimits": {
                "provider": "grok",
                "available": True,
                "subscriptionTier": "SuperGrok Heavy",
                "period": {"usedPercent": 26, "resetsAt": 1788406835074},
                "onDemandCap": 0,
                "onDemandUsed": 0,
                "prepaidBalance": 0,
            },
            "usageUpdatedAt": 1788211133689,
        }
    )
    assert result["name"] == "Grok Build"
    assert result["plan"] == "SuperGrok Heavy"
    assert result["windows"][0]["usedPct"] == 26
    assert "Grok Bot" not in result["detail"]


def test_traycer_claude_quota_includes_model_scoped_limits() -> None:
    result = _claude_from_traycer_result(
        {
            "rateLimits": {
                "available": True,
                "subscriptionType": "max",
                "fiveHour": {"usedPercent": 0, "resetsAt": 1788228599709},
                "sevenDay": {"usedPercent": 41, "resetsAt": 1788368399709},
                "modelScoped": [{"displayName": "Fable", "usedPercent": 0}],
                "extraUsage": {"isEnabled": False},
            },
            "usageUpdatedAt": 1788211411970,
        }
    )
    assert result["status"] == "exact"
    assert [window["usedPct"] for window in result["windows"]] == [0, 41, 0]
    assert result["windows"][2]["label"] == "Weekly · Fable"


def test_grok_empty_new_week_is_exact_zero_not_missing(tmp_path: Path) -> None:
    path = tmp_path / "unified.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": "2026-09-03T12:08:16.237Z",
                "msg": "billing: fetched credits config",
                "ctx": {
                    "subscriptionTier": "SuperGrok Heavy",
                    "config": {
                        "currentPeriod": {
                            "type": "USAGE_PERIOD_TYPE_WEEKLY",
                            "start": "2026-09-03T03:40:35Z",
                            "end": "2026-09-10T03:40:35Z",
                        },
                        "historyLen": 0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = _grok_limits_from_log(
        path,
        now=limits.datetime(2026, 9, 3, 12, tzinfo=limits.UTC),
    )
    assert result["status"] == "exact"
    assert result["windows"][0]["usedPct"] == 0
    assert result["windows"][0]["resetAt"] == "2026-09-10T03:40:35Z"


def test_traycer_agent_list_is_authoritative_for_live_activity(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / ".traycer" / "cli" / "bin" / "traycer.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_run(command, **kwargs):
        assert command[-2:] == ["agent", "list"]
        assert "--all" not in command
        assert kwargs["env"]["TRAYCER_EPIC_ID"] == "epic-1"
        assert kwargs["env"]["TRAYCER_AGENT_ID"] == "sender-1"
        return SimpleNamespace(
            stdout="\n".join(
                [
                    json.dumps({"type": "progress", "status": "ok"}),
                    json.dumps(
                        {
                            "type": "result",
                            "status": "ok",
                            "data": {
                                "agents": [
                                    {"id": "stale", "active": False},
                                    {"id": "live", "active": True},
                                ]
                            },
                        }
                    ),
                ]
            )
        )

    monkeypatch.setattr("spend_app.limits.subprocess.run", fake_run)
    result = _traycer_active_agents_uncached("epic-1", "sender-1")
    assert result["activeIds"] == ["live"]


def test_traycer_activity_does_not_launch_cli_while_idle(monkeypatch) -> None:
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("idle activity must not launch Traycer CLI")

    monkeypatch.setattr("spend_app.limits._traycer_active_agents_uncached", fail_if_called)
    with limits._TRAYCER_ACTIVITY_LOCK:
        limits._TRAYCER_ACTIVITY_SNAPSHOTS.clear()
        limits._TRAYCER_ACTIVITY_REFRESHING.clear()
    result = _traycer_active_agent_ids("idle-epic", "sender", potential_activity=False)
    assert result == set()
    assert called is False


def test_traycer_projection_does_not_treat_unmatched_turn_start_as_live() -> None:
    now_ms = 1_788_279_300_000
    projection = {
        "title": "Finished agent",
        "lifecycle": {"state": "active"},
        "settings": {"harnessId": "grok", "model": "grok-4.6"},
        "events": [
            {
                "body": {
                    "type": "turn.started",
                    "timestamp": now_ms - 1_000,
                    "metadata": {},
                }
            }
        ],
    }
    active, unmetered = _traycer_projection_activity(
        chat_id="finished",
        projection=projection,
        updated_at_ms=now_ms - 1_000,
        active_ids=set(),
        now_ms=now_ms,
    )
    assert active is None
    assert unmetered is None

    active, _ = _traycer_projection_activity(
        chat_id="finished",
        projection=projection,
        updated_at_ms=now_ms - 1_000,
        active_ids={"finished"},
        now_ms=now_ms,
    )
    assert active is not None
    assert active["chatId"] == "finished"
    assert active["model"] == "grok-4.6"


def test_traycer_projection_fallback_is_recent_and_unarchived_only() -> None:
    now_ms = 1_788_279_300_000
    projection = {
        "lifecycle": {"state": "active"},
        "events": [
            {
                "body": {
                    "type": "turn.started",
                    "timestamp": now_ms - 10_000,
                    "metadata": {},
                }
            }
        ],
    }
    recent, _ = _traycer_projection_activity(
        chat_id="recent",
        projection=projection,
        updated_at_ms=now_ms - 10_000,
        active_ids=None,
        now_ms=now_ms,
    )
    assert recent is not None

    projection["lifecycle"]["state"] = "archived"
    archived, _ = _traycer_projection_activity(
        chat_id="recent",
        projection=projection,
        updated_at_ms=now_ms - 10_000,
        active_ids=None,
        now_ms=now_ms,
    )
    assert archived is None

    projection["lifecycle"]["state"] = "active"
    projection["events"][0]["body"]["timestamp"] = now_ms - 120_000
    stale, _ = _traycer_projection_activity(
        chat_id="recent",
        projection=projection,
        updated_at_ms=now_ms - 120_000,
        active_ids=None,
        now_ms=now_ms,
    )
    assert stale is None


def test_finite_number_keeps_factual_zero_and_rejects_non_numbers() -> None:
    assert _finite_number(0) == 0.0
    assert _finite_number(12.5) == 12.5
    assert _finite_number(None) is None
    assert _finite_number("12") is None
    assert _finite_number(True) is None
    assert _finite_number(float("nan")) is None


def test_codex_limits_omit_windows_without_percent(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-31T12:01:00Z",
                "payload": {
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {"window_minutes": 300, "resets_at": 1788747938},
                        "secondary": {
                            "used_percent": 33.5,
                            "window_minutes": 10080,
                            "resets_at": 1788747938,
                        },
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)
    result = _codex_limits()
    assert result["status"] == "exact"
    assert [window["key"] for window in result["windows"]] == ["secondary"]
    assert result["windows"][0]["usedPct"] == 33.5


def test_claude_oauth_omits_windows_without_utilization(tmp_path: Path, monkeypatch) -> None:
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fixture-token", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_get(*_args, **_kwargs):
        request = httpx.Request("GET", "https://api.anthropic.com/api/oauth/usage")
        return httpx.Response(
            200,
            request=request,
            json={
                "five_hour": {"resets_at": "2026-08-31T17:00:00Z"},
                "seven_day": {"utilization": 41, "resets_at": "2026-09-04T23:59:59Z"},
            },
        )

    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = _claude_limits_uncached()
    assert result["status"] == "exact"
    assert [window["key"] for window in result["windows"]] == ["weekly"]
    assert result["windows"][0]["usedPct"] == 41


def test_claude_oauth_refreshes_expired_access_token_and_preserves_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BURNRATE_CLAUDE_OAUTH_REFRESH", "1")
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
                    "scopes": ["user:inference", "user:profile"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_post(url, **kwargs):
        assert url == "https://platform.claude.com/v1/oauth/token"
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
    result = _claude_limits_uncached()
    saved = json.loads(credentials.read_text(encoding="utf-8"))

    assert result["status"] == "exact"
    assert [window["usedPct"] for window in result["windows"]] == [7, 42]
    assert saved["unrelated"] == {"keep": True}
    assert saved["claudeAiOauth"]["accessToken"] == "fresh-access"
    assert saved["claudeAiOauth"]["refreshToken"] == "rotated-refresh"
    assert saved["claudeAiOauth"]["scopes"] == ["user:inference", "user:profile"]
    assert saved["claudeAiOauth"]["rateLimitTier"] == "default_claude_max_20x"
    assert saved["claudeAiOauth"]["expiresAt"] > 1


def test_cursor_limits_keep_included_window_out_of_required_keys(
    tmp_path: Path, monkeypatch
) -> None:
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

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, headers=None, json=None):
            request = httpx.Request("POST", url)
            if url.endswith("GetCurrentPeriodUsage"):
                payload = {
                    "planUsage": {
                        "autoPercentUsed": 55.0,
                        "apiPercentUsed": 4.0,
                        "totalSpend": 0,
                        "limit": 0,
                    },
                    "billingCycleEnd": 1788406835074,
                }
            elif url.endswith("GetPlanInfo"):
                payload = {"planInfo": {"planName": "Ultra", "includedAmountCents": 0}}
            else:
                payload = {"noUsageBasedAllowed": True}
            return httpx.Response(200, request=request, json=payload)

    monkeypatch.setattr("spend_app.limits.httpx.Client", FakeClient)
    result = _cursor_limits_uncached()
    keys = [window["key"] for window in result["windows"]]
    assert "cursor_models" in keys
    assert "other_models" in keys
    models = next(window for window in result["windows"] if window["key"] == "cursor_models")
    assert models["usedPct"] == 55.0


def test_zai_limits_do_not_invent_zero_percent(tmp_path: Path, monkeypatch) -> None:
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"zai-coding-plan": {"key": "fixture-token"}}), encoding="utf-8")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_get(*_args, **_kwargs):
        request = httpx.Request("GET", "https://api.z.ai/api/monitor/usage/quota/limit")
        return httpx.Response(
            200,
            request=request,
            json={
                "data": {
                    "limits": [
                        {"number": 5, "usage": 0, "currentValue": 0, "nextResetTime": None},
                        {
                            "number": 1,
                            "usage": 140000,
                            "currentValue": 25900,
                            "percentage": 18.5,
                            "nextResetTime": 1788406835074,
                        },
                    ]
                }
            },
        )

    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = _zai_limits_uncached()
    by_key = {window["key"]: window for window in result["windows"]}
    assert by_key["5h"]["usedPct"] is None
    assert by_key["weekly"]["usedPct"] == 18.5


def test_zai_omitted_current_value_is_unavailable_not_zero(tmp_path: Path, monkeypatch) -> None:
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"zai-coding-plan": {"key": "fixture-token"}}), encoding="utf-8")
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_get(*_args, **_kwargs):
        request = httpx.Request("GET", "https://api.z.ai/api/monitor/usage/quota/limit")
        return httpx.Response(
            200,
            request=request,
            json={"data": {"limits": [{"number": 5, "usage": 140000}, {"number": 1, "usage": 140000}]}},
        )

    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = _zai_limits_uncached()
    assert result["status"] == "unavailable"
    for window in result["windows"]:
        assert window["used"] is None
        assert window["usedPct"] is None


def test_zai_quota_falls_back_to_zcode_coding_plan_key(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".zcode" / "v2" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "provider": {
                    "builtin:zai-coding-plan": {
                        "options": {"apiKey": "zcode-plan-key"}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.Path.home", lambda: tmp_path)

    def fake_get(*_args, **kwargs):
        assert kwargs["headers"]["Authorization"] == "zcode-plan-key"
        request = httpx.Request("GET", "https://api.z.ai/api/monitor/usage/quota/limit")
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 200,
                "success": True,
                "data": {
                    "limits": [
                        {"number": 5, "usage": 28_000, "currentValue": 19, "remaining": 27_980, "percentage": 1},
                        {"number": 1, "usage": 140_000, "currentValue": 5_408, "remaining": 134_591, "percentage": 3},
                    ]
                },
            },
        )

    monkeypatch.setattr("spend_app.limits.httpx.get", fake_get)
    result = _zai_limits_uncached()
    assert result["status"] == "exact"
    assert result["name"] == "Z.AI Coding Plan"
    assert [window["usedPct"] for window in result["windows"]] == [1, 3]
