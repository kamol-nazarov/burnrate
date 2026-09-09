"""PV1 collection-evidence regressions using the loader's row shape.

These tests deliberately put source health through ``load_source_health``
instead of handing ``_collection_evidence`` a pre-normalized dictionary.  The
connection is a fake query-row provider so the checks remain unit-only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from spend_app.plans_value import _collection_evidence, assemble_group_valuation
from spend_app.plans_value_store import load_source_health


AS_OF = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _HealthConnection:
    """Small fake for the bounded health queries only."""

    def __init__(self, latest, successes=(), bindings=None):
        self.latest = list(latest)
        self.successes = list(successes)
        self.bindings = bindings
        self.sql = []

    def execute(self, statement, parameters=()):
        normalized = " ".join(str(statement).split())
        self.sql.append(normalized)
        if "MAX(id)" in normalized and "GROUP BY source" in normalized:
            return _Rows(self.latest)
        if "MAX(finished_at)" in normalized and "status IN" in normalized:
            return _Rows(self.successes)
        if "FROM app_meta" in normalized:
            value = None if self.bindings is None else json.dumps(self.bindings)
            return _Rows([{"value": value}]) if value is not None else _Rows([])
        raise AssertionError(f"unexpected SQL in unit fake: {normalized}")


def _group(tool_keys=("codex",)):
    return {
        "group_id": "tool:codex",
        "name": "Codex",
        "tool_keys": tuple(tool_keys),
        "plan_ids": ("p1",),
        "plans": [],
        "active_intervals": [],
        "accrued_cost": Decimal("80"),
    }


def _health_rows(connection):
    return load_source_health(connection, as_of=AS_OF)


def test_loader_payload_scopes_codex_and_preserves_unrelated_cursor_failure():
    connection = _HealthConnection(
        [
            {
                "source": "codex_local",
                "started_at": "2026-09-09T03:59:00Z",
                "finished_at": "2026-09-09T04:00:00Z",
                "status": "success",
                "events_written": 2,
                "error": None,
            },
            {
                "source": "cursor_local",
                "started_at": "2026-09-09T03:58:00Z",
                "finished_at": "2026-09-09T03:58:01Z",
                "status": "failed",
                "events_written": 0,
                "error": "redacted cursor failure",
            },
        ],
        successes=[
            {"source": "codex_local", "last_success_at": "2026-09-09T04:00:00Z"},
            {"source": "cursor_local", "last_success_at": None},
        ],
    )

    rows = _health_rows(connection)
    codex = next(row for row in rows if row["source"] == "codex_local")
    cursor = next(row for row in rows if row["source"] == "cursor_local")

    assert codex["toolKeys"] == ["codex"]
    assert codex["lastAttemptAt"] == "2026-09-09T04:00:00Z"
    assert codex["lastSuccessAt"] == "2026-09-09T04:00:00Z"
    assert codex["freshness"]["state"] == "recent"
    assert cursor["toolKeys"] == ["cursor"]
    assert cursor["status"] == "failed"

    evidence = _collection_evidence(rows, tool_keys=("codex",))
    assert evidence["status"] == "healthy"
    assert [row["source"] for row in evidence["sources"]] == ["codex_local"]


def test_loader_retains_old_success_and_marks_it_stale():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "codex_local",
                    "started_at": "2026-08-01T11:59:00Z",
                    "finished_at": "2026-08-01T12:00:00Z",
                    "status": "success",
                    "events_written": 4,
                    "error": None,
                }
            ],
            successes=[
                {"source": "codex_local", "last_success_at": "2026-08-01T12:00:00Z"}
            ],
        )
    )
    codex = next(row for row in rows if row["source"] == "codex_local")
    assert codex["lastSuccessAt"] == "2026-08-01T12:00:00Z"
    assert codex["freshness"]["state"] == "stale"
    assert codex["freshness"]["ageSeconds"] > 0
    assert _collection_evidence(rows, tool_keys=("codex",))["status"] == "stale"


def test_newer_failure_keeps_previous_success_and_safe_reason():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "codex_local",
                    "started_at": "2026-09-09T03:59:00Z",
                    "finished_at": "2026-09-09T04:00:00Z",
                    "status": "failed",
                    "events_written": 0,
                    "error": "redacted provider failure",
                }
            ],
            successes=[
                {"source": "codex_local", "last_success_at": "2026-09-08T04:00:00Z"}
            ],
        )
    )
    codex = next(row for row in rows if row["source"] == "codex_local")
    assert codex["status"] == "failed"
    assert codex["lastAttemptAt"] == "2026-09-09T04:00:00Z"
    assert codex["lastSuccessAt"] == "2026-09-08T04:00:00Z"
    assert "redacted provider failure" in codex["reason"]
    assert _collection_evidence(rows, tool_keys=("codex",))["status"] == "failed"


def test_missing_timestamps_are_unknown_and_success_does_not_prove_history():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "codex_local",
                    "started_at": None,
                    "finished_at": None,
                    "status": "success",
                    "events_written": 1,
                    "error": None,
                }
            ],
            successes=[{"source": "codex_local", "last_success_at": None}],
        )
    )
    codex = next(row for row in rows if row["source"] == "codex_local")
    assert codex["freshness"]["state"] == "unknown"
    evidence = _collection_evidence(rows, tool_keys=("codex",))
    assert evidence["status"] == "unknown"
    assert evidence["complete"] is False
    assert "historical" in evidence["note"].lower()


def test_shared_opencode_zcode_health_includes_both_registered_lanes():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "opencode_local",
                    "started_at": "2026-09-09T03:59:00Z",
                    "finished_at": "2026-09-09T04:00:00Z",
                    "status": "success",
                    "events_written": 1,
                    "error": None,
                },
                {
                    "source": "zcode_local",
                    "started_at": "2026-09-09T03:58:00Z",
                    "finished_at": "2026-09-09T03:58:01Z",
                    "status": "failed",
                    "events_written": 0,
                    "error": "zcode source unavailable",
                },
            ],
            successes=[
                {"source": "opencode_local", "last_success_at": "2026-09-09T04:00:00Z"},
                {"source": "zcode_local", "last_success_at": None},
            ],
        )
    )
    shared = _collection_evidence(rows, tool_keys=("opencode", "zcode"))
    assert shared["status"] == "failed"
    assert {row["source"] for row in shared["sources"]} == {
        "opencode_local",
        "zcode_local",
    }
    opencode = next(row for row in shared["sources"] if row["source"] == "opencode_local")
    assert "source usage capability is partial" in opencode["coverage"].lower()


def test_disabled_source_is_distinct_without_reading_credentials():
    rows = _health_rows(
        _HealthConnection(
            [],
            successes=[],
            bindings={
                "version": 1,
                "bindings": {
                    "codex_local": {
                        "enabled": False,
                        "state": "disabled",
                        "detail": r"C:\\Users\\private\\secret",
                    }
                },
            },
        )
    )
    codex = next(row for row in rows if row["source"] == "codex_local")
    assert codex["configuredState"] == "disabled"
    assert codex["status"] == "never"
    assert "private" not in json.dumps(codex).lower()
    assert _collection_evidence(rows, tool_keys=("codex",))["status"] == "disabled"


def test_unrelated_health_change_does_not_change_numeric_valuation():
    group = _group()
    event = {
        "source": "codex_local",
        "tool_key": "codex",
        "model_key": "gpt-5",
        "occurred_at": "2026-09-05T12:00:00Z",
        "input_tokens": 10,
        "output_tokens": 0,
        "computed_cost_usd": "12",
    }
    healthy = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "codex_local",
                    "started_at": "2026-09-09T03:59:00Z",
                    "finished_at": "2026-09-09T04:00:00Z",
                    "status": "success",
                    "events_written": 1,
                    "error": None,
                }
            ],
            successes=[{"source": "codex_local", "last_success_at": "2026-09-09T04:00:00Z"}],
        )
    )
    with_unrelated = healthy + [
        {
            "source": "cursor_local",
            "toolKeys": ["cursor"],
            "status": "failed",
            "configuredState": "configured",
            "freshness": {"state": "unknown", "ageSeconds": None, "asOf": "2026-09-09T04:00:00Z"},
        }
    ]
    first = assemble_group_valuation(group, [event], [], healthy)
    second = assemble_group_valuation(group, [event], [], with_unrelated)
    assert second["usageValueUsd"] == first["usageValueUsd"]
    assert second["multiple"] == first["multiple"]
    assert second["collectionEvidence"]["status"] == first["collectionEvidence"]["status"]


def test_global_unattributed_issue_is_not_attached_to_a_group():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": r"C:\\Users\\private\\unknown.db",
                    "started_at": "2026-09-09T03:59:00Z",
                    "finished_at": "2026-09-09T04:00:00Z",
                    "status": "failed",
                    "events_written": 0,
                    "error": "raw response in C:\\Users\\private\\secret.txt",
                }
            ],
            successes=[],
        )
    )
    global_row = next(row for row in rows if row["source"] == "unattributed")
    assert global_row["toolKeys"] == []
    assert global_row["relevant"] is False
    evidence = _collection_evidence(rows, tool_keys=("codex",))
    assert all(row["source"] != "unattributed" for row in evidence["sources"])
    assert evidence["status"] != "failed"
    encoded = json.dumps(global_row)
    assert "private" not in encoded.lower()
    assert "secret.txt" not in encoded.lower()
    assert "raw response" not in encoded.lower()


def test_unconfigured_same_tool_lane_does_not_contaminate_configured_lane():
    rows = _health_rows(
        _HealthConnection(
            [
                {
                    "source": "cursor_local",
                    "started_at": "2026-09-09T03:59:00Z",
                    "finished_at": "2026-09-09T04:00:00Z",
                    "status": "success",
                    "events_written": 1,
                    "error": None,
                },
                {
                    "source": "cursor_usage_service",
                    "started_at": "2026-09-09T03:58:00Z",
                    "finished_at": "2026-09-09T03:58:01Z",
                    "status": "skipped",
                    "events_written": 0,
                    "error": "adaptive cadence",
                },
            ],
            successes=[
                {"source": "cursor_local", "last_success_at": "2026-09-09T04:00:00Z"}
            ],
        )
    )
    evidence = _collection_evidence(rows, tool_keys=("cursor",))
    assert evidence["status"] == "healthy"
    assert [row["source"] for row in evidence["sources"]] == ["cursor_local"]


def test_cadence_skip_retains_recent_success_without_claiming_missing_configuration():
    rows = _health_rows(_HealthConnection(
        [{"source": "cursor_usage_service", "status": "skipped", "error": "adaptive cadence", "finished_at": "2026-09-09T04:00:00Z"}],
        successes=[{"source": "cursor_usage_service", "last_success_at": "2026-09-09T03:59:00Z"}],
    ))
    source = next(row for row in rows if row["source"] == "cursor_usage_service")
    assert source["configuredState"] == "configured"
    assert source["status"] == "skipped"
    assert source["freshness"]["state"] == "recent"
    assert _collection_evidence(rows, tool_keys=("cursor",))["status"] == "healthy"


def test_redacted_prefix_does_not_allow_arbitrary_response_text():
    rows = _health_rows(_HealthConnection(
        [{"source": "codex_local", "status": "failed", "error": "redacted confidential conversation contents"}],
    ))
    source = next(row for row in rows if row["source"] == "codex_local")
    assert "confidential" not in source["reason"]
    assert "conversation" not in source["reason"]
