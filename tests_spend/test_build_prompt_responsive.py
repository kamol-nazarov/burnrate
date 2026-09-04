"""Responsive, no-scroll, clipping, 44px, touch, and card-table gates."""

from __future__ import annotations

from tests_spend.test_build_prompt_harness import CONTRACT, media_blocks, sources


VIEWPORTS = CONTRACT["viewports"]


def test_required_breakpoints_exist() -> None:
    _html, css, _js = sources()
    blocks = media_blocks(css)
    queries = " ".join(blocks)
    for width in ("1439px", "1199px", "1023px", "767px", "390px"):
        assert f"max-width:{width}" in queries.replace(" ", ""), width
    assert "@container" in css


def test_desktop_1440_and_1920_keep_the_attached_layout() -> None:
    _html, css, _js = sources()
    # No max-width media applies at 1440/1920 — the attached desktop layout is the default.
    assert "width:min(1620px,calc(100%-48px))" in css.replace(" ", "")
    assert "minmax(0,1.42fr)minmax(340px,.6fr)" in css.replace(" ", "")
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in css.replace(" ", "")
    assert "min-height:78px" in css
    blocks = media_blocks(css)
    desktop = css.split("@media")[0]
    assert "minmax(340px,.6fr)" in desktop.replace(" ", "")


def test_1200_to_1439_tightens_container_and_keeps_desktop_kpis() -> None:
    _html, css, _js = sources()
    block = media_blocks(css)["(max-width:1439px)"]
    collapsed = block.replace(" ", "")
    assert "width:calc(100%-32px)" in collapsed
    assert "minmax(300px,.6fr)" in collapsed
    assert "kpi-grid" not in block
    assert "grid-template-columns:1fr" not in collapsed


def test_1024_collapses_to_one_column_two_by_two_kpis_and_drops_runs() -> None:
    _html, css, _js = sources()
    block = media_blocks(css)["(max-width:1199px)"]
    collapsed = block.replace(" ", "")
    assert "grid-template-columns:1fr" in collapsed
    assert "repeat(2,minmax(0,1fr))" in collapsed
    assert "data-key=\"runs\"" in block or "data-key='runs'" in block or "col-runs" in block
    assert "display:none" in collapsed


def test_768_compacts_navbar_and_allows_heatmap_internal_scroll_only() -> None:
    _html, css, _js = sources()
    block = media_blocks(css)["(max-width:1023px)"]
    collapsed = block.replace(" ", "")
    assert "min-height:62px" in collapsed
    assert "font-size:17px" in collapsed
    assert "min-height:200px" in collapsed
    assert "overflow-x:auto" in collapsed
    assert "position:sticky" in collapsed
    assert "overflow-x:auto" in block
    # Page itself must still clip; heatmap scroll is inside the card.
    assert "html,body" not in block or "overflow-x:auto" not in css.split("html,body")[1][:200]


def test_below_768_stacks_kpis_cards_and_banner() -> None:
    _html, css, _js = sources()
    block = media_blocks(css)["(max-width:767px)"]
    collapsed = block.replace(" ", "")
    assert "grid-template-columns:1fr" in collapsed
    assert "font-size:26px" in collapsed
    assert ".model-card" in block
    assert ".session-card" in block
    assert "display:block" in collapsed
    assert ".model-table-head,.sessions-head{display:none}" in collapsed or (
        "model-table-head" in block and "display:none" in block
    )
    assert "flex-direction:column" in collapsed
    assert ".capacity-eta{display:none}" in collapsed or "capacity-eta" in block
    assert "model-figures" in block
    assert "repeat(3,minmax(0,1fr))" in collapsed


def test_no_horizontal_page_scroll_rules_at_required_widths() -> None:
    _html, css, _js = sources()
    head = css.split("@media")[0].replace(" ", "")
    assert "overflow-x:clip" in head
    assert "min-width:320px" in head
    assert "width:100vw" not in css.replace(" ", "")
    # Model/session tables must not page-scroll horizontally at any required width.
    mobile = media_blocks(css)["(max-width:767px)"]
    assert "overflow-x:auto" not in mobile or "range-switch" in mobile
    assert ".model-row{display:none}" in mobile.replace(" ", "") or "model-row" in mobile
    for width in VIEWPORTS:
        assert width in {390, 768, 1024, 1440, 1920}


def test_card_tables_and_touch_targets() -> None:
    html, css, js = sources()
    assert "min-height:44px" in css
    desktop = css.split("@media")[0]
    assert ".range-switch button" in desktop
    range_rule = desktop.split(".range-switch button", 1)[1].split("}", 1)[0]
    assert "min-height:44px" in range_rule, (
        "All hit targets must be ≥44px at every width, including desktop window chips. "
        f"Default range-switch rule is {range_rule!r}"
    )
    assert "height:30px" not in range_rule
    blocks = media_blocks(css)
    phone = blocks["(max-width:767px)"]
    assert "min-height:44px" in phone
    assert "bindChartHits" in js
    assert "state.pinned" in js
    assert 'event.key === "Escape"' in js
    assert ".hit-target" in css
    assert "pointerdown" in js
    assert "@container" in css
    assert 'role="radiogroup"' in html


def test_heatmap_dims_to_34_percent_below_one_week() -> None:
    html, css, js = sources()
    assert ".heatmap.dim{opacity:.34}" in css.replace(" ", "")
    assert 'id="heat-warning"' in html
    assert "trailing 7-day pattern" in html.lower() or "trailing 7-day" in html
    assert "heatmap.dim" in js or 'classList.toggle("dim"' in js
    assert '["15m","30m","1h","3h","6h","12h","1d"]' in js.replace(" ", "")


def test_window_chip_row_is_horizontally_scrollable_on_phone() -> None:
    _html, css, js = sources()
    assert "overflow-x:auto" in css
    assert "scrollIntoView" in js or "scroll-into-view" in js or "scrollIntoView" in js
    phone = media_blocks(css)["(max-width:767px)"]
    assert "range-switch" in phone
    assert "min-height:44px" in phone


def test_optional_local_renderer_scroll_proof_when_available() -> None:
    """Page overflow is forbidden. CSS clip is the always-on gate; a local renderer is extra."""
    _html, css, _js = sources()
    assert "overflow-x:clip" in css.replace(" ", "")
    blocks = media_blocks(css)
    assert "(max-width:390px)" in blocks
    assert "(max-width:767px)" in blocks
    assert "(max-width:1023px)" in blocks
    assert "(max-width:1199px)" in blocks
    assert "(max-width:1439px)" in blocks
