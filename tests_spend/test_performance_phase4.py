"""Phase 4 of docs/PERFORMANCE_PLAN.md: smoother frames.

The easing loop runs only while a value is still moving, bound elements are
tracked in a registry instead of a per-frame document query, below-the-fold
panels use content-visibility, and touch devices get shorter transitions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")
CSS = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")


def _region(start: str, end: str = "\nfunction ") -> str:
    return JS.split(start, 1)[1].split(end, 1)[0]


def test_easing_loop_is_demand_driven_and_stops_when_settled() -> None:
    assert "let easeFrame = null;" in JS
    assert "function scheduleEase()" in JS
    assert "if (easeFrame == null) easeFrame = requestAnimationFrame(tickEase);" in JS
    tick = _region("function tickEase()")
    assert "easeFrame = null;" in tick
    assert "if (pending) easeFrame = requestAnimationFrame(tickEase);" in tick
    assert "state.easeFrames += 1;" in tick
    assert "requestAnimationFrame(tickEase);\n}" not in tick.replace("if (pending) easeFrame = requestAnimationFrame(tickEase);", "")
    ease_fn = _region("function ease(key, target)")
    assert "if (state.disp[key] !== n) scheduleEase();" in ease_fn
    assert "scheduleEase();\nsetInterval(refreshCurrent, POLL_MS);" in JS
    assert "\nrequestAnimationFrame(tickEase);\n" not in JS


def test_easing_loop_behaviour_in_node() -> None:
    """Run the real ease/tickEase source under a fake rAF and count frames."""
    ease_src = "function ease(key, target)" + _region("function ease(key, target)", "\nlet easeFrame")
    loop_src = "let easeFrame = null;\n" + JS.split("let easeFrame = null;\n", 1)[1].split("\nfunction easeNodes", 1)[0]
    tick_src = "function tickEase()" + _region("function tickEase()")
    harness = f"""
const EASE_RATE = 0.13;
const finite = value => {{ if (value == null || value === "") return null; const n = Number(value); return Number.isFinite(n) ? n : null; }};
const state = {{targets: Object.create(null), disp: Object.create(null), easeFrames: 0}};
let painted = 0;
function paintEase() {{ painted++; }}
let queue = [];
function requestAnimationFrame(fn) {{ queue.push(fn); return queue.length; }}
{ease_src}
{loop_src}
{tick_src}
function flush() {{ let frames = 0; while (queue.length) {{ const fn = queue.shift(); fn(); frames++; if (frames > 10000) throw new Error('runaway'); }} return frames; }}
const idleFrames = flush();
ease("tracked", 10);
const settleFrames = flush();
const settledValue = state.disp.tracked;
const framesBefore = state.easeFrames;
const afterIdle = flush();
ease("tracked", 10);
const sameTarget = flush();
ease("tracked", 20);
const moveFrames = flush();
console.log(JSON.stringify({{idleFrames, settleFrames, settledValue, afterIdle, sameTarget, moveFrames, painted, framesBefore, framesAfter: state.easeFrames, finalValue: state.disp.tracked, queued: queue.length}}));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["idleFrames"] == 0, "no frames while nothing is animating"
    assert report["settleFrames"] == 1 and report["settledValue"] == 10, "a brand-new value snaps in one frame"
    assert report["afterIdle"] == 0, "the loop stops once settled"
    assert report["sameTarget"] == 0, "re-binding an unchanged target schedules nothing"
    assert 20 < report["moveFrames"] < 200, "a moving value eases over a bounded number of frames"
    assert abs(report["finalValue"] - 20) < 1e-6
    assert report["queued"] == 0


def test_bound_elements_come_from_a_registry_not_a_per_frame_query() -> None:
    paint = _region("function paintEase()")
    assert "easeNodes().forEach" in paint
    assert 'document.querySelectorAll("[data-ease]")' not in paint
    registry = _region("function easeNodes()")
    assert 'state.easeNodes = [...document.querySelectorAll("[data-ease]")]' in registry
    assert "function invalidateEaseNodes()" in JS
    reconcile = _region("function reconcileChildren(")
    assert "invalidateEaseNodes();" in reconcile
    bind = _region("function bindEase(")
    assert "invalidateEaseNodes();" in bind
    for view in ("overview", "detail", "diagnostics"):
        tail = JS.split(f'if (PROBE) writeProbe("{view}");', 1)[0][-320:]
        assert "invalidateEaseNodes();" in tail, view
    assert "easeFrames: state.easeFrames" in JS
    assert "easing: easeFrame != null" in JS


def test_below_the_fold_panels_are_contained_and_touch_transitions_are_short() -> None:
    assert ".heat-panel{margin-top:14px;content-visibility:auto;contain-intrinsic-size:auto 420px}" in CSS
    assert ".sessions-panel{margin-top:14px;content-visibility:auto;contain-intrinsic-size:auto 360px}" in CSS
    touch = CSS.split("@media(hover:none){", 1)[1].split("}\n}", 1)[0]
    assert ".segment" in touch and ".track i" in touch and "transition-duration:250ms" in touch
    # Reduced motion still wins: it follows the touch block and disables everything.
    assert CSS.index("@media(hover:none){") < CSS.index("@media(prefers-reduced-motion:reduce){")
