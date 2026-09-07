"""Cross-source reconciliation (Release A contract 4.3, task A06).

Different feeds may describe the SAME provider event (Cursor Admin API and a
Cursor CSV export both carry the provider event id) or different events that
merely look alike. Only a verified shared provider event id collapses into
one canonical measured event; account/scope differences never merge, and an
ambiguous overlap is reported, not summed.

Canonical identity mapping is content-addressed by source pair so existing
raw ids (and their stable history) are untouched: reconciliation happens at
read time over the loaded rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# Sources whose rows carry the provider's own event id in the raw_id suffix
# ``<canonical-source>:<event id>``. A row from any of these maps to
# ``cursor-usage:<event id>``.
_CURSOR_EVENT_SOURCES = ("cursor_admin", "cursor_csv")


@dataclass(frozen=True)
class ReconciledEvent:
    """One canonical measured event plus its provenance count."""

    row: dict
    provenance: tuple[str, ...]
    suppressed: tuple[dict, ...]


def canonical_event_id(source: str, raw_id: str) -> str | None:
    """Map source-specific raw ids onto a shared canonical identity.

    Returns None when the row's identity is source-local (no cross-source
    claim is possible) — those rows always stay independent.
    """
    if source in _CURSOR_EVENT_SOURCES:
        match = re.match(r"^cursor-(?:admin|csv):(.+)$", raw_id)
        if match:
            return f"cursor-usage:{match.group(1)}"
    return None


def _authority_score(row: dict) -> tuple:
    """Preference order among provenance copies of one canonical event.

    A copy that carries a provider-reported charge is authoritative over a
    token-only copy; then more complete telemetry wins; then the earlier
    source name keeps results deterministic under import order.
    """
    charge = row.get("cost_usd")
    has_charge = 1 if charge is not None else 0
    complete = 1 if row.get("telemetry_complete", 1) else 0
    return (has_charge, complete, str(row.get("source", "")))


def reconcile_events(rows: Sequence[dict]) -> tuple[list[ReconciledEvent], dict]:
    """Collapse rows sharing a verified canonical id; keep everything else.

    Distinct accounts never merge: the account/scope key participates in the
    group identity. Import order never changes which copy survives because
    selection is by the deterministic authority score. Suppressed copies are
    reported (never silently destroyed) and their totals are returned so
    callers can show unresolved overlap explicitly.
    """
    groups: dict[tuple[str | None, str], list[dict]] = {}
    order: list[tuple[str | None, str]] = []
    suppressed_events = 0
    suppressed_by_source: dict[str, int] = {}
    for row in rows:
        canonical = canonical_event_id(str(row.get("source", "")), str(row.get("raw_id", "")))
        if canonical is None:
            key = (None, f"__local__{row.get('raw_id', '')}")
        else:
            # Different accounts/modes with the same provider event id remain
            # different events; scope participates in identity.
            key = (row.get("account_key"), canonical)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    output: list[ReconciledEvent] = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            output.append(ReconciledEvent(members[0], (str(members[0].get("source", "")),), ()))
            continue
        ordered = sorted(members, key=_authority_score, reverse=True)
        kept = ordered[0]
        rest = ordered[1:]
        suppressed_events += len(rest)
        for copy in rest:
            source = str(copy.get("source", ""))
            suppressed_by_source[source] = suppressed_by_source.get(source, 0) + 1
        output.append(
            ReconciledEvent(
                kept,
                tuple(str(copy.get("source", "")) for copy in ordered),
                tuple(rest),
            )
        )
    report = {
        "canonicalEvents": len(output),
        "inputRows": len(rows),
        "suppressedDuplicates": suppressed_events,
        "suppressedBySource": dict(sorted(suppressed_by_source.items())),
    }
    return output, report
