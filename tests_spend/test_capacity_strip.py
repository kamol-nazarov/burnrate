"""Compact capacity strip contract.

The overview capacity panel renders one lane per provider that has a
measurable limit; providers without one (pay as you go, unavailable pollers)
are folded into a footer sentence instead of a full row. Full notes stay on
hover and in Data health. The window selector sits below the strip because
quotas do not depend on the analysis window.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "spend_web"
JS = (ROOT / "spend.js").read_text(encoding="utf-8")
CSS = (ROOT / "spend.css").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def _capacity_fn() -> str:
    return JS.split("function renderCapacity(data)", 1)[1].split("\nfunction renderActivity", 1)[0]


def test_lanes_only_carry_measurable_limits_and_the_rest_go_to_the_footer() -> None:
    fn = _capacity_fn()
    assert "function providerHasQuota(provider)" in JS
    has_quota = JS.split("function providerHasQuota(provider)", 1)[1].split("\n}", 1)[0]
    assert "!provider.isPayg" in has_quota and "finite(row.pct) != null" in has_quota
    assert "all.filter(providerHasQuota)" in fn
    assert "all.filter(provider => !providerHasQuota(provider))" in fn
    assert 'class="capacity-lane"' in fn and 'class="capacity-foot"' in fn
    assert "foot.hidden = !withoutQuota.length" in fn
    # Pay as you go never invents a figure: unavailable balance is said in words, not as $0.
    assert "management key required" in fn
    assert "funds remaining" in fn
    assert "quota unavailable" in fn


def test_lane_surface_keeps_full_note_on_hover_and_secondary_windows_as_thin_bars() -> None:
    fn = _capacity_fn()
    assert 'setAttr(node, "title", rows.map(row => `${row.label || "Limit"}: ${capacityNote(row)}`)' in fn
    assert 'bar.classList.toggle("thin", index > 0)' in fn
    assert "models.length > 1" in fn, "window captions appear only when a provider has several limits"
    assert "bindWidth(fill, model.easeKey, model.eased, fresh)" in fn, "bars stay on the easing registry"
    assert 'reconcileChildren(body, providers' in fn


def test_lane_tone_thresholds_match_pressure_colors() -> None:
    tone = JS.split("function laneTone(value)", 1)[1].split("\n}", 1)[0]
    assert 'n >= 85) return "hot"' in tone
    assert 'n >= 60) return "warm"' in tone
    assert 'return "quiet"' in tone
    assert '.capacity-lane[data-tone="quiet"] .lane-pct{color:var(--secondary)}' in CSS
    assert '.capacity-lane[data-tone="warm"] .lane-pct{color:var(--amber)}' in CSS
    assert '.capacity-lane[data-tone="hot"] .lane-pct' in CSS and "#dc6c78" in CSS


def test_short_limit_labels_drop_provider_and_unit_words() -> None:
    fn = JS.split("function shortLimitLabel(label, providerName)", 1)[1].split("\n}", 1)[0]
    assert "startsWith(name.toLowerCase())" in fn
    assert re.search(r"\(window\|credits\|limit\|quota\)\$", fn)

    def short(label: str, provider: str) -> str:
        text = label.strip()
        if provider and text.lower().startswith(provider.lower()):
            text = text[len(provider):].strip()
        text = re.sub(r"^(claude|codex|z\.ai|cursor|grok build|openrouter)\s+", "", text, flags=re.I)
        text = re.sub(r"\s+(window|credits|limit|quota)$", "", text, flags=re.I).strip()
        return text or label

    assert short("Codex weekly window", "Codex") == "weekly"
    assert short("Claude 5-hour window", "Claude Code") == "5-hour"
    assert short("Z.AI weekly credits", "Z.AI / OpenCode") == "weekly"
    assert short("Other Models", "Cursor") == "Other Models"


def test_mobile_lane_hides_the_eta_column_and_moves_it_into_the_subtitle() -> None:
    phone = CSS.split("@media(max-width:767px){", 1)[1].split("\n}\n", 1)[0].replace(" ", "")
    assert ".capacity-lane.lane-eta{display:none}" in phone
    assert ".lane-sub-eta{display:inline}" in phone
    assert ".lane-sub-eta{display:none}" in CSS.split("@media")[0].replace(" ", "")
    assert ".capacity-lane{grid-template-columns:minmax(0,1fr)56px" in phone


def test_window_selector_sits_below_the_capacity_strip() -> None:
    capacity = HTML.index('class="panel capacity-panel"')
    selector = HTML.index('id="range-switch"')
    kpis = HTML.index('id="kpi-grid"')
    assert capacity < selector < kpis
