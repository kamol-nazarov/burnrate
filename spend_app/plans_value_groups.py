"""Pure plan grouping and active-interval unions for Plans & Value.

Groups plans that share inseparable telemetry (same canonical tool / alias
set) so configured cost can be summed while each usage event is attributed
at most once against the UNION of active intervals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc
ZERO = Decimal(0)

# Tool keys accepted on plan terms. OpenCode and ZCode share Z.AI telemetry.
SUPPORTED_TOOLS = (
    "codex",
    "claude-code",
    "cursor",
    "grok",
    "opencode",
    "zcode",
    "openrouter",
    "xai",
    "antigravity",
    "custom",
)

# Canonical association -> event tool_keys that share inseparable telemetry.
TOOL_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "opencode": ("opencode", "zcode"),
}

GROUP_DISPLAY_NAMES: dict[str, str] = {
    "opencode": "Z.AI (OpenCode / ZCode)",
    "codex": "Codex",
    "claude-code": "Claude Code",
    "cursor": "Cursor",
    "grok": "Grok",
    "openrouter": "OpenRouter",
    "xai": "xAI",
    "antigravity": "Antigravity",
    "custom": "Custom",
}


def canonical_tool_key(tool_key: str) -> str:
    """Map a stored/plan tool association to its grouping canonical key."""
    key = str(tool_key or "").strip().lower()
    if key == "zcode":
        return "opencode"
    return key


def event_tool_keys_for(canonical: str) -> tuple[str, ...]:
    """Telemetry tool_keys that belong to a canonical association group."""
    return TOOL_ALIAS_GROUPS.get(canonical, (canonical,))


def union_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge half-open ``[start, end)`` intervals into a disjoint union.

    Empty or inverted intervals (end <= start) are dropped. Touching endpoints
    merge (``[a,b)`` + ``[b,c)`` -> ``[a,c)``) so contiguous successor spans
    stay one interval without multiplying coverage.
    """
    cleaned: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if start is None or end is None:
            continue
        start_utc = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
        end_utc = end.astimezone(UTC) if end.tzinfo else end.replace(tzinfo=UTC)
        if end_utc <= start_utc:
            continue
        cleaned.append((start_utc, end_utc))
    if not cleaned:
        return []
    cleaned.sort(key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = [cleaned[0]]
    for start, end in cleaned[1:]:
        cur_start, cur_end = merged[-1]
        if start <= cur_end:
            if end > cur_end:
                merged[-1] = (cur_start, end)
        else:
            merged.append((start, end))
    return merged


def is_in_intervals(
    moment: datetime, intervals: list[tuple[datetime, datetime]]
) -> bool:
    """True when ``moment`` falls in any half-open interval ``[start, end)``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    else:
        moment = moment.astimezone(UTC)
    for start, end in intervals:
        if start <= moment < end:
            return True
    return False


def _parse_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


def term_window_utc(
    start_date: date, end_date: date | None, zone: ZoneInfo
) -> tuple[datetime, datetime | None]:
    """Inclusive local dates -> half-open UTC ``[start, end)`` (end may be open)."""
    from spend_app.plans_value_periods import term_effective_window

    return term_effective_window(start_date, end_date, zone)


def _clip_interval(
    start: datetime,
    end: datetime | None,
    period_start: datetime,
    period_end: datetime,
) -> tuple[datetime, datetime] | None:
    clipped_start = max(start, period_start)
    clipped_end = period_end if end is None else min(end, period_end)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def _accrue_term(
    amount_usd: Any,
    cadence: str,
    term_start: date,
    term_end: date | None,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
) -> Decimal:
    from spend_app.plans_value_periods import accrue_term_cost

    return accrue_term_cost(
        amount_usd, cadence, term_start, term_end, window_start, window_end, zone
    )


@dataclass
class PlanGroup:
    """One inseparable telemetry association with contributing plan costs."""

    group_id: str
    name: str
    tool_keys: tuple[str, ...]
    plan_ids: tuple[Any, ...]
    plans: list[dict] = field(default_factory=list)
    active_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    accrued_cost: Decimal = ZERO

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "tool_keys": self.tool_keys,
            "plan_ids": self.plan_ids,
            "plans": list(self.plans),
            "active_intervals": list(self.active_intervals),
            "accrued_cost": self.accrued_cost,
        }


def _plan_identity(plan: dict) -> Any:
    for key in ("plan_id", "id", "logical_plan_id"):
        if plan.get(key) is not None:
            return plan[key]
    return plan.get("name")


def _plan_display_name(plan: dict) -> str:
    name = plan.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(_plan_identity(plan))


def _group_display_name(canonical: str, contributing: list[dict]) -> str:
    if canonical in GROUP_DISPLAY_NAMES and (
        canonical == "opencode" or len(contributing) > 1
    ):
        return GROUP_DISPLAY_NAMES[canonical]
    if len(contributing) == 1:
        return _plan_display_name(contributing[0])
    if canonical in GROUP_DISPLAY_NAMES:
        return GROUP_DISPLAY_NAMES[canonical]
    names = [_plan_display_name(item) for item in contributing]
    return " + ".join(names)


def build_groups(
    plans: list[dict],
    period_start_utc: datetime,
    period_end_utc: datetime,
    zone: ZoneInfo,
) -> list[PlanGroup]:
    """Group plans by inseparable tool association over the comparison period.

    Each input plan/term dict should include at least ``tool_key``, dates, and
    either ``accrued_cost`` or enough fields to accrue
    (``amount_usd``, ``cadence``, ``start_date``, optional ``end_date``).

    Successive terms of one logical plan that change tool association land in
    different groups for their respective active windows — cost is not copied
    across groups. Multiple plans that share a canonical tool form one group
    whose ``active_intervals`` are the union of clipped term windows.
    """
    if period_start_utc.tzinfo is None:
        period_start_utc = period_start_utc.replace(tzinfo=UTC)
    else:
        period_start_utc = period_start_utc.astimezone(UTC)
    if period_end_utc.tzinfo is None:
        period_end_utc = period_end_utc.replace(tzinfo=UTC)
    else:
        period_end_utc = period_end_utc.astimezone(UTC)

    buckets: dict[str, list[dict]] = {}
    intervals_by_canonical: dict[str, list[tuple[datetime, datetime]]] = {}

    for plan in plans:
        tool_key = str(plan.get("tool_key") or "").strip().lower()
        if not tool_key:
            continue
        canonical = canonical_tool_key(tool_key)
        start_date = _parse_date(plan.get("start_date"))
        if start_date is None:
            continue
        end_date = _parse_date(plan.get("end_date"))
        term_start_utc, term_end_utc = term_window_utc(start_date, end_date, zone)
        clipped = _clip_interval(
            term_start_utc, term_end_utc, period_start_utc, period_end_utc
        )
        # Scheduled-only (no overlap with period): omit from active comparison.
        if clipped is None:
            continue

        if "accrued_cost" in plan and plan["accrued_cost"] is not None:
            cost = _as_decimal(plan["accrued_cost"])
        else:
            cost = _accrue_term(
                plan.get("amount_usd", 0),
                str(plan.get("cadence") or "monthly"),
                start_date,
                end_date,
                period_start_utc,
                period_end_utc,
                zone,
            )

        detail = {
            "plan_id": _plan_identity(plan),
            "name": _plan_display_name(plan),
            "tool_key": tool_key,
            "canonical_tool_key": canonical,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date else None,
            "active_interval": clipped,
            "accrued_cost": cost,
        }
        for extra in ("term_id", "id", "amount_usd", "cadence", "status"):
            if extra in plan and extra not in detail:
                detail[extra] = plan[extra]

        buckets.setdefault(canonical, []).append(detail)
        intervals_by_canonical.setdefault(canonical, []).append(clipped)

    groups: list[PlanGroup] = []
    for canonical in sorted(buckets.keys()):
        contributing = buckets[canonical]
        # Stable order: by plan id then start.
        contributing.sort(
            key=lambda item: (
                str(item["plan_id"]),
                item["start_date"],
                item.get("term_id") or 0,
            )
        )
        plan_ids = tuple(dict.fromkeys(item["plan_id"] for item in contributing))
        active = union_intervals(intervals_by_canonical[canonical])
        accrued = sum((item["accrued_cost"] for item in contributing), ZERO)
        groups.append(
            PlanGroup(
                group_id=f"tool:{canonical}",
                name=_group_display_name(canonical, contributing),
                tool_keys=event_tool_keys_for(canonical),
                plan_ids=plan_ids,
                plans=contributing,
                active_intervals=active,
                accrued_cost=accrued,
            )
        )
    return groups


def assign_events_to_groups(
    events: list[dict], groups: list[PlanGroup] | list[dict]
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Match each event to at most one group by tool_key and active interval.

    Returns ``(group_events_by_group_id, unassigned_events)``. An event is
    unassigned when its tool has no configured group, or ``occurred_at`` falls
    outside that group's union of active intervals.
    """
    normalized: list[tuple[str, tuple[str, ...], list[tuple[datetime, datetime]]]] = []
    by_group: dict[str, list[dict]] = {}
    for group in groups:
        if isinstance(group, PlanGroup):
            group_id = group.group_id
            tool_keys = tuple(group.tool_keys)
            intervals = list(group.active_intervals)
        else:
            group_id = str(group["group_id"])
            tool_keys = tuple(group["tool_keys"])
            intervals = list(group["active_intervals"])
        by_group[group_id] = []
        normalized.append((group_id, tool_keys, intervals))

    # Index tool_key -> candidate groups (normally one).
    by_tool: dict[str, list[tuple[str, list[tuple[datetime, datetime]]]]] = {}
    for group_id, tool_keys, intervals in normalized:
        for tool in tool_keys:
            by_tool.setdefault(tool, []).append((group_id, intervals))

    unassigned: list[dict] = []
    for event in events:
        tool = str(event.get("tool_key") or "").strip().lower()
        occurred = event.get("occurred_at")
        if not isinstance(occurred, datetime):
            unassigned.append(event)
            continue
        candidates = by_tool.get(tool) or []
        matched_id: str | None = None
        for group_id, intervals in candidates:
            if is_in_intervals(occurred, intervals):
                matched_id = group_id
                break
        if matched_id is None:
            unassigned.append(event)
        else:
            by_group[matched_id].append(event)

    return by_group, unassigned
