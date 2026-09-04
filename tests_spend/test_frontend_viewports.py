from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "spend_web"
FIXTURES = ROOT / "tests_spend" / "fixtures"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
VIEWPORTS = (390, 768, 1024, 1440, 1920)


def _json(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "BurnrateFixture/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        fixture = getattr(self.server, "fixture", "populated")
        if path in {"/", "/index.html"}:
            self._send((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/spend.css":
            self._send((WEB / "spend.css").read_bytes(), "text/css; charset=utf-8")
            return
        if path == "/spend.js":
            self._send((WEB / "spend.js").read_bytes(), "application/javascript; charset=utf-8")
            return
        if path == "/favicon.svg":
            self._send((WEB / "favicon.svg").read_bytes(), "image/svg+xml")
            return
        if path == "/api/spend/summary":
            names = {
                "empty": "frontend_summary_empty.json",
                "stale": "frontend_summary_stale.json",
                "unpriced": "frontend_summary_unpriced.json",
                "error": "frontend_summary_error.json",
            }
            self._send(_json(names.get(fixture, "frontend_summary.json")), "application/json")
            return
        if path == "/api/spend/entity":
            self._send(_json("frontend_entity.json"), "application/json")
            return
        if path == "/api/spend/health":
            name = "frontend_health_failed.json" if fixture in {"error", "stale"} else "frontend_health.json"
            self._send(_json(name), "application/json")
            return
        self._send(b"not found", "text/plain", 404)


def _serve(fixture: str) -> ThreadingHTTPServer:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), FixtureHandler)
    server.fixture = fixture
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _chrome_dump(url: str, width: int, tmp: Path, budget_ms: int = 8000) -> str:
    profile = tmp / f"chrome-{width}-{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    dump = tmp / f"dump-{width}.html"
    cmd = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--disable-dev-shm-usage",
        f"--user-data-dir={profile}",
        f"--window-size={width},1100",
        f"--virtual-time-budget={budget_ms}",
        "--dump-dom",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        check=False,
    )
    html = result.stdout or ""
    dump.write_text(html, encoding="utf-8")
    if "PROBE:" not in html and result.returncode != 0:
        raise AssertionError(f"chrome dump failed rc={result.returncode} stderr={result.stderr[-2000:]}")
    return html


def _probe(html: str) -> dict:
    marker = "PROBE:"
    if marker in html:
        raw = html.split(marker, 1)[1]
        raw = raw.split("</title>", 1)[0].split("<", 1)[0]
        return json.loads(raw)
    if 'id="probe-report"' in html:
        block = html.split('id="probe-report"', 1)[1].split("</pre>", 1)[0]
        text = block.split(">", 1)[1]
        return json.loads(text)
    raise AssertionError("probe report missing from dump-dom")


def test_js_and_css_syntax_gates() -> None:
    js = subprocess.run(["node", "--check", str(WEB / "spend.js")], capture_output=True, text=True, check=False)
    assert js.returncode == 0, js.stderr
    css = (WEB / "spend.css").read_text(encoding="utf-8")
    assert "animation-fill-mode:both" not in css
    assert "animation-fill-mode: both" not in css
    for width in ("1439px", "1199px", "1023px", "767px", "390px"):
        assert f"@media(max-width:{width})" in css
    assert "overflow-x:clip" in css
    assert "min-height:44px" in css


def test_viewports_have_no_horizontal_page_scroll(tmp_path: Path) -> None:
    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed; viewport evidence requires Chrome")
    server = _serve("populated")
    try:
        evidence = []
        for width in VIEWPORTS:
            html = _chrome_dump(
                f"http://127.0.0.1:{server.server_port}/?probe=1&window=1d",
                width,
                tmp_path,
                budget_ms=18_000,
            )
            report = _probe(html)
            evidence.append({"width": width, **report})
            assert report["overflow"] is False, report
            assert report["scrollWidth"] <= report["clientWidth"] + 1, report
            assert report["loading"] is False
            assert "$0.00" not in report["tracked"]
            assert report["minTarget"] >= 44, report
            assert all(label != "0.0%" for label in report.get("paygPct") or []), report
            assert report["meterHeight"] >= 32, report
            assert report["barStable"] is True, report
            shot = tmp_path / f"burnrate-{width}.png"
            subprocess.run(
                [
                    str(CHROME),
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    f"--user-data-dir={tmp_path / ('shot-' + str(width))}",
                    f"--window-size={width},1100",
                    "--virtual-time-budget=6000",
                    f"--screenshot={shot}",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"http://127.0.0.1:{server.server_port}/?probe=1&window=1d",
                ],
                capture_output=True,
                timeout=40,
                check=False,
            )
            assert shot.exists() and shot.stat().st_size > 1000
        (tmp_path / "viewport-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()


def test_state_dumps_loading_empty_stale_unpriced(tmp_path: Path) -> None:
    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed; state evidence requires Chrome")

    loading_server = _serve("populated")
    try:
        loading = urlopen(f"http://127.0.0.1:{loading_server.server_port}/", timeout=5).read().decode("utf-8")
        assert "burnrate-skeleton" in loading
        assert "$0.00" not in loading
        assert 'id="coverage-banner"' in loading
        assert 'class="burnrate-nav"' in loading
        assert 'class="loading"' in loading
        assert ">0 live<" not in loading
    finally:
        loading_server.shutdown()
        loading_server.server_close()

    cases = {
        "empty": "frontend_summary_empty.json",
        "stale": "frontend_summary_stale.json",
        "unpriced": "frontend_summary_unpriced.json",
        "error": "frontend_summary_error.json",
    }
    for name in cases:
        server = _serve(name)
        try:
            html = _chrome_dump(
                f"http://127.0.0.1:{server.server_port}/?probe=1&window=1d",
                1440,
                tmp_path / name,
            )
            report = _probe(html)
            if name == "empty":
                assert "No models in this window" in html or report["tracked"] in {"—", ""}
                assert report["tracked"] == "—"
                assert "$0.00" not in report["tracked"]
            if name == "stale":
                assert report["status"] == "stale"
            if name == "error":
                assert report["status"] == "error"
                assert "openai_admin" in html
            if name == "unpriced":
                assert "—" in html
                assert report["tracked"] == "—"
                assert "$0.00" not in report["tracked"]
                assert "cursor:claude-opus-5" in html
                assert "credential missing" in html or "Quota unavailable" in html
                assert "NO DATA" not in html
                assert "1 live" in html or "2 live" not in html
                assert all(label != "0.0%" for label in report.get("paygPct") or []), report
                assert not re.search(r"(?<!\d)0\.0%", html)
            assert report["overflow"] is False
            assert report["minTarget"] >= 44, report
        finally:
            server.shutdown()
            server.server_close()
