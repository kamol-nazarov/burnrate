"""Real production JS/DOM with deferred network responses and manual poll ticks."""
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests_spend.test_frontend_viewports import CHROME, FixtureHandler, _chrome_dump
from tests_spend.test_performance_phase2 import _free_port, _serve_on

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    "resume_diagnostics_ok", "resume_diagnostics_fail", "resume_detail_ok", "resume_detail_fail",
    "ownership", "ownership_fail", "ranges", "range_failure", "detail_back", "detail_back_home",
    "late_detail_ok", "late_detail_fail", "late_diagnostics_ok", "late_diagnostics_fail",
    "health_ok", "health_fail", "health_across_ticks", "poll_summary", "poll_detail", "poll_diagnostics",
    "entity_identity", "cancel", "cache_reuse", "mismatched_response", "abort_transport", "standalone_startup",
    "snapshot_valid", "snapshot_expired", "snapshot_mismatch", "snapshot_stored_expired",
]


class RaceHandler(FixtureHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            html = self.server.assets["index.html"].decode("utf-8")
            html = html.replace("<head>", '<head><script src="/race_boot.js"></script>')
            html = html.replace("</head>", '<script src="/race_cases.js" defer></script></head>')
            html = html.replace("</body>", '<pre id="race-result"></pre></body>')
            self._send(html.encode(), "text/html")
        elif path == "/race_boot.js":
            fixtures = {
                name: json.loads((ROOT / f"tests_spend/fixtures/frontend_{name}.json").read_text())
                for name in ("summary", "entity", "health")
            }
            boot = "window.RACE_FIXTURES=" + json.dumps(fixtures) + ";"
            boot += "window.RACE_CASE=" + json.dumps(self.server.case) + ";"
            boot += (ROOT / "tests_spend/race_boot.js").read_text(encoding="utf-8")
            self._send(boot.encode(), "application/javascript")
        elif path == "/race_cases.js":
            self._send((ROOT / "tests_spend/race_cases.js").read_bytes(), "application/javascript")
        elif path.startswith("/api/"):
            raise AssertionError("All API traffic must use controlled deferred responses")
        elif path.lstrip("/") in self.server.assets:
            if path == "/request-state.js" and self.server.case == "standalone_startup":
                self._send(b"/* compatibility script intentionally empty */", "application/javascript")
                return
            self._send(self.server.assets[path.lstrip("/")], "text/css" if path.endswith(".css") else "application/javascript")
        else:
            super().do_GET()


@pytest.mark.browser
@pytest.mark.parametrize("case", CASES)
def test_production_request_lifecycle(case, tmp_path):
    if not CHROME.exists():
        pytest.skip("Chrome is required for production DOM race coverage")
    server = _serve_on(_free_port(), RaceHandler, "populated")
    server.case = case
    assets = ("index.html", "spend.js", "spend.css", "request-state.js")
    if os.environ.get("BURNRATE_RACE_BASELINE") == "1":
        # Read the reviewed source into the test server; never reset or edit
        # the working tree to demonstrate pre-fix failures.
        server.assets = {
            name: subprocess.check_output(
                ["git", "show", "3b013244ca3688856327776aca46928579668cdb:spend_web/" + name],
                cwd=ROOT,
            )
            for name in assets if name != "request-state.js"
        }
    else:
        server.assets = {name: (ROOT / "spend_web" / name).read_bytes() for name in assets}
    try:
        html = _chrome_dump(f"http://127.0.0.1:{server.server_port}/?window=1d", 1440, tmp_path, budget_ms=3000)
    finally:
        server.shutdown()
        server.server_close()
    match = re.search(r'<pre id="race-result">(.*?)</pre>', html, re.S)
    assert match and match[1], "Race runner did not finish"
    import html as html_module
    result = json.loads(html_module.unescape(match[1]))
    assert result["pass"], result.get("error")
