from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "spend_web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")
JS = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")
FIXTURES = ROOT / "tests_spend" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_loading_chrome_never_flashes_zero_money() -> None:
    assert 'class="loading"' in HTML
    assert "burnrate-skeleton" in HTML
    assert 'id="live-count"' in HTML
    live = HTML.split('id="live-count"', 1)[1].split("</span>", 1)[0]
    assert "0 live" not in live
    assert "$0.00" not in HTML
    assert "background:#252a32" in CSS


def test_empty_window_fixture_stays_unknown_not_zero() -> None:
    empty = _load("frontend_summary_empty.json")
    assert empty["totals"]["trackedValue"] is None
    assert empty["totals"]["tokens"] == 0
    assert empty["models"] == []
    assert empty["heatmapFallback"] is True
    assert "No models in this window" in JS
    assert "Token activity will appear here once usage events exist" in JS
    assert "Provider quotas will appear here" in JS
    assert "No agents are running right now" in JS


def test_stale_and_failed_ingest_copy_is_wired() -> None:
    stale = _load("frontend_summary_stale.json")
    failed = _load("frontend_summary_error.json")
    health = _load("frontend_health_failed.json")
    assert stale["status"] == "stale"
    assert failed["status"] == "error"
    assert failed["failingSource"] == "openai_admin"
    assert health["ingest"][0]["status"] == "failed"
    assert 'label.textContent = "Stale"' in JS
    assert 'label.textContent = "Ingest failed"' in JS
    assert "failingSource" in JS
    assert '[data-state="stale"]' in CSS
    assert '[data-state="error"]' in CSS
    assert '[data-state="live"] .burnrate-status-dot{animation:ping' in CSS


def test_fixtures_are_synthetic_not_operator_dumps() -> None:
    forbidden = ("vehicledesk", "tailscale", "tail982", "crt-consolidation", "s-7420")
    for name in (
        "frontend_summary.json",
        "frontend_summary_empty.json",
        "frontend_summary_stale.json",
        "frontend_summary_unpriced.json",
        "frontend_summary_error.json",
        "frontend_entity.json",
        "frontend_health.json",
        "frontend_health_failed.json",
        "demo_summary.json",
    ):
        text = (FIXTURES / name).read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{name} still contains {token}"


def test_unpriced_and_unavailable_quota_fixtures_are_honest() -> None:
    payload = _load("frontend_summary_unpriced.json")
    unpriced = next(model for model in payload["models"] if model["value"] is None)
    assert unpriced["tokens"] > 0
    cursor = next(item for item in payload["capacity"] if item["providerKey"] == "cursor")
    assert cursor["peakPct"] is None
    assert cursor["rows"][0]["pct"] is None
    payg = next(item for item in payload["capacity"] if item["isPayg"])
    assert payg["peakPct"] is None
    assert payload["totals"]["trackedValue"] is None
    assert "Quota unavailable" in JS
    assert "no quota" in JS
    assert "isLiveRow" in JS
    running = next(item for item in payload["activity"] if item["state"] == "running")
    idle = next(item for item in payload["activity"] if item["state"] == "no_data")
    assert running["name"]
    assert idle["name"]
