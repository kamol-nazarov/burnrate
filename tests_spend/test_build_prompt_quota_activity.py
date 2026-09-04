"""Quota grouping, PAYG, pressure colors, and live-activity derivation."""

from __future__ import annotations

from pathlib import Path

from spend_app.quotas import (
    agent_run_records,
    is_live_run,
    openrouter_quota_samples,
    order_capacity_rows,
    poll_activity,
    pressure_color,
)
from tests_spend.test_build_prompt_harness import CONTRACT, client_for, complete_world, sources


REQUIRED = CONTRACT["requiredLimits"]


def test_capacity_is_grouped_by_provider_with_prompt_limit_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    by_key = {card["providerKey"]: card for card in body["capacity"]}
    for provider in ("claude-code", "grok", "codex", "cursor", "opencode", "openrouter"):
        assert provider in by_key, provider
    assert by_key["claude-code"]["plan"] == "Claude Max"
    assert by_key["grok"]["plan"] == "SuperGrok Heavy"
    assert by_key["codex"]["plan"] == "ChatGPT Pro"
    assert by_key["cursor"]["plan"] == "Ultra"
    assert by_key["opencode"]["plan"] == "Max"
    assert by_key["openrouter"]["plan"] in {"Pay as you go", "Pay-as-you-go", "pay as you go"}
    claude_keys = {row["limitKey"] for row in by_key["claude-code"]["rows"]}
    assert any(key in claude_keys for key in ("5h", "session", "five_hour"))
    assert "weekly" in claude_keys
    cursor_labels = " ".join(row["label"] for row in by_key["cursor"]["rows"]).lower()
    assert "cursor" in cursor_labels
    assert "included value" not in cursor_labels
    zai_labels = " ".join(row["label"] for row in by_key["opencode"]["rows"]).lower()
    assert "5-hour" in zai_labels or "5h" in zai_labels
    assert "weekly" in zai_labels


def test_providers_sort_by_peak_and_openrouter_is_pinned_last(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    cards = body["capacity"]
    assert cards[-1]["providerKey"] == "openrouter"
    assert cards[-1]["isPayg"] is True
    peaks = [card["peakPct"] for card in cards if not card["isPayg"] and card["peakPct"] is not None]
    assert peaks == sorted(peaks, reverse=True)
    expected_primary = {
        "cursor": "cursor_models",
        "claude-code": "5h",
        "opencode": "5h",
    }
    for card in cards:
        if card["providerKey"] in expected_primary:
            assert card["rows"][0]["limitKey"] == expected_primary[card["providerKey"]]
            assert card["rows"][0]["isPrimary"] is True


def test_openrouter_payg_displays_remaining_funds(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    card = next(item for item in body["capacity"] if item["providerKey"] == "openrouter")
    row = card["rows"][0]
    assert row["resetsAt"] is None
    _html, css, js = sources()
    assert "fundsRemainingUsd" in js
    assert ".track.payg" in css
    samples = openrouter_quota_samples(
        collector=lambda: {
            "status": "exact",
            "windows": [{"key": "balance", "remainingUsd": 42.5}],
        }
    )
    assert samples
    assert samples[0].is_payg is True
    assert samples[0].used == 42.5


def test_supporting_capacity_notes_are_derived_from_the_same_row_percentage() -> None:
    _html, _css, js = sources()
    note = js.split("function capacityNote(row)", 1)[1].split("function sortQuotaRows", 1)[0]
    assert "Number(row.allowance) * Number(row.pct) / 100" in note.replace(" ", "") or (
        "allowance" in note and "pct" in note
    )
    assert "formatUnit(row.used" in note
    assert "literal" not in note


def test_pressure_colors_match_the_attached_file_not_a_shifted_scale() -> None:
    import inspect

    _html, _css, js = sources()
    color_fn = js.split("function quotaColor(value)", 1)[1].split("function formatUnit", 1)[0]
    assert "n >= 85" in color_fn
    assert "n >= 60" in color_fn
    assert "n >= 30" in color_fn
    assert "#dc6c78" in color_fn
    assert "#d9a441" in color_fn
    assert "#78a8f8" in color_fn
    assert "#63c689" in color_fn
    source = inspect.getsource(pressure_color)
    assert "85" in source and "60" in source and "30" in source, (
        "Quota pressure colors are <30 green, <60 blue, <85 amber, else red. "
        f"Helper disagrees:\n{source}"
    )


def test_activity_live_pill_is_derived_from_rows_and_nodata_does_not_animate(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    states = {row["id"]: row["state"] for row in body["activity"]}
    assert states["run-live"].upper() in {"LIVE", "RUNNING"}
    assert states["run-idle"].upper().replace("_", " ") in {"NO DATA", "NODATA"}
    html, css, js = sources()
    assert 'id="live-count"' in html
    render = js.split("function renderActivity(data)", 1)[1].split("\nfunction ", 1)[0]
    compact_render = render.replace(" ", "").replace("\n", "")
    assert "rows.filter(isLiveRow)" in compact_render
    assert "${liveRows.length} live" in render
    assert 'toggle("idle",liveRows.length===0)' in compact_render
    assert ".activity-row.nodata" in css
    assert ".activity-row.nodata i.dot{background:var(--amber);animation:none}" in css.replace("\n", "") or (
        "nodata" in css and "animation:none" in css
    )
    assert 'style.animationDelay = `${delay}ms`' in js
    assert "index * 260" in js


def test_poller_state_vocabulary_must_drive_the_same_live_count() -> None:
    """Traycer poller writes running/no_data; the dashboard counts LIVE. They must meet."""
    records = agent_run_records(
        {
            "activeAgents": [
                {
                    "chatId": "abc",
                    "title": "Orchestrator",
                    "model": "claude-opus-5",
                    "startedAt": "2026-08-30T20:00:00Z",
                }
            ],
            "unmeteredTurns": [
                {
                    "chatId": "def",
                    "title": "Idle",
                    "model": "grok-4.6",
                    "startedAt": "2026-08-30T18:00:00Z",
                    "stoppedAt": "2026-08-30T18:10:00Z",
                }
            ],
        }
    )
    assert len(records) == 2
    live = [row for row in records if is_live_run(row.state)]
    nodata = [row for row in records if not is_live_run(row.state)]
    assert len(live) == 1
    assert len(nodata) == 1
    assert is_live_run("live") is True
    assert is_live_run("running") is True
    assert is_live_run("LIVE") is True
    assert is_live_run("RUNNING") is True
    assert is_live_run("no_data") is False
    _html, _css, js = sources()
    live_fn = js.split("function isLiveRow(row)", 1)[1].split("\nfunction ", 1)[0]
    compact_live = live_fn.replace(" ", "").replace("\n", "")
    assert 'flag==="live"' in compact_live or "flag==='live'" in compact_live
    assert 'flag==="running"' in compact_live or "flag==='running'" in compact_live
    assert "||" in compact_live


def test_order_capacity_rows_pins_openrouter_last() -> None:
    from spend_app.quotas import QuotaSample

    rows = [
        QuotaSample("openrouter", "balance", "Balance", "usd", "x", is_payg=True),
        QuotaSample("codex", "weekly", "Weekly", "pct", "x", pct=23),
        QuotaSample("claude-code", "weekly", "Weekly", "pct", "x", pct=43),
        QuotaSample("claude-code", "5h", "5h", "pct", "x", pct=9),
    ]
    ordered = order_capacity_rows(rows)
    assert ordered[-1].provider_key == "openrouter"
    claude = [row for row in ordered if row.provider_key == "claude-code"]
    assert [row.limit_key for row in claude] == ["5h", "weekly"]
