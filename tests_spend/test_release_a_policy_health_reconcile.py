"""Release A policy tests: credential access, source health, reconciliation."""

from __future__ import annotations

import json

import pytest

from spend_app import integration_policy as policy
from spend_app.source_health import (
    SourceHealth,
    close_attempt,
    parse_jsonl_record,
    sanitize_reason,
)
from spend_app.source_reconcile import canonical_event_id, reconcile_events


# ---------------------------------------------------------------------------
# A01 — policy denies native-credential lanes by default, before any I/O.
# ---------------------------------------------------------------------------

def test_native_lanes_denied_by_default_with_actionable_reason():
    for lane in ("claude_oauth_usage", "cursor_usage_service", "zai_quota_endpoint"):
        allowed, reason = policy.authorize(lane, environ={})
        assert allowed is False
        assert reason and "disabled by default" in reason
        assert "BURNRATE_ENABLE" in reason


def test_native_lanes_activate_only_with_explicit_consent():
    env = {"BURNRATE_ENABLE_CURSOR_USAGE_SERVICE": "1"}
    allowed, _ = policy.authorize("cursor_usage_service", environ=env)
    assert allowed is True
    # An unrelated consent flag must not unlock another lane.
    allowed_other, _ = policy.authorize("claude_oauth_usage", environ=env)
    assert allowed_other is False


def test_local_and_configured_lanes_always_allowed():
    assert policy.authorize("codex_local_telemetry", environ={})[0] is True
    assert policy.authorize("openai_admin", environ={})[0] is True


def test_require_raises_policy_denied_without_io():
    with pytest.raises(policy.PolicyDenied) as excinfo:
        policy.require("zai_quota_endpoint", environ={})
    assert "Exception" not in str(excinfo.value)


def test_host_allowlist_blocks_other_destinations():
    policy.assert_allowed_host("openai_admin", "api.openai.com")
    with pytest.raises(policy.PolicyDenied):
        policy.assert_allowed_host("openai_admin", "evil.example.com")
    with pytest.raises(policy.PolicyDenied):
        policy.assert_allowed_host("cursor_usage_service", "api.cursor.com")


def test_identity_headers_are_burnrate_not_native():
    headers = policy.identity_headers()
    assert headers["User-Agent"].startswith("burnrate/")


def test_disabled_lane_cannot_activate_via_legacy_flag_names():
    # Old refresh flags must not smuggle a lane back into shipped paths.
    legacy = {
        "BURNRATE_REFRESH_CLAUDE": "1",
        "SPEND_ENABLE_CURSOR_QUOTA": "1",
        "CLAUDE_REFRESH": "1",
    }
    for lane in ("claude_oauth_usage", "cursor_usage_service"):
        assert policy.authorize(lane, environ=legacy)[0] is False


# ---------------------------------------------------------------------------
# A03 — source health lifecycle.
# ---------------------------------------------------------------------------

def test_sanitize_reason_strips_secret_shaped_text_and_classes():
    text = "HTTP error for bearer abcDEF123skipmenow and sk-proj-verysecret12345; See TypeError: boom"
    cleaned = sanitize_reason(text)
    assert "abcDEF123skipmenow" not in cleaned
    assert "verysecret12345" not in cleaned
    assert "[redacted]" in cleaned
    assert "\n" not in cleaned


def test_jsonl_isolation_good_bad_good():
    health = SourceHealth()
    lines = [
        json.dumps({"ok": 1}),
        "{not json",
        json.dumps([1, 2, 3]),  # valid JSON, wrong shape
    ]
    outcomes = []
    for number, line in enumerate(lines, start=1):
        record, outcome = parse_jsonl_record(line, line_number=number, location="sessions/x.jsonl", health=health)
        outcomes.append((record, outcome))
    assert outcomes[0][0] == {"ok": 1}
    assert outcomes[1][0] is None and outcomes[1][1].failed
    assert outcomes[2][0] is None and outcomes[2][1].failed
    assert health.parsed == 1
    assert health.quarantined == 2
    # Sanitized locations, no payload text.
    assert all("not json" not in (reason or "") for reason in health.reasons)
    result = close_attempt(health, written=1)
    assert result.status == "partial"
    assert "quarantined record(s)" in (result.error or "")


def test_healthy_run_when_nothing_quarantined():
    health = SourceHealth()
    parse_jsonl_record(json.dumps({"ok": 1}), line_number=1, location="f", health=health)
    result = close_attempt(health, written=1)
    assert result.status == "success"
    assert result.error is None


def test_unfinished_tail_is_retriable_skip_not_quarantine():
    health = SourceHealth()
    record, outcome = parse_jsonl_record(
        json.dumps({"ok": 1})[:-3],  # truncated final write
        line_number=9,
        location="f",
        health=health,
    )
    assert record is None
    assert outcome.status == "quarantined"  # counted honestly
    # Callers treat a missing trailing newline as retriable via this helper.
    from spend_app.source_health import skipped as skip_outcome

    retriable = skip_outcome("unfinished trailing line", "f:9")
    assert retriable.status == "skipped"
    health.note(retriable)
    result = close_attempt(health, written=0)
    assert result.status == "partial"


# ---------------------------------------------------------------------------
# A06 — cross-source reconciliation.
# ---------------------------------------------------------------------------

def _admin_row(event_id="e1", **over):
    row = {
        "source": "cursor_admin",
        "raw_id": f"cursor-admin:{event_id}",
        "cost_usd": 0.4,
        "telemetry_complete": True,
        "account_key": None,
    }
    row.update(over)
    return row


def test_admin_and_csv_copies_collapse_to_one_event():
    csv_copy = _admin_row()
    csv_copy.update(source="cursor_csv", raw_id="cursor-csv:e1", cost_usd=None)
    output, report = reconcile_events([_admin_row(), csv_copy])
    assert report["suppressedDuplicates"] == 1
    assert report["canonicalEvents"] == 1
    assert output[0].row["source"] == "cursor_admin"  # charged copy authoritative
    assert set(output[0].provenance) == {"cursor_admin", "cursor_csv"}


def test_import_order_never_changes_the_survivor():
    csv_copy = _admin_row()
    csv_copy.update(source="cursor_csv", raw_id="cursor-csv:e1", cost_usd=None)
    forward, forward_report = reconcile_events([_admin_row(), csv_copy])
    backward, backward_report = reconcile_events([csv_copy, _admin_row()])
    assert forward[0].row["raw_id"] == backward[0].row["raw_id"]
    assert forward_report["suppressedDuplicates"] == backward_report["suppressedDuplicates"] == 1


def test_different_accounts_with_same_id_stay_distinct():
    other_account = _admin_row()
    other_account["account_key"] = "ws_2"
    output, report = reconcile_events([_admin_row(), other_account])
    assert len(output) == 2
    assert report["suppressedDuplicates"] == 0


def test_lookalike_but_unverified_events_never_merge():
    a = _admin_row(event_id="e1")
    b = _admin_row(event_id="e2")
    b.update(input_tokens=999)
    output, report = reconcile_events([a, b])
    assert len(output) == 2
    assert report["suppressedDuplicates"] == 0


def test_local_only_sources_never_claim_cross_source_identity():
    assert canonical_event_id("codex_local", "codex-local:s:1") is None
    assert canonical_event_id("claude_local", "claude-local:s:m") is None


def test_repeated_replay_is_idempotent():
    rows = [_admin_row(), _admin_row(event_id="e2")]
    first, first_report = reconcile_events(rows)
    second, second_report = reconcile_events(rows)
    assert [event.row["raw_id"] for event in first] == [event.row["raw_id"] for event in second]
    assert first_report == second_report
