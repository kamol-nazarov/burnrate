import json
from datetime import UTC, datetime

import pytest

from spend_app.db import connect, initialize
from spend_app.diagnostics import detect_local_sources, safe_reason, source_reports

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "status,detected,finished,expected",
    [
        (None, False, None, "not_detected"),
        (None, True, None, "detected_without_history"),
        ("skipped", False, "2026-09-01T11:59:00Z", "configuration_missing"),
        ("success", None, "2026-09-01T11:59:00Z", "healthy"),
        ("success", None, "2026-08-31T11:59:00Z", "stale"),
        ("partial", True, "2026-09-01T11:59:00Z", "partial"),
        ("failed", True, "2026-09-01T11:59:00Z", "failed"),
    ],
)
def test_source_state_without_provider_probes(
    tmp_path, status, detected, finished, expected
):
    db = tmp_path / "s.db"
    initialize(db)
    with connect(db) as c:
        if status:
            c.execute(
                "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES('codex_local',?,?,?,?,?)",
                (
                    "2026-09-01T11:59:00Z",
                    finished,
                    status,
                    0,
                    "optional configuration not configured"
                    if status == "skipped"
                    else None,
                ),
            )
        report = next(
            r
            for r in source_reports(c, NOW, {"codex_local": detected})
            if r["source"] == "codex_local"
        )
        assert report["state"] == expected
        assert report["nextAction"] and "quota" not in report["measurements"]


def test_failure_after_success_is_visible_and_redacted(tmp_path):
    db = tmp_path / "s.db"
    initialize(db)
    with connect(db) as c:
        for status, stamp, error in [
            ("success", "2026-09-01T11:59:00Z", None),
            (
                "failed",
                "2026-09-01T12:00:00Z",
                r"Access denied C:\Users\Private\secrets token-sensitive",
            ),
        ]:
            c.execute(
                "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES('claude_local',?,?,?,?,?)",
                (stamp, stamp, status, 0, error),
            )
        row = next(r for r in source_reports(c, NOW) if r["source"] == "claude_local")
        assert row["state"] == "failed"
        assert (
            row["lastSuccess"] == "2026-09-01T11:59:00Z"
            and row["lastAttempt"] == "2026-09-01T12:00:00Z"
        )
        assert "read access" in row["nextAction"]
        assert "Private" not in json.dumps(row) and "token-sensitive" not in json.dumps(
            row
        )


def test_metadata_detection_is_bounded_to_supplied_home(tmp_path):
    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    result = detect_local_sources(tmp_path)
    assert result["codex_local"] is True and result["claude_local"] is False


def test_onboarding_health_reads_do_not_probe_or_mutate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from spend_app.api import create_app
    from tests_spend.test_api import add_fixture_event, make_settings
    db = tmp_path / "s.db"
    app = create_app(make_settings(db), enable_scheduler=False, now=NOW)

    def forbidden(*a, **k):
        raise AssertionError("Provider probe or credential mutation on read")

    monkeypatch.setattr("spend_app.api.snapshot_limits", forbidden)
    monkeypatch.setattr("spend_app.diagnostics.detect_local_sources", forbidden)
    client = TestClient(app)
    with connect(db) as c:
        before = c.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    setup = client.get("/api/onboarding").json()
    assert setup["hasHistory"] is False and setup["timezone"] == "America/New_York"
    response = client.get("/api/spend/health")
    assert response.status_code == 200 and response.json()["sources"]
    with connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == before
    add_fixture_event(db)
    assert client.get("/api/onboarding").json()["hasHistory"] is True


def test_reason_does_not_return_raw_payload():
    text = safe_reason(
        "upstream failed with account-id and an arbitrary private path", "grok_local"
    )
    assert "account-id" not in text and "private path" not in text


def test_path_hint_is_home_relative_without_filesystem(monkeypatch):
    from pathlib import Path
    from spend_app.diagnostics import LOCAL_PATHS, source_path_hint
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(AssertionError("home lookup")))
    for source, parts in LOCAL_PATHS.items():
        assert source_path_hint(source) == "~/" + "/".join(parts)
    assert source_path_hint("openai_admin") is None


def test_path_hint_is_in_diagnostics_report_without_database():
    from unittest.mock import Mock
    class Connection:
        def execute(self, sql, params=()):
            if "SELECT DISTINCT" in sql or "FROM pricing_gaps" in sql:
                return []
            if "SELECT MAX(finished_at)" in sql:
                return Mock(fetchone=lambda: (None,))
            if "COUNT(*)" in sql:
                return Mock(fetchone=lambda: (0, 0))
            if "ORDER BY id DESC" in sql:
                return Mock(fetchone=lambda: None)
            raise AssertionError(sql)
    reports = source_reports(Connection(), NOW, {"codex_local": True})
    codex = next(row for row in reports if row["source"] == "codex_local")
    assert codex["path"] == "~/.codex/sessions"
    assert codex["state"] == "detected_without_history"
    assert all(row["path"] is None or row["path"].startswith("~/") for row in reports)


def test_integration_metadata_exposes_booleans_not_values():
    from types import SimpleNamespace
    from spend_app.diagnostics import integration_reports
    settings = SimpleNamespace(openai_admin_key="private-key-do-not-return", anthropic_admin_key=None, cursor_api_key=None)
    reports = integration_reports(settings, environ={"BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE":"true", "BURNRATE_ENABLE_CURSOR_USAGE_SERVICE":"false", "OPENROUTER_MANAGEMENT_KEY":"another-private-key"})
    assert len(reports) == 7
    by_setting = {row["setting"]: row for row in reports}
    assert by_setting["OPENAI_ADMIN_KEY"]["configured"] is True
    assert by_setting["OPENAI_ADMIN_KEY"]["host"] == "api.openai.com"
    assert by_setting["BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE"]["configured"] is True
    assert by_setting["BURNRATE_ENABLE_CURSOR_USAGE_SERVICE"]["configured"] is False
    assert all(type(row["configured"]) is bool for row in reports)
    assert "private-key" not in json.dumps(reports)


def test_integration_metadata_preserves_vault_connections_without_secret_read():
    from types import SimpleNamespace
    from spend_app.diagnostics import integration_reports
    reports = integration_reports(SimpleNamespace(), environ={}, bindings={"openai_admin":{"enabled":True,"credentialRef":"opaque-reference"},"cursor_admin":{"enabled":False,"credentialRef":"disabled-reference"}})
    by_setting = {row["setting"]: row for row in reports}
    assert by_setting["OPENAI_ADMIN_KEY"]["configured"] and by_setting["OPENAI_ADMIN_KEY"]["managed"]
    assert not by_setting["CURSOR_API_KEY"]["configured"]
    assert "reference" not in json.dumps(reports)
