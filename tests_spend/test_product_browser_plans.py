import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from tests_spend.test_api import make_settings
from tests_spend.test_frontend_viewports import CHROME, FixtureHandler, _chrome_dump
from tests_spend.test_performance_phase2 import _free_port, _serve_on

ROOT = Path(__file__).resolve().parents[1]


class ProductHandler(FixtureHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            text = (ROOT / "spend_web/index.html").read_text(encoding="utf-8")
            text = text.replace(
                "</head>", '<script src="/product-test.js" defer></script></head>'
            )
            text = text.replace("</body>", '<pre id="product-result"></pre></body>')
            self._send(text.encode(), "text/html")
        elif path == "/product-test.js":
            self._send(
                (ROOT / "tests_spend/product_browser_cases.js").read_bytes(),
                "application/javascript",
            )
        elif path in {
            "/api/subscriptions",
            "/api/onboarding",
            "/api/subscriptions/value",
        }:
            response = self.server.client.get(self.path)
            self._send(response.content, "application/json", response.status_code)
        else:
            super().do_GET()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        response = self.server.client.post(
            self.path,
            content=raw,
            headers={
                key: self.headers[key]
                for key in ("Origin", "Host", "Content-Type", "X-BURNRATE-Request")
                if key in self.headers
            },
        )
        if not self.server.lost_reply and response.status_code == 200:
            self.server.lost_reply = True
            self._send(
                b'{"error":"Simulated lost acknowledgement"}', "application/json", 503
            )
        else:
            self._send(response.content, "application/json", response.status_code)


@pytest.mark.browser
@pytest.mark.parametrize("width", [390, 1440])
def test_plan_workflow_persists_and_recovers(width, tmp_path, monkeypatch):
    if not CHROME.exists():
        pytest.skip("Chrome required")
    monkeypatch.setattr("spend_app.subscriptions.SUBSCRIPTION_SEEDS", ())
    import spend_app.product_api as api

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 12, tzinfo=UTC)

    monkeypatch.setattr(api, "datetime", Clock)
    database = tmp_path / "plans.db"
    server = _serve_on(_free_port(), ProductHandler, "populated")
    server.lost_reply = False
    server.client = TestClient(
        create_app(make_settings(database), enable_scheduler=False),
        base_url=f"http://127.0.0.1:{server.server_port}",
    )
    try:
        result = _chrome_dump(
            f"http://127.0.0.1:{server.server_port}/?window=1d",
            width,
            tmp_path,
            budget_ms=25000,
        )
    finally:
        server.shutdown()
        server.server_close()
        server.client.close()
    found = re.search(r'<pre id="product-result">(.*?)</pre>', result, re.DOTALL)
    assert found and found[1], "Browser workflow did not finish"
    payload = json.loads(html.unescape(found[1]))
    assert payload["pass"], payload.get("error")
