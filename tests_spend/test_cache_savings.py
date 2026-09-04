from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from spend_app.aggregate import _cache_savings, aggregate_summary
from spend_app.db import UnpricedUsageEvent, connect, initialize, upsert_unpriced_event
from spend_app.pricing import PricingEngine
from tests_spend.test_aggregation import TZ, add_event


ROOT = Path(__file__).resolve().parents[1]


def _cached_event(**overrides) -> dict:
    event = {
        "model_key": "cursor:gemini-3.8-flash-high",
        "occurred_at": "2026-09-02T19:38:00Z",
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 10,
        "telemetry_complete": True,
    }
    event.update(overrides)
    return event


def test_cache_savings_is_the_exact_no_cache_counterfactual() -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    saved = _cache_savings(events=[_cached_event()], pricing=pricing)
    expected = Decimal(80) * (Decimal("0.75") - Decimal("0.075")) / Decimal(1_000_000)
    assert Decimal(str(saved)) == expected


def test_cache_savings_is_unavailable_when_cached_rows_are_unpriced() -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    event = _cached_event(
        model_key="missing:model",
        occurred_at=datetime(2026, 9, 2, tzinfo=UTC).isoformat(),
        output_tokens=0,
    )
    assert _cache_savings(events=[event], pricing=pricing) is None


def test_cache_savings_is_unavailable_when_any_cached_row_is_unpriced() -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    priced = _cached_event()
    unpriced = _cached_event(model_key="missing:model", output_tokens=0)
    assert _cache_savings(events=[priced, unpriced], pricing=pricing) is None


def test_cache_savings_is_unavailable_when_any_cached_row_has_incomplete_telemetry() -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    complete = _cached_event()
    incomplete = _cached_event(raw_id="incomplete", telemetry_complete=False)
    assert _cache_savings(events=[complete, incomplete], pricing=pricing) is None


def test_summary_cache_savings_is_none_when_any_cached_row_is_unpriced(tmp_path: Path) -> None:
    database = tmp_path / "mixed-cache.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    occurred = datetime(2026, 8, 30, 18, tzinfo=UTC)
    add_event(
        database,
        pricing,
        raw_id="priced-cached",
        tool="cursor",
        model="cursor:grok-4.6",
        session="cursor-1",
        occurred=occurred,
        input_tokens=200_000,
        cached=40_000,
        writes=0,
        output=10_000,
    )
    with connect(database) as connection:
        upsert_unpriced_event(
            connection,
            UnpricedUsageEvent(
                source="opencode_local",
                tool_key="opencode",
                model_key="opencode:unlisted-model",
                occurred_at="2026-08-30T16:00:00Z",
                session_id="opencode-1",
                project="fixture-project",
                input_tokens=1_000,
                cached_input_tokens=900,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=50,
                reasoning_tokens=0,
                unclassified_tokens=0,
                telemetry_complete=True,
                cost_usd=None,
                raw_id="opencode-unlisted",
                ingested_at="2026-08-30T23:00:00Z",
            ),
        )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 23, tzinfo=UTC),
    )
    assert summary["cacheSavings"] is None


def test_summary_cache_savings_is_complete_when_every_cached_row_is_priced(tmp_path: Path) -> None:
    database = tmp_path / "complete-cache.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    add_event(
        database,
        pricing,
        raw_id="priced-cached",
        tool="cursor",
        model="cursor:grok-4.6",
        session="cursor-1",
        occurred=datetime(2026, 8, 30, 18, tzinfo=UTC),
        input_tokens=200_000,
        cached=40_000,
        writes=0,
        output=10_000,
    )
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        now=datetime(2026, 8, 30, 23, tzinfo=UTC),
    )
    expected = Decimal(40_000) * (Decimal("2.00") - Decimal("0.50")) / Decimal(1_000_000)
    assert summary["cacheSavings"] is not None
    assert Decimal(str(summary["cacheSavings"])) == expected
