"""Action Center's existing-token, exact-origin, bounded-JSON API boundary."""
import os
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from spend_app.attention_store import Conflict, Repository, Service
from spend_app.connection_api import access_allowed, payload


def attention_router(settings, pricing, *, service=None, token=None):
    router = APIRouter()
    service = service or Service(Repository(settings.database_path), pricing, settings.timezone)
    token = os.getenv("BURNRATE_ACCESS_TOKEN", "").strip() if token is None else token

    @router.get("/api/attention")
    def read(request: Request, detail: bool = True):
        if not access_allowed(request, token):
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            return JSONResponse(service.read(detail), headers={"Cache-Control": "no-store"})
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, AttributeError):
            return JSONResponse({"error": "Attention evaluation is unavailable"}, status_code=503)

    @router.post("/api/attention")
    async def update(request: Request):
        if not access_allowed(request, token):
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            body = await payload(request)
            return JSONResponse(await run_in_threadpool(service.update, body), headers={"Cache-Control": "no-store"})
        except PermissionError:
            return JSONResponse({"error": "Use the configured BURNRATE origin"}, status_code=403)
        except Conflict as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except (ValueError, TypeError, KeyError, AttributeError):
            return JSONResponse({"error": "Invalid attention operation or thresholds"}, status_code=422)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "Could not save attention; retry with the same request identifier"}, status_code=503)

    return router
