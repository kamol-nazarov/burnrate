from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "spend_web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")
JS = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")
SUMMARY = json.loads((ROOT / "tests_spend" / "fixtures" / "frontend_summary.json").read_text(encoding="utf-8"))
ENTITY = json.loads((ROOT / "tests_spend" / "fixtures" / "frontend_entity.json").read_text(encoding="utf-8"))
HEALTH = json.loads((ROOT / "tests_spend" / "fixtures" / "frontend_health.json").read_text(encoding="utf-8"))


def test_frontend_wires_only_frozen_payload_endpoints() -> None:
    assert "/api/spend/summary" in JS
    assert "/api/spend/entity" in JS
    assert "/api/spend/health" in JS
    assert "/api/spend/nav" not in JS
    assert "/api/spend/limits" not in JS
    assert "knownSpend" not in JS
    assert "referenceCost" not in JS
    assert "renderNavbar(data.nav)" not in JS


def test_frontend_has_no_fabricated_telemetry_literals() -> None:
    assert "0.62" not in JS
    assert "|| 6" not in JS
    assert "?? 15" not in JS
    assert "OpenRouter PAYG" not in JS
    assert 'item.key === "output" ? 1.2' not in JS
    assert "rows[0]" not in JS
    assert "tick:" not in JS
    assert "drift" not in JS
    assert "bias(" not in JS
    assert "resets Sep" not in JS
    assert "cache < 95" not in JS
    assert "cache < 90" not in JS


def test_easing_loop_is_paint_only() -> None:
    assert "function paintEase()" in JS
    assert "function tickEase()" in JS
    assert "if (changed) paintEase();" in JS
    assert "if (changed) renderCurrent" not in JS
    assert "function renderCurrent" not in JS
    assert "function bindChartHits()" in JS
    tick_body = JS.split("function tickEase()", 1)[1].split("function setQuery", 1)[0]
    assert "renderOverview" not in tick_body
    assert "renderDetail" not in tick_body
    assert "innerHTML" not in tick_body
    assert "paintEase" in tick_body


def test_background_refresh_preserves_scroll_and_reuses_meter_nodes() -> None:
    assert "function renderPreservingScroll(background, render)" in JS
    preserve = JS.split("function renderPreservingScroll", 1)[1].split("function reconcileSegmentColumns", 1)[0]
    assert "window.scrollX" in preserve
    assert "window.scrollY" in preserve
    assert "requestAnimationFrame(restore)" in preserve
    # A load that first painted a stored snapshot also updates in place.
    assert "renderPreservingScroll(background || painted, renderOverview)" in JS
    assert "renderPreservingScroll(background, renderDetail)" in JS
    assert "renderPreservingScroll(background, renderDiagnostics)" in JS
    assert "function reconcileSegmentColumns" in JS
    assert "function bucketKeyOf" in JS
    # Server bucket keys first: axis labels repeat inside calendar windows and
    # bucketStart moves on the rolling edge, so neither may be the primary key.
    assert "bucket.bucketKey || bucket.bucketStart || bucket.label || index" in JS
    assert "bucket.label || bucket.bucketStart || index" not in JS
    assert "dataset.bucketKey" in JS
    assert "dataset.segmentKey" in JS
    chart = JS.split("function renderChart(data)", 1)[1].split("function renderChartHover", 1)[0]
    detail = JS.split("function renderDetail()", 1)[1].split("function renderDetailHover", 1)[0]
    assert "reconcileSegmentColumns(plot" in chart
    assert 'plot.innerHTML = series.map' not in chart
    assert '$("detail-bars").innerHTML = series.map' not in detail
    assert "reconcileSegmentColumns($(\"detail-bars\")" in detail
    change_range = JS.split("function changeRange", 1)[1].split("function renderCapacity", 1)[0]
    assert 'classList.remove("settled")' not in change_range
    assert "overflow-anchor:none" in CSS


def test_successful_background_refresh_clears_a_transient_error_banner() -> None:
    # A single failed background poll (e.g. during a service restart) must not
    # pin the error banner once the next poll succeeds.
    for loader in ("async function loadSummary", "async function loadEntity", "async function loadDiagnostics"):
        body = JS.split(loader, 1)[1].split("\nasync function ", 1)[0].split("\nfunction ", 1)[0]
        assert "clearError();" in body.split("renderPreservingScroll", 1)[0], loader


def test_activity_rows_are_reconciled_by_agent_id() -> None:
    activity = JS.split("function renderActivity", 1)[1].split("function renderKpis", 1)[0]
    assert "row.id" in activity
    assert "dataset.activityId" in activity
    assert "new Map" in activity
    assert "body.innerHTML = rows.map" not in activity
    assert "liveRows.forEach" in activity
    assert 'querySelector(":scope > .skel-stack")?.remove()' in activity


def test_capacity_rows_sorted_and_missing_pct_stays_unknown() -> None:
    assert "function sortQuotaRows" in JS
    assert "return sortQuotaRows(rows)" in JS
    assert "sortQuotaRows((provider.rows" in JS
    assert "finite(lead.peakPct) || 0" not in JS
    assert "finite(provider.peakPct) || 0" not in JS
    assert "peak == null" in JS
    assert 'eased == null ? unknown' in JS or "eased == null ? unknown" in JS.replace("\n", "")


def test_session_share_uses_payload_or_shown_rows() -> None:
    assert "sessions.shownTotal" in JS
    assert "row.sharePct" in JS
    assert "shareOfShown" in JS


def test_poll_and_ease_constants_match_contract() -> None:
    assert "const EASE_RATE = 0.13;" in JS
    assert "const POLL_MS = 15000;" in JS
    assert "const HEADROOM = 8;" in JS
    assert "const PLOT = 92;" in JS
    assert "--gutter:46px" in CSS
    assert "payload?.cadenceSeconds" in JS
    assert "`${cadenceSeconds}s cadence`" in JS
    assert "function stableScale" in JS
    assert "rawScale > previous" in JS
    scale_fn = JS.split("function stableScale", 1)[1].split("\nfunction ", 1)[0]
    assert "anchor" in scale_fn and "presentKeys.has(entry.anchor)" in scale_fn


def test_sections_are_reconciled_in_place_not_rebuilt_every_refresh() -> None:
    assert "function reconcileChildren" in JS
    assert "dataset.rkey" in JS
    for rebuilt in (
        '$("capacity-body").innerHTML = providers.map',
        '$("model-table-body").innerHTML = rows.map',
        '$("legend-cards").innerHTML = tools.map',
        '$("heatmap").innerHTML = DAYS.map',
        '$("mix-rows").innerHTML = rows.map',
        '$("subscription-rows").innerHTML = rows.length',
        '$("sessions-body").innerHTML = rows.length',
        '$("detail-kpis").innerHTML = kpis.map',
        '$("diagnostic-grid").innerHTML = (data.ingest',
        'hits.innerHTML = series.map',
    ):
        assert rebuilt not in JS, rebuilt
    for reconciled in (
        'reconcileChildren(body, providers',
        'reconcileChildren(body, entries',
        'reconcileChildren(legend, tools',
        'reconcileChildren($("heatmap"), DAYS',
        'reconcileChildren($("mix-rows"), rows',
        'reconcileChildren($("detail-kpis"), kpis',
        'renderQuotaRows(root, rows, idPrefix)',
        'renderHitTargets(',
    ):
        assert reconciled in JS, reconciled
    # The Y axis only recomputes once the bucket that set it leaves the chart.
    scale_fn = JS.split("function stableScale", 1)[1].split("\nfunction ", 1)[0]
    assert "!anchorPresent" in scale_fn


def test_visual_structure_and_responsive_contract() -> None:
    for token in (
        'id="coverage-banner"',
        'id="capacity-body"',
        'id="activity-body"',
        'id="kpi-grid"',
        'id="effective-rate"',
        'id="chart-shell"',
        'id="waste-headline"',
        'id="forecast-value"',
        'id="model-table-body"',
        'id="mix-bar"',
        'id="subscription-rows"',
        'id="heatmap"',
        'id="detail-view"',
        'id="detail-kpis"',
        'id="sessions-body"',
        'id="diagnostics-view"',
    ):
        assert token in HTML
    assert "width:min(1620px,calc(100% - 48px))" in CSS
    assert "minmax(0,1.42fr) minmax(340px,.6fr)" in CSS
    for width in ("1439px", "1199px", "1023px", "767px", "390px"):
        assert f"@media(max-width:{width})" in CSS
    assert "min-height:44px" in CSS
    assert "overflow-x:clip" in CSS
    assert "animation-fill-mode:both" not in CSS
    assert "@keyframes pillBreathe" in CSS
    assert "@keyframes liveDot" in CSS
    assert "@keyframes livePulse" in CSS
    assert "@keyframes sweep" in CSS


def test_frozen_fixture_keys_are_consumed() -> None:
    summary_keys = (
        "navigation",
        "coverage",
        "totals",
        "capacity",
        "activity",
        "waste",
        "cacheSavings",
        "projected",
        "mix",
        "series",
        "tools",
        "models",
        "subscriptions",
        "heatmap",
        "cadenceMinutes",
        "generatedAt",
        "cadenceSeconds",
        "heatmapFallback",
        "failingSource",
    )
    for key in summary_keys:
        assert key in SUMMARY
        assert key in JS
    for key in (
        "effectiveCostPerMillionTokens",
        "knownEffectiveCostPerMillionTokens",
        "effectiveCostPricedTokens",
        "effectiveCostCoveragePct",
        "effectiveCostComplete",
    ):
        assert key in SUMMARY["totals"]
        assert key in JS
    assert SUMMARY["navigation"]["burnRatePerDay"] >= 0
    assert ENTITY["sessions"]["shownTotal"] < ENTITY["value"]
    assert len(ENTITY["sessions"]["rows"]) <= min(6, ENTITY["runs"])
    assert "ingest" in HEALTH
    assert "quotas" in HEALTH
    assert "pricingGaps" in HEALTH
    assert "providerVsComputedVariancePct" in HEALTH
    for key in ("value", "shareOfTrackedValue", "opportunity", "providerLimits", "sessions"):
        assert key in ENTITY
        assert key in JS


def test_accessibility_and_touch_affordances() -> None:
    assert 'class="skip-link"' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'id="chart-live"' in HTML
    assert 'role="radiogroup"' in HTML
    assert "bindChartHits" in JS
    assert 'event.key === "Escape"' in JS
    assert 'event.key === "ArrowLeft"' in JS
    assert "@media(prefers-reduced-motion:reduce)" in CSS
    assert re.search(r"min-height:\s*44px", CSS)
    overview = HTML.split('id="overview-view"', 1)[1].split('id="detail-view"', 1)[0]
    assert "<h1" in overview and 'id="overview-heading"' in overview
    assert 'aria-pressed="true"' in HTML
    assert 'aria-pressed="false"' in HTML
    assert 'setAttribute("aria-pressed"' in JS
    assert "fonts.googleapis.com" not in HTML
    assert "REDUCE_MOTION" in JS
    assert "min-height:36px" not in CSS


def test_finite_rejects_null_undefined_and_empty_before_number() -> None:
    finite_fn = JS.split("const finite", 1)[1].split("const unknown", 1)[0]
    assert "value == null" in finite_fn
    assert 'value === ""' in finite_fn or "value === ''" in finite_fn
    assert finite_fn.find("null") < finite_fn.find("Number(")
    assert "skipPct=payg||pctRaw==null" in JS.replace(" ", "")
    quota = JS.split("function renderQuotaRows", 1)[1].split("function renderNavbar", 1)[0]
    assert "ease(easeKey, null)" in quota
    assert "Math.max(0, Math.min(100, pctRaw))" in quota
    assert "Math.max(0, Math.min(100, pctRaw))" not in quota.split("skipPct", 1)[0]


def test_ease_null_clears_target_and_disp() -> None:
    ease_fn = JS.split("function ease(key, target)", 1)[1].split("function live", 1)[0]
    assert "delete state.targets[key]" in ease_fn
    assert "delete state.disp[key]" in ease_fn
    assert "finite(target)" in ease_fn
    paint_fn = JS.split("function paintEase()", 1)[1].split("function paintComposite", 1)[0]
    assert "!(key in state.targets)" in paint_fn
    assert "function live(key)" in JS
    assert "return key in state.targets ? state.disp[key] : undefined" in JS


def test_unknown_survives_subsequent_raf_paint_frames() -> None:
    """Mirror the JS ease/paint registry: a later rAF must not overwrite — with a stale number."""
    targets: dict[str, float] = {}
    disp: dict[str, float] = {}

    def ease(key: str, target: float | None) -> float | None:
        if target is None:
            targets.pop(key, None)
            disp.pop(key, None)
            return None
        targets[key] = target
        return disp[key] if key in disp else target

    def paint(nodes: dict[str, str]) -> dict[str, str]:
        painted = dict(nodes)
        for key, text in nodes.items():
            if key not in targets:
                continue
            painted[key] = f"${disp[key]:.2f}" if key == "tracked" else f"{disp[key]:.1f}%"
            _ = text
        return painted

    ease("tracked", 12.5)
    ease("capPeak", 43.0)
    disp["tracked"] = 12.5
    disp["capPeak"] = 43.0
    nodes = {"tracked": "$12.50", "capPeak": "43.0%"}
    ease("tracked", None)
    ease("capPeak", None)
    nodes = {"tracked": "—", "capPeak": "—"}
    for _ in range(5):
        nodes = paint(nodes)
        assert nodes["tracked"] == "—"
        assert nodes["capPeak"] == "—"
    assert "tracked" not in targets
    assert "capPeak" not in disp


def test_missing_subscription_heatmap_bars_and_waste_stay_unknown() -> None:
    assert "finite(row.monthlyEquivalent ?? row.amountUsd) || 0" not in JS
    assert "supplied.length ? supplied.reduce" in JS
    assert 'monthly == null ? unknown' in JS
    assert "Number(cell?.value) || 0" not in JS
    assert "value == null ? unknown" in JS
    assert 'button.dataset.value === "" ? unknown' in JS
    assert "Math.max(0.5, ((Number(bucket.tokens) || 0)" not in JS
    assert "height:tok == null ? null" in JS
    assert '≈${usd(ease("leak' not in JS
    assert 'leak == null ? unknown' in JS


def test_live_payload_aliases_and_activity_states_are_consumed() -> None:
    assert "heatmapFallback" in JS
    assert "failingSource" in JS
    assert "function isLiveRow" in JS
    assert 'flag === "live" || flag === "running"' in JS
    assert "function isCacheOpportunity" in JS
    assert "cache_gap" in JS
    assert "Number(item.total) || 0" not in JS
    assert "Number(bucket.byTool" not in JS


def test_frozen_resting_and_reduced_motion_gates() -> None:
    assert "html.frozen" in CSS
    assert "body.settled" in CSS
    assert "animation-fill-mode:both" not in CSS
    assert "animation-fill-mode: both" not in CSS
    assert "capacity-note-eta" in CSS
    assert "container-type:inline-size" in CSS
    assert "@media(max-width:1199px)" in CSS
    assert ".range-switch button,.mode-switch button{" in CSS
    assert "min-height:44px;height:44px" in CSS
