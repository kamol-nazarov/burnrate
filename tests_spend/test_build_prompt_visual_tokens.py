"""Exact visual tokens and timings from BURNRATE Dashboard.dc.html."""

from __future__ import annotations

import re

from tests_spend.test_build_prompt_harness import CONTRACT, sources


def test_palette_tokens_are_ported_1_to_1() -> None:
    _html, css, _js = sources()
    palette = CONTRACT["palette"]
    for token in (
        palette["page"],
        palette["panels"],
        palette["hover"],
        palette["text"],
        palette["secondary"],
        palette["muted"],
        palette["dim"],
        palette["accent"],
        palette["green"],
        palette["amber"],
        palette["red"],
        palette["violet"],
        palette["cyan"],
        palette["warnBg"],
        palette["warnBorder"],
        palette["dangerBg"],
        palette["dangerBorder"],
        palette["goodBg"],
        palette["goodBorder"],
        palette["navBg"],
    ):
        assert token in css, token
    for inset in palette["insets"]:
        assert inset in css, inset
    for border in palette["borders"]:
        assert border in css, border


def test_typography_layout_and_chart_geometry_match_the_attached_file() -> None:
    html, css, js = sources()
    fonts = html + css
    assert "system-ui" in fonts
    assert "Segoe UI" in fonts
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "Inter" not in fonts
    assert "JetBrains" not in fonts
    assert "font-size:10px" in css and "letter-spacing:.12em" in css and "text-transform:uppercase" in css
    assert "font-size:21px" in css and "letter-spacing:-.02em" in css
    assert "letter-spacing:-.045em" in css
    assert "width:min(1620px,calc(100%-48px))" in css.replace(" ", "")
    assert "minmax(0,1.42fr)minmax(340px,.6fr)" in css.replace(" ", "")
    assert "align-items:stretch" in css
    assert "repeat(5,minmax(0,1fr))" in css.replace(" ", "") or "repeat(5, minmax(0,1fr))" in css
    assert "min-height:78px" in css
    assert "backdrop-filter:blur(14px)" in css.replace(" ", "")
    assert "--gutter:46px" in css.replace(" ", "")
    assert "const HEADROOM = 8;" in js
    assert "const PLOT = 92;" in js
    assert "opacity:.42" in css or "opacity: .42" in css or ".day-column.dim{opacity:.42}" in css.replace(" ", "")
    assert "margin-top:auto" in css
    assert "BURNRATE" in html
    assert "letter-spacing:.035em" in css
    assert "font:700 20px/.88" in css


def test_brand_mark_and_tab_identity_are_present() -> None:
    html, css, _js = sources()
    assert '<title>BURNRATE · AI Cost Intelligence</title>' in html
    assert 'href="/favicon.svg?v=1"' in html
    assert 'class="burnrate-mark"' in html
    assert 'class="burnrate-tagline">AI cost intelligence<' in html
    assert ".burnrate-brand:hover .burnrate-mark" in css


def test_activity_and_poll_timings_match_the_attached_file() -> None:
    _html, css, js = sources()
    assert "pillBreathe 2.6s" in css
    assert "liveDot 2.2s" in css
    assert "livePulse 2.2s" in css
    assert "sweep 3.4s linear" in css
    assert "index * 260" in js
    assert "const EASE_RATE = 0.13;" in js
    assert "const POLL_MS = 15000;" in js
    assert "growIn 600ms cubic-bezier(.2,.7,.25,1)" in css.replace(" ", "").replace("var(--ease)", ".2,.7,.25,1") or (
        "growIn 600ms" in css and "--ease:.2,.7,.25,1" in css.replace(" ", "")
    )
    assert "scaleY" in css
    assert "transform-origin:bottom" in css.replace(" ", "")


def test_window_switcher_coverage_banner_and_panel_copy_exist() -> None:
    html, css, _js = sources()
    for token in (
        'id="coverage-banner"',
        'id="capacity-body"',
        'id="activity-body"',
        'id="kpi-grid"',
        'id="chart-shell"',
        'id="waste-headline"',
        'id="forecast-value"',
        'id="model-table-body"',
        'id="mix-bar"',
        'id="subscription-rows"',
        'id="heatmap"',
        'id="detail-view"',
        'id="sessions-body"',
        'id="diagnostics-view"',
        'id="heat-warning"',
        'id="live-count"',
    ):
        assert token in html, token
    assert "Partial pricing coverage" in html or "coverage-title" in html
    assert ".coverage-banner" in css
    assert "What runs out first" in html
    assert "Running now" in html
    assert "Where money leaks" in html


def test_frontend_deletes_static_file_placeholders() -> None:
    html, _css, js = sources()
    for forbidden in (
        "0.62",
        "|| 6",
        "?? 15",
        "OpenRouter PAYG",
        "resets Sep",
        "cache < 95",
        "tick:",
        "bias(",
        "drift",
    ):
        assert forbidden not in js, forbidden
    assert "value * 0.62" not in html
    assert "OpenRouter PAYG" not in html
    assert "fonts.googleapis.com" not in html
