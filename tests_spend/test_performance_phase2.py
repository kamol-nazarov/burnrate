"""Phase 2 of docs/PERFORMANCE_PLAN.md: instant paint from a local snapshot.

A repeat visit paints the last successful payload for the requested window
before any fetch returns, but that snapshot is never presented as live: the
navbar reads Stale until a real fetch succeeds, and a failed API still leaves
the real numbers on screen with the stale marker.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests_spend.test_frontend_viewports import CHROME, FixtureHandler, _chrome_dump, _probe


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")


def test_snapshot_storage_is_versioned_bounded_and_best_effort() -> None:
    assert 'const SNAPSHOT_PREFIX = "burnrate:snapshot:v1:";' in JS
    assert "const SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000;" in JS
    for name in ("readSnapshot", "writeSnapshot", "pruneSnapshots", "paintSnapshot"):
        assert f"function {name}(" in JS, name
    read = JS.split("function readSnapshot", 1)[1].split("\nfunction ", 1)[0]
    assert "age > SNAPSHOT_MAX_AGE_MS" in read
    assert "catch" in read and "return null" in read
    write = JS.split("function writeSnapshot", 1)[1].split("\nfunction ", 1)[0]
    assert "storedAt: new Date().toISOString()" in write
    assert "catch {}" in write
    prune = JS.split("function pruneSnapshots", 1)[1].split("\nfunction ", 1)[0]
    assert '!key.startsWith(SNAPSHOT_PREFIX)' in prune
    assert "pruneSnapshots();\nloadSummary();" in JS


def test_snapshot_is_painted_first_but_never_presented_as_live() -> None:
    paint = JS.split("function paintSnapshot", 1)[1].split("\nfunction ", 1)[0]
    assert 'status: "stale", snapshot: true' in paint
    assert "renderOverview()" in paint
    load = JS.split("async function loadSummary", 1)[1].split("\nasync function ", 1)[0]
    assert "state.summary.window?.key !== state.window) && paintSnapshot(state.window)" in load
    assert "const showLoading = !painted && (!background || !state.summary);" in load
    assert "writeSnapshot(state.window, data, health);" in load
    assert load.index("clearError();") < load.index("writeSnapshot(")
    navbar = JS.split("function renderNavbar", 1)[1].split("\nfunction ", 1)[0]
    assert "payload?.snapshot ? `as of ${stamp}`" in navbar
    assert 'label.textContent = "Stale";' in navbar


class OfflineHandler(FixtureHandler):
    """Serves the dashboard shell but answers every API call with 503."""

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path.startswith("/api/"):
            self._send(json.dumps({"detail": "offline"}).encode("utf-8"), "application/json", 503)
            return
        super().do_GET()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _serve_on(port: int, handler, fixture: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.fixture = fixture
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_repeat_visit_paints_snapshot_and_stays_stale_when_api_is_down(tmp_path: Path) -> None:
    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed; snapshot evidence requires Chrome")
    port = _free_port()
    url = f"http://127.0.0.1:{port}/?probe=1&window=1d"
    # Visit 1: live fixture; the page stores its snapshot for this origin.
    live = _serve_on(port, FixtureHandler, "populated")
    try:
        first = _probe(_chrome_dump(url, 1440, tmp_path, budget_ms=12_000))
    finally:
        live.shutdown()
        live.server_close()
    assert first["status"] == "live"
    assert first["tracked"] not in {"", "—"}
    # Visit 2: same origin and Chrome profile, but every API call fails.
    offline = _serve_on(port, OfflineHandler, "populated")
    try:
        html = _chrome_dump(url, 1440, tmp_path, budget_ms=12_000)
    finally:
        offline.shutdown()
        offline.server_close()
    second = _probe(html)
    assert second["tracked"] == first["tracked"], second
    assert second["status"] == "stale", second
    assert second["loading"] is False
    assert "as of" in html
    assert 'id="error-banner" role="alert"' in html and "hidden" not in html.split('id="error-banner"', 1)[1].split(">", 1)[0]
