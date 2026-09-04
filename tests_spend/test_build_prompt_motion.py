"""Animation-fill, resting size, and looping-motion guards."""

from __future__ import annotations

import re

from tests_spend.test_build_prompt_harness import keyframes, sources


LOOPING_NAMES = {"pillBreathe", "liveDot", "livePulse", "sweep", "ping"}


def test_animation_fill_mode_both_is_forbidden() -> None:
    html, css, js = sources()
    for blob in (html, css, js):
        collapsed = blob.replace(" ", "").lower()
        assert "animation-fill-mode:both" not in collapsed
        assert "animationfillmode:both" not in collapsed
        assert "fill-mode: both" not in blob.lower() or "animation-fill-mode: both" not in blob.lower()


def test_looping_effects_never_animate_height_or_width() -> None:
    _html, css, _js = sources()
    frames = keyframes(css)
    for name in LOOPING_NAMES:
        assert name in frames, name
        body = frames[name]
        assert not re.search(r"(^|[;{])\s*height\s*:", body), name
        assert not re.search(r"(^|[;{])\s*width\s*:", body), name
    # Data-change height/width transitions may use the specified curve; looping must not.
    assert "transition:height 560ms cubic-bezier" in css.replace(" ", "") or "transition:height" in css
    infinite = [
        line
        for line in css.splitlines()
        if "infinite" in line and "animation" in line
    ]
    assert infinite
    for line in infinite:
        assert "height" not in line or "min-height" in line


def test_one_shot_data_animations_rest_at_their_final_size() -> None:
    _html, css, _js = sources()
    frames = keyframes(css)
    # growIn/fillIn/rowIn are one-shot; without fill-mode:both they rest at the `to` keyframe.
    assert "from{transform:scaleY(0)}" in frames["growIn"].replace(" ", "")
    assert "to{transform:scaleY(1)}" in frames["growIn"].replace(" ", "")
    grow_uses = [line for line in css.splitlines() if "growIn" in line]
    assert grow_uses
    assert all("infinite" not in line for line in grow_uses)
    assert "burnrate-meter__bar" not in css


def test_prefers_reduced_motion_disables_looping_and_transitions() -> None:
    _html, css, js = sources()
    assert "@media(prefers-reduced-motion:reduce)" in css.replace(" ", "")
    reduced = css.split("@media(prefers-reduced-motion:reduce)", 1)[1].split("@", 1)[0]
    assert "animation:none!important" in reduced.replace(" ", "")
    assert "transition:none!important" in reduced.replace(" ", "")
    ranges = js.split("function renderRanges", 1)[1].split("function changeRange", 1)[0]
    assert "prefers-reduced-motion" in ranges
    assert 'behavior: reduceMotion ? "auto" : "smooth"' in ranges


def test_easing_loop_is_one_raf_at_thirteen_percent() -> None:
    _html, _css, js = sources()
    assert "const EASE_RATE = 0.13;" in js
    assert "function tickEase()" in js
    assert "requestAnimationFrame(tickEase)" in js
    tick = js.split("function tickEase()", 1)[1].split("function setQuery", 1)[0]
    assert "delta * EASE_RATE" in tick
    assert "if (changed) paintEase();" in tick
    assert "innerHTML" not in tick
    assert "renderOverview" not in tick
    schedule = js.split("function scheduleEase()", 1)[1].split("function easeNodes", 1)[0]
    assert "prefers-reduced-motion" in js
    assert "REDUCE_MOTION" in schedule
    assert "requestAnimationFrame(tickEase)" in schedule


def test_live_rows_stagger_and_nodata_rows_have_no_looping_effect() -> None:
    html, css, js = sources()
    assert "index * 260" in js
    assert ".activity-row.live" in css
    assert ".activity-row.nodata" in css
    nodata = css.split(".activity-row.nodata", 1)[1][:400]
    assert "animation:none" in nodata.replace(" ", "")
    assert "NO DATA" in js
    assert "LIVE" in js
    assert "sweep" in js
    assert 'id="live-pill"' in html
