from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from spend_app import aggregate
from spend_app.db import connect
from spend_app.pricing import PricingEngine, UnpricedModelError
from tests_spend.test_aggregation import fixture_database

ROOT = Path(__file__).resolve().parents[1]


def test_astra_uses_official_rates_and_long_context_boundary() -> None:
    from spend_app.aggregate import COVERAGE_TARGETS, display_model

    engine = PricingEngine.load(ROOT / "pricing")
    effective = datetime(2026, 9, 4, 20, 42, 18, 748000, tzinfo=UTC)
    price = engine.resolve("gpt-6-astra", effective)
    assert price.input_per_mtok == Decimal("10")
    assert price.cached_input_per_mtok == Decimal("1")
    assert price.cache_write_per_mtok == Decimal("12.5")
    assert price.output_per_mtok == Decimal("50")
    assert price.long_context_threshold == 272_000
    assert price.long_context_inclusive is False
    assert price.long_input_multiplier == Decimal("2")
    assert price.long_output_multiplier == Decimal("1.5")
    assert price.source_url == "https://developers.openai.com/api/docs/models/gpt-6-astra"
    assert display_model("gpt-6-astra") == "GPT-6 Astra"
    assert ("codex", "gpt-6-astra", "GPT-6 Astra") in COVERAGE_TARGETS

    base = engine.components(
        model_key="gpt-6-astra",
        occurred_at=effective,
        input_tokens=100_000,
        cached_input_tokens=90_000,
        cache_write_tokens=10_000,
        output_tokens=10_000,
    )
    assert base == {
        "fresh_input": Decimal("0.10"),
        "cached_input": Decimal("0.09"),
        "cache_write": Decimal("0.125"),
        "output": Decimal("0.50"),
    }
    at_threshold = engine.compute(
        model_key="gpt-6-astra",
        occurred_at=effective,
        input_tokens=272_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=100_000,
    )
    above_threshold = engine.compute(
        model_key="gpt-6-astra",
        occurred_at=effective,
        input_tokens=272_001,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=100_000,
    )
    assert at_threshold == Decimal("7.72")
    assert above_threshold == Decimal("12.94002")
    with pytest.raises(UnpricedModelError):
        engine.resolve("gpt-6-astra", datetime(2026, 9, 4, 20, 42, 18, 747999, tzinfo=UTC))


def test_event_cache_preserves_values_and_invalidates_edits(tmp_path):
    database, pricing = fixture_database(tmp_path)
    with connect(database) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM usage_events")]
    expected = aggregate._enrich(events, pricing, {}, _cache=False)
    assert aggregate._enrich(events, pricing, {}) == expected
    changed = [dict(row) for row in events]
    changed[0]["cost_usd"] = 123.0
    assert aggregate._enrich(changed, pricing, {}) == aggregate._enrich(changed, pricing, {}, _cache=False)
    assert aggregate._enrich(events, pricing, {}) == expected
