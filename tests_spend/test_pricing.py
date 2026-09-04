import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.reference_rates import compute as compute_reference_cost


ROOT = Path(__file__).resolve().parents[1]
MILLION = Decimal("1000000")

_OFFICIAL_SOURCE_HOSTS = (
    "https://developers.openai.com/",
    "https://platform.claude.com/",
    "https://cursor.com/",
    "https://docs.x.ai/",
    "https://docs.z.ai/",
    "https://openrouter.ai/",
    "https://ai.google.dev/",
)


def test_openai_sol_pricing_math_matches_spec_formula() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    cost = engine.compute(
        model_key="gpt-5.6-sol",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        input_tokens=1_000,
        cached_input_tokens=400,
        cache_write_tokens=100,
        output_tokens=200,
    )
    assert cost == Decimal("0.007060")


def test_alias_resolves_to_effective_price() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    resolved = engine.resolve("claude-opus-5[1m]", datetime(2026, 8, 30, 12, tzinfo=UTC))
    assert resolved.model_key == "claude-opus-5"
    assert resolved.cached_input_per_mtok == Decimal("0.5")


def test_unpriced_model_fails_loudly() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    with pytest.raises(UnpricedModelError):
        engine.compute(
            model_key="unknown-model",
            occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
        )


def test_price_does_not_apply_before_effective_date() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    with pytest.raises(UnpricedModelError):
        engine.resolve("gpt-5.6-sol", datetime(2026, 8, 29, 23, 59, tzinfo=UTC))


def test_rejects_cached_input_above_total_input() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    with pytest.raises(ValueError):
        engine.compute(
            model_key="gpt-5.6-sol",
            occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
            input_tokens=100,
            cached_input_tokens=101,
            cache_write_tokens=0,
            output_tokens=0,
        )


def test_long_context_multiplier_uses_published_openai_rule() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    cost = engine.compute(
        model_key="gpt-5.6-sol",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        input_tokens=300_000,
        cached_input_tokens=200_000,
        cache_write_tokens=0,
        output_tokens=10_000,
    )
    # Input classes are 2x; output is 1.5x above the 272K threshold.
    assert cost == Decimal("1.260")


def test_anthropic_one_hour_cache_write_uses_distinct_rate() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    cost = engine.compute(
        model_key="claude-opus-5",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=100,
        cache_write_1h_tokens=100,
        output_tokens=0,
    )
    assert cost == Decimal("0.001")


def test_sonnet_5_standard_price_is_the_official_intro_rate() -> None:
    # Primary fact from the official Anthropic pricing page: the $2/$10
    # introductory price became the standard price and the scheduled
    # 2026-09-01 increase to $3/$15 will not occur.
    engine = PricingEngine.load(ROOT / "pricing")
    kwargs = dict(
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    for occurred in (
        datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC),
    ):
        assert engine.compute(model_key="claude-sonnet-5", occurred_at=occurred, **kwargs) == (
            Decimal("2") + Decimal("10")
        )
        resolved = engine.resolve("claude-sonnet-5", occurred)
        assert resolved.input_per_mtok == Decimal("2")
        assert resolved.output_per_mtok == Decimal("10")
        assert resolved.cached_input_per_mtok == Decimal("0.2")
        assert resolved.cache_write_per_mtok == Decimal("2.5")
        assert resolved.cache_write_1h_per_mtok == Decimal("4")
        assert resolved.effective_to is None


def test_xai_long_context_tier_is_inclusive_at_200k() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    short = engine.compute(
        model_key="supergrok:grok-4.6",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        input_tokens=199_999,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000,
    )
    assert short == Decimal("199999") / MILLION * Decimal("2") + Decimal("1000") / MILLION * Decimal("6")
    at_threshold = engine.compute(
        model_key="xai:grok-4.6",
        occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        input_tokens=200_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000,
    )
    # "Reaches the listed token threshold" is billed at the higher rate: 4/12.
    assert at_threshold == Decimal("200000") / MILLION * Decimal("4") + Decimal("1000") / MILLION * Decimal("12")
    price = engine.resolve("xai:grok-4.6", datetime(2026, 8, 30, 12, tzinfo=UTC))
    assert price.long_context_threshold == 200_000
    assert price.long_context_inclusive is True


def test_glm_flash_promo_and_list_rates_are_effective_dated() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    kwargs = dict(
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    promo_kwargs = dict(kwargs, occurred_at=datetime(2026, 8, 30, 12, tzinfo=UTC))
    # Official Z.AI promo card (and matching OpenRouter metadata): $0.075 in /
    # $0.25 out per MTok -> $0.325 for 1M in + 1M out.
    assert engine.compute(model_key="opencode:glm-5.3-flash", **promo_kwargs) == Decimal("0.325")
    assert engine.compute(model_key="openrouter:glm-5.3-flash", **promo_kwargs) == Decimal("0.325")

    last_promo = dict(kwargs, occurred_at=datetime(2026, 9, 9, 15, 59, 59, tzinfo=UTC))
    assert engine.compute(model_key="opencode:glm-5.3-flash", **last_promo) == Decimal("0.325")
    assert engine.compute(model_key="openrouter:glm-5.3-flash", **last_promo) == Decimal("0.325")

    boundary = datetime(2026, 9, 9, 16, 0, 0, tzinfo=UTC)
    list_kwargs = dict(kwargs, occurred_at=boundary)
    # Z.AI list rates apply from the promo boundary: $0.15 in / $0.50 out.
    assert engine.compute(model_key="opencode:glm-5.3-flash", **list_kwargs) == Decimal("0.65")
    resolved = engine.resolve("opencode:glm-5.3-flash", datetime(2026, 9, 10, tzinfo=UTC))
    assert resolved.cached_input_per_mtok == Decimal("0.03")
    assert resolved.effective_from == datetime(2026, 9, 9, 16, 0, 0, tzinfo=UTC)
    # The OpenRouter promo row must not silently apply past its recorded
    # boundary: unpriced until refreshed official metadata is recorded.
    with pytest.raises(UnpricedModelError):
        engine.resolve("openrouter:glm-5.3-flash", boundary)

    # Before the documented floor (official OpenRouter creation timestamp) the
    # model is unpriced rather than back-dated.
    before_floor = dict(kwargs, occurred_at=datetime(2026, 8, 26, 13, 59, 0, tzinfo=UTC))
    with pytest.raises(UnpricedModelError):
        engine.resolve("opencode:glm-5.3-flash", before_floor["occurred_at"])
    with pytest.raises(UnpricedModelError):
        engine.resolve("openrouter:glm-5.3-flash", before_floor["occurred_at"])


def test_openrouter_yaml_row_matches_recorded_official_fixture() -> None:
    fixture = json.loads(
        (ROOT / "tests_spend" / "fixtures" / "openrouter_models_glm53.json").read_text(
            encoding="utf-8"
        )
    )
    (model,) = fixture["data"]
    assert model["id"] == "z-ai/glm-5.3-flash"
    engine = PricingEngine.load(ROOT / "pricing")
    price = engine.resolve(
        "openrouter:glm-5.3-flash", datetime.fromtimestamp(model["created"], tz=UTC)
    )
    # Fixture per-token rates convert exactly to the YAML per-MTok rates.
    assert price.input_per_mtok == Decimal(model["pricing"]["prompt"]) * MILLION
    assert price.output_per_mtok == Decimal(model["pricing"]["completion"]) * MILLION
    assert price.cached_input_per_mtok == Decimal(model["pricing"]["input_cache_read"]) * MILLION
    assert price.cache_write_per_mtok == 0
    assert price.effective_from == datetime.fromtimestamp(model["created"], tz=UTC)
    assert price.is_exact is False
    assert price.provider == "openrouter"


def test_reference_wrapper_delegates_to_unified_engine() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    occurred = datetime(2026, 8, 30, 12, tzinfo=UTC)
    short_kwargs = dict(
        occurred_at=occurred,
        input_tokens=1_000,
        cached_input_tokens=900,
        cache_write_tokens=25,
        output_tokens=50,
    )
    wrapper_value, wrapper_rate = compute_reference_cost(
        model_key="cursor:grok-4.6", **short_kwargs
    )
    engine_value = engine.compute(
        model_key="cursor:grok-4.6", cache_write_1h_tokens=0, **short_kwargs
    )
    assert wrapper_value == engine_value
    # Official Cursor rates: fresh 100 @ $2, cached 900 @ $0.5, writes free, out 50 @ $6.
    assert wrapper_value == Decimal("0.00095")
    price = engine.resolve("cursor:grok-4.6", occurred)
    assert wrapper_rate is not None
    assert wrapper_rate.source_url == price.source_url
    assert wrapper_rate.label == "Cursor published usage rate"

    long_kwargs = dict(
        occurred_at=occurred,
        input_tokens=250_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000,
    )
    wrapper_long, wrapper_long_rate = compute_reference_cost(
        model_key="supergrok:grok-4.6", **long_kwargs
    )
    assert wrapper_long == engine.compute(
        model_key="supergrok:grok-4.6", cache_write_1h_tokens=0, **long_kwargs
    )
    assert wrapper_long == Decimal("250000") / MILLION * Decimal("4") + Decimal("1000") / MILLION * Decimal("12")
    assert wrapper_long_rate is not None
    assert wrapper_long_rate.label == "xAI API-equivalent rate"
    assert wrapper_long_rate.source_url == "https://docs.x.ai/developers/pricing"

    glm_kwargs = dict(
        occurred_at=occurred,
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    for model_key, label in (
        ("opencode:glm-5.3-flash", "Z.AI published rate"),
        ("openrouter:glm-5.3-flash", "OpenRouter official model rate"),
    ):
        wrapper_glm, wrapper_glm_rate = compute_reference_cost(model_key=model_key, **glm_kwargs)
        assert wrapper_glm == engine.compute(
            model_key=model_key, cache_write_1h_tokens=0, **glm_kwargs
        )
        assert wrapper_glm == Decimal("0.325")
        assert wrapper_glm_rate is not None
        assert wrapper_glm_rate.label == label


def test_price_metadata_enables_is_exact_derivation() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    occurred = datetime(2026, 8, 30, 12, tzinfo=UTC)
    sol = engine.resolve("gpt-5.6-sol", occurred)
    assert sol.provider == "openai"
    assert sol.is_exact is True
    opus = engine.resolve("claude-opus-5", occurred)
    assert opus.provider == "anthropic"
    assert opus.is_exact is True
    for model_key, provider in (
        ("cursor:grok-4.6", "cursor"),
        ("cursor:composer-2.5", "cursor"),
        ("supergrok:grok-4.6", "xai"),
    ):
        price = engine.resolve(model_key, occurred)
        assert price.provider == provider
        assert price.is_exact is False


def test_every_priced_entry_is_official_effective_dated_and_nonnegative() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    assert engine.prices
    for price in engine.prices:
        if price.owner_asserted:
            # Owner-asserted cards are the one sanctioned exception to the
            # official-host rule; they must be derived and https, never exact.
            assert price.is_exact is False
            assert price.source_url.startswith("https://")
        else:
            assert price.source_url.startswith(_OFFICIAL_SOURCE_HOSTS)
        assert price.provider
        assert price.effective_to is None or price.effective_from < price.effective_to
        for rate in (
            price.input_per_mtok,
            price.cached_input_per_mtok,
            price.cache_write_per_mtok,
            price.output_per_mtok,
            price.long_input_multiplier,
            price.long_output_multiplier,
        ):
            assert rate >= 0
        assert price.cache_write_1h_per_mtok is None or price.cache_write_1h_per_mtok >= 0
        if price.long_context_threshold is not None:
            assert price.long_context_threshold > 0


def test_cursor_gemini_38_flash_matches_cursor_official_card() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    occurred = datetime(2026, 9, 2, 12, tzinfo=UTC)
    for model_key in ("cursor:gemini-3.8-flash", "cursor:gemini-3.8-flash-high"):
        price = engine.resolve(model_key, occurred)
        assert price.model_key == "cursor:gemini-3.8-flash"
        assert price.input_per_mtok == Decimal("0.75")
        assert price.cache_write_per_mtok == 0
        assert price.cached_input_per_mtok == Decimal("0.075")
        assert price.output_per_mtok == Decimal("3.50")
        assert price.source_url.startswith("https://cursor.com/")
        assert "openrouter.ai" not in price.source_url


def test_google_gemini_38_flash_cites_ai_google_dev_and_encodes_intro_window() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    intro = engine.resolve("antigravity:gemini-3.8-flash", datetime(2026, 9, 2, tzinfo=UTC))
    assert intro.source_url == "https://ai.google.dev/gemini-api/docs/pricing"
    assert intro.input_per_mtok == Decimal("0.75")
    assert intro.cached_input_per_mtok == Decimal("0.075")
    assert intro.cache_write_per_mtok == 0
    assert intro.output_per_mtok == Decimal("3.75")
    assert intro.effective_to == datetime(2027, 1, 1, tzinfo=UTC)
    high = engine.resolve("antigravity:gemini-3.8-flash-high", datetime(2026, 12, 31, 23, tzinfo=UTC))
    assert high.model_key == "antigravity:gemini-3.8-flash"
    assert high.output_per_mtok == Decimal("3.75")
    standard = engine.resolve("antigravity:gemini-3.8-flash", datetime(2027, 1, 1, tzinfo=UTC))
    assert standard.input_per_mtok == Decimal("1.50")
    assert standard.cached_input_per_mtok == Decimal("0.15")
    assert standard.cache_write_per_mtok == 0
    assert standard.output_per_mtok == Decimal("7.50")
    wrapper_value, wrapper_rate = compute_reference_cost(
        model_key="antigravity:gemini-3.8-flash",
        occurred_at=datetime(2026, 9, 2, tzinfo=UTC),
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    assert wrapper_value == Decimal("0.75") + Decimal("3.75")
    assert wrapper_rate is not None
    assert wrapper_rate.label == "Google API-equivalent rate"


def test_reference_rates_module_has_no_personal_plan_defaults() -> None:
    import spend_app.reference_rates as reference_rates

    assert not hasattr(reference_rates, "PLAN_REFERENCES")
    source = (ROOT / "spend_app" / "reference_rates.py").read_text(encoding="utf-8")
    assert "monthlyUsd" not in source
    assert "133.333" not in source


def test_no_third_party_rate_authority_in_pricing_sources() -> None:
    forbidden = ("requesty", "litellm", "helicone", "openrouter.ai/provider")
    for path in sorted((ROOT / "pricing").glob("*.yaml")):
        text = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            assert marker not in text, f"{path.name} must not cite {marker}"
    google_text = (ROOT / "pricing" / "google.yaml").read_text(encoding="utf-8").lower()
    cursor_38 = [
        price
        for price in PricingEngine.load(ROOT / "pricing").prices
        if price.model_key == "cursor:gemini-3.8-flash"
    ]
    assert "openrouter.ai" not in google_text
    assert cursor_38 and all(price.source_url.startswith("https://cursor.com/") for price in cursor_38)
    wrapper_source = (ROOT / "spend_app" / "reference_rates.py").read_text(encoding="utf-8")
    # The wrapper must not duplicate rate literals: Decimal( appears only for MILLION.
    assert wrapper_source.count("Decimal(") == 1


def _write_engine(tmp_path: Path, documents: dict[str, str]) -> PricingEngine:
    for name, text in documents.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return PricingEngine.load(tmp_path)


_MINIMAL_ROW = """
provider: openai
exactness: exact
prices:
  - model_key: m1
    input_per_mtok: 1
    cached_input_per_mtok: 0.1
    cache_write_per_mtok: 1.25
    output_per_mtok: 2
    effective_from: 2026-01-01T00:00:00Z
    source_url: https://developers.openai.com/api/docs/models/m1
"""


def test_load_rejects_negative_rate(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace("input_per_mtok: 1", "input_per_mtok: -1")
    with pytest.raises(ValueError, match="nonnegative"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_overlapping_revisions_for_canonical_model(tmp_path: Path) -> None:
    document = (
        _MINIMAL_ROW
        + "  - model_key: m1\n"
        + "    input_per_mtok: 2\n"
        + "    cached_input_per_mtok: 0.1\n"
        + "    cache_write_per_mtok: 2.5\n"
        + "    output_per_mtok: 4\n"
        + "    effective_from: 2026-02-01T00:00:00Z\n"
        + "    source_url: https://developers.openai.com/api/docs/models/m1\n"
    )
    with pytest.raises(ValueError, match="overlapping revisions"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_invalid_effective_interval(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace(
        "    source_url:",
        "    effective_to: 2025-12-31T00:00:00Z\n    source_url:",
    )
    with pytest.raises(ValueError, match="invalid effective interval"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_alias_colliding_with_canonical_model_key(tmp_path: Path) -> None:
    colliding = _MINIMAL_ROW.replace("model_key: m1", "model_key: m2").replace(
        "    input_per_mtok: 1",
        "    aliases: [m1]\n    input_per_mtok: 1",
    )
    with pytest.raises(ValueError, match="alias 'm1' collides"):
        _write_engine(tmp_path, {"openai.yaml": _MINIMAL_ROW, "other.yaml": colliding})


def test_load_rejects_ambiguous_alias_across_overlapping_windows(tmp_path: Path) -> None:
    second = _MINIMAL_ROW.replace("model_key: m1", "model_key: m2").replace(
        "    input_per_mtok: 1",
        "    aliases: [shared]\n    input_per_mtok: 1",
    )
    first = _MINIMAL_ROW.replace(
        "    input_per_mtok: 1",
        "    aliases: [shared]\n    input_per_mtok: 1",
    )
    with pytest.raises(ValueError, match="ambiguously"):
        _write_engine(tmp_path, {"openai.yaml": first, "other.yaml": second})


def test_load_rejects_unknown_exactness(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace("exactness: exact", "exactness: guessed")
    with pytest.raises(ValueError, match="exactness"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_missing_provider(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace("provider: openai\n", "")
    with pytest.raises(ValueError, match="provider"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_missing_official_source_url(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace(
        "    source_url: https://developers.openai.com/api/docs/models/m1\n", ""
    )
    with pytest.raises(ValueError, match="source_url"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_luna_cites_its_own_model_page() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    price = engine.resolve("gpt-5.6-luna", datetime(2026, 8, 30, 12, tzinfo=UTC))
    assert price.source_url == "https://developers.openai.com/api/docs/models/gpt-5.6-luna"
    assert "/compare" not in price.source_url


def test_future_dated_zai_revision_names_official_announcement() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    price = engine.resolve("opencode:glm-5.3-flash", datetime(2026, 9, 9, 16, tzinfo=UTC))
    assert price.source_url.startswith("https://docs.z.ai/")
    assert price.effective_from == datetime(2026, 9, 9, 16, tzinfo=UTC)


def test_load_rejects_comparison_page_citation(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace(
        "source_url: https://developers.openai.com/api/docs/models/m1",
        "source_url: https://developers.openai.com/api/docs/models/compare",
    )
    with pytest.raises(ValueError, match="comparison page"):
        _write_engine(tmp_path, {"openai.yaml": document})


def test_load_rejects_non_official_source_scheme(tmp_path: Path) -> None:
    document = _MINIMAL_ROW.replace(
        "source_url: https://developers.openai.com/api/docs/models/m1",
        "source_url: http://example.com/rates",
    )
    with pytest.raises(ValueError, match="official https"):
        _write_engine(tmp_path, {"openai.yaml": document})


_OWNER_ASSERTED_ROW = """
provider: openai
exactness: derived
source_policy: owner-asserted
prices:
  - model_key: owner-model
    input_per_mtok: 2.5
    cached_input_per_mtok: 0.25
    cache_write_per_mtok: 3.125
    output_per_mtok: 15
    effective_from: 2026-09-01T00:00:00Z
    source_url: https://example.invalid/models/owner-model
    source_note: figures supplied by the product owner on 2026-09-02
"""


def test_owner_asserted_card_loads_as_derived_without_official_host(tmp_path: Path) -> None:
    engine = _write_engine(tmp_path, {"owner.yaml": _OWNER_ASSERTED_ROW})
    price = engine.resolve("owner-model", datetime(2026, 9, 2, tzinfo=UTC))
    assert price.owner_asserted is True
    assert price.is_exact is False
    assert price.source_url == "https://example.invalid/models/owner-model"


def test_owner_asserted_card_requires_source_note(tmp_path: Path) -> None:
    document = _OWNER_ASSERTED_ROW.replace(
        "    source_note: figures supplied by the product owner on 2026-09-02", ""
    )
    with pytest.raises(ValueError, match="source_note"):
        _write_engine(tmp_path, {"owner.yaml": document})


def test_owner_asserted_card_can_never_be_exact(tmp_path: Path) -> None:
    document = _OWNER_ASSERTED_ROW.replace("exactness: derived", "exactness: exact")
    with pytest.raises(ValueError, match="owner-asserted"):
        _write_engine(tmp_path, {"owner.yaml": document})


def test_unofficial_host_still_rejected_without_owner_assertion(tmp_path: Path) -> None:
    document = _OWNER_ASSERTED_ROW.replace("source_policy: owner-asserted", "")
    with pytest.raises(ValueError, match="official"):
        _write_engine(tmp_path, {"owner.yaml": document})
