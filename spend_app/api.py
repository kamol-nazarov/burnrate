from __future__ import annotations

import hashlib
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from spend_app import __version__


ASSET_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
ASSET_REVALIDATE_CACHE = "no-cache"
GZIP_MINIMUM_SIZE = 1024
ASSET_SETTLE_SECONDS = 5
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
)
_ASSET_VERSIONS: dict[str, tuple[tuple[int, int], str]] = {}


def asset_version(path: Path) -> str:
    """Content hash of a static asset, cached by file mtime and size.

    The HTML references ``spend.js?v=<hash>`` so browsers may cache assets
    immutably: any edit changes the hash, which changes the URL, so a stale
    file can never be pinned by a cache.
    """
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _ASSET_VERSIONS.get(str(path))
    # A file edited within the last few seconds is always re-hashed: two
    # same-size writes inside one mtime tick would otherwise share a stale hash.
    settled = time.time() - stat.st_mtime > ASSET_SETTLE_SECONDS
    if cached and cached[0] == signature and settled:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    if settled:
        _ASSET_VERSIONS[str(path)] = (signature, digest)
    return digest


def render_index(web_root: Path) -> str:
    html = (web_root / "index.html").read_text(encoding="utf-8")
    for name, marker in (("spend.css", "42"), ("spend.js", "42"), ("favicon.svg", "1")):
        html = html.replace(f"/{name}?v={marker}", f"/{name}?v={asset_version(web_root / name)}")
    return html


def _asset_cache_header(request: Request, path: Path) -> str:
    requested = request.query_params.get("v")
    if requested and requested == asset_version(path):
        return ASSET_IMMUTABLE_CACHE
    return ASSET_REVALIDATE_CACHE

from spend_app.aggregate import (
    WINDOW_SPECS,
    aggregate_entity,
    aggregate_health,
    aggregate_health_cached,
    aggregate_nav,
    aggregate_summary,
    aggregate_summary_cached,
    canonicalize_window,
    data_clock,
    record_summary_request,
)
from spend_app.config import Settings, load_settings
from spend_app.db import initialize
from spend_app.pricing import PricingEngine
from spend_app.limits import collect_limits
from spend_app.scheduler import create_scheduler


SPEND_WINDOWS = set(WINDOW_SPECS) | {"7d", "30d", "MTD", "YTD", "All"}


def create_app(
    settings: Settings | None = None,
    *,
    enable_scheduler: bool = True,
    now: datetime | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    initialize(settings.database_path)
    pricing = PricingEngine.load(settings.pricing_path)
    scheduler = create_scheduler(settings, pricing) if enable_scheduler else None
    resource_stack = ExitStack()
    web_root = Path(resource_stack.enter_context(as_file(files("spend_web"))))

    def current_now() -> datetime:
        return (now or datetime.now(UTC)).astimezone(UTC)

    def slot_now() -> datetime:
        # Live requests describe the data as of the latest completed ingest
        # cycle, so every viewer and the pre-warm job share one clock until
        # the next cycle; an injected clock (tests) is passed through untouched.
        if now is not None:
            return current_now()
        return data_clock(settings.database_path, settings.local_ingest_interval_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            aggregate_summary(
                database_path=settings.database_path,
                pricing=pricing,
                window_key="1d",
                tool="all",
                timezone=settings.timezone,
                cache_threshold=settings.cache_hit_threshold,
                cadence_seconds=settings.local_ingest_interval_seconds,
                now=current_now(),
            )
        except Exception:
            pass
        if scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            resource_stack.close()

    app = FastAPI(title="BURNRATE", version=__version__, lifespan=lifespan)
    app.state._resource_stack = resource_stack
    # Compress HTML, CSS, JS and JSON for clients that accept it (143 KB of
    # static assets otherwise travel uncompressed over the tailnet).
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    def _window_or_400(window: str) -> str:
        if window not in SPEND_WINDOWS:
            raise HTTPException(status_code=400, detail="unsupported spend window")
        return canonicalize_window(window)

    @app.get("/api/spend/summary")
    def spend_summary(
        window: str = Query(default="1d"),
        tool: str = Query(default="all"),
    ) -> dict:
        resolved = _window_or_400(window)
        record_summary_request(resolved, tool)
        return aggregate_summary_cached(
            database_path=settings.database_path,
            pricing=pricing,
            window_key=resolved,
            tool=tool,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            cadence_seconds=settings.local_ingest_interval_seconds,
            now=slot_now(),
        )

    @app.get("/api/spend/nav")
    def spend_nav() -> dict:
        return aggregate_nav(
            database_path=settings.database_path,
            pricing=pricing,
            timezone=settings.timezone,
            cadence_seconds=settings.local_ingest_interval_seconds,
            now=current_now(),
        )

    @app.get("/api/spend/entity")
    def spend_entity(
        kind: str = Query(..., pattern="^(model|tool)$"),
        key: str = Query(..., min_length=1),
        window: str = Query(default="1d"),
    ) -> dict:
        resolved = _window_or_400(window)
        return aggregate_entity(
            database_path=settings.database_path,
            pricing=pricing,
            kind=kind,
            key=key,
            window_key=resolved,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            now=slot_now(),
        )

    @app.get("/api/spend/health")
    def spend_health() -> dict:
        return aggregate_health_cached(
            database_path=settings.database_path,
            timezone=settings.timezone,
            now=slot_now(),
        )

    @app.get("/api/spend/limits")
    def spend_limits() -> dict:
        return collect_limits()

    @app.get("/api/spend")
    def spend_root() -> dict:
        return {
            "service": "BURNRATE",
            "version": __version__,
            "endpoints": [
                "/api/spend/summary",
                "/api/spend/nav",
                "/api/spend/entity",
                "/api/spend/health",
                "/api/spend/limits",
            ],
        }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/")
    def spend_frontend():
        return HTMLResponse(
            render_index(web_root),
            headers={"Cache-Control": ASSET_REVALIDATE_CACHE},
        )

    @app.get("/spend.css")
    def spend_css(request: Request):
        path = web_root / "spend.css"
        return FileResponse(
            path,
            media_type="text/css",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/spend.js")
    def spend_js(request: Request):
        path = web_root / "spend.js"
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/favicon.svg")
    def favicon(request: Request):
        path = web_root / "favicon.svg"
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    return app


app = create_app()
