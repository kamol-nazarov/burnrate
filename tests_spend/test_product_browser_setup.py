import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from tests_spend.test_api import add_fixture_event, make_settings
from tests_spend.test_frontend_viewports import CHROME, FixtureHandler, _chrome_dump
from tests_spend.test_performance_phase2 import _free_port, _serve_on

ROOT = Path(__file__).resolve().parents[1]
RUNNER = """
(async()=>{
 const el=id=>document.getElementById(id);
 const wait=async check=>{for(let i=0;i<400;i++){if(check())return;await new Promise(r=>setTimeout(r,25));}throw Error('UI did not settle');};
 const assert=(b,m)=>{if(!b)throw Error(m);};
 await wait(()=>el('setup-timezone').textContent.includes('Timezone:'));
 const upgraded=HISTORY;
 const phase=localStorage.getItem('setup-test-phase');
 if(!phase){
   assert(el('setup-guidance').hidden,'setup must remain optional for every user');
   el('nav-connect').click();await wait(()=>el('harness-manager').open);el('show-setup-help').click();
   assert(!el('setup-guidance').hidden,'setup guidance did not open');
   el('setup-dismiss').click();localStorage.setItem('setup-test-phase','dismissed');location.reload();return;
 }
 assert(el('setup-guidance').hidden,'dismissal did not persist');
 el('nav-connect').click();await wait(()=>el('harness-manager').open);el('show-setup-help').click();assert(!el('setup-guidance').hidden,'setup not resumable');
 el('setup-done').click();assert(localStorage.getItem('burnrate:setup:v1')==='complete','completion not persisted');
 assert(document.activeElement.id==='nav-connect','focus not restored');
 el('diagnostics-button').click();await wait(()=>!el('diagnostics-view').hidden&&el('source-guidance').children.length>0);
 assert(el('source-guidance').textContent.includes('Complete a supported turn'),'actionable next step missing');
 el('setup-result').textContent=JSON.stringify({pass:true});
})().catch(e=>document.getElementById('setup-result').textContent=JSON.stringify({pass:false,error:e.message}));
"""


class SetupHandler(FixtureHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            text = (ROOT / "spend_web/index.html").read_text(encoding="utf-8")
            text = text.replace(
                "</head>", '<script src="/setup-test.js" defer></script></head>'
            )
            text = text.replace("</body>", '<pre id="setup-result"></pre></body>')
            self._send(text.encode(), "text/html")
        elif path == "/setup-test.js":
            self._send(
                (
                    "const HISTORY=" + json.dumps(self.server.history) + ";" + RUNNER
                ).encode(),
                "application/javascript",
            )
        elif path in {"/api/onboarding", "/api/spend/health"}:
            response = self.server.client.get(path)
            self._send(response.content, "application/json", response.status_code)
        else:
            super().do_GET()


@pytest.mark.browser
@pytest.mark.parametrize("history", [False, True])
def test_setup_is_optional_resumable_and_persisted(history, tmp_path):
    if not CHROME.exists():
        pytest.skip("Chrome required")
    db = tmp_path / "setup.db"
    app = create_app(make_settings(db), enable_scheduler=False)
    if history:
        add_fixture_event(db)
    server = _serve_on(_free_port(), SetupHandler, "populated")
    server.history = history
    server.client = TestClient(app)
    try:
        page = _chrome_dump(
            f"http://127.0.0.1:{server.server_port}/?window=1d",
            390,
            tmp_path,
            budget_ms=15000,
        )
    finally:
        server.shutdown()
        server.server_close()
        server.client.close()
    match = re.search(r'<pre id="setup-result">(.*?)</pre>', page, re.DOTALL)
    assert match and match[1]
    result = json.loads(html.unescape(match[1]))
    assert result["pass"], result.get("error")
