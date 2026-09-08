from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from spend_app import aggregate


def test_heatmap_excludes_unknown_values_without_losing_known_spend(monkeypatch):
    when = datetime(2026, 9, 7, 12, tzinfo=UTC)
    enriched = [
        {"when": when, "spend": Decimal("2.50")},
        {"when": when, "spend": None},
        {"when": when.replace(hour=13), "spend": None},
        {"when": when.replace(hour=14), "spend": Decimal("0")},
    ]
    monkeypatch.setattr(aggregate, "_load_events", lambda *args: [])
    monkeypatch.setattr(aggregate, "_load_unpriced_events", lambda *args: [])
    monkeypatch.setattr(aggregate, "_cost_buckets", lambda *args: {})
    monkeypatch.setattr(aggregate, "_enrich", lambda *args: enriched)
    monkeypatch.setattr(aggregate, "_connection_identity", lambda *args: "unit")
    monkeypatch.setattr(aggregate, "_range_fingerprint", lambda *args: "unit")
    monkeypatch.setattr(aggregate, "_memo", lambda name, key, compute: compute())

    cells = aggregate._heatmap_cells(
        object(), object(), when, when.replace(hour=15), "all", ZoneInfo("UTC")
    )
    assert cells == {(0, 12): Decimal("2.50"), (0, 14): Decimal("0")}
