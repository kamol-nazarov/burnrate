"""Null vs zero, em-dash, and ≈ placement from the original prompt."""

from __future__ import annotations

from pathlib import Path

from tests_spend.test_build_prompt_harness import (
    CONTRACT,
    client_for,
    complete_world,
    gap_world,
    sources,
)


def test_unpriced_model_is_emdash_never_zero_or_guessed(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = gap_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    unlisted = next(model for model in body["models"] if model["key"] == "opencode:unlisted-model")
    assert unlisted["tokens"] == 1_050
    assert unlisted["value"] is None
    assert unlisted["value"] != 0
    assert body["totals"]["trackedValue"] is None
    health = client.get("/api/spend/health").json()
    assert "opencode:unlisted-model" in health["pricingGaps"]
    _html, _css, js = sources()
    assert 'unknown = "—"' in js or "unknown = '—'" in js
    render_models = js.split("function renderModels(data)", 1)[1].split("\nfunction ", 1)[0]
    assert "model.value == null" in render_models
    compact_models = render_models.replace(" ", "").replace("\n", "")
    assert "unpriced?unknown" in compact_models


def test_unavailable_quota_is_not_a_fake_zero_percent(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    cursor = next(card for card in body["capacity"] if card["providerKey"] == "cursor")
    other = next(row for row in cursor["rows"] if "other" in row["limitKey"] or "Other" in row["label"])
    assert other["pct"] is None
    assert other["used"] is None
    assert other["allowance"] is None
    assert other["pct"] != 0
    assert "unavailable" in (other["unit"] or "").lower() or "omitted" in (other["label"] or "").lower()
    _html, _css, js = sources()
    assert "finite(provider.peakPct) || 0" not in js
    assert "eased == null ? unknown" in js or 'eased == null ? unknown' in js.replace("\n", "")
    assert "Quota unavailable" in js


def test_payg_openrouter_never_renders_a_zero_quota_bar(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    openrouter = next(card for card in body["capacity"] if card["providerKey"] == "openrouter")
    assert openrouter["isPayg"] is True
    assert openrouter["peakPct"] is None
    assert openrouter["peakPct"] != 0
    row = openrouter["rows"][0]
    assert row["pct"] is None
    assert row["allowance"] is None
    assert openrouter["monthToDateUsd"] is not None
    _html, css, js = sources()
    assert ".track.payg" in css
    assert "repeating-linear-gradient" in css
    assert "no quota" in js
    assert "this month" in js


def test_empty_window_does_not_flash_zero_dollars() -> None:
    html, css, js = sources()
    assert "class=\"loading\"" in html or "class='loading'" in html
    assert "burnrate-skeleton" in html
    assert "#252a32" in css
    assert "Never" not in js or "setLoading" in js
    assert "$0.00" not in js
    assert "body.loading" in css
    assert "function setLoading(on)" in js


def test_approx_marker_is_exactly_the_published_rate_set() -> None:
    _html, _css, js = sources()
    exact = set(CONTRACT["exactTools"])
    derived = set(CONTRACT["derivedTools"])
    assert exact == {"codex", "claude-code"}
    assert derived == {"cursor", "opencode", "grok", "openrouter"}
    marked = js.split("function markedUsd(value, exact)", 1)[1].split("function tokens", 1)[0]
    assert "exact === false ? \"≈\" + text : text" in marked.replace(" ", "").replace("\n", "") or (
        "exact === false" in marked and "≈" in marked
    )
    assert "OpenRouter PAYG" not in js


def test_factual_zero_quota_stays_zero_and_is_distinct_from_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    zai = next(card for card in body["capacity"] if card["providerKey"] == "opencode")
    five = next(row for row in zai["rows"] if row["limitKey"] in {"5h", "five_hour"} or "5-hour" in row["label"])
    assert five["pct"] == 0 or five["pct"] == 0.0
    assert five["used"] == 0 or five["used"] == 0.0
    assert five["allowance"] == 30000
    cursor = next(card for card in body["capacity"] if card["providerKey"] == "cursor")
    other = next(row for row in cursor["rows"] if row["pct"] is None)
    assert other["pct"] is None
    assert five["pct"] != other["pct"]
