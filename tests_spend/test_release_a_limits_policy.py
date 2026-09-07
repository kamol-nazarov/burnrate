"""Release A acceptance: credential-access policy enforcement (A01/A13).

Poison every native-credential reader and HTTP transport: with default
settings, imports, safe GETs, scheduler cycles and CLI health paths must
never touch them, and old refresh flags must not unlock a lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import limits
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import initialize
from spend_app.quotas import default_quota_collectors, poll_quotas

NOW = "2026-09-06T12:00:00Z"


def _settings(tmp_path: Path) -> Settings:
    database = tmp_path / "spend.db"
    initialize(database)
    return Settings(
        database_path=database,
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        cursor_import_path=tmp_path / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


@pytest.fixture()
def poisoned(monkeypatch, tmp_path):
    """Poison the primitives the real paths would reach if any gate leaked.

    The Cursor token reader and both network primitives explode loudly; the
    Claude/Z.AI credential files are moved away by pointing Path.home at an
    empty directory, so an unguarded lane would fall through to the poisoned
    network instead of finding nothing quietly.
    """
    calls = []

    def poison(name):
        def _boom(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not run under default policy")

        return _boom

    monkeypatch.setattr(limits.httpx, "get", poison("httpx_get"))
    monkeypatch.setattr(limits.httpx, "Client", poison("httpx_client"))
    # File-read primitive: reached only if a lane gets past its policy gate
    # AND the credential store exists. Never touched by denied lanes.
    monkeypatch.setattr(limits, "sqlite_read_only", poison("sqlite_read_only"))
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setattr(limits.Path, "home", classmethod(lambda cls: empty_home))
    return calls


def test_safe_reads_and_app_boot_never_probe(poisoned, tmp_path, monkeypatch):
    # Legacy flags must be inert.
    monkeypatch.setenv("BURNRATE_REFRESH_CLAUDE", "1")
    monkeypatch.setenv("SPEND_ENABLE_CURSOR_QUOTA", "1")
    client = TestClient(create_app(_settings(tmp_path), enable_scheduler=False))
    for path in ("/api/spend/summary", "/api/spend/nav", "/api/spend/health", "/api/onboarding", "/api/spend/limits"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert poisoned == []


def test_scheduled_quota_polling_denies_native_lanes(poisoned, tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE", raising=False)
    monkeypatch.delenv("BURNRATE_ENABLE_CURSOR_USAGE_SERVICE", raising=False)
    monkeypatch.delenv("BURNRATE_ENABLE_ZAI_QUOTA", raising=False)
    database = tmp_path / "spend.db"
    # A fresh lane scheduler: the module-global one may still owe back-off
    # from an earlier test's polls.
    from spend_app.quotas import QuotaLaneScheduler

    result = poll_quotas(database, lanes=QuotaLaneScheduler(), now=lambda: NOW)
    assert set(result["polledProviders"]) == {"antigravity", "claude-code", "codex", "cursor", "grok", "openrouter", "opencode"}
    assert poisoned == []
    import sqlite3

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT provider_key, label FROM quotas WHERE polled_at=?", (NOW,)
        ).fetchall()
    labels = {provider: label for provider, label in rows}
    # Denial copy is actionable: names the consent variable, no class names.
    assert "BURNRATE_ENABLE_ZAI_QUOTA" in labels.get("opencode", "")
    assert "BURNRATE_ENABLE_CURSOR_USAGE_SERVICE" in labels.get("cursor", "")
    assert "Exception" not in " ".join(labels.values())


def test_cli_doctor_touches_no_credentials(poisoned, tmp_path, monkeypatch):
    from spend_app.cli import main

    database = tmp_path / "spend.db"
    initialize(database)
    settings = _settings(tmp_path)
    monkeypatch.setattr("spend_app.cli.load_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["burnrate", "doctor"])
    assert main() == 0
    assert poisoned == []


def test_consent_unlocks_only_the_named_lane(poisoned, tmp_path, monkeypatch):
    limits._CACHE.clear()
    def no_desktop_snapshot():
        raise FileNotFoundError("No local snapshot")
    monkeypatch.setattr("spend_app.quotas._claude_desktop_limits_uncached", no_desktop_snapshot)
    monkeypatch.setenv("BURNRATE_ENABLE_ZAI_QUOTA", "1")
    database = tmp_path / "spend.db"
    collectors = default_quota_collectors(database)
    # The ZAI lane runs now (the fixture home has no key file, so the lane
    # reports unavailable without inventing values)...
    samples = collectors["opencode"]()
    assert samples
    # ...while the other native lanes stay denied with actionable copy.
    for provider in ("cursor", "claude-code"):
        for sample in collectors[provider]():
            assert sample.pct is None
            assert "BURNRATE_ENABLE" in (sample.reason or "")
    assert poisoned == []  # no network, no native token access


def test_access_token_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "secret-token")
    client = TestClient(create_app(_settings(tmp_path), enable_scheduler=False))
    assert client.get("/api/spend/summary").status_code == 401
    assert client.get("/api/spend/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/api/spend/summary", headers={"Authorization": "Bearer secret-token"}).status_code
        == 200
    )


def test_limits_snapshot_reports_persisted_rows(tmp_path):
    from spend_app.db import connect, upsert_quota

    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        upsert_quota(
            connection,
            provider_key="codex",
            limit_key="weekly",
            label="Codex weekly window",
            unit="pct",
            source="codex_local_telemetry",
            polled_at=NOW,
            pct=42.0,
            resets_at=None,
        )
    payload = limits.snapshot_limits(database)
    codex = next(provider for provider in payload["providers"] if provider["key"] == "codex")
    assert codex["status"] == "exact"
    assert codex["windows"][0]["usedPct"] == 42.0
    assert payload["snapshot"] is True
