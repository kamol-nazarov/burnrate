"""Phase 1 of docs/PERFORMANCE_PLAN.md: faster first paint.

Gates: compressed transfers, hash-versioned immutable assets with a
revalidated HTML shell, non-blocking web fonts, and the early data prefetch
that starts before spend.js has downloaded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.api import ASSET_IMMUTABLE_CACHE, ASSET_REVALIDATE_CACHE, asset_version, create_app
from spend_app.config import Settings


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "spend_web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "spend.js").read_text(encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        cursor_import_path=tmp_path / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    return TestClient(create_app(settings, enable_scheduler=False))


def test_assets_and_json_are_gzip_compressed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for path in ("/", "/spend.js", "/spend.css", "/api/spend/summary?window=1d&tool=all"):
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200, path
        assert response.headers.get("content-encoding") == "gzip", path
    raw = client.get("/spend.js", headers={"Accept-Encoding": "identity"})
    assert raw.headers.get("content-encoding") is None
    compressed = client.get("/spend.js", headers={"Accept-Encoding": "gzip"})
    # The streamed response is chunked, so compare bytes actually transferred.
    assert compressed.num_bytes_downloaded < 30_000 < raw.num_bytes_downloaded
    assert compressed.content == raw.content


def test_html_references_hash_versioned_assets_and_revalidates(tmp_path: Path) -> None:
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == ASSET_REVALIDATE_CACHE
    body = page.text
    assert "?v=42" not in body
    assert "/favicon.svg?v=1" not in body
    js_version = hashlib.sha256((WEB / "spend.js").read_bytes()).hexdigest()[:12]
    css_version = hashlib.sha256((WEB / "spend.css").read_bytes()).hexdigest()[:12]
    icon_version = hashlib.sha256((WEB / "favicon.svg").read_bytes()).hexdigest()[:12]
    assert f"/spend.js?v={js_version}" in body
    assert f"/spend.css?v={css_version}" in body
    assert f"/favicon.svg?v={icon_version}" in body
    assert asset_version(WEB / "spend.js") == js_version


def test_matching_asset_version_is_immutable_and_stale_version_revalidates(tmp_path: Path) -> None:
    client = _client(tmp_path)
    current = asset_version(WEB / "spend.js")
    fresh = client.get(f"/spend.js?v={current}")
    assert fresh.status_code == 200
    assert fresh.headers["cache-control"] == ASSET_IMMUTABLE_CACHE
    assert fresh.headers.get("etag")
    stale = client.get("/spend.js?v=41")
    assert stale.headers["cache-control"] == ASSET_REVALIDATE_CACHE
    bare = client.get("/spend.css")
    assert bare.headers["cache-control"] == ASSET_REVALIDATE_CACHE
    icon = client.get(f"/favicon.svg?v={asset_version(WEB / 'favicon.svg')}")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.headers["cache-control"] == ASSET_IMMUTABLE_CACHE
    api = client.get("/api/spend/health")
    assert api.headers["cache-control"] == "no-store"


def test_asset_version_tracks_content_changes(tmp_path: Path) -> None:
    asset = tmp_path / "asset.js"
    asset.write_text("const a = 1;", encoding="utf-8")
    first = asset_version(asset)
    asset.write_text("const a = 2;", encoding="utf-8")
    second = asset_version(asset)
    assert first != second
    assert len(second) == 12


def test_web_fonts_do_not_block_first_paint() -> None:
    head = HTML.split("</head>", 1)[0]
    assert "fonts.googleapis.com" not in head
    assert "fonts.gstatic.com" not in head
    assert "preconnect" not in head
    css = (WEB / "spend.css").read_text(encoding="utf-8")
    assert "system-ui" in css
    assert "Segoe UI" in css
    assert "Inter" not in css
    assert "JetBrains" not in css
    # The local stylesheet still loads before the script, and the script stays deferred.
    assert head.index('href="/spend.css?v=42"') < head.index('src="/spend.js?v=42" defer')


def test_head_prefetch_starts_data_requests_before_the_script() -> None:
    head = HTML.split("</head>", 1)[0]
    assert "window.__prefetch" in head
    assert head.index("window.__prefetch") < head.index('src="/spend.js?v=42"')
    assert '"15m", "30m", "1h", "3h", "6h", "12h", "1d", "1w", "1mo", "mtd", "ytd", "all"' in head
    assert 'fetch(`/api/spend/summary?window=${encodeURIComponent(key)}&tool=all`, {cache: "no-store"})' in head
    assert 'fetch("/api/spend/health", {cache: "no-store"})' in head
    load = JS.split("async function loadSummary", 1)[1].split("\nasync function ", 1)[0]
    assert "window.__prefetch.window === state.window && !state.summary" in load
    assert "window.__prefetch = null;" in load
    assert "prefetch?.summary" in load and "prefetch?.health" in load
    assert "async function jsonFetch(url, prefetched)" in JS
    assert 'await (prefetched || fetch(url, {cache:"no-store"}))' in JS
