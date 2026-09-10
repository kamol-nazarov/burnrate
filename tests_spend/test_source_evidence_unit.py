"""Source-shaped characterization under fail-closed external boundaries."""

import json
import socket
import sqlite3
import subprocess
import ctypes
import builtins
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx  # Load third-party distribution metadata before application guards.


def denied(*args, **kwargs):
    raise AssertionError("unexpected external access in source-evidence unit")


@contextmanager
def isolated():
    with ExitStack() as stack:
        for target in ("sqlite3.connect", "socket.socket.connect", "subprocess.Popen",
                       "pathlib.Path.home", "pathlib.Path.open", "pathlib.Path.glob",
                       "pathlib.Path.rglob", "pathlib.Path.iterdir"):
            stack.enter_context(patch(target, denied))
        if hasattr(ctypes, "WinDLL"):
            stack.enter_context(patch("ctypes.WinDLL", denied))
        yield


# Guard before importing the actual loader and its transitive registry imports.
with isolated(), patch("pathlib.Path.home", return_value=Path("C:/source-evidence-fixture")):
    from spend_app import diagnostics, plans_value, plans_value_store, source_health

# Pytest may read this controlled test source for assertion rewriting. Its
# application imports are already loaded above under the stricter boundary.
from tests_spend.test_plans_value_health_unit import _HealthConnection
from tests_spend import test_plans_value_health_unit as health_cases


AS_OF = datetime(2026, 9, 9, 4, tzinfo=UTC)
ROWS = [
    {"source": "codex_local", "started_at": "2026-09-09T03:59:00Z",
     "finished_at": "2026-09-09T04:00:00Z", "status": "success", "events_written": 2, "error": None},
    {"source": "cursor_local", "started_at": "2026-09-09T03:59:00Z",
     "finished_at": "2026-09-09T04:00:00Z", "status": "failed", "events_written": 0,
     "error": "permission denied at C:\\Users\\synthetic\\secret.txt"},
    {"source": "grok_local", "started_at": "2026-09-09T03:59:00Z",
     "finished_at": "2026-09-09T04:00:00Z", "status": "partial", "events_written": 1,
     "error": "Unpriced models: synthetic-model"},
    {"source": "cursor_usage_service", "started_at": "2026-09-09T03:59:00Z",
     "finished_at": "2026-09-09T04:00:00Z", "status": "skipped", "events_written": 0,
     "error": "adaptive cadence"},
]
SUCCESSES = [
    {"source": "codex_local", "last_success_at": "2026-09-09T04:00:00Z"},
    {"source": "cursor_local", "last_success_at": "2026-09-08T04:00:00Z"},
    {"source": "grok_local", "last_success_at": "2026-09-09T04:00:00Z"},
    {"source": "cursor_usage_service", "last_success_at": "2026-09-09T03:59:00Z"},
]


class DiagnosticConnection:
    def __init__(self):
        self.queries = []

    def execute(self, statement, parameters=()):
        self.queries.append((statement, parameters))
        source = parameters[0] if parameters else None
        if "SELECT DISTINCT source" in statement:
            return [(row["source"],) for row in ROWS]
        if "ORDER BY id DESC" in statement:
            value = next((row for row in ROWS if row["source"] == source), None)
        elif "MAX(finished_at)" in statement:
            value = (next((row["last_success_at"] for row in SUCCESSES if row["source"] == source), None),)
        elif "COUNT(*)" in statement:
            value = (2, 0) if source == "codex_local" else (0, 1) if source == "grok_local" else (0, 0)
        elif "FROM pricing_gaps" in statement:
            return [("synthetic-model",)] if source == "grok_local" else []
        else:
            raise AssertionError("unexpected diagnostic query")
        return type("Result", (), {"fetchone": lambda self: value})()


def characterization():
    with isolated():
        connection = _HealthConnection(ROWS, SUCCESSES)
        health = plans_value_store.load_source_health(connection, AS_OF)
        diagnostic_connection = DiagnosticConnection()
        reports = diagnostics.source_reports(diagnostic_connection, AS_OF)
        return {"health": health, "diagnostics": reports,
                "codex": plans_value._collection_evidence(health, tool_keys=("codex",)),
                "cursor": plans_value._collection_evidence(health, tool_keys=("cursor",)),
                "queryCounts": [len(connection.sql), len(diagnostic_connection.queries)]}


def test_complete_payloads_and_query_counts_match_characterization():
    expected = json.loads((Path(__file__).parent / "fixtures/source_evidence_response.json").read_text())
    assert characterization() == expected


def test_independent_scope_prior_success_and_pricing_policy():
    result = characterization()
    health = {row["source"]: row for row in result["health"]}
    reports = {row["source"]: row for row in result["diagnostics"]}
    assert result["codex"]["status"] == "healthy"
    assert result["cursor"]["status"] == "failed"
    assert health["cursor_local"]["lastSuccessAt"] == "2026-09-08T04:00:00Z"
    assert reports["cursor_local"]["lastSuccess"] == "2026-09-08T04:00:00Z"
    assert health["cursor_local"]["freshness"]["ageSeconds"] == 86400
    assert health["grok_local"]["status"] == reports["grok_local"]["state"] == "partial"
    assert health["cursor_usage_service"]["configuredState"] == "configured"
    assert reports["cursor_usage_service"]["state"] == "configuration_missing"
    assert result["queryCounts"] == [3, 57]
    assert "secret.txt" not in json.dumps(result)


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("adaptive cadence", "adaptive cadence"),
    ("failure at /home/synthetic/file; contact person@example.invalid", "failure at [path omitted]; contact [redacted]"),
    ("prompt text is private", "The latest source attempt reported a problem; other sources continue independently."),
    ("Bearer synthetic-token", "[redacted]"),
    ("2 quarantined record(s)\nextra", "2 quarantined record(s) extra"),
])
def test_collection_text_has_independent_expected_redaction(raw, expected):
    with isolated():
        assert plans_value._collection_safe_text(raw) == expected


@pytest.mark.parametrize("value,state,age", [
    (None, "unknown", None), ("invalid", "unknown", None),
    ("2026-09-10T04:00:00Z", "unknown", None),
    ("2026-09-08T04:00:00Z", "stale", 86400),
])
def test_success_freshness_is_not_manufactured(value, state, age):
    with isolated():
        rows = plans_value_store.load_source_health(_HealthConnection([], [{"source": "codex_local", "last_success_at": value}]), AS_OF)
    codex = next(row for row in rows if row["source"] == "codex_local")
    assert codex["freshness"] == {"state": state, "ageSeconds": age, "asOf": "2026-09-09T04:00:00Z"}


def test_guards_fail_instead_of_returning_empty_evidence():
    with isolated():
        for operation in (lambda: sqlite3.connect(":memory:"), Path.home,
                          lambda: Path("synthetic").read_text(),
                          lambda: subprocess.Popen(["unused"]),
                          lambda: socket.socket().connect(("127.0.0.1", 1))):
            with pytest.raises(AssertionError, match="unexpected external access"):
                operation()


@pytest.mark.parametrize("case", [getattr(health_cases, name) for name in sorted(dir(health_cases)) if name.startswith("test_")], ids=lambda case: case.__name__)
def test_existing_loader_contracts_under_strict_boundaries(case):
    with isolated():
        case()


def test_pure_module_import_and_legacy_sanitizer_surface():
    from spend_app import source_evidence

    # Read only this tracked module; execute its fresh import body with external
    # access denied and imports restricted to the sole standard-library dependency.
    source = Path(source_evidence.__file__).read_text()
    real_import = builtins.__import__

    def only_re(name, *args, **kwargs):
        assert name == "re", f"unexpected pure-layer dependency: {name}"
        return real_import(name, *args, **kwargs)

    with isolated(), patch("builtins.__import__", only_re):
        namespace = {"__name__": "source_evidence_isolated"}
        exec(compile(source, "source_evidence.py", "exec"), namespace)
        assert namespace["sanitize_reason"]("Basic synthetic-token\nproblem") == "[redacted] problem"
        assert namespace["source_reason_text"]("at /tmp/synthetic/file") == "at [path omitted]"
    assert source_health.sanitize_reason is source_evidence.sanitize_reason


@pytest.mark.parametrize("raw,expected", [
    ("adaptive cadence", "Collection deferred to the established cadence."),
    ("2 quarantined record(s) with synthetic context", "2 quarantined record(s)"),
    ("pricing gap", "Some model pricing is unavailable; measured usage remains available."),
    ("permission denied", "Access to local usage metadata was denied."),
    ("redacted provider failure", "redacted provider failure"),
    ("raw response /tmp/synthetic/private", "The latest source attempt reported a problem; other sources continue independently."),
])
def test_loader_retains_its_reason_vocabulary(raw, expected):
    with isolated():
        assert plans_value_store._safe_health_reason(raw, "codex_local") == expected
