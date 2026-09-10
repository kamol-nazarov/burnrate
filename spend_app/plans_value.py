"""Plans & Value: pure reference valuation, coverage, multiples, and report assembly.

Three quantities stay separate:

- **reference value** — API-equivalent usage at effective-dated published rates
  (``computed_cost_usd`` / priced components). Never configured expense, never
  provider-reported charges as a fallback rate.
- **configured cost** — accrued GROUP expense over the same interval (input).
- **collection evidence** — source freshness / partial capture; independent of
  whether recorded rows were priced.

API-only admin traffic (``openai_admin``, ``anthropic_admin``,
``provider_cost_buckets``) and charge-only / non-comparable rows are excluded
from the numerator and explained. The ratio is a **Reference-value multiple**,
not ROI or a cancellation verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.plans_value_groups import SUPPORTED_TOOLS, canonical_tool_key
from spend_app.source_evidence import source_reason_text
from spend_app.value_summary import ValueSummary, ratio_with_coverage

UTC = timezone.utc
ZERO = Decimal(0)
SCHEMA_VERSION = 1

# Known API-key / invoice bucket traffic is not subscription-plan reference usage.
API_ONLY_SOURCES = frozenset(
    {
        "openai_admin",
        "anthropic_admin",
        "provider_cost_buckets",
    }
)

REASON_ZERO_COST = "zero_cost"
REASON_NO_RECORDS = "no_records"
REASON_NO_PRICED_VALUE = "no_priced_value"
REASON_INCOMPATIBLE_SCOPE = "incompatible_scope"
REASON_UNAVAILABLE = "unavailable"

STATUS_COMPLETE = "complete"
STATUS_LOWER_BOUND = "lower_bound"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_RECORDS = "no_records"
STATUS_MEASURED_ZERO = "measured_zero"


def _as_mapping(obj: Any) -> Mapping[str, Any]:
    if isinstance(obj, Mapping):
        return obj
    if hasattr(obj, "__dataclass_fields__"):
        return {key: getattr(obj, key) for key in obj.__dataclass_fields__}
    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_") and not callable(value)
        }
    raise TypeError(f"expected mapping or object, got {type(obj)!r}")


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        for key in keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        return default
    for key in keys:
        if hasattr(obj, key):
            value = getattr(obj, key)
            if value is not None:
                return value
    return default


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _money_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_when(event: Mapping[str, Any]) -> datetime | None:
    when = event.get("when")
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=UTC)
    raw = event.get("occurred_at")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def measured_tokens(event: Mapping[str, Any]) -> int:
    """Token convention matching aggregate._measured (input includes cache reads)."""
    input_tokens = int(event.get("input_tokens") or 0)
    cached = int(event.get("cached_input_tokens") or 0)
    fresh = max(input_tokens - cached, 0)
    return (
        cached
        + fresh
        + int(event.get("cache_write_tokens") or 0)
        + int(event.get("output_tokens") or 0)
        + int(event.get("unclassified_tokens") or 0)
    )


def is_api_only_source(source: str | None) -> bool:
    return str(source or "").strip() in API_ONLY_SOURCES


def is_charge_only(event: Mapping[str, Any]) -> bool:
    """Provider charge without defensible measured reference components."""
    if event.get("charge_only") is True or event.get("reference_eligible") is False:
        return True
    tokens = measured_tokens(event)
    computed = _decimal(event.get("computed_cost_usd"))
    if computed is None:
        computed = _decimal(event.get("computed_total"))
    reported = _decimal(event.get("cost_usd"))
    if tokens <= 0 and reported is not None and (computed is None or computed == ZERO):
        return True
    return False


def is_noncomparable(event: Mapping[str, Any]) -> bool:
    if event.get("scope_conflict") is True or event.get("comparable") is False:
        return True
    if event.get("noncomparable") is True or event.get("incompatible_scope") is True:
        return True
    reason = str(event.get("exclusion_reason") or event.get("scope_reason") or "")
    return reason in {"scope_conflict", "incompatible_scope", "mixed_scope", "noncomparable"}


def resolve_event_reference_usd(
    event: Mapping[str, Any],
    pricing: PricingEngine | None = None,
) -> Decimal | None:
    """Defensible reference USD for one event.

    Uses stored ``computed_cost_usd`` (event-time pricing) when present.
    Optionally prices token components via ``PricingEngine`` at event time.
    Never falls back to provider-reported ``cost_usd``.
    """
    if is_api_only_source(event.get("source")):
        return None
    if is_charge_only(event) or is_noncomparable(event):
        return None

    computed = _decimal(event.get("computed_cost_usd"))
    if computed is None:
        computed = _decimal(event.get("computed_total"))
    if computed is None:
        computed = _decimal(event.get("reference_value_usd"))
    if computed is not None:
        return computed if computed >= ZERO else None

    if pricing is None:
        return None
    when = _parse_when(event)
    model_key = event.get("model_key")
    if when is None or not model_key:
        return None
    try:
        return pricing.compute(
            model_key=str(model_key),
            occurred_at=when,
            input_tokens=int(event.get("input_tokens") or 0),
            cached_input_tokens=int(event.get("cached_input_tokens") or 0),
            cache_write_tokens=int(event.get("cache_write_tokens") or 0),
            cache_write_1h_tokens=int(event.get("cache_write_1h_tokens") or 0),
            output_tokens=int(event.get("output_tokens") or 0),
        )
    except (UnpricedModelError, ValueError):
        return None


def _model_key(event: Mapping[str, Any]) -> str:
    return str(event.get("model_key") or "unknown")


def _tool_key(event: Mapping[str, Any]) -> str:
    return str(event.get("tool_key") or "unknown")


def _interval_payload(intervals: Any) -> list[dict]:
    output: list[dict] = []
    for item in intervals or ():
        if isinstance(item, Mapping):
            start = item.get("startUtc") or item.get("start_utc") or item.get("start")
            end = item.get("endUtc") or item.get("end_utc") or item.get("end")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            start, end = item[0], item[1]
        else:
            continue
        if isinstance(start, datetime):
            start = _iso(start)
        if isinstance(end, datetime):
            end = _iso(end)
        output.append({"startUtc": start, "endUtc": end})
    return output


def _plan_payload(plans: Any) -> list[dict]:
    rows: list[dict] = []
    for plan in plans or ():
        mapping = _as_mapping(plan) if not isinstance(plan, Mapping) else plan
        amount = _decimal(
            mapping.get("accrued_cost")
            if mapping.get("accrued_cost") is not None
            else mapping.get("accruedCostUsd")
            if mapping.get("accruedCostUsd") is not None
            else mapping.get("amount_usd")
            if mapping.get("amount_usd") is not None
            else mapping.get("configuredCostUsd")
        )
        rows.append(
            {
                "planId": mapping.get("plan_id") or mapping.get("planId") or mapping.get("id"),
                "name": mapping.get("name"),
                "toolKey": mapping.get("tool_key") or mapping.get("toolKey"),
                "accruedCostUsd": _money_str(amount) if amount is not None else None,
                "cadence": mapping.get("cadence"),
                "amountUsd": (
                    _money_str(_decimal(mapping.get("amount_usd") or mapping.get("amountUsd")))
                    if (mapping.get("amount_usd") is not None or mapping.get("amountUsd") is not None)
                    else None
                ),
            }
        )
    return rows


def _collection_tool_keys(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    values = mapping.get("toolKeys")
    if isinstance(values, str):
        values = (values,)
    output: list[str] = []
    for value in values or ():
        key = str(value or "").strip().lower()
        if key in SUPPORTED_TOOLS and key not in output:
            output.append(key)
    return tuple(output)


def _collection_source_id(value: Any) -> str:
    import re

    text = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", text):
        return text
    return "unattributed"


def _collection_source_label(value: Any, source: str) -> str:
    import re

    text = str(value or "").strip()
    if not text or len(text) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()/+\-]{0,79}", text):
        return source.replace("_", " ").title()
    return text


def _collection_safe_text(value: Any) -> str | None:
    """Keep the report projection seam while sharing metadata redaction."""
    return source_reason_text(value)


def _collection_freshness(mapping: Mapping[str, Any]) -> dict:
    value = mapping.get("freshness")
    if isinstance(value, Mapping):
        state = str(value.get("state") or "unknown").lower()
        age = value.get("ageSeconds")
        return {
            "state": state,
            "ageSeconds": age,
            "asOf": value.get("asOf"),
        }
    return {"state": "unknown", "ageSeconds": None, "asOf": None}


def _collection_evidence(
    source_health: Any,
    *,
    tool_keys: tuple[str, ...] | list[str],
    as_of: datetime | None = None,
) -> dict:
    """Project only relevant source lanes into one group's evidence.

    The loader's ``toolKeys``/``relevant`` fields are authoritative.  A row
    with an empty association is treated as a global issue and is never
    attached to every plan group.
    """
    wanted = {canonical_tool_key(str(key).strip().lower()) for key in tool_keys if key}
    sources: list[dict] = []
    statuses: list[str] = []
    for row in source_health or ():
        mapping = row if isinstance(row, Mapping) else _as_mapping(row)
        if mapping.get("relevant") is False:
            continue
        source = _collection_source_id(mapping.get("source"))
        row_tools = _collection_tool_keys(mapping)
        if not row_tools or not wanted:
            continue
        row_canonical = {canonical_tool_key(key) for key in row_tools}
        if not wanted.intersection(row_canonical):
            continue

        status = str(mapping.get("status") or "unknown").lower()
        configured = str(mapping.get("configuredState") or "unknown").lower()
        freshness = _collection_freshness(mapping)
        last_attempt = (
            mapping.get("lastAttemptAt")
        )
        last_success = mapping.get("lastSuccessAt")
        reason = _collection_safe_text(mapping.get("reason"))
        association = mapping.get("associationKey")
        if association not in {canonical_tool_key(key) for key in row_tools}:
            association = None
        if association is None and row_tools:
            canonical = {canonical_tool_key(key) for key in row_tools}
            association = next(iter(canonical)) if len(canonical) == 1 else None
        projected = {
            "source": source,
            "sourceLabel": _collection_source_label(mapping.get("sourceLabel"), source),
            "toolKeys": list(row_tools),
            "associationKey": association,
            "configuredState": configured,
            "status": status,
            "lastAttemptAt": last_attempt,
            "lastSuccessAt": last_success,
            "freshness": freshness,
            "coverage": mapping.get("coverage") or "unknown",
            "reason": reason,
            "eventsWritten": mapping.get("eventsWritten") or 0,
        }
        sources.append(projected)

    # Optional/unconfigured lanes sharing a tool must not make an otherwise
    # healthy configured lane look failed.  Keep them visible when they are
    # the only evidence for a group so "missing" and "disabled" remain
    # distinct states.
    if any(row["configuredState"] == "configured" for row in sources):
        sources = [
            row
            for row in sources
            if row["configuredState"] not in {"missing", "disabled", "unknown"}
        ]
    statuses = [row["status"] for row in sources]

    if not sources:
        overall = "unknown"
        label = "No collection evidence for this group"
    elif any(state == "disabled" for state in (row["configuredState"] for row in sources)):
        overall = "disabled"
        label = "Collection source disabled"
    elif any(state == "missing" for state in (row["configuredState"] for row in sources)):
        overall = "missing"
        label = "Collection source not configured"
    elif any(status in {"failed", "error"} for status in statuses):
        overall = "failed"
        label = "Collection failed for at least one source"
    elif any(status in {"partial", "incomplete"} for status in statuses):
        overall = "partial"
        label = "Partial collection evidence"
    elif any(row["freshness"]["state"] == "stale" for row in sources):
        overall = "stale"
        label = "Stale collection evidence"
    elif any(status == "never" for status in statuses):
        overall = "never"
        label = "No collection observed yet"
    elif all(
        (status == "success" or (status == "skipped" and row["reason"] == "Collection deferred to the established cadence."))
        and row["freshness"]["state"] == "recent"
        for status, row in zip(statuses, sources)
    ):
        overall = "healthy"
        label = "Healthy recent collection"
    else:
        overall = "unknown"
        label = "Collection evidence incomplete"

    return {
        "status": overall,
        "complete": False,
        "label": label,
        "sources": sources,
        "note": (
            "Collection evidence describes the current poll and source freshness; "
            "it does not prove complete historical capture for this period, "
            "including Last month. It is separate from whether recorded usage was priced."
        ),
    }


def _accumulate_events(
    *,
    events: list[dict] | None,
    unpriced_events: list[dict] | None,
    pricing: PricingEngine | None = None,
) -> dict:
    summary = ValueSummary()
    excluded_api_only = 0
    excluded_charge_only = 0
    excluded_scope = 0
    exclusion_notes: list[dict] = []
    tool_tokens: dict[str, int] = {}
    unpriced_model_stats: dict[str, dict[str, int]] = {}
    priced_model_refs: list[str] = []

    def note_exclusion(event: Mapping[str, Any], code: str, detail: str) -> None:
        exclusion_notes.append(
            {
                "reasonCode": code,
                "detail": detail,
                "source": event.get("source"),
                "toolKey": _tool_key(event),
                "modelKey": _model_key(event),
            }
        )

    for raw in events or ():
        event = raw if isinstance(raw, Mapping) else _as_mapping(raw)
        tokens = measured_tokens(event)
        tool_tokens[_tool_key(event)] = tool_tokens.get(_tool_key(event), 0) + tokens
        summary.measured_tokens += tokens
        summary.events += 1

        if is_api_only_source(event.get("source")):
            excluded_api_only += 1
            note_exclusion(
                event,
                "api_only_source",
                "API-only admin/invoice traffic is not subscription reference value",
            )
            reported = _decimal(event.get("cost_usd"))
            if reported is not None:
                summary.add_reported_charge(reported)
            continue

        if is_noncomparable(event):
            excluded_scope += 1
            note_exclusion(
                event,
                "incompatible_scope",
                "Conflicting or mixed scope cannot safely join this plan comparison",
            )
            continue

        if is_charge_only(event):
            excluded_charge_only += 1
            note_exclusion(
                event,
                "charge_only",
                "Provider-reported charge without measured reference components",
            )
            reported = _decimal(event.get("cost_usd"))
            if reported is not None:
                summary.add_reported_charge(reported)
            continue

        amount = resolve_event_reference_usd(event, pricing=pricing)
        incomplete = event.get("telemetry_complete") is False
        if amount is None:
            summary.add_unpriced(tokens, model=_model_key(event), complete=not incomplete)
            stats = unpriced_model_stats.setdefault(
                _model_key(event), {"events": 0, "tokens": 0}
            )
            stats["events"] += 1
            stats["tokens"] += tokens
            continue

        summary.add_priced(amount, tokens, event=True)
        if incomplete:
            summary.incomplete_telemetry_events += 1
        model = _model_key(event)
        if model not in priced_model_refs:
            priced_model_refs.append(model)

    for raw in unpriced_events or ():
        event = raw if isinstance(raw, Mapping) else _as_mapping(raw)
        tokens = measured_tokens(event)
        tool_tokens[_tool_key(event)] = tool_tokens.get(_tool_key(event), 0) + tokens
        summary.measured_tokens += tokens
        summary.events += 1

        if is_api_only_source(event.get("source")):
            excluded_api_only += 1
            note_exclusion(
                event,
                "api_only_source",
                "API-only admin/invoice traffic is not subscription reference value",
            )
            continue
        if is_noncomparable(event):
            excluded_scope += 1
            note_exclusion(
                event,
                "incompatible_scope",
                "Conflicting or mixed scope cannot safely join this plan comparison",
            )
            continue

        incomplete = event.get("telemetry_complete") is False
        summary.add_unpriced(tokens, model=_model_key(event), complete=not incomplete)
        stats = unpriced_model_stats.setdefault(_model_key(event), {"events": 0, "tokens": 0})
        stats["events"] += 1
        stats["tokens"] += tokens

    return {
        "summary": summary,
        "excluded_api_only": excluded_api_only,
        "excluded_charge_only": excluded_charge_only,
        "excluded_scope": excluded_scope,
        "exclusion_notes": exclusion_notes,
        "tool_tokens": tool_tokens,
        "unpriced_model_stats": unpriced_model_stats,
        "priced_model_refs": priced_model_refs,
    }


def _usage_status(summary: ValueSummary, *, excluded_total: int) -> tuple[str, Decimal | None]:
    """Map accumulator state onto Plans & Value usage statuses."""
    comparable_events = summary.priced_events + summary.unpriced_events
    if comparable_events == 0 and summary.events == 0 and excluded_total == 0:
        return STATUS_NO_RECORDS, None
    if comparable_events == 0:
        # Exclusions only, or nothing defensible.
        return STATUS_NO_RECORDS if summary.events == 0 else STATUS_UNAVAILABLE, None

    known = summary.priced_value if summary.priced_events > 0 else None
    if known is None:
        return STATUS_UNAVAILABLE, None

    complete = summary.complete
    if complete and known == ZERO:
        return STATUS_MEASURED_ZERO, ZERO
    if complete:
        return STATUS_COMPLETE, known
    return STATUS_LOWER_BOUND, known


def _multiple_payload(
    *,
    usage_value: Decimal | None,
    usage_status: str,
    accrued_cost: Decimal,
    summary: ValueSummary,
    excluded_scope: int,
) -> dict:
    if excluded_scope > 0 and usage_value is None and summary.priced_events == 0:
        # Pure incompatible scope with no defensible numerator.
        return {
            "multiple": None,
            "multipleBasis": "unavailable",
            "multipleReasonCode": REASON_INCOMPATIBLE_SCOPE,
            "multipleReason": (
                "Incompatible or conflicting scope prevents a reference-value multiple"
            ),
        }

    if accrued_cost <= ZERO:
        return {
            "multiple": None,
            "multipleBasis": "unavailable",
            "multipleReasonCode": REASON_ZERO_COST,
            "multipleReason": "No positive configured accrued group cost for this interval",
        }

    if usage_status == STATUS_NO_RECORDS:
        return {
            "multiple": None,
            "multipleBasis": "unavailable",
            "multipleReasonCode": REASON_NO_RECORDS,
            "multipleReason": "No recorded usage in this interval",
        }

    if usage_value is None:
        return {
            "multiple": None,
            "multipleBasis": "unavailable",
            "multipleReasonCode": REASON_NO_PRICED_VALUE,
            "multipleReason": "No defensible reference value for recorded usage",
        }

    verdict = ratio_with_coverage(summary, accrued_cost, denominator_complete=True)
    # Genuine measured-zero is a ratio of 0, not an invalid 0x placeholder.
    if usage_status == STATUS_MEASURED_ZERO:
        return {
            "multiple": 0.0,
            "multipleBasis": "ratio",
            "multipleReasonCode": None,
            "multipleReason": (
                "Reference-value multiple = 0 / configured cost "
                "(measured zero reference value over positive accrued cost)"
            ),
        }

    if usage_status == STATUS_LOWER_BOUND and usage_value > ZERO:
        return {
            "multiple": float(usage_value / accrued_cost),
            "multipleBasis": "lower_bound",
            "multipleReasonCode": None,
            "multipleReason": (
                "Qualified lower-bound Reference-value multiple: known priced subtotal "
                "over complete positive configured group cost"
            ),
        }

    if verdict.get("basis") == "ratio" and verdict.get("value") is not None:
        return {
            "multiple": float(verdict["value"]),
            "multipleBasis": "ratio",
            "multipleReasonCode": None,
            "multipleReason": (
                "Reference-value multiple = recorded reference value / "
                "positive configured accrued group cost"
            ),
        }

    return {
        "multiple": None,
        "multipleBasis": "unavailable",
        "multipleReasonCode": REASON_UNAVAILABLE,
        "multipleReason": str(verdict.get("reason") or "Reference-value multiple unavailable"),
    }


def _pricing_coverage_payload(
    *,
    summary: ValueSummary,
    usage_status: str,
    unpriced_model_stats: dict[str, dict[str, int]],
    excluded_api_only: int,
    excluded_charge_only: int,
    excluded_scope: int,
) -> dict:
    unpriced_models = [
        {
            "modelKey": model,
            "events": stats["events"],
            "tokens": stats["tokens"],
        }
        for model, stats in sorted(unpriced_model_stats.items())
    ]
    if usage_status == STATUS_NO_RECORDS:
        status = "no_records"
        label = "No recorded usage"
    elif usage_status == STATUS_UNAVAILABLE:
        status = "unavailable"
        label = (
            f"Amount unavailable ({summary.unpriced_events} unpriced event(s))"
            if summary.unpriced_events
            else "Amount unavailable"
        )
    elif usage_status == STATUS_LOWER_BOUND:
        status = "partial"
        parts = []
        if summary.unpriced_events:
            parts.append(
                f"{len(unpriced_models)} unpriced model(s)"
                if unpriced_models
                else f"{summary.unpriced_events} unpriced event(s)"
            )
        if summary.incomplete_telemetry_events:
            parts.append(f"{summary.incomplete_telemetry_events} incomplete telemetry")
        label = "Partial (" + ", ".join(parts) + ")" if parts else "Partial"
    elif usage_status in {STATUS_COMPLETE, STATUS_MEASURED_ZERO}:
        status = "complete"
        label = f"Complete ({summary.priced_events} events)"
    else:
        status = "unavailable"
        label = "Pricing coverage unavailable"

    return {
        "status": status,
        "label": label,
        "pricedEvents": summary.priced_events,
        "unpricedEvents": summary.unpriced_events,
        "pricedTokens": summary.priced_tokens,
        "unpricedTokens": summary.unpriced_tokens,
        "unpricedModels": unpriced_models,
        "incompleteTelemetryEvents": summary.incomplete_telemetry_events,
        "excludedApiOnlyEvents": excluded_api_only,
        "excludedChargeOnlyEvents": excluded_charge_only,
        "excludedScopeEvents": excluded_scope,
        "note": (
            "Pricing coverage describes RECORDED usage only; it does not claim "
            "complete account or device capture."
        ),
    }


def assemble_group_valuation(
    group: Any,
    events: list[dict] | None,
    unpriced_events: list[dict] | None,
    source_health: list[dict] | None,
    *,
    pricing: PricingEngine | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Build one plan/group comparison row with explanation data."""
    group_map = group if isinstance(group, Mapping) else _as_mapping(group)
    group_id = _get(group_map, "group_id", "groupId", default="unknown")
    name = _get(group_map, "name", default=str(group_id))
    tool_keys = tuple(
        _get(group_map, "tool_keys", "toolKeys", default=()) or ()
    )
    plan_ids = tuple(_get(group_map, "plan_ids", "planIds", default=()) or ())
    plans = _get(group_map, "plans", default=[]) or []
    active_intervals = _get(group_map, "active_intervals", "activeIntervals", default=[]) or []
    accrued = _decimal(_get(group_map, "accrued_cost", "accruedCostUsd", "configuredCostUsd"))
    if accrued is None:
        accrued = ZERO
    if accrued < ZERO:
        raise ValueError("configured accrued cost must be nonnegative")

    accumulated = _accumulate_events(
        events=list(events or []),
        unpriced_events=list(unpriced_events or []),
        pricing=pricing,
    )
    summary: ValueSummary = accumulated["summary"]
    excluded_total = (
        accumulated["excluded_api_only"]
        + accumulated["excluded_charge_only"]
        + accumulated["excluded_scope"]
    )
    usage_status, usage_value = _usage_status(summary, excluded_total=excluded_total)
    multiple = _multiple_payload(
        usage_value=usage_value,
        usage_status=usage_status,
        accrued_cost=accrued,
        summary=summary,
        excluded_scope=accumulated["excluded_scope"],
    )
    collection = _collection_evidence(source_health, tool_keys=tool_keys, as_of=as_of)
    coverage = _pricing_coverage_payload(
        summary=summary,
        usage_status=usage_status,
        unpriced_model_stats=accumulated["unpriced_model_stats"],
        excluded_api_only=accumulated["excluded_api_only"],
        excluded_charge_only=accumulated["excluded_charge_only"],
        excluded_scope=accumulated["excluded_scope"],
    )
    plan_rows = _plan_payload(plans)
    interval_rows = _interval_payload(active_intervals)

    if usage_status == STATUS_NO_RECORDS:
        usage_label = "No recorded usage"
    elif usage_status == STATUS_UNAVAILABLE:
        usage_label = "Unavailable"
    elif usage_status == STATUS_LOWER_BOUND:
        usage_label = f"≥ {_money_str(usage_value)}"
    elif usage_status == STATUS_MEASURED_ZERO:
        usage_label = "0"
    else:
        usage_label = _money_str(usage_value)

    association_basis = (
        _get(group_map, "association_basis", "associationBasis")
        or "configured_tool_association"
    )

    cost_formula_parts = []
    for plan in plan_rows:
        if plan.get("accruedCostUsd") is not None:
            cost_formula_parts.append(f"{plan.get('name') or plan.get('planId')}={plan['accruedCostUsd']}")
    cost_formula = (
        " + ".join(cost_formula_parts) + f" = {_money_str(accrued)}"
        if cost_formula_parts
        else f"group accrued cost = {_money_str(accrued)}"
    )

    if multiple["multiple"] is None:
        multiple_formula = multiple["multipleReason"]
    elif multiple["multipleBasis"] == "lower_bound":
        multiple_formula = (
            f"≥ {_money_str(usage_value)} / {_money_str(accrued)} "
            f"= ≥ {multiple['multiple']:.4g}× (qualified lower bound)"
        )
    else:
        multiple_formula = (
            f"{_money_str(usage_value)} / {_money_str(accrued)} "
            f"= {multiple['multiple']:.4g}× (Reference-value multiple)"
        )

    explanation = {
        "period": {
            "activeIntervals": interval_rows,
            "note": "Usage is matched to the union of applicable active intervals for this group.",
        },
        "configuredCost": {
            "amountUsd": _money_str(accrued),
            "plans": plan_rows,
            "formula": cost_formula,
            "note": (
                "Configured cost is calendar-prorated plan expense over the same "
                "interval as usage — not verified cash."
            ),
        },
        "tools": {
            "toolKeys": list(tool_keys),
            "associationBasis": association_basis,
            "label": "Configured tool association",
            "note": (
                "A configured tool association is a comparison assumption, not proof "
                "of provider account identity. Tool name alone is not account evidence."
            ),
        },
        "referenceValue": {
            "amountUsd": _money_str(usage_value),
            "status": usage_status,
            "basis": (
                "known-subtotal"
                if usage_status == STATUS_LOWER_BOUND
                else "complete"
                if usage_status in {STATUS_COMPLETE, STATUS_MEASURED_ZERO}
                else None
            ),
            "pricedEvents": summary.priced_events,
            "unpricedEvents": summary.unpriced_events,
            "unpricedModels": coverage["unpricedModels"],
            "excluded": accumulated["exclusion_notes"],
            "note": (
                "Reference value uses measured components at effective-dated published "
                "rates. Configured expense and provider-reported charges are not added. "
                "Observed charges are not fallback reference prices."
            ),
        },
        "collection": collection,
        "multiple": {
            "label": "Reference-value multiple",
            "formula": multiple_formula,
            "value": multiple["multiple"],
            "basis": multiple["multipleBasis"],
            "reasonCode": multiple["multipleReasonCode"],
            "reason": multiple["multipleReason"],
            "note": (
                "Compares reference consumption at API rates with configured cost — "
                "not ROI, money saved, productivity, or a cancellation verdict."
            ),
        },
        "limits": {
            "pricingVsCollection": (
                "Pricing coverage is about recorded rows; collection evidence is about "
                "whether history was captured. Healthy polls do not prove historical completeness."
            ),
            "freshness": collection.get("label"),
        },
    }

    return {
        "groupId": group_id,
        "name": name,
        "toolKeys": list(tool_keys),
        "planIds": list(plan_ids),
        "plans": plan_rows,
        "activeIntervals": interval_rows,
        "configuredCostUsd": _money_str(accrued),
        "usageValueUsd": _money_str(usage_value),
        "usageValueStatus": usage_status,
        "usageValueLabel": usage_label,
        "usageValueBasis": explanation["referenceValue"]["basis"],
        "multiple": multiple["multiple"],
        "multipleBasis": multiple["multipleBasis"],
        "multipleReasonCode": multiple["multipleReasonCode"],
        "multipleReason": multiple["multipleReason"],
        "pricingCoverage": coverage,
        "collectionEvidence": collection,
        "attribution": {
            "basis": association_basis,
            "label": "Configured tool association",
            "note": explanation["tools"]["note"],
        },
        "events": summary.events,
        "tokens": summary.measured_tokens,
        "pricedTokens": summary.priced_tokens,
        "unpricedTokens": summary.unpriced_tokens,
        "toolBreakdown": [
            {"toolKey": key, "tokens": tokens}
            for key, tokens in sorted(accumulated["tool_tokens"].items())
        ],
        "reportedChargesUsd": _money_str(summary.reported_charges)
        if summary.reported_charge_events
        else None,
        "explanation": explanation,
    }


def assemble_unassigned_summary(
    unassigned_events: list[dict] | None,
    unassigned_unpriced: list[dict] | None,
    *,
    pricing: PricingEngine | None = None,
) -> dict:
    """Summarize usage outside any safe plan/group association."""
    accumulated = _accumulate_events(
        events=list(unassigned_events or []),
        unpriced_events=list(unassigned_unpriced or []),
        pricing=pricing,
    )
    summary: ValueSummary = accumulated["summary"]
    excluded_total = (
        accumulated["excluded_api_only"]
        + accumulated["excluded_charge_only"]
        + accumulated["excluded_scope"]
    )
    usage_status, usage_value = _usage_status(summary, excluded_total=excluded_total)
    coverage = _pricing_coverage_payload(
        summary=summary,
        usage_status=usage_status,
        unpriced_model_stats=accumulated["unpriced_model_stats"],
        excluded_api_only=accumulated["excluded_api_only"],
        excluded_charge_only=accumulated["excluded_charge_only"],
        excluded_scope=accumulated["excluded_scope"],
    )
    explanation = {
        "title": "Unassigned / no configured plan",
        "note": (
            "Usage without an applicable plan or safe association. "
            "No configured cost or Reference-value multiple is invented for this bucket."
        ),
        "referenceValue": {
            "amountUsd": _money_str(usage_value),
            "status": usage_status,
            "excluded": accumulated["exclusion_notes"],
        },
        "pricingCoverage": coverage,
        "tools": [
            {"toolKey": key, "tokens": tokens}
            for key, tokens in sorted(accumulated["tool_tokens"].items())
        ],
    }
    return {
        "usageValueUsd": _money_str(usage_value),
        "usageValueStatus": usage_status,
        "events": summary.events,
        "tokens": summary.measured_tokens,
        "pricedTokens": summary.priced_tokens,
        "unpricedTokens": summary.unpriced_tokens,
        "toolBreakdown": explanation["tools"],
        "pricingCoverage": coverage,
        "excludedApiOnlyEvents": accumulated["excluded_api_only"],
        "excludedChargeOnlyEvents": accumulated["excluded_charge_only"],
        "excludedScopeEvents": accumulated["excluded_scope"],
        "explanation": explanation,
    }


def assemble_plans_value_report(
    period_key: str,
    as_of: datetime,
    zone: ZoneInfo,
    groups_data: list[dict],
    unassigned_data: dict,
    sources_data: list[dict],
    from_utc: datetime,
    to_utc: datetime,
) -> dict:
    """Assemble the Plans & Value JSON response envelope."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    if from_utc.tzinfo is None:
        from_utc = from_utc.replace(tzinfo=UTC)
    if to_utc.tzinfo is None:
        to_utc = to_utc.replace(tzinfo=UTC)

    local_from = from_utc.astimezone(zone).date()
    local_to_exclusive = to_utc.astimezone(zone).date()
    # Half-open local end: last included local calendar date is the day before
    # end when end is local midnight; otherwise the end's local date.
    if (
        to_utc.astimezone(zone).timetz().hour == 0
        and to_utc.astimezone(zone).timetz().minute == 0
        and to_utc.astimezone(zone).timetz().second == 0
        and to_utc.astimezone(zone).timetz().microsecond == 0
    ):
        from datetime import timedelta

        local_end = local_to_exclusive - timedelta(days=1)
    else:
        local_end = local_to_exclusive

    return {
        "schemaVersion": SCHEMA_VERSION,
        "period": period_key,
        "asOf": _iso(as_of),
        "fromUtc": _iso(from_utc),
        "toUtc": _iso(to_utc),
        "localFrom": local_from.isoformat(),
        "localTo": local_end.isoformat(),
        "timezone": getattr(zone, "key", str(zone)),
        "generatedAt": _iso(datetime.now(UTC)),
        "groups": list(groups_data or []),
        "unassigned": unassigned_data or assemble_unassigned_summary([], []),
        "sources": list(sources_data or []),
        "labels": {
            "multiple": "Reference-value multiple",
            "usageValue": "Recorded API-equivalent usage value",
            "configuredCost": "Configured cost accrued",
        },
        "notes": [
            "Reference value excludes configured expense and provider-reported charges.",
            "Pricing coverage is separate from collection evidence.",
            "Tool association is a comparison assumption, not account proof.",
        ],
    }
