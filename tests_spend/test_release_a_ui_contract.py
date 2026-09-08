"""Release A UI contract tests (A11/A12): money truth, units, focus, zones."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend_src" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend_src" / "spend.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend_src" / "spend.js").read_text(encoding="utf-8")


def test_subscription_labels_are_cadence_honest() -> None:
    # C12: an annual plan states its annual amount; no monthly-ternary bug.
    assert 'cadence === "annual" ? "/yr"' in JS
    assert 'row.cadence.includes("3")' not in JS


def test_heatmap_carries_reference_usage_units_end_to_end() -> None:
    # N03: cells format as USD reference usage, not tokens, in hover text,
    # aria labels and the accessible table caption.
    assert "reference usage" in JS
    assert 'usd(n) + " reference usage"' in JS
    assert 'data.heatmapUnit || "reference usage (USD at documented rates)"' in JS
    assert "Reference usage by weekday and hour" in HTML
    # The tokens() formatter must not be used for heat values anymore.
    heat_body = JS.split("function renderHeat(data)", 1)[1].split("function renderOverview", 1)[0]
    assert "tokens(button.dataset.value)" not in heat_body
    assert "tokens(peak.dataset.value)" not in heat_body


def test_heat_cells_are_nonoverlapping_real_targets() -> None:
    # C11: no negative-margin pseudo-targets; the cell is the hit area.
    assert "margin:-13px" not in CSS
    assert ".heat-cell:after" not in CSS
    assert "height:20px" in CSS
    # Focus visibility is a real outline, not just z-order.
    assert ".heat-cell:hover,.heat-cell:focus-visible{outline" in CSS


def test_dim_text_meets_contrast_floor() -> None:
    # C14: the dim pairs move from ~3.1:1 to >=4.5:1 on the surface.
    assert "--muted:#98a0ac" in CSS
    assert "--dim:#7b8290" in CSS
    assert "--muted:#858c98" not in CSS
    assert "--dim:#5d646f" not in CSS


def test_heatmap_keyboard_grid_is_one_tab_stop() -> None:
    # C14: roving tabindex across the 168 cells; arrows move within.
    assert 'class="heat-cell" tabindex="-1"' in JS
    assert "moveHeatFocus" in JS
    assert "ArrowUp:-24" in JS and "ArrowDown:24" in JS
    assert "cell.tabIndex = index === state.heatFocus ? 0 : -1" in JS


def test_range_arrows_transfer_real_focus() -> None:
    # C14: arrowing between ranges moves focus to the active radio.
    assert "active.focus()" in JS
    assert "button.tabIndex = -1" in JS


def test_status_region_announces_transitions_not_timestamps() -> None:
    # C14: dedicated live region; the per-poll stamp never enters it.
    assert 'id="status-live"' in HTML
    assert 'id="diagnostics-button" type="button" data-state' in HTML
    live_button = HTML.split('id="diagnostics-button"', 1)[0]
    assert 'aria-live="polite"' not in live_button.rsplit("<button", 1)[-1]
    assert "state.lastStatusAnnounce" in JS


def test_reset_labels_use_the_configured_zone() -> None:
    # C13: calendar labels honor the payload's displayTimezone.
    assert "timeZone: state.displayTz || undefined" in JS
    assert "displayTimezone" in JS or "displayTz" in JS


def test_forecast_labels_state_the_comparison_basis() -> None:
    assert "accrued (same period)" in JS
    assert "lower bound" in JS
    assert "multipleBasis" in JS


def test_probe_reports_heat_targets_separately() -> None:
    assert "minHeatCell" in JS
    probe_body = JS.split("function minTargetSize()", 1)[1].split("function writeProbe", 1)[0]
    assert 'el.classList.contains("heat-cell")' in probe_body
