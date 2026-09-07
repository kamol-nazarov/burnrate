import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.db import connect, initialize
from spend_app.diagnostics import detect_local_sources, safe_reason, source_reports
from tests_spend.test_api import add_fixture_event, make_settings

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
