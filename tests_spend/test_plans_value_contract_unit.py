"""Pure response fixture shared with the fake-DOM renderer tests."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app.plans_value import (
    assemble_group_valuation,
    assemble_plans_value_report,
    assemble_unassigned_summary,
)
from spend_app.plans_value_store import load_source_health
from tests_spend.test_plans_value_health_unit import _HealthConnection


def response_fixture():
    start = datetime(2026, 9, 1, 4, tzinfo=UTC)
    end = datetime(2026, 9, 9, 4, tzinfo=UTC)
    group = {
        "group_id": "tool:codex",
        "name": "Coding plan",
        "tool_keys": ["codex"],
        "plan_ids": [1],
        "plans": [{"id": 1, "name": "Coding plan", "accruedCostUsd": "80"}],
        "active_intervals": [(start, end)],
        "accrued_cost": "80",
    }
    event = {
        "source": "codex_local",
        "tool_key": "codex",
        "model_key": "example",
        "occurred_at": start,
        "input_tokens": 1000,
        "computed_cost_usd": "2400",
    }
    unknown = {**event, "model_key": "unpriced-model", "computed_cost_usd": None}
    charge = {**event, "charge_only": True, "computed_cost_usd": None, "cost_usd": "25"}
    connection = _HealthConnection(
        [{
            "source": "codex_local",
            "started_at": "2026-09-09T03:58:00Z",
            "finished_at": "2026-09-09T03:59:00Z",
            "status": "failed",
            "events_written": 0,
            "error": "redacted provider failure",
            "raw_response": "never expose this private raw response",
        }],
        successes=[{"source": "codex_local", "last_success_at": "2026-08-01T12:00:00Z"}],
    )
    health = load_source_health(connection, as_of=end)
    value = assemble_group_valuation(group, [event, charge], [unknown], health)
    report = assemble_plans_value_report(
        "this_month", end, ZoneInfo("America/New_York"), [value],
        assemble_unassigned_summary([], []), health, start, end,
    )
    report.pop("generatedAt")
    return report


def test_response_matches_fake_dom_fixture():
    fixture = Path(__file__).parent / "fixtures" / "plans_value_response.json"
    assert response_fixture() == json.loads(fixture.read_text(encoding="utf-8"))
