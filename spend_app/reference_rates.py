"""Temporary compatibility wrapper over the unified effective-dated PricingEngine.

pricing/*.yaml is the single pricing authority. Nothing in this module may
duplicate a rate literal: ``resolve``/``compute`` delegate to
``spend_app.pricing.PricingEngine`` and ``RATES`` is derived on demand.
Plan amounts live in the subscriptions table, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from spend_app.pricing import Price, PricingEngine, UnpricedModelError


ROOT = Path(__file__).resolve().parents[1]
MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ReferenceRate:
    model_key: str
    input_per_mtok: Decimal
    cached_per_mtok: Decimal
    output_per_mtok: Decimal
    effective_from: datetime
    effective_to: datetime | None
    label: str
    source_url: str


_PROVIDER_LABELS = {
    "openai": "OpenAI official model rate",
    "anthropic": "Anthropic official model rate",
    "cursor": "Cursor published usage rate",
    "xai": "xAI API-equivalent rate",
    "zai": "Z.AI published rate",
    "openrouter": "OpenRouter official model rate",
    "google": "Google API-equivalent rate",
}


@lru_cache(maxsize=1)
def _engine() -> PricingEngine:
    return PricingEngine.load(ROOT / "pricing")


def _label(price: Price) -> str:
    return _PROVIDER_LABELS.get(price.provider, f"{price.provider} official model rate")


def _to_reference_rate(price: Price) -> ReferenceRate:
    return ReferenceRate(
        model_key=price.model_key,
        input_per_mtok=price.input_per_mtok,
        cached_per_mtok=price.cached_input_per_mtok,
        output_per_mtok=price.output_per_mtok,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
        label=_label(price),
        source_url=price.source_url,
    )


def resolve(model_key: str, occurred_at: datetime) -> ReferenceRate | None:
    when = occurred_at.astimezone(UTC)
    try:
        price = _engine().resolve(model_key, when)
    except (UnpricedModelError, ValueError):
        return None
    return _to_reference_rate(price)


def compute(
    *,
    model_key: str,
    occurred_at: datetime,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> tuple[Decimal | None, ReferenceRate | None]:
    when = occurred_at.astimezone(UTC)
    try:
        value = _engine().compute(
            model_key=model_key,
            occurred_at=when,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_1h_tokens=0,
            output_tokens=output_tokens,
        )
    except (UnpricedModelError, ValueError):
        # Unpriced model or incoherent token components: fail closed to
        # "no attributable value" rather than guessing a rate.
        return None, None
    return value, resolve(model_key, when)


def _rates() -> tuple[ReferenceRate, ...]:
    return tuple(_to_reference_rate(price) for price in _engine().prices)


def __getattr__(name: str):
    if name == "RATES":
        return _rates()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
