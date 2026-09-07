"""Release A acceptance fixtures: value/coverage and plan math (A09/A10).

The golden fixture from the task cards: one priced event with reference value
$10 over 1,000,000 priced tokens; one unpriced event with 500,000 measured
tokens; configured cost $2. Expected: reference value $10 with partial
coverage, 500,000 unpriced tokens, configured cost $2 reported separately;
never $12 as usage value; the priced-subset average is $10/Mtok with its
denominator and coverage stated.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from spend_app.plan_math import Expense, PlanTerms, accrue, compare, forecast, underuse_verdict
from spend_app.timeutil import local_day
from spend_app.value_summary import ValueSummary, ratio_with_coverage

UTC = timezone.utc


def test_golden_mixed_pricing_fixture():
    summary = ValueSummary()
    summary.add_priced(Decimal("10"), 1_000_000)
    summary.add_unpriced(500_000, model="mystery/model", complete=True)
    summary.set_configured_cost(Decimal("2"))
    summary.measured_tokens = 1_500_000
    # Reference value is $10, never $12.
    assert summary.usage_value == Decimal("10")
    assert summary.usage_value != Decimal("10") + Decimal("2")
    assert summary.configured_cost == Decimal("2")
    coverage = summary.coverage()
    assert coverage["usageValueKnown"] is True
    assert coverage["usageValueComplete"] is False
    assert coverage["usageValueBasis"] == "known-subtotal"
    assert coverage["unpricedTokens"] == 500_000
    assert coverage["unpricedModels"] == ["mystery/model"]
    # Subset average: $10 per million over the priced denominator only.
    assert summary.priced_subset_average_per_million == Decimal("10")
    assert coverage["pricedSubsetAvgPerMTok"] == 10.0
    # Lower bound only against the complete positive configured cost.
    verdict = ratio_with_coverage(summary, Decimal("2"))
    assert verdict["basis"] == "lower_bound"
    assert verdict["value"] == 5.0
    # Incomplete cost denominator -> unavailable, not a lower bound.
    incomplete = ratio_with_coverage(summary, Decimal("2"), denominator_complete=False)
    assert incomplete["basis"] == "unavailable"


def test_all_unpriced_shows_unavailable_not_zero():
    summary = ValueSummary()
    summary.add_unpriced(1000, model="x")
    assert summary.usage_value is None
    assert summary.coverage()["usageValueKnown"] is False


def test_exact_zero_is_preserved_against_absent():
    summary = ValueSummary()
    summary.add_priced(Decimal(0), 0)  # documented zero price
    assert summary.usage_value == Decimal(0)
    assert summary.priced_subset_average_per_million is None  # zero tokens priced


def test_provider_charges_stay_separate_from_reference_value():
    summary = ValueSummary()
    summary.add_priced(Decimal("3"))
    summary.add_reported_charge(Decimal("7.50"))
    assert summary.usage_value == Decimal("3")
    assert summary.reported_charges == Decimal("7.50")


def test_negative_contributions_rejected():
    summary = ValueSummary()
    for call in (
        lambda: summary.add_priced(Decimal(-1)),
        lambda: summary.add_reported_charge(Decimal(-5)),
        lambda: summary.set_configured_cost(Decimal(-2)),
    ):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("negative monetary input must be rejected")


def test_no_events_ratio_unavailable():
    summary = ValueSummary()
    summary.set_configured_cost(Decimal("5"))
    verdict = ratio_with_coverage(summary, Decimal("5"))
    assert verdict["basis"] == "unavailable"


# ---------------------------------------------------------------------------
# A10 plan comparison mathematics.
# ---------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")
SEP_1_2026 = datetime(2026, 9, 1, 4, tzinfo=UTC)  # local midnight in New York


def test_hypothetical_100dollar_september_plan_three_day_ratio():
    """Task-card fixture: $100 September plan; after three complete days,
    $30 of reference usage corresponds to ~$10 accrued and a 3x same-period
    ratio — and is NOT underuse because $30 < $100."""
    plan = PlanTerms("codex", "ChatGPT Pro", Decimal("100"), "monthly", date(2026, 9, 1))
    window = (SEP_1_2026, SEP_1_2026 + timedelta(days=3))
    comparison = compare(
        plans=[plan],
        window=window,
        zone=NY,
        usage_value=Decimal("30"),
        usage_complete=True,
        tool_key="codex",
        coverage_ratio=1.0,
    )
    assert Decimal("9.90") < comparison.accrued_cost <= Decimal("10.00")
    assert comparison.ratio["basis"] == "ratio"
    assert 2.9 < comparison.ratio["value"] < 3.1
    # Full month cost is $100; the comparison must NOT have used it.
    assert comparison.cost_total < Decimal("11")
    verdict = underuse_verdict(comparison)
    assert verdict["verdict"] == "none"


def test_thirty_one_day_month_and_leap_year_accrual_sums_to_price():
    plan = PlanTerms("codex", "Plan", Decimal("100"), "monthly", date(2026, 1, 1), end_date=date(2026, 12, 31))
    leap_plan = PlanTerms("codex", "Plan", Decimal("100"), "monthly", date(2024, 1, 1), end_date=date(2024, 12, 31))
    january_days = [local_day(date(2026, 1, d), NY) for d in range(1, 32)]
    february_days = [local_day(date(2024, 2, d), NY) for d in range(1, 30)]
    january = accrue([plan], january_days)
    february = accrue([leap_plan], february_days)
    # Daily proration sums to the configured price within decimal precision.
    assert abs(january - Decimal("100")) < Decimal("0.000001")
    assert abs(february - Decimal("100")) < Decimal("0.000001")


def test_future_plan_and_end_date_are_inert():
    plan = PlanTerms("codex", "Future", Decimal("100"), "monthly", date(2026, 10, 1))
    plan_ended = PlanTerms("grok", "Retired", Decimal("80"), "monthly", date(2026, 1, 1), end_date=date(2026, 8, 31))
    window = (SEP_1_2026, SEP_1_2026 + timedelta(days=5))
    comparison = compare(
        plans=[plan, plan_ended],
        window=window,
        zone=NY,
        usage_value=Decimal("10"),
        usage_complete=True,
    )
    assert comparison.accrued_cost == 0
    assert comparison.ratio["basis"] == "unavailable"
    assert comparison.plan_names == []


def test_zero_cost_plan_never_yields_a_ratio_or_verdict():
    plan = PlanTerms("opencode", "Free tier", Decimal("0"), "monthly", date(2026, 9, 1))
    window = (SEP_1_2026, SEP_1_2026 + timedelta(days=3))
    comparison = compare(
        plans=[plan], window=window, zone=NY, usage_value=Decimal("12"), usage_complete=True, tool_key="opencode"
    )
    assert comparison.ratio["basis"] == "unavailable"
    assert underuse_verdict(comparison)["verdict"] == "unavailable"


def test_extras_count_and_caps_do_not():
    plan = PlanTerms("cursor", "Ultra", Decimal("200"), "monthly", date(2026, 9, 1))
    window = (SEP_1_2026, SEP_1_2026 + timedelta(days=3))
    comparison = compare(
        plans=[plan],
        window=window,
        zone=NY,
        usage_value=Decimal("50"),
        usage_complete=True,
        expenses=[Expense("cursor", Decimal("12.50"), note="verified overage")],
        tool_key="cursor",
    )
    # Verified extras are typed input; a cap is simply not an expense.
    assert comparison.extra_expenses == Decimal("12.50")
    assert comparison.cost_total == comparison.accrued_cost + Decimal("12.50")
    assert "reported extras" in comparison.cost_scope


def test_partial_coverage_blocks_the_verdict():
    plan = PlanTerms("codex", "Pro", Decimal("100"), "monthly", date(2026, 9, 1))
    window = (SEP_1_2026, SEP_1_2026 + timedelta(days=10))
    comparison = compare(
        plans=[plan],
        window=window,
        zone=NY,
        usage_value=Decimal("2"),
        usage_complete=False,
        tool_key="codex",
        coverage_ratio=0.4,
    )
    assert underuse_verdict(comparison)["verdict"] == "unavailable"


def test_forecast_uses_observed_pace_and_states_assumptions():
    plan = PlanTerms("codex", "Pro", Decimal("100"), "monthly", date(2026, 9, 1))
    month_days = 30 * 24 * 3600
    result = forecast(
        plans=[plan],
        month_local_date=date(2026, 9, 6),
        zone=NY,
        mtd_usage_value=Decimal("45"),
        mtd_usage_complete=True,
        elapsed_seconds=5 * 24 * 3600,
    )
    assert result["available"] is True
    # 45 observed over 5 days extrapolates to ~270 for the month.
    assert 260 < result["value"] < 285
    assert result["planCost"] == 100.0
    assert "not a bill" in result["assumptions"]


def test_forecast_refuses_insufficient_samples():
    plan = PlanTerms("codex", "Pro", Decimal("100"), "monthly", date(2026, 9, 1))
    result = forecast(
        plans=[plan],
        month_local_date=date(2026, 9, 2),
        zone=NY,
        mtd_usage_value=Decimal("1"),
        mtd_usage_complete=True,
        elapsed_seconds=2 * 3600,
    )
    assert result["available"] is False
