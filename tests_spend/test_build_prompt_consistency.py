"""The 13 non-negotiable internal-consistency gates from the original prompt."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from tests_spend.test_build_prompt_harness import (
    cents,
    client_for,
    complete_world,
    sources,
    usd_js,
)


def _summary(client, window: str = "1d") -> dict:
    response = client.get("/api/spend/summary", params={"window": window, "tool": "all"})
    assert response.status_code == 200
    return response.json()


def test_c1_itemized_leaks_equal_waste_headline_and_day_times_30_equals_month(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    items = body["waste"]["items"]
    item_sum = sum((cents(item["perDay"]) for item in items), Decimal(0))
    per_day = cents(body["waste"]["perDay"])
    per_month = cents(body["waste"]["perMonth"])
    assert item_sum == per_day
    assert per_day * Decimal(30) == per_month
    html, _css, js = sources()
    assert 'id="waste-headline"' in html
    assert "waste.perDay" in js or 'ease("wasteDay"' in js
    assert "items.map" in js or "waste-items" in js


def test_c2_chart_bucket_totals_equal_measured_tokens_kpi(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    series_total = sum(Decimal(str(point["total"])) for point in body["series"])
    assert abs(float(series_total) - body["totals"]["tokens"]) < 1e-6
    for tool in body["tools"]:
        tool_series = sum(
            Decimal(str(point["byTool"].get(tool["key"], 0))) for point in body["series"]
        )
        assert abs(float(tool_series) - tool["tokens"]) < 1e-6
    _html, _css, js = sources()
    assert "ease(\"tokens\"" in js or "ease('tokens'" in js
    assert "hover-value" in js


def test_c3_per_tool_tokens_and_value_plus_subscriptions_equal_totals(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    assert sum(tool["tokens"] for tool in body["tools"]) == body["totals"]["tokens"]
    usage = sum(
        (Decimal(str(tool["value"])) for tool in body["tools"] if tool["value"] is not None),
        Decimal(0),
    )
    tracked = Decimal(str(body["totals"]["trackedValue"]))
    priced = Decimal(str(body["totals"]["priced"]))
    published = Decimal(str(body["totals"]["publishedRate"]))
    subscriptions = tracked - priced - published
    assert abs(float(usage - priced - published)) < 1e-4
    assert subscriptions >= 0
    assert abs(float(priced + published + subscriptions) - float(tracked)) < 1e-6
    assert any(row["monthlyEquivalent"] for row in body["subscriptions"] if row["monthlyEquivalent"])


def test_c4_token_mix_shares_sum_to_100_on_overview_and_every_drilldown(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    assert {item["key"] for item in body["mix"]} == {"cached_input", "fresh_input", "output"}
    assert abs(sum(item["share"] for item in body["mix"]) - 100) <= 0.1
    assert sum(item["tokens"] for item in body["mix"]) == body["totals"]["tokens"]
    for model in body["models"]:
        entity = client.get(
            "/api/spend/entity",
            params={"kind": "model", "key": model["key"], "window": "1d"},
        ).json()
        assert abs(sum(item["share"] for item in entity["mix"]) - 100) <= 0.1
        assert {item["key"] for item in entity["mix"]} == {"cached_input", "fresh_input", "output"}
    for tool in body["tools"]:
        entity = client.get(
            "/api/spend/entity",
            params={"kind": "tool", "key": tool["key"], "window": "1d"},
        ).json()
        if entity["tokens"]:
            assert abs(sum(item["share"] for item in entity["mix"]) - 100) <= 0.1


def test_c5_drilldown_share_numerator_is_the_entity_kpi(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    assert entity["value"] is not None
    tracked = body["totals"]["trackedValue"]
    assert tracked
    expected_share = entity["value"] / tracked * 100
    assert abs(entity["shareOfTrackedValue"] - expected_share) < 1e-6
    _html, _css, js = sources()
    assert "shareOfTrackedValue" in js
    assert "of tracked value" in js


def test_c6_session_rows_sum_below_entity_total_and_count_is_min_of_six_and_runs(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    rows = entity["sessions"]["rows"]
    assert entity["runs"] >= 2
    assert len(rows) == min(6, entity["runs"])
    assert len(rows) != 6 or entity["runs"] >= 6
    shown = sum(Decimal(str(row["value"])) for row in rows if row["value"] is not None)
    assert abs(float(shown) - entity["sessions"]["shownTotal"]) < 1e-6
    assert entity["sessions"]["shownTotal"] < entity["value"]
    _html, _css, js = sources()
    assert "shownTotal" in js
    assert "min(6" in js or "Top " in js
    assert "$X of $Y" not in js
    assert " of " in js and "sessions-foot" in js


def test_c7_supporting_notes_are_bound_to_eased_values_not_literal_strings() -> None:
    _html, _css, js = sources()
    assert "function paintComposite()" in js
    assert "function capacityNote(row)" in js
    assert "resets Sep" not in js
    assert "0 of 30,000" not in js
    assert "tick:" not in js
    assert "bias(" not in js
    composite = js.split("function paintComposite()", 1)[1].split("function tickEase()", 1)[0]
    assert "live(\"tracked\")" in composite or "live('tracked')" in composite
    assert "wasteDay" in composite
    assert "usd(priced)" in composite and "usd(published)" in composite
    note_fn = js.split("function capacityNote(row)", 1)[1].split("function sortQuotaRows", 1)[0]
    assert "row.allowance" in note_fn
    assert "row.pct" in note_fn
    assert "row.used" in note_fn
    assert "formatReset(row.resetsAt)" in note_fn


def test_c8_already_saved_and_recoverable_share_one_rate_basis(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "cursor:grok-4.6", "window": "1d"},
    ).json()
    assert body["cacheSavings"] is not None
    assert entity["opportunity"]["alreadySaved"] is not None
    cache_items = [item for item in body["waste"]["items"] if "cache" in item["key"]]
    assert cache_items
    _html, _css, js = sources()
    assert "0.62" not in js
    assert "cacheSavings" in js
    assert "alreadySaved" in js or "opp-saved" in js


def test_c9_record_counts_tokens_and_shares_change_with_the_window(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    short = _summary(client, "15m")
    day = _summary(client, "1d")
    week = _summary(client, "1w")
    assert short["totals"]["records"] < day["totals"]["records"] < week["totals"]["records"]
    assert short["totals"]["tokens"] < day["totals"]["tokens"] < week["totals"]["tokens"]
    assert short["window"]["buckets"] == 15
    assert day["window"]["buckets"] == 24
    assert week["window"]["buckets"] == 28

    def shares(payload: dict) -> dict[str, float]:
        total = payload["totals"]["tokens"]
        return {tool["key"]: tool["tokens"] / total for tool in payload["tools"] if total}

    assert shares(short) != shares(day)
    assert shares(day) != shares(week)
    _html, _css, js = sources()
    assert "number(totals.records)" in js or "totals.records" in js


def test_c10_one_money_format_per_scale_and_approx_only_on_published_rate_figures() -> None:
    assert usd_js(9.41) == "$9.41"
    assert usd_js(14) == "$14.00"
    assert usd_js(14) != "$14"
    assert usd_js(2323) == "$2,323"
    _html, css, js = sources()
    assert re.search(r"n >= 1000 \? \"\$\" \+ .*toFixed\(2\)", js.replace("\n", " ")) or (
        "n >= 1000" in js and "toFixed(2)" in js
    )
    usd_fn = js.split("function usd(value)", 1)[1].split("function markedUsd", 1)[0]
    assert "toFixed(2)" in usd_fn
    marked = js.split("function markedUsd(value, exact)", 1)[1].split("function tokens", 1)[0]
    assert "exact === false" in marked
    assert "≈" in marked
    assert "exact === true" not in marked or "exact !== false" in marked
    # Exact-rate tools never receive the marker from isExact true.
    assert "tool.isExact === false" in js
    assert "model.isExact === false" in js


def test_c11_reingest_gate_is_owned_by_the_replay_module() -> None:
    # Presence check only — executable replay lives in test_build_prompt_ingest_replay.py.
    from tests_spend import test_build_prompt_ingest_replay as replay

    assert hasattr(replay, "test_codex_local_replay_writes_zero_new_rows")
    assert hasattr(replay, "test_claude_local_replay_writes_zero_new_rows")
    assert hasattr(replay, "test_cursor_csv_replay_writes_zero_new_rows")
    assert hasattr(replay, "test_openai_admin_fixture_replay_writes_zero_new_rows")


def test_c12_animation_fill_and_resting_size_owned_by_motion_module() -> None:
    from tests_spend import test_build_prompt_motion as motion

    assert hasattr(motion, "test_animation_fill_mode_both_is_forbidden")
    assert hasattr(motion, "test_looping_effects_never_animate_height_or_width")
    assert hasattr(motion, "test_one_shot_data_animations_rest_at_their_final_size")


def test_c13_responsive_no_scroll_owned_by_responsive_module() -> None:
    from tests_spend import test_build_prompt_responsive as responsive

    assert hasattr(responsive, "test_no_horizontal_page_scroll_rules_at_required_widths")
    assert hasattr(responsive, "test_card_tables_and_touch_targets")
