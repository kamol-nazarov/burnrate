"""Remote-only native Attention workflow with production services and fake evidence."""
import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import pytest

from tests_spend.attention_fakes import MemoryRepository, Prices, NOW, SCOPE, Service, set_quota, attempt, gap
from tests_spend.test_frontend_viewports import CHROME, FixtureHandler, _chrome_dump
from tests_spend.test_performance_phase2 import _free_port, _serve_on

ROOT = Path(__file__).resolve().parents[1]


class AttentionHandler(FixtureHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = (ROOT / "spend_web/index.html").read_text(encoding="utf-8")
            page = page.replace("</head>", '<script src="/attention-test.js" defer></script></head>')
            page = page.replace("</body>", '<pre id="attention-result"></pre></body>')
            self._send(page.encode(), "text/html")
        elif path == "/attention-test.js":
            self._send((ROOT / "tests_spend/attention_browser_cases.js").read_bytes(), "application/javascript")
        elif path == "/api/attention":
            self._send(json.dumps(self.server.attention.read()).encode(), "application/json")
        else:
            super().do_GET()

    def do_POST(self):
        assert urlparse(self.path).path == "/api/attention"
        assert self.headers.get("X-BURNRATE-Request") == "1"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(json.dumps(self.server.attention.update(body)).encode(), "application/json")


@pytest.mark.browser
@pytest.mark.parametrize("width", [390, 1440])
def test_attention_native_actions_and_reopen(width, tmp_path):
    if not CHROME.exists():
        pytest.skip("Chrome required")
    repository = MemoryRepository()
    set_quota(repository, 82)
    repository.attempts = [attempt()]
    repository.gaps = [gap()]
    service = Service(repository, Prices(), "America/New_York", lambda: NOW)
    with patch("spend_app.codex_quota.current_scope", lambda: SCOPE):
        assert service.evaluate()
    server = _serve_on(_free_port(), AttentionHandler, "populated")
    server.attention = service
    try:
        page = _chrome_dump(f"http://127.0.0.1:{server.server_port}/?window=1d", width, tmp_path, budget_ms=15000)
    finally:
        server.shutdown()
        server.server_close()
    found = re.search(r'<pre id="attention-result">(.*?)</pre>', page, re.S)
    assert found, "Attention browser workflow did not finish"
    result = json.loads(html.unescape(found[1]))
    assert result["pass"], result.get("error")
    quota = next(item for item in service.read()["current"] if item["family"] == "quota")
    assert quota["acknowledged"] and not quota["snoozed"]
