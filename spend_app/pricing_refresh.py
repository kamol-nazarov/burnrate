"""Pricing refresh drafting and audit (Release A task A08).

A draft/review tool, not an automatic rate writer: it converts a captured
official metadata snapshot into candidate rate-card rows, produces a
deterministic content diff against the approved cards, and refuses to invent
history. Three distinct times are tracked:

- ``observed_at`` — when the snapshot was captured (refresh time);
- ``effective_from`` — the actual known effective instant, only when the
  provider documents one;
- ``model_created_at`` — when the model appeared in the catalog. NEVER a
  price-start proof: a current catalog entry is not a history of old rates,
  and may describe a lowest/reference rate rather than the routed charge.

Re-running on an identical snapshot yields an identical empty diff —
observation timestamps may differ but never mint duplicate revisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

MILLION = Decimal("1000000")
_RATE_KEYS = ("input", "cached_input", "cache_write", "cache_write_1h", "output")


@dataclass(frozen=True)
class CandidateRate:
    model_key: str
    provider: str
    per_mtok: dict[str, Decimal | None]
    currency: str
    documented_zero: tuple[str, ...]
    effective_from: datetime | None
    model_created_at: datetime | None
    observed_at: datetime
    source_url: str
    tier: str | None = None
    context_threshold: int | None = None


@dataclass
class RefreshDraft:
    snapshot_id: str
    observed_at: datetime
    added: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    coverage_warnings: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed)

    def to_json(self) -> str:
        return json.dumps(
            {
                "snapshotId": self.snapshot_id,
                "observedAt": self.observed_at.isoformat().replace("+00:00", "Z"),
                "added": self.added,
                "changed": self.changed,
                "unchanged": self.unchanged,
                "coverageWarnings": self.coverage_warnings,
                "rejections": self.rejections,
            },
            indent=2,
            sort_keys=True,
        )


def _decimal_per_token(value: object, *, label: str, model: str, rejections: list[str]) -> Decimal | None:
    """Per-token string/number -> Decimal per-million tokens.

    Missing fields stay None (a documented absence), documented "0" becomes a
    real zero, and invalid/nonfinite/negative data rejects the FIELD, never
    silently coerces.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError):
        rejections.append(f"{model}: {label} is not a valid decimal ({value!r})")
        return None
    if not parsed.is_finite():
        rejections.append(f"{model}: {label} is not finite ({value!r})")
        return None
    if parsed < 0:
        rejections.append(f"{model}: {label} is negative ({value!r})")
        return None
    return parsed * MILLION


def _parse_time(value: object) -> datetime | None:
    # PyYAML resolves ISO timestamps to datetime objects; accept both forms.
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_snapshot(document: dict, *, provider: str, observed_at: datetime) -> tuple[list[CandidateRate], list[str]]:
    """Extract candidate rates from an authorized provider metadata snapshot.

    Accepts the OpenRouter-style shape (``data[].id`` plus a nested pricing
    object with per-token strings, ``created`` epoch seconds as the
    model-created time). No invented defaults: missing fields stay missing,
    and the model-created time is carried separately from any effective time.
    """
    rates: list[CandidateRate] = []
    rejections: list[str] = []
    rows = document.get("data") if isinstance(document.get("data"), list) else [document]
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("model_key") or "").strip()
        if not model_id:
            rejections.append("snapshot row without a model identifier")
            continue
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else row
        per_mtok: dict[str, Decimal | None] = {}
        documented_zero: list[str] = []
        for key in _RATE_KEYS:
            raw = pricing.get(f"{key}_per_token")
            value = _decimal_per_token(raw, label=f"{key}_per_token", model=model_id, rejections=rejections)
            per_mtok[key] = value
            if raw is not None and str(raw).strip() in {"0", "0.0", "0.00"}:
                documented_zero.append(key)
        created = _parse_time_epoch(row.get("created"))
        rates.append(
            CandidateRate(
                model_key=model_id,
                provider=provider,
                per_mtok=per_mtok,
                currency=str(pricing.get("currency") or "USD"),
                documented_zero=tuple(documented_zero),
                # A catalog snapshot proves existence at observation time; an
                # effective instant needs explicit provider documentation.
                effective_from=None,
                model_created_at=created,
                observed_at=observed_at,
                source_url=str(document.get("source_url") or ""),
                tier=str(pricing.get("tier")) if pricing.get("tier") else None,
                context_threshold=row.get("context_length"),
            )
        )
    return rates, rejections


def _parse_time_epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return _parse_time(value)


def _plain(value: Decimal | None) -> str | None:
    """Normalize a rate for comparison output: no exponent, no trailing zeros."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def draft_refresh(
    *,
    snapshot: dict,
    provider: str,
    approved_cards: dict[str, dict],
    observed_at: datetime,
) -> RefreshDraft:
    """Deterministic diff of a snapshot against the approved card fields.

    ``approved_cards`` maps model_key -> {rate key -> per-million Decimal|None}.
    The snapshot fingerprint (content hash) is part of the draft id, so the
    same source snapshot re-submitted later compares identical and yields no
    new revision proposal.
    """
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    draft = RefreshDraft(
        snapshot_id=f"{provider}:{fingerprint}",
        observed_at=observed_at,
    )
    candidates, rejections = parse_snapshot(snapshot, provider=provider, observed_at=observed_at)
    draft.rejections.extend(rejections)
    seen = set()
    for candidate in candidates:
        seen.add(candidate.model_key)
        proposed = {
            key: (value.quantize(Decimal("1e-12")) if value is not None else None)
            for key, value in candidate.per_mtok.items()
        }
        current = approved_cards.get(candidate.model_key)
        if current is None:
            draft.added.append(
                {
                    "modelKey": candidate.model_key,
                    "proposed": {k: _plain(v) for k, v in proposed.items()},
                    "effectiveFrom": None,
                    "modelCreatedAt": (
                        candidate.model_created_at.isoformat().replace("+00:00", "Z")
                        if candidate.model_created_at
                        else None
                    ),
                    "note": "model-created time is provenance only, not an effective rate start",
                }
            )
            continue
        diffs = {}
        for key, value in proposed.items():
            existing = current.get(key)
            if value != existing:
                diffs[key] = {"from": _plain(existing), "to": _plain(value)}
        if diffs:
            draft.changed.append({"modelKey": candidate.model_key, "fields": diffs})
        else:
            draft.unchanged.append(candidate.model_key)
    for model_key in sorted(set(approved_cards) - seen):
        draft.coverage_warnings.append(
            f"{model_key}: absent from the snapshot — keep the approved card; absence is not a price change"
        )
    return draft


def expiring_cards(cards: list[dict], *, now: datetime) -> list[dict]:
    """Cards with an effective_to after ``now``, soonest first.

    History priced inside ``[.., effective_to)`` stays priced forever; only
    events at/after the expiry lose the card. A successor rate is added ONLY
    from evidence; the audit lists what will become unpriced instead.
    """
    warnings = []
    for card in cards:
        expiry = card.get("effective_to")
        if expiry is None:
            continue
        if expiry > now:
            warnings.append(
                {
                    "modelKey": card.get("model_key"),
                    "expiresAt": expiry.isoformat().replace("+00:00", "Z"),
                    "note": "events inside the valid interval stay priced; after expiry they become unpriced until an evidenced successor exists",
                }
            )
    return sorted(warnings, key=lambda item: item["expiresAt"])


def load_card_expiries(pricing_directory: Path) -> list[dict]:
    """Read approved cards' expiries from YAML for the audit warning."""
    import yaml

    cards: list[dict] = []
    for path in sorted(Path(pricing_directory).glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for row in document.get("prices") or []:
            expiry = row.get("effective_to")
            parsed = _parse_time(expiry) if expiry else None
            if parsed is not None:
                cards.append({"model_key": str(row.get("model_key")), "effective_to": parsed})
    return cards
