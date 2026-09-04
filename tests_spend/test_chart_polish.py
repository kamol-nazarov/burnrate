"""Trend chart presentation contract.

Grid ticks are round figures from a 1-2-2.5-5 ladder, the plot uses one
continuous baseline without fake zero-value bar segments, only the topmost
visible segment carries the rounded cap, and the cumulative line ends in a marker.
"""

from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "spend_web"
JS = (ROOT / "spend.js").read_text(encoding="utf-8")
CSS = (ROOT / "spend.css").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def nice_max(value: float) -> float:
    """Mirror of spend.js niceMax."""
    v = max(value or 0, 0.01)
    mag = 10 ** math.floor(math.log10(v / 4))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag * 4 >= v)
    return step * 4


def test_nice_max_gives_round_quarter_ticks() -> None:
    fn = JS.split("function niceMax(value)", 1)[1].split("\n}", 1)[0]
    assert "[1, 2, 2.5, 5, 10]" in fn and "candidate * 4 >= v" in fn
    assert nice_max(7_500_000) == 8_000_000
    assert nice_max(8_500_000) == 10_000_000
    assert nice_max(3_500_000) == 4_000_000
    assert nice_max(9_000) == 10_000
    assert nice_max(1_000_000) == 1_000_000
    for peak in (12.0, 999.0, 42_000.0, 6.7e6, 2.1e9):
        scale = nice_max(peak)
        assert scale >= peak
        assert scale < peak * 4.01, "the peak is never squashed below a quarter of the plot"
    assert "function tickLabel(value)" in JS
    assert 'tickLabel(scale * fr)' in JS


def test_baseline_and_bar_caps() -> None:
    assert '<div class="grid-line${fr === 0 ? " base" : ""}"' in JS
    assert ".grid-line.base{border-top-color:#2a303a}" in CSS
    assert ".day-column::after{" not in CSS
    assert 'segment.classList.toggle("top", visible)' in JS
    assert "gap:0;height:100%" in CSS
    assert ".segment.top{border-radius:3px 3px 0 0}" in CSS
    # Existing motion and touch contracts stay intact.
    assert "@keyframes growIn{from{transform:scaleY(0)}to{transform:scaleY(1)}}" in CSS
    assert ".day-column.dim{opacity:.42}" in CSS


def test_cumulative_line_has_an_endpoint_marker() -> None:
    assert 'id="cumulative-end"' in HTML
    assert 'setAttr($("cumulative-end"), "cx"' in JS
    assert 'setAttr($("cumulative-end"), "cy"' in JS
    assert ".cumulative-plot .end{fill:var(--accent)" in CSS
