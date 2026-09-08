"""Settings-only protected discovery, verification and connection mutations."""
import json
import os
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spend_app.connections import Conflict, Service
from spend_app.connection_paths import LocationError


def access_allowed(request, token):
    """The existing API-wide bearer boundary, shared with the application."""
    return not token or not request.url.path.startswith("/api/") or request.headers.get("authorization", "") == f"Bearer {token}"


def trusted(request, mutation=True):
    host = request.headers.get("host", "")
    allowed = {"127.0.0.1", "localhost", "::1"} | {s.strip().lower() for s in os.getenv("BURNRATE_ALLOWED_HOSTS", "").split(",") if s.strip()}
    origins = {s.strip() for s in os.getenv("BURNRATE_ALLOWED_ORIGINS", "").split(",") if s.strip()}
    try:
        parsed = urlsplit("//" + host)
        if parsed.hostname not in allowed or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        origin_text = request.headers.get("origin", "")
        if not origin_text and not mutation:
            return request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}
        origin = urlsplit(origin_text)
        return (origin.scheme in {"http", "https"} and origin.netloc == host and
                (origin.scheme == request.url.scheme or origin_text in origins) and
                not any((origin.username, origin.password, origin.path, origin.query, origin.fragment)))
    except ValueError:
        return False


async def payload(request):
    if not trusted(request) or request.headers.get("x-burnrate-request") != "1":
        raise PermissionError("Request rejected: use the configured BURNRATE origin.")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise LocationError("Use application/json.")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > 16384:
            raise LocationError("Request too large.")
        chunks.extend(chunk)
    body = json.loads(chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(body, dict):
        raise LocationError("Expected a JSON object.")
    return body


def connection_router(settings):
    router = APIRouter()
    service = Service(settings)

    @router.get("/api/connections")
    def status(request: Request):
        if not trusted(request, False):
            return JSONResponse({"error": "Use the configured BURNRATE origin."}, status_code=403)
        return JSONResponse(service.list(), headers={"Cache-Control": "no-store"})

    async def action(request, method):
        try:
            body = await payload(request)
            # Run bounded filesystem/vault calls outside the event loop using
            # FastAPI's existing worker pool, not a connection-specific worker.
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(method, body)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except LocationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "Invalid connection request."}, status_code=422)
        except (sqlite3.Error, OSError):
            return JSONResponse({"error": "Could not save. Retry with the same request identifier."}, status_code=503)

    @router.post("/api/connections/discover")
    async def discover(request: Request):
        return await action(request, lambda body: service.discover())

    @router.post("/api/connections/verify")
    async def verify(request: Request):
        return await action(request, service.verify)

    @router.post("/api/connections")
    async def mutate(request: Request):
        return await action(request, service.mutate)

    return router
