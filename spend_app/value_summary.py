"""Pure value and coverage accounting (Release A contract 4.1, task A09).

Three economic quantities are kept apart everywhere in this module:

- **usage_value** — API-equivalent reference value of measured tokens priced
  at documented rates. A comparison figure, never a payment.
- **configured_cost** — calendar-prorated configured plan expense over the
  stated interval. Not verified cash.
- **reported_charges** — provider-reported amounts (invoice/bucket/charged
  costs), authoritative at their reported scope, period and currency.

``usage_value`` is never the sum of the other two. Every result carries its
coverage, so partial windows show a labeled known subtotal instead of a
synthetic $0, and all-unpriced windows show ``None`` (unavailable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal(0)
MILLION = Decimal("1000000")


@dataclass(frozen=True)
class Component:
    """One priced observation contribution."""

    amount: Decimal
    tokens: int = 0


@dataclass
class ValueSummary:
    """Accumulator for one window/scope of observations."""

    priced_value: Decimal = ZERO
    priced_tokens: int = 0
    priced_events: int = 0
    unpriced_tokens: int = 0
    unpriced_events: int = 0
    unpriced_models: list[str] = field(default_factory=list)
    incomplete_telemetry_events: int = 0
    reported_charges: Decimal = ZERO
    reported_charge_events: int = 0
    configured_cost: Decimal = ZERO
    measured_tokens: int = 0
    events: int = 0

    def add_priced(self, amount: Decimal, tokens: int = 0, *, event: bool = True) -> None:
        if amount < 0:
            raise ValueError("reference value contributions are nonnegative")
        self.priced_value += amount
        self.priced_tokens += max(0, int(tokens))
        if event:
            self.priced_events += 1

    def add_unpriced(self, tokens: int = 0, *, model: str | None = None, complete: bool = True) -> None:
        self.unpriced_tokens += max(0, int(tokens))
        self.unpriced_events += 1
        if model and model not in self.unpriced_models:
            self.unpriced_models.append(model)
        if not complete:
            self.incomplete_telemetry_events += 1

    def add_reported_charge(self, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("reported charges are nonnegative")
        self.reported_charges += amount
        self.reported_charge_events += 1

    def set_configured_cost(self, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("configured cost is nonnegative")
        self.configured_cost = amount

    @property
    def usage_value(self) -> Decimal | None:
        """Known reference subtotal, or None when nothing could be priced.

        A positive priced subset is a labeled known subtotal (a floor for the
        window's reference value), not the whole-window value; ``complete``
        says whether any observation stayed unpriced.
        """
        if self.priced_events == 0 and self.reported_charge_events == 0:
            return None
        return self.priced_value

    @property
    def complete(self) -> bool:
        return self.unpriced_events == 0 and self.incomplete_telemetry_events == 0

    @property
    def any_amount_known(self) -> bool:
        return self.usage_value is not None

    @property
    def priced_subset_average_per_million(self) -> Decimal | None:
        """Mean reference $/Mtok over the priced subset only.

        The denominator is the priced token set alone: this is a SUBSET
        AVERAGE and must not be presented as the full-window average or as a
        bound on it.
        """
        if self.priced_tokens <= 0:
            return None
        return self.priced_value * MILLION / Decimal(self.priced_tokens)

    def coverage(self) -> dict:
        known = self.usage_value
        return {
            "usageValueKnown": known is not None,
            "usageValueComplete": self.complete,
            "usageValueBasis": "known-subtotal" if known is not None and not self.complete else "complete",
            "pricedTokens": self.priced_tokens,
            "unpricedTokens": self.unpriced_tokens,
            "pricedEvents": self.priced_events,
            "unpricedEvents": self.unpriced_events,
            "unpricedModels": sorted(self.unpriced_models),
            "incompleteTelemetryEvents": self.incomplete_telemetry_events,
            "pricedSubsetAvgPerMTok": (
                float(self.priced_subset_average_per_million)
                if self.priced_subset_average_per_million is not None
                else None
            ),
            "measuredTokens": self.measured_tokens,
        }


def ratio_lower_bound(numerator: Decimal, denominator: Decimal) -> dict:
    """Reference-value / configured-cost with honest availability rules.

    - positive numerator and positive complete denominator -> ``value`` with
      basis ``ratio``;
    - nonnegative numerator with a *complete* positive cost denominator and
      partial reference coverage -> labeled ``lower_bound``;
    - incomplete cost denominator or nonpositive/unknown parts -> the ratio is
      ``unavailable`` rather than an automatic lower bound.

    The denominator must be configured cost over the same interval as the
    numerator; callers own that compatibility check.
    """
    if denominator <= 0:
        return {"value": None, "basis": "unavailable", "reason": "no positive cost denominator"}
    if numerator < 0:
        return {"value": None, "basis": "unavailable", "reason": "unknown numerator"}
    return {
        "value": float(numerator / denominator),
        "basis": "ratio",
    }


def ratio_with_coverage(summary: ValueSummary, denominator: Decimal, *, denominator_complete: bool = True) -> dict:
    if denominator <= 0:
        return {"value": None, "basis": "unavailable", "reason": "no positive cost denominator"}
    if not denominator_complete:
        return {"value": None, "basis": "unavailable", "reason": "cost denominator is incomplete"}
    numerator = summary.priced_value
    if summary.complete and numerator > 0:
        return {"value": float(numerator / denominator), "basis": "ratio"}
    if numerator > 0:
        return {
            "value": float(numerator / denominator),
            "basis": "lower_bound",
            "note": "priced subset over complete configured cost",
        }
    return {"value": None, "basis": "unavailable", "reason": "no priced reference value in this window"}
