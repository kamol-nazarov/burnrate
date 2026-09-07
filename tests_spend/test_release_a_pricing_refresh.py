"""Release A pricing refresh contract tests (A08)."""

from __future__ import annotations
from pathlib import Path

from datetime import UTC, datetime
from decimal import Decimal

from spend_app.pricing_refresh import (
    draft_refresh,
    expiring_cards,
    load_card_expiries,
    parse_snapshot,
)

OBSERVED = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _snapshot(pricing_overrides: dict | None = None, **row_over):
    pricing = {
        "input_per_token": "0.000002",  # $2 / Mtok
        "output_per_token": "0.000008",
        **(pricing_overrides or {}),
    }
    row = {"id": "z-ai/glm-5.3-flash", "pricing": pricing, "created": 1785000000}
    row.update(row_over)
    return {"data": [row], "source_url": "https://openrouter.ai/api/v1/models"}


def test_per_token_strings_convert_to_decimal_per_million():
    rates, rejections = parse_snapshot(_snapshot(), provider="openrouter", observed_at=OBSERVED)
    assert rejections == []
    rate = rates[0]
    assert rate.per_mtok["input"] == Decimal("2")
    assert rate.per_mtok["output"] == Decimal("8")
    assert rate.per_mtok["cached_input"] is None  # missing stays missing
    # Model-created time is provenance, never an effective rate start.
    assert rate.effective_from is None
    assert rate.model_created_at is not None


def test_invalid_negative_and_documented_zero_are_distinguished():
    snapshot = _snapshot(
        {
            "input_per_token": "-0.5",  # negative
            "cached_input_per_token": "abc",  # invalid
            "cache_write_per_token": "0",  # documented zero
        }
    )
    rates, rejections = parse_snapshot(snapshot, provider="openrouter", observed_at=OBSERVED)
    rate = rates[0]
    assert rate.per_mtok["input"] is None
    assert rate.per_mtok["cached_input"] is None
    assert rate.per_mtok["cache_write"] == 0
    assert rate.documented_zero == ("cache_write",)
    assert len(rejections) == 2
    assert any("negative" in text for text in rejections)
    assert any("not a valid decimal" in text for text in rejections)


def test_diff_against_approved_cards_and_identical_rerun_is_empty():
    approved = {
        "z-ai/glm-5.3-flash": {
            "input": Decimal("2"),
            "output": Decimal("8"),
            "cached_input": None,
            "cache_write": None,
            "cache_write_1h": None,
        }
    }
    first = draft_refresh(snapshot=_snapshot(), provider="openrouter", approved_cards=approved, observed_at=OBSERVED)
    assert not first.has_changes
    assert first.unchanged == ["z-ai/glm-5.3-flash"]
    # A later observation of the SAME snapshot must not propose duplicates.
    second = draft_refresh(
        snapshot=_snapshot(),
        provider="openrouter",
        approved_cards=approved,
        observed_at=OBSERVED.replace(hour=13),
    )
    assert not second.has_changes
    assert first.snapshot_id == second.snapshot_id  # content-addressed
    # A changed input rate drafts a field-level change for review.
    changed = draft_refresh(
        snapshot=_snapshot({"input_per_token": "0.000003"}),
        provider="openrouter",
        approved_cards=approved,
        observed_at=OBSERVED,
    )
    assert changed.has_changes
    assert changed.changed[0]["fields"]["input"]["to"] == "3"


def test_absent_model_is_a_coverage_warning_not_a_deletion():
    approved = {
        "z-ai/glm-5.3-flash": {"input": None, "output": None, "cached_input": None, "cache_write": None, "cache_write_1h": None},
        "legacy/model": {"input": Decimal("1"), "output": Decimal("2"), "cached_input": None, "cache_write": None, "cache_write_1h": None},
    }
    draft = draft_refresh(snapshot=_snapshot(), provider="openrouter", approved_cards=approved, observed_at=OBSERVED)
    assert any("legacy/model" in warning for warning in draft.coverage_warnings)
    assert not any(change["modelKey"] == "legacy/model" for change in draft.changed)


def test_expiry_audit_lists_without_inventing_successors():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    cards = [
        {
            "modelKey": "openrouter:glm-5.3-flash",
            "effective_to": datetime(2026, 9, 9, 16, 0, tzinfo=UTC),
        },
        {"modelKey": "openai/gpt-6-astra", "effective_to": None},
    ]
    warnings = expiring_cards(cards, now=now)
    assert len(warnings) == 1
    assert warnings[0]["expiresAt"] == "2026-09-09T16:00:00Z"
    assert "stay priced" in warnings[0]["note"]
    assert "successor" in warnings[0]["note"]


def test_card_expiry_audit_reads_yaml(tmp_path: Path):
    (tmp_path / "openrouter.yaml").write_text(
        """
provider: openrouter
exactness: derived
prices:
  - model_key: openrouter:glm-5.3-flash
    input_per_mtok: 2
    cached_input_per_mtok: 0.2
    cache_write_per_mtok: 2
    output_per_mtok: 8
    effective_from: 2026-08-01T16:00:00Z
    effective_to: 2026-09-09T16:00:00Z
    source_url: https://openrouter.ai/z-ai/glm-5.3-flash
""",
        encoding="utf-8",
    )
    cards = load_card_expiries(tmp_path)
    assert len(cards) == 1
    assert cards[0]["effective_to"] == datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
