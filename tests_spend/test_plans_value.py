"""Unit tests for Plans & Value reference valuation (Cases 2, 7, 8, 9, 10)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spend_app.plans_value import (
    API_ONLY_SOURCES,
    assemble_group_valuation,
    assemble_plans_value_report,
    assemble_unassigned_summary,
    measured_tokens,
    resolve_event_reference_usd,
)
from spend_app.pricing import PricingEngine
from spend_app.plans_value_store import load_source_health
from tests_spend.test_plans_value_health_unit import AS_OF, _HealthConnection

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def _group(**overrides):
    base = {
        "group_id": "tool:codex",
        "name": "ChatGPT Pro",
        "tool_keys": ("codex",),
        "plan_ids": ("plan-codex",),
        "plans": [
            {
                "plan_id": "plan-codex",
                "name": "ChatGPT Pro",
                "tool_key": "codex",
                "accrued_cost": Decimal("80"),
                "amount_usd": Decimal("300"),
                "cadence": "monthly",
            }
        ],
        "active_intervals": [
            (
                datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
                datetime(2026, 9, 9, 4, 0, tzinfo=UTC),
            )
        ],
        "accrued_cost": Decimal("80"),
    }
    base.update(overrides)
    return base


def _priced_event(**overrides):
    base = {
        "source": "codex_local",
        "tool_key": "codex",
        "model_key": "gpt-5",
        "occurred_at": "2026-09-05T12:00:00Z",
        "input_tokens": 1_000_000,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 0,
        "computed_cost_usd": "2400",
        "cost_usd": None,
        "telemetry_complete": True,
        "raw_id": "priced-1",
    }
    base.update(overrides)
    return base


def _unpriced_event(**overrides):
    base = {
        "source": "codex_local",
        "tool_key": "codex",
        "model_key": "mystery/model",
        "occurred_at": "2026-09-06T12:00:00Z",
        "input_tokens": 1000,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 0,
        "unclassified_tokens": 0,
        "telemetry_complete": True,
        "cost_usd": None,
        "raw_id": "unpriced-1",
    }
    base.update(overrides)
    return base


def _health(*rows):
    latest = [
        {
            "source": row["source"],
            "status": row["status"],
            "started_at": row.get("finished_at", "2026-09-09T03:59:00Z"),
            "finished_at": row.get("finished_at", "2026-09-09T03:59:00Z"),
            "events_written": 1,
            "error": row.get("note"),
        }
        for row in rows
    ]
    successes = [
        {"source": row["source"], "last_success_at": row["finished_at"]}
        for row in latest if row["status"] in {"success", "partial"}
    ]
    return load_source_health(_HealthConnection(latest, successes), as_of=AS_OF)


# ---------------------------------------------------------------------------
# Case 1 shape (baseline for Case 2): $80 cost, $2400 value, 30x
# ---------------------------------------------------------------------------


def test_fully_priced_group_ratio_case1_shape():
    row = assemble_group_valuation(
        _group(),
        [_priced_event()],
        [],
        _health({"source": "codex_local", "status": "success"}),
    )
    assert row["configuredCostUsd"] == "80"
    assert row["usageValueUsd"] == "2400"
    assert row["usageValueStatus"] == "complete"
    assert row["multiple"] == pytest.approx(30.0)
    assert row["multipleBasis"] == "ratio"
    assert row["multipleReasonCode"] is None
    assert "Reference-value multiple" in row["explanation"]["multiple"]["label"]
    assert "not ROI" in row["explanation"]["multiple"]["note"]


# ---------------------------------------------------------------------------
# Case 2: unpriced tokens keep known subtotal + lower-bound; all-unpriced ≠ $0
# ---------------------------------------------------------------------------


def test_case2_partial_keeps_known_subtotal_and_lower_bound_multiple():
    row = assemble_group_valuation(
        _group(),
        [_priced_event()],
        [_unpriced_event(input_tokens=1000)],
        _health({"source": "codex_local", "status": "success"}),
    )
    assert row["usageValueUsd"] == "2400"
    assert row["usageValueStatus"] == "lower_bound"
    assert row["usageValueBasis"] == "known-subtotal"
    assert row["multiple"] == pytest.approx(30.0)
    assert row["multipleBasis"] == "lower_bound"
    assert row["pricingCoverage"]["status"] == "partial"
    assert row["pricingCoverage"]["unpricedTokens"] == 1000
    assert row["pricingCoverage"]["unpricedModels"][0]["modelKey"] == "mystery/model"
    assert row["usageValueLabel"].startswith("≥")


def test_case2_all_unpriced_is_unavailable_not_zero():
    row = assemble_group_valuation(
        _group(accrued_cost=Decimal("80")),
        [],
        [_unpriced_event(input_tokens=1000)],
        [],
    )
    assert row["usageValueUsd"] is None
    assert row["usageValueStatus"] == "unavailable"
    assert row["multiple"] is None
    assert row["multipleBasis"] == "unavailable"
    assert row["multipleReasonCode"] == "no_priced_value"
    assert row["tokens"] == 1000
    assert row["pricingCoverage"]["unpricedModels"]


# ---------------------------------------------------------------------------
# Case 7: zero expense vs measured zero vs no records
# ---------------------------------------------------------------------------


def test_case7_zero_expense_no_invalid_ratio():
    row = assemble_group_valuation(
        _group(accrued_cost=Decimal("0"), plans=[{"plan_id": "free", "name": "Free", "accrued_cost": 0}]),
        [_priced_event(computed_cost_usd="12")],
        [],
        [],
    )
    assert row["configuredCostUsd"] == "0"
    assert row["usageValueUsd"] == "12"
    assert row["multiple"] is None
    assert row["multipleBasis"] == "unavailable"
    assert row["multipleReasonCode"] == "zero_cost"
    assert row["multiple"] != float("inf")


def test_case7_measured_zero_distinct_from_no_records():
    zero = assemble_group_valuation(
        _group(),
        [_priced_event(computed_cost_usd="0", input_tokens=0, output_tokens=0)],
        [],
        [],
    )
    empty = assemble_group_valuation(_group(), [], [], [])

    assert zero["usageValueUsd"] == "0"
    assert zero["usageValueStatus"] == "measured_zero"
    assert zero["multiple"] == 0.0
    assert zero["multipleBasis"] == "ratio"

    assert empty["usageValueUsd"] is None
    assert empty["usageValueStatus"] == "no_records"
    assert empty["multiple"] is None
    assert empty["multipleReasonCode"] == "no_records"
    assert empty["usageValueLabel"] == "No recorded usage"
    assert zero["usageValueStatus"] != empty["usageValueStatus"]


# ---------------------------------------------------------------------------
# Case 8: API-only / charge-only / conflicting scope stay out of numerator
# ---------------------------------------------------------------------------


def test_case8_api_only_sources_excluded_from_reference_value():
    for source in sorted(API_ONLY_SOURCES):
        row = assemble_group_valuation(
            _group(),
            [
                _priced_event(
                    source=source,
                    computed_cost_usd="500",
                    cost_usd="500",
                    raw_id=f"{source}-1",
                )
            ],
            [],
            [],
        )
        assert row["usageValueUsd"] is None
        assert row["usageValueStatus"] in {"unavailable", "no_records"} or row["events"] >= 1
        assert row["pricingCoverage"]["excludedApiOnlyEvents"] == 1
        assert row["reportedChargesUsd"] == "500"
        # Tool name match must not launder API-only into reference value.
        assert row["usageValueUsd"] != "500"
        assert any(
            note["reasonCode"] == "api_only_source"
            for note in row["explanation"]["referenceValue"]["excluded"]
        )


def test_case8_charge_only_and_scope_conflict_excluded():
    row = assemble_group_valuation(
        _group(),
        [
            {
                "source": "codex_local",
                "tool_key": "codex",
                "model_key": "invoice-line",
                "occurred_at": "2026-09-05T12:00:00Z",
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "cost_usd": "42.00",
                "computed_cost_usd": None,
                "raw_id": "charge-only",
            },
            _priced_event(
                computed_cost_usd="10",
                scope_conflict=True,
                raw_id="conflict-1",
                input_tokens=100,
            ),
            _priced_event(computed_cost_usd="7", raw_id="ok-1", input_tokens=50),
        ],
        [],
        [],
    )
    assert row["usageValueUsd"] == "7"
    assert row["pricingCoverage"]["excludedChargeOnlyEvents"] == 1
    assert row["pricingCoverage"]["excludedScopeEvents"] == 1
    assert row["attribution"]["basis"] == "configured_tool_association"
    assert "not account" in row["attribution"]["note"].lower() or "not proof" in row["attribution"]["note"].lower()


def test_case8_observed_charge_never_fallback_reference_rate():
    amount = resolve_event_reference_usd(
        {
            "source": "codex_local",
            "tool_key": "codex",
            "model_key": "gpt-5",
            "occurred_at": "2026-09-05T12:00:00Z",
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "cost_usd": "99.99",
            # No computed reference — must not fall back to cost_usd.
        }
    )
    assert amount is None


# ---------------------------------------------------------------------------
# Case 9: priced rows + stale collection ≠ complete coverage; unknowns visible
# ---------------------------------------------------------------------------


def test_case9_stale_collection_does_not_claim_complete_coverage():
    row = assemble_group_valuation(
        _group(),
        [
            _priced_event(raw_id="a"),
            _priced_event(raw_id="b", model_key="gpt-5-codex", computed_cost_usd="100"),
        ],
        [_unpriced_event(model_key="unknown/model-z", input_tokens=50)],
        _health(
            {
                "source": "codex_local",
                "tool_key": "codex",
                "status": "success",
                "finished_at": "2026-09-07T04:00:00Z",
                "coverage": "partial",
                "note": "last success 2d ago",
            }
        ),
    )
    assert row["usageValueUsd"] == "2500"
    assert row["usageValueStatus"] == "lower_bound"
    assert row["pricingCoverage"]["status"] == "partial"
    assert row["collectionEvidence"]["status"] == "stale"
    assert row["collectionEvidence"]["complete"] is False
    models = {m["modelKey"] for m in row["pricingCoverage"]["unpricedModels"]}
    assert "unknown/model-z" in models
    # Unknown models must not hide other priced rows.
    assert row["pricingCoverage"]["pricedEvents"] == 2
    assert "recorded" in row["pricingCoverage"]["note"].lower()


def test_case9_healthy_poll_still_not_historical_complete():
    row = assemble_group_valuation(
        _group(),
        [_priced_event()],
        [],
        _health({"source": "codex_local", "status": "success"}),
    )
    assert row["pricingCoverage"]["status"] == "complete"
    assert row["collectionEvidence"]["status"] == "healthy"
    assert row["collectionEvidence"]["complete"] is False
    assert "historical" in row["explanation"]["limits"]["pricingVsCollection"].lower()


# ---------------------------------------------------------------------------
# Case 10: event-time pricing; charges not fallback; token conventions
# ---------------------------------------------------------------------------


def test_case10_uses_computed_cost_not_observed_charge():
    row = assemble_group_valuation(
        _group(accrued_cost=Decimal("10")),
        [
            _priced_event(
                computed_cost_usd="2.40",
                cost_usd="999.00",
                input_tokens=1_000_000,
            )
        ],
        [],
        [],
    )
    assert row["usageValueUsd"] == "2.4"
    assert row["multiple"] == pytest.approx(0.24)
    assert row["reportedChargesUsd"] is None


def test_case10_pricing_engine_respects_event_time_boundaries():
    engine = PricingEngine.load(ROOT / "pricing")
    # Pick a model that exists in packaged pricing.
    sample = None
    for price in engine.prices:
        if price.model_key:
            sample = price
            break
    assert sample is not None

    when = sample.effective_from
    event = {
        "source": "codex_local",
        "tool_key": "codex",
        "model_key": sample.model_key,
        "occurred_at": when,
        "input_tokens": 1_000_000,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 0,
        "cost_usd": "12345",  # must be ignored
    }
    priced = resolve_event_reference_usd(event, pricing=engine)
    assert priced is not None
    assert priced == engine.compute(
        model_key=sample.model_key,
        occurred_at=when,
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
    )
    assert priced != Decimal("12345")

    # Before any card is effective → unpriced, not charge fallback.
    early = dict(event)
    early["occurred_at"] = datetime(2000, 1, 1, tzinfo=UTC)
    early["model_key"] = "definitely-not-a-real-model-xyz"
    assert resolve_event_reference_usd(early, pricing=engine) is None


def test_case10_token_convention_matches_measured_components():
    event = _priced_event(
        input_tokens=1000,
        cached_input_tokens=400,
        cache_write_tokens=50,
        output_tokens=20,
    )
    # measured = cached + fresh + cache_write + output = 400 + 600 + 50 + 20
    assert measured_tokens(event) == 1070
    row = assemble_group_valuation(_group(), [event], [], [])
    assert row["tokens"] == 1070
    assert row["pricedTokens"] == 1070


# ---------------------------------------------------------------------------
# Unassigned + report envelope
# ---------------------------------------------------------------------------


def test_unassigned_summary_has_no_invented_cost_or_multiple():
    summary = assemble_unassigned_summary(
        [_priced_event(tool_key="cursor", computed_cost_usd="15", raw_id="u1")],
        [_unpriced_event(tool_key="grok", input_tokens=200, raw_id="u2")],
    )
    assert summary["usageValueUsd"] == "15"
    assert summary["usageValueStatus"] == "lower_bound"
    assert summary["tokens"] == 1_000_000 + 200
    tools = {row["toolKey"] for row in summary["toolBreakdown"]}
    assert tools == {"cursor", "grok"}
    assert "multiple" not in summary
    assert "configuredCostUsd" not in summary
    assert "no configured plan" in summary["explanation"]["title"].lower() or "Unassigned" in summary["explanation"]["title"]


def test_assemble_plans_value_report_envelope():
    group_row = assemble_group_valuation(_group(), [_priced_event()], [], [])
    unassigned = assemble_unassigned_summary([], [])
    as_of = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
    report = assemble_plans_value_report(
        "this_month",
        as_of,
        NY,
        [group_row],
        unassigned,
        _health({"source": "codex_local", "status": "success"}),
        datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
        as_of,
    )
    assert report["schemaVersion"] == 1
    assert report["period"] == "this_month"
    assert report["timezone"] == "America/New_York"
    assert report["fromUtc"].startswith("2026-09-01")
    assert report["localFrom"] == "2026-09-01"
    assert len(report["groups"]) == 1
    assert report["groups"][0]["multiple"] == pytest.approx(30.0)
    assert report["unassigned"]["usageValueStatus"] == "no_records"
    assert report["labels"]["multiple"] == "Reference-value multiple"


def test_explanation_contains_required_sections():
    row = assemble_group_valuation(
        _group(),
        [_priced_event()],
        [_unpriced_event()],
        _health({"source": "codex_local", "status": "partial"}),
    )
    explanation = row["explanation"]
    assert explanation["period"]["activeIntervals"]
    assert explanation["configuredCost"]["formula"]
    assert explanation["tools"]["toolKeys"] == ["codex"]
    assert explanation["referenceValue"]["unpricedModels"]
    assert explanation["collection"]["status"] == "partial"
    assert explanation["multiple"]["basis"] == "lower_bound"
    assert explanation["limits"]["pricingVsCollection"]


def test_group_dataclass_like_object_accepted():
    class PlanGroup:
        def __init__(self):
            self.group_id = "tool:claude-code"
            self.name = "Claude"
            self.tool_keys = ("claude-code",)
            self.plan_ids = ("p1",)
            self.plans = []
            self.active_intervals = []
            self.accrued_cost = Decimal("40")

    row = assemble_group_valuation(
        PlanGroup(),
        [_priced_event(tool_key="claude-code", source="claude_local", computed_cost_usd="90")],
        [],
        [],
    )
    assert row["groupId"] == "tool:claude-code"
    assert row["multiple"] == pytest.approx(2.25)
