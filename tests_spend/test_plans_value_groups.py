"""Unit tests for Plans & Value plan grouping and interval unions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from spend_app.plans_value_groups import (
    PlanGroup,
    assign_events_to_groups,
    build_groups,
    canonical_tool_key,
    event_tool_keys_for,
    is_in_intervals,
    union_intervals,
)
from spend_app.timeutil import local_day

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def _sep_day_start(day: int) -> datetime:
    return local_day(date(2026, 9, day), NY).start


def _sep_day_end_exclusive(day: int) -> datetime:
    """Half-open end just after the inclusive local calendar day ``day``."""
    return local_day(date(2026, 9, day) + timedelta(days=1), NY).start


def test_union_intervals_merges_overlap_and_preserves_gaps():
    a = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC))
    b = (datetime(2026, 9, 4, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC))
    c = (datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 9, 22, tzinfo=UTC))
    assert union_intervals([a, b, c]) == [
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)),
        (datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 9, 22, tzinfo=UTC)),
    ]


def test_union_intervals_merges_touching_endpoints():
    left = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC))
    right = (datetime(2026, 9, 5, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC))
    assert union_intervals([right, left]) == [
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC))
    ]


def test_union_intervals_drops_empty_and_normalizes_naive():
    empty = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    naive = (datetime(2026, 9, 2), datetime(2026, 9, 3))
    assert union_intervals([empty, naive]) == [
        (datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))
    ]


def test_is_in_intervals_half_open():
    intervals = [
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC)),
    ]
    assert is_in_intervals(datetime(2026, 9, 1, tzinfo=UTC), intervals)
    assert is_in_intervals(datetime(2026, 9, 4, 23, tzinfo=UTC), intervals)
    assert not is_in_intervals(datetime(2026, 9, 5, tzinfo=UTC), intervals)
    assert not is_in_intervals(datetime(2026, 8, 31, tzinfo=UTC), intervals)


def test_opencode_zcode_share_canonical_association():
    assert canonical_tool_key("zcode") == "opencode"
    assert canonical_tool_key("opencode") == "opencode"
    assert event_tool_keys_for("opencode") == ("opencode", "zcode")
    assert event_tool_keys_for("codex") == ("codex",)


def test_case_3_inseparable_plans_sum_cost_share_usage_once():
    """Case 3: two plans on same tool -> one $60 group; usage not doubled."""
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(9)  # through Sep 8 local
    plans = [
        {
            "id": 1,
            "name": "Codex Plus",
            "tool_key": "codex",
            "amount_usd": "200",
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": None,
            "accrued_cost": Decimal("40"),
        },
        {
            "id": 2,
            "name": "Codex Seat",
            "tool_key": "codex",
            "amount_usd": "100",
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": None,
            "accrued_cost": Decimal("20"),
        },
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert len(groups) == 1
    group = groups[0]
    assert group.group_id == "tool:codex"
    assert group.tool_keys == ("codex",)
    assert group.plan_ids == (1, 2)
    assert group.accrued_cost == Decimal("60")
    assert [p["accrued_cost"] for p in group.plans] == [Decimal("40"), Decimal("20")]

    events = [
        {
            "raw_id": f"e{i}",
            "tool_key": "codex",
            "occurred_at": datetime(2026, 9, 3, 12, tzinfo=UTC),
            "usage_value": Decimal("10"),
        }
        for i in range(9)
    ]  # $90 of reference usage as nine $10 events
    by_group, unassigned = assign_events_to_groups(events, groups)
    assert unassigned == []
    matched = by_group["tool:codex"]
    assert len(matched) == 9
    usage = sum((e["usage_value"] for e in matched), Decimal(0))
    assert usage == Decimal("90")
    # Child plans keep their own costs; group usage is not attributed per child.
    assert sum(p["accrued_cost"] for p in group.plans) == group.accrued_cost
    assert usage / group.accrued_cost == Decimal("1.5")


def test_case_4_one_opencode_zcode_plan_cost_once_events_from_both_tools():
    """Case 4: one Z.AI plan; OpenCode and ZCode events each counted once."""
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(9)
    plans = [
        {
            "plan_id": 10,
            "name": "Z.AI GLM",
            "tool_key": "opencode",
            "amount_usd": "300",
            "cadence": "monthly",
            "start_date": date(2026, 9, 1),
            "end_date": None,
            "accrued_cost": Decimal("80"),
        }
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert len(groups) == 1
    group = groups[0]
    assert group.group_id == "tool:opencode"
    assert group.name == "Z.AI (OpenCode / ZCode)"
    assert group.tool_keys == ("opencode", "zcode")
    assert group.plan_ids == (10,)
    assert group.accrued_cost == Decimal("80")

    events = [
        {
            "raw_id": "oc1",
            "tool_key": "opencode",
            "occurred_at": datetime(2026, 9, 2, 15, tzinfo=UTC),
        },
        {
            "raw_id": "zc1",
            "tool_key": "zcode",
            "occurred_at": datetime(2026, 9, 3, 15, tzinfo=UTC),
        },
        {
            "raw_id": "oc2",
            "tool_key": "opencode",
            "occurred_at": datetime(2026, 9, 4, 15, tzinfo=UTC),
        },
    ]
    by_group, unassigned = assign_events_to_groups(events, groups)
    assert unassigned == []
    assert {e["raw_id"] for e in by_group["tool:opencode"]} == {"oc1", "zc1", "oc2"}
    assert len(by_group["tool:opencode"]) == 3


def test_case_4_zcode_stored_tool_key_still_canonicalizes():
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(5)
    plans = [
        {
            "id": 11,
            "name": "ZCode seat",
            "tool_key": "zcode",
            "start_date": "2026-09-01",
            "end_date": "2026-09-04",
            "accrued_cost": Decimal("10"),
        }
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert groups[0].group_id == "tool:opencode"
    assert groups[0].tool_keys == ("opencode", "zcode")


def test_case_5_overlapping_plans_union_without_multiplying_events():
    """Case 5: Plan A Sep1–5 + Plan B Sep4–10 => union Sep1–10; events once."""
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(11)
    plans = [
        {
            "id": "A",
            "name": "Cursor A",
            "tool_key": "cursor",
            "start_date": "2026-09-01",
            "end_date": "2026-09-05",
            "accrued_cost": Decimal("25"),
        },
        {
            "id": "B",
            "name": "Cursor B",
            "tool_key": "cursor",
            "start_date": "2026-09-04",
            "end_date": "2026-09-10",
            "accrued_cost": Decimal("35"),
        },
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert len(groups) == 1
    group = groups[0]
    assert group.accrued_cost == Decimal("60")
    # Union of inclusive Sep 1–5 and Sep 4–10 is Sep 1–10 local.
    assert group.active_intervals == [(_sep_day_start(1), _sep_day_end_exclusive(10))]

    overlap_event = {
        "raw_id": "overlap",
        "tool_key": "cursor",
        "occurred_at": local_day(date(2026, 9, 4), NY).start + timedelta(hours=12),
    }
    early = {
        "raw_id": "early",
        "tool_key": "cursor",
        "occurred_at": local_day(date(2026, 9, 2), NY).start + timedelta(hours=1),
    }
    late = {
        "raw_id": "late",
        "tool_key": "cursor",
        "occurred_at": local_day(date(2026, 9, 10), NY).start + timedelta(hours=1),
    }
    after = {
        "raw_id": "after",
        "tool_key": "cursor",
        "occurred_at": _sep_day_end_exclusive(10),
    }
    by_group, unassigned = assign_events_to_groups(
        [overlap_event, early, late, after], groups
    )
    assert {e["raw_id"] for e in by_group["tool:cursor"]} == {"overlap", "early", "late"}
    assert [e["raw_id"] for e in unassigned] == ["after"]


def test_case_5_tool_association_change_preserves_disjoint_cost():
    """Same logical plan changing tools: cost stays in each group's window."""
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(11)
    plans = [
        {
            "plan_id": 7,
            "term_id": 1,
            "name": "Seat",
            "tool_key": "codex",
            "start_date": "2026-09-01",
            "end_date": "2026-09-05",
            "accrued_cost": Decimal("40"),
        },
        {
            "plan_id": 7,
            "term_id": 2,
            "name": "Seat",
            "tool_key": "claude-code",
            "start_date": "2026-09-06",
            "end_date": "2026-09-10",
            "accrued_cost": Decimal("50"),
        },
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert {g.group_id for g in groups} == {"tool:codex", "tool:claude-code"}
    by_id = {g.group_id: g for g in groups}
    assert by_id["tool:codex"].accrued_cost == Decimal("40")
    assert by_id["tool:claude-code"].accrued_cost == Decimal("50")
    # Cost for plan 7 is not duplicated across groups.
    assert by_id["tool:codex"].plan_ids == (7,)
    assert by_id["tool:claude-code"].plan_ids == (7,)
    assert sum(g.accrued_cost for g in groups) == Decimal("90")

    mid_codex = {
        "raw_id": "c",
        "tool_key": "codex",
        "occurred_at": local_day(date(2026, 9, 3), NY).start,
    }
    mid_claude = {
        "raw_id": "a",
        "tool_key": "claude-code",
        "occurred_at": local_day(date(2026, 9, 7), NY).start,
    }
    wrong_window = {
        "raw_id": "w",
        "tool_key": "codex",
        "occurred_at": local_day(date(2026, 9, 7), NY).start,
    }
    by_group, unassigned = assign_events_to_groups(
        [mid_codex, mid_claude, wrong_window], groups
    )
    assert [e["raw_id"] for e in by_group["tool:codex"]] == ["c"]
    assert [e["raw_id"] for e in by_group["tool:claude-code"]] == ["a"]
    assert [e["raw_id"] for e in unassigned] == ["w"]


def test_case_5_ended_and_scheduled_plans_and_out_of_interval_unassigned():
    period_start = _sep_day_start(1)
    period_end = _sep_day_start(9)  # this_month through Sep 8
    plans = [
        {
            "id": 1,
            "name": "Ended Grok",
            "tool_key": "grok",
            "start_date": "2026-08-01",
            "end_date": "2026-09-03",
            "accrued_cost": Decimal("15"),
            "status": "ended",
        },
        {
            "id": 2,
            "name": "Future Grok",
            "tool_key": "grok",
            "start_date": "2026-09-20",
            "end_date": None,
            "accrued_cost": Decimal("99"),
            "status": "scheduled",
        },
        {
            "id": 3,
            "name": "Active Cursor",
            "tool_key": "cursor",
            "start_date": "2026-09-01",
            "end_date": None,
            "accrued_cost": Decimal("40"),
        },
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    ids = {g.group_id for g in groups}
    # Scheduled-only future plan does not create a comparison group.
    assert "tool:grok" in ids
    assert "tool:cursor" in ids
    grok = next(g for g in groups if g.group_id == "tool:grok")
    assert grok.plan_ids == (1,)
    assert grok.accrued_cost == Decimal("15")
    assert grok.active_intervals == [(_sep_day_start(1), _sep_day_end_exclusive(3))]

    events = [
        {
            "raw_id": "in",
            "tool_key": "grok",
            "occurred_at": local_day(date(2026, 9, 2), NY).start,
        },
        {
            "raw_id": "out",
            "tool_key": "grok",
            "occurred_at": local_day(date(2026, 9, 5), NY).start,
        },
        {
            "raw_id": "orphan",
            "tool_key": "antigravity",
            "occurred_at": local_day(date(2026, 9, 2), NY).start,
        },
    ]
    by_group, unassigned = assign_events_to_groups(events, groups)
    assert [e["raw_id"] for e in by_group["tool:grok"]] == ["in"]
    assert {e["raw_id"] for e in unassigned} == {"out", "orphan"}


def test_assign_events_accepts_dict_shaped_groups():
    group = {
        "group_id": "tool:xai",
        "tool_keys": ("xai",),
        "active_intervals": [
            (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC))
        ],
    }
    events = [
        {
            "raw_id": "1",
            "tool_key": "xai",
            "occurred_at": datetime(2026, 9, 2, tzinfo=UTC),
        },
        {
            "raw_id": "2",
            "tool_key": "xai",
            "occurred_at": datetime(2026, 9, 11, tzinfo=UTC),
        },
    ]
    by_group, unassigned = assign_events_to_groups(events, [group])
    assert [e["raw_id"] for e in by_group["tool:xai"]] == ["1"]
    assert [e["raw_id"] for e in unassigned] == ["2"]


def test_build_groups_accrues_when_cost_omitted():
    period_start = _sep_day_start(1)
    # asOf Sep 9 00:00 NY == eight complete days Sep 1–8
    period_end = datetime(2026, 9, 9, 4, tzinfo=UTC)
    plans = [
        {
            "id": 1,
            "name": "Codex",
            "tool_key": "codex",
            "amount_usd": "300",
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": None,
        }
    ]
    groups = build_groups(plans, period_start, period_end, NY)
    assert len(groups) == 1
    # 8 * (300/30) = 80
    assert groups[0].accrued_cost == Decimal("80")


def test_plan_group_as_dict_round_trip_fields():
    group = PlanGroup(
        group_id="tool:custom",
        name="Custom",
        tool_keys=("custom",),
        plan_ids=(1,),
        plans=[{"plan_id": 1, "accrued_cost": Decimal("1")}],
        active_intervals=[
            (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))
        ],
        accrued_cost=Decimal("1"),
    )
    payload = group.as_dict()
    assert payload["group_id"] == "tool:custom"
    assert payload["accrued_cost"] == Decimal("1")
    assert payload["tool_keys"] == ("custom",)
