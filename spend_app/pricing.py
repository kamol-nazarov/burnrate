from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml


MILLION = Decimal("1000000")

_EXACTNESS_VALUES = ("exact", "derived")
_SOURCE_POLICY_VALUES = ("official", "owner-asserted")
_OFFICIAL_SOURCE_PREFIXES = (
    "https://developers.openai.com/",
    "https://platform.claude.com/",
    "https://cursor.com/",
    "https://docs.x.ai/",
    "https://docs.z.ai/",
    "https://openrouter.ai/",
    "https://ai.google.dev/",
)


class UnpricedModelError(LookupError):
    pass


@dataclass(frozen=True)
class Price:
    model_key: str
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    cache_write_1h_per_mtok: Decimal | None
    output_per_mtok: Decimal
    long_context_threshold: int | None
    long_input_multiplier: Decimal
    long_output_multiplier: Decimal
    effective_from: datetime
    effective_to: datetime | None
    source_url: str
    aliases: tuple[str, ...]
    provider: str
    is_exact: bool
    long_context_inclusive: bool = False
    # True when the card comes from a document declaring
    # `source_policy: owner-asserted`: the product owner vouches for the
    # figures and the source_url is not an official provider page. Only
    # allowed with `exactness: derived`.
    owner_asserted: bool = False


def _parse_time(value: object, *, field: str, path: Path) -> datetime:
    if value is None:
        raise ValueError(f"{path}: missing required {field}")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path}: invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_time(value: object, *, field: str, path: Path) -> datetime | None:
    if value is None:
        return None
    return _parse_time(value, field=field, path=path)


def _parse_decimal(value: object, *, field: str, label: str, path: Path) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValueError(f"{path}: {label} has invalid {field}: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{path}: {label} {field} must be nonnegative, got {parsed}")
    return parsed


def _validate(prices: tuple[Price, ...]) -> None:
    canonical_keys = {price.model_key for price in prices}
    by_model: dict[str, list[Price]] = {}
    alias_owners: dict[str, list[Price]] = {}
    for price in prices:
        if price.effective_to is not None and price.effective_to <= price.effective_from:
            raise ValueError(
                f"model {price.model_key!r} has an invalid effective interval: "
                f"effective_to {price.effective_to.isoformat()} must be after "
                f"effective_from {price.effective_from.isoformat()}"
            )
        if price.long_context_threshold is not None and price.long_context_threshold <= 0:
            raise ValueError(
                f"model {price.model_key!r} long_context_threshold must be positive, "
                f"got {price.long_context_threshold}"
            )
        if price.effective_from > datetime.now(UTC) and not any(
            price.source_url.startswith(prefix) for prefix in _OFFICIAL_SOURCE_PREFIXES
        ):
            raise ValueError(
                f"model {price.model_key!r} future-dated revision must name the official "
                "announcement that fixes the date"
            )
        by_model.setdefault(price.model_key, []).append(price)
        for alias in price.aliases:
            if alias in canonical_keys:
                raise ValueError(
                    f"alias {alias!r} collides with a canonical model_key of "
                    f"{price.model_key!r}"
                )
            alias_owners.setdefault(alias, []).append(price)
    for model_key, revisions in by_model.items():
        revisions.sort(key=lambda price: price.effective_from)
        for current, following in zip(revisions, revisions[1:]):
            if current.effective_to is None or following.effective_from < current.effective_to:
                raise ValueError(
                    f"model {model_key!r} has overlapping revisions: "
                    f"{current.effective_from.isoformat()}.."
                    f"{current.effective_to.isoformat() if current.effective_to else 'open'} "
                    f"overlaps revision effective from {following.effective_from.isoformat()}"
                )
    for alias, owners in alias_owners.items():
        owners.sort(key=lambda price: price.effective_from)
        for current, following in zip(owners, owners[1:]):
            if current.effective_to is None or following.effective_from < current.effective_to:
                raise ValueError(
                    f"alias {alias!r} resolves ambiguously between "
                    f"{current.model_key!r} and {following.model_key!r}"
                )


class PricingEngine:
    def __init__(self, prices: list[Price]) -> None:
        self.prices = tuple(prices)
        _validate(self.prices)
        # Candidate index by canonical key and alias. resolve() runs once per
        # event during aggregation, so scanning every card per event dominated
        # summary latency.
        index: dict[str, list[Price]] = {}
        for price in self.prices:
            index.setdefault(price.model_key, []).append(price)
            for alias in price.aliases:
                index.setdefault(alias, []).append(price)
        self._index = {key: tuple(value) for key, value in index.items()}

    @classmethod
    def load(cls, directory: Path) -> "PricingEngine":
        prices: list[Price] = []
        for path in sorted(Path(directory).glob("*.yaml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            provider = str(document.get("provider") or "").strip()
            if not provider:
                raise ValueError(f"{path}: missing required top-level 'provider'")
            exactness = str(document.get("exactness") or "").strip()
            if exactness not in _EXACTNESS_VALUES:
                raise ValueError(
                    f"{path}: 'exactness' must be one of {_EXACTNESS_VALUES}, got {exactness!r}"
                )
            is_exact = exactness == "exact"
            source_policy = str(document.get("source_policy") or "official").strip()
            if source_policy not in _SOURCE_POLICY_VALUES:
                raise ValueError(
                    f"{path}: 'source_policy' must be one of {_SOURCE_POLICY_VALUES}, "
                    f"got {source_policy!r}"
                )
            owner_asserted = source_policy == "owner-asserted"
            if owner_asserted and is_exact:
                raise ValueError(
                    f"{path}: 'source_policy: owner-asserted' requires 'exactness: derived'; "
                    "owner-asserted figures are never exact"
                )
            for row in document.get("prices") or []:
                label = f"[{row.get('model_key', '?')}]"
                cache_write_1h = row.get("cache_write_1h_per_mtok")
                threshold = row.get("long_context_threshold")
                prices.append(
                    Price(
                        model_key=str(row["model_key"]),
                        input_per_mtok=_parse_decimal(
                            row["input_per_mtok"], field="input_per_mtok", label=label, path=path
                        ),
                        cached_input_per_mtok=_parse_decimal(
                            row["cached_input_per_mtok"],
                            field="cached_input_per_mtok",
                            label=label,
                            path=path,
                        ),
                        cache_write_per_mtok=_parse_decimal(
                            row["cache_write_per_mtok"],
                            field="cache_write_per_mtok",
                            label=label,
                            path=path,
                        ),
                        cache_write_1h_per_mtok=(
                            _parse_decimal(
                                cache_write_1h,
                                field="cache_write_1h_per_mtok",
                                label=label,
                                path=path,
                            )
                            if cache_write_1h is not None
                            else None
                        ),
                        output_per_mtok=_parse_decimal(
                            row["output_per_mtok"], field="output_per_mtok", label=label, path=path
                        ),
                        long_context_threshold=(
                            int(threshold) if threshold is not None else None
                        ),
                        long_input_multiplier=_parse_decimal(
                            row.get("long_input_multiplier", 1),
                            field="long_input_multiplier",
                            label=label,
                            path=path,
                        ),
                        long_output_multiplier=_parse_decimal(
                            row.get("long_output_multiplier", 1),
                            field="long_output_multiplier",
                            label=label,
                            path=path,
                        ),
                        effective_from=_parse_time(
                            row.get("effective_from"), field="effective_from", path=path
                        ),
                        effective_to=_parse_optional_time(
                            row.get("effective_to"), field="effective_to", path=path
                        ),
                        source_url=_require_source_url(
                            row, label=label, path=path, owner_asserted=owner_asserted
                        ),
                        aliases=tuple(str(alias) for alias in row.get("aliases", []) or []),
                        provider=provider,
                        is_exact=is_exact,
                        long_context_inclusive=bool(row.get("long_context_inclusive", False)),
                        owner_asserted=owner_asserted,
                    )
                )
        return cls(prices)

    def resolve(self, model_key: str, occurred_at: datetime) -> Price:
        when = occurred_at.astimezone(UTC)
        candidates = [
            price
            for price in self._index.get(model_key, ())
            if price.effective_from <= when
            and (price.effective_to is None or when < price.effective_to)
        ]
        if not candidates:
            raise UnpricedModelError(f"No effective price for {model_key!r} at {when.isoformat()}")
        return max(candidates, key=lambda price: price.effective_from)

    def compute(
        self,
        *,
        model_key: str,
        occurred_at: datetime,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        cache_write_1h_tokens: int = 0,
    ) -> Decimal:
        return sum(
            self.components(
                model_key=model_key,
                occurred_at=occurred_at,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
                output_tokens=output_tokens,
                cache_write_1h_tokens=cache_write_1h_tokens,
            ).values(),
            Decimal(0),
        )

    def components(
        self,
        *,
        model_key: str,
        occurred_at: datetime,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        cache_write_1h_tokens: int = 0,
    ) -> dict[str, Decimal]:
        if cached_input_tokens > input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if cache_write_1h_tokens > cache_write_tokens:
            raise ValueError("cache_write_1h_tokens cannot exceed cache_write_tokens")
        price = self.resolve(model_key, occurred_at)
        fresh_input = input_tokens - cached_input_tokens
        is_long = (
            price.long_context_threshold is not None
            and (
                input_tokens >= price.long_context_threshold
                if price.long_context_inclusive
                else input_tokens > price.long_context_threshold
            )
        )
        input_multiplier = price.long_input_multiplier if is_long else Decimal(1)
        output_multiplier = price.long_output_multiplier if is_long else Decimal(1)
        cache_write_5m_tokens = cache_write_tokens - cache_write_1h_tokens
        cache_write_1h_rate = price.cache_write_1h_per_mtok or price.cache_write_per_mtok
        return {
            "fresh_input": Decimal(fresh_input) / MILLION * price.input_per_mtok * input_multiplier,
            "cached_input": Decimal(cached_input_tokens)
            / MILLION
            * price.cached_input_per_mtok
            * input_multiplier,
            "cache_write": (
                Decimal(cache_write_5m_tokens) / MILLION * price.cache_write_per_mtok
                + Decimal(cache_write_1h_tokens) / MILLION * cache_write_1h_rate
            )
            * input_multiplier,
            "output": Decimal(output_tokens) / MILLION * price.output_per_mtok * output_multiplier,
        }


def _require_source_url(
    row: dict, *, label: str, path: Path, owner_asserted: bool = False
) -> str:
    source_url = str(row.get("source_url") or "").strip()
    if not source_url.startswith("https://"):
        raise ValueError(
            f"{path}: {label} source_url must be an official https URL, got {source_url!r}"
        )
    if owner_asserted:
        # The owner vouches for the figures; the URL records where they were
        # seen and a note must say so, but no official-host check applies.
        if not str(row.get("source_note") or "").strip():
            raise ValueError(
                f"{path}: {label} owner-asserted card must carry a source_note "
                "explaining who supplied the figures and when"
            )
        return source_url
    canonical = source_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if canonical.endswith("/compare") or "/compare/" in canonical + "/":
        raise ValueError(
            f"{path}: {label} source_url must be the model's own official page, "
            f"not a comparison page: {source_url!r}"
        )
    if not any(source_url.startswith(prefix) for prefix in _OFFICIAL_SOURCE_PREFIXES):
        raise ValueError(
            f"{path}: {label} source_url must be the provider's own official page, got {source_url!r}"
        )
    return source_url
