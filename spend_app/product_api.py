"""Local product endpoints. No provider readers or harness mutation."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spend_app.db import connect
from spend_app.plan_service import PlanConflict, PlanError, apply_mutation, list_plans


def product_router(settings, pricing=None):
    router = APIRouter()
    allowed = {"127.0.0.1", "localhost", "::1"}
    allowed.update(
        host.strip().lower()
        for host in os.getenv("BURNRATE_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    external_origins = {
        origin.strip()
        for origin in os.getenv("BURNRATE_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }

    def today():
        return datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date()

    @router.get("/api/subscriptions")
    def plans():
        with connect(settings.database_path) as connection:
            return {**list_plans(connection, today()), "timezone": settings.timezone}

    @router.get("/api/subscriptions/value")
    def subscription_value(period: str = "this_month"):
        if period not in ("this_month", "last_month"):
            return JSONResponse(
                {"error": f"Invalid period '{period}'. Use 'this_month' or 'last_month'."},
                status_code=422,
            )
        resolved_pricing = pricing
        if resolved_pricing is None:
            from spend_app.pricing import PricingEngine

            resolved_pricing = PricingEngine.load(settings.pricing_path)
        from spend_app.plans_value_store import fetch_plans_value_report

        with connect(settings.database_path) as connection:
            report = fetch_plans_value_report(
                connection=connection,
                settings=settings,
                pricing=resolved_pricing,
                period=period,
                as_of=datetime.now(UTC),
            )
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @router.post("/api/subscriptions")
    async def mutate(request: Request):
        # JSON + a non-simple header prevent HTML form CSRF; exact Origin and
        # Host checks also reject DNS rebinding and cross-origin fetches.
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit("//" + host)
            origin = urlsplit(request.headers.get("origin", ""))
            same_scheme = (
                origin.scheme == request.url.scheme
                or request.headers.get("origin") in external_origins
            )
            trusted = (
                parsed.hostname in allowed
                and origin.scheme in {"http", "https"}
                and same_scheme
                and origin.netloc == host
            )
            trusted = (
                trusted
                and not parsed.username
                and not parsed.password
                and not origin.path
                and not origin.query
                and not origin.fragment
            )
        except ValueError:
            trusted = False
        if not trusted or request.headers.get("x-burnrate-request") != "1":
            return JSONResponse(
                {"error": "Write rejected: use the configured BURNRATE origin"},
                status_code=403,
            )
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse({"error": "Use application/json"}, status_code=415)
        raw = await request.body()
        if len(raw) > 16384:
            return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            body = json.loads(
                raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            with connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                result = apply_mutation(connection, body, today())
            return result
        except PlanConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except (ValueError, TypeError, KeyError, OverflowError):
            return JSONResponse(
                {"error": "Invalid subscription payload"}, status_code=422
            )
        except sqlite3.Error:
            return JSONResponse(
                {
                    "error": "Could not persist the change. Retry with the same request identifier."
                },
                status_code=503,
            )

    return router
