from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app import __version__
from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic_admin
from spend_app.adapters.codex_local import ingest as ingest_codex_local
from spend_app.adapters.claude_local import ingest as ingest_claude_local
from spend_app.adapters.cursor_admin import ingest as ingest_cursor_admin
from spend_app.adapters.cursor_csv import ingest as ingest_cursor_csv
from spend_app.adapters.cursor_local import ingest as ingest_cursor_local
from spend_app.adapters.opencode_local import ingest as ingest_opencode_local
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.adapters.traycer_local import ingest as ingest_traycer_local
from spend_app.config import Settings, load_settings
from spend_app.db import initialize
from spend_app.db import connect
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription, materialize_subscription_days


BACKFILL_SOURCES = (
    "codex-local",
    "claude-local",
    "traycer-local",
    "cursor-local",
    "cursor-csv",
    "opencode-local",
    "openai-admin",
    "anthropic-admin",
    "cursor-admin",
)

DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 17331
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})
_RUNTIME_IMPORTS = (
    "fastapi",
    "uvicorn",
    "apscheduler",
    "yaml",
    "dotenv",
    "httpx",
    "tzdata",
    "starlette",
)
_WEB_ASSETS = ("index.html", "spend.css", "spend.js", "favicon.svg")


def _parse_utc(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def default_source_path(source: str, settings: Settings) -> str:
    home = Path.home()
    return {
        "codex-local": str(home / ".codex" / "sessions" / "**" / "*.jsonl"),
        "claude-local": str(home / ".claude" / "projects" / "**" / "*.jsonl"),
        "traycer-local": str(home / ".traycer" / "host" / "epic-state" / "**" / "chat" / "chat.db"),
        "cursor-local": str(
            home / ".cursor" / "projects" / "**" / "sdk-agent-store" / "*" / "index.db"
        ),
        "opencode-local": str(home / ".local" / "share" / "opencode" / "opencode.db"),
        "cursor-csv": str(settings.cursor_import_path),
    }[source]


def _local_path(source: str, session_glob: str | None, settings: Settings) -> str:
    return session_glob if session_glob else default_source_path(source, settings)


def require_loopback_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized in _WILDCARD_HOSTS or normalized not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}; BURNRATE listens on {DEFAULT_SERVE_HOST} only"
        )
    return host


def _cmd_init(settings: Settings) -> int:
    data_root = settings.database_path.parent
    data_root.mkdir(parents=True, exist_ok=True)
    initialize(settings.database_path)
    print(
        json.dumps(
            {
                "database": str(settings.database_path),
                "dataRoot": str(data_root),
                "status": "initialized",
            }
        )
    )
    return 0


def _check_python() -> dict[str, str]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] != (3, 12):
        return {"id": "python", "status": "fail", "detail": f"{version} (need 3.12)"}
    return {"id": "python", "status": "ok", "detail": version}


def _check_imports() -> dict[str, str]:
    missing: list[str] = []
    for name in _RUNTIME_IMPORTS:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return {"id": "imports", "status": "fail", "detail": "missing: " + ", ".join(missing)}
    return {"id": "imports", "status": "ok", "detail": ", ".join(_RUNTIME_IMPORTS)}


def _check_tzdata(timezone: str) -> dict[str, str]:
    try:
        import tzdata  # noqa: F401
        ZoneInfo(timezone)
    except Exception as exc:
        return {"id": "tzdata", "status": "fail", "detail": f"{timezone}: {exc}"}
    return {"id": "tzdata", "status": "ok", "detail": timezone}


def _check_database(database_path: Path) -> dict[str, str]:
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        probe = database_path.parent / ".burnrate-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        connection = sqlite3.connect(database_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
    except Exception as exc:
        return {"id": "database", "status": "fail", "detail": str(exc)}
    if integrity != "ok":
        return {"id": "database", "status": "fail", "detail": f"integrity_check={integrity}"}
    return {"id": "database", "status": "ok", "detail": str(database_path)}


def _check_pricing(pricing_path: Path) -> dict[str, str]:
    try:
        engine = PricingEngine.load(pricing_path)
    except Exception as exc:
        return {"id": "pricing", "status": "fail", "detail": str(exc)}
    cards = list(Path(pricing_path).glob("*.yaml"))
    if not engine.prices or not cards:
        return {
            "id": "pricing",
            "status": "fail",
            "detail": f"no pricing cards loaded from {pricing_path}",
        }
    return {
        "id": "pricing",
        "status": "ok",
        "detail": f"{len(engine.prices)} prices from {len(cards)} cards",
    }


def _check_web_assets() -> dict[str, str]:
    try:
        root = files("spend_web")
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        return {"id": "web_assets", "status": "fail", "detail": f"spend_web is not importable: {exc}"}
    missing = [name for name in _WEB_ASSETS if not root.joinpath(name).is_file()]
    if missing:
        return {"id": "web_assets", "status": "fail", "detail": "missing " + ", ".join(missing)}
    return {"id": "web_assets", "status": "ok", "detail": ", ".join(_WEB_ASSETS)}


def _check_bind() -> dict[str, str]:
    try:
        require_loopback_host(DEFAULT_SERVE_HOST)
    except ValueError as exc:
        return {"id": "bind", "status": "fail", "detail": str(exc)}
    return {
        "id": "bind",
        "status": "ok",
        "detail": f"{DEFAULT_SERVE_HOST}:{DEFAULT_SERVE_PORT}",
    }


def _cmd_doctor(settings: Settings) -> int:
    checks = [
        _check_python(),
        _check_imports(),
        _check_tzdata(settings.timezone),
        _check_database(settings.database_path),
        _check_pricing(settings.pricing_path),
        _check_web_assets(),
        _check_bind(),
    ]
    payload = {
        "ok": all(check["status"] == "ok" for check in checks),
        "checks": checks,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def _cmd_serve(host: str, port: int) -> int:
    try:
        host = require_loopback_host(host)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import uvicorn

    print(f"BURNRATE listening on http://{host}:{port}/", flush=True)
    uvicorn.run("spend_app.api:app", host=host, port=port, log_level="info")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="burnrate")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", aliases=["init-db"], help="Create the local database")
    subparsers.add_parser(
        "doctor",
        help="Check Python, timezone data, database, pricing, and localhost bind",
    )
    serve = subparsers.add_parser("serve", help="Run the dashboard on 127.0.0.1")
    serve.add_argument("--host", default=DEFAULT_SERVE_HOST)
    serve.add_argument("--port", default=DEFAULT_SERVE_PORT, type=int)
    backfill = subparsers.add_parser(
        "backfill",
        help="Advanced: ingest historical usage from a local or admin source",
    )
    backfill.add_argument("source", choices=list(BACKFILL_SOURCES))
    backfill.add_argument(
        "--session-glob",
        default=None,
        help="Local path or glob. When omitted, each source uses its own default.",
    )
    backfill.add_argument(
        "--start",
        help="UTC start bound for openai-admin, anthropic-admin, and cursor-admin backfill",
    )
    backfill.add_argument(
        "--end",
        help="UTC end bound for openai-admin, anthropic-admin, and cursor-admin backfill",
    )
    subscriptions = subparsers.add_parser("subscription")
    subscription_commands = subscriptions.add_subparsers(dest="subscription_command", required=True)
    add = subscription_commands.add_parser("add")
    add.add_argument("--tool-key", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--amount-usd", required=True, type=float)
    add.add_argument(
        "--cadence", choices=["monthly", "quarterly", "annual"], default="monthly"
    )
    add.add_argument("--start-date", required=True)
    add.add_argument("--end-date")
    subscription_commands.add_parser("list")
    args = parser.parse_args()

    if args.command == "serve":
        return _cmd_serve(args.host, args.port)

    settings = load_settings()
    if args.command in {"init", "init-db"}:
        return _cmd_init(settings)
    if args.command == "doctor":
        return _cmd_doctor(settings)
    if args.command == "backfill" and args.source == "openai-admin":
        end = _parse_utc(args.end, datetime.now(UTC))
        start = _parse_utc(args.start, end - timedelta(hours=2))
        result = ingest_openai_admin(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            admin_key=settings.openai_admin_key,
            start=start,
            end=end,
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "codex-local":
        result = ingest_codex_local(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            session_glob=_local_path(args.source, args.session_glob, settings),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "anthropic-admin":
        end = _parse_utc(args.end, datetime.now(UTC))
        start = _parse_utc(args.start, end - timedelta(hours=2))
        result = ingest_anthropic_admin(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            admin_key=settings.anthropic_admin_key,
            start=start,
            end=end,
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "claude-local":
        result = ingest_claude_local(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            session_glob=_local_path(args.source, args.session_glob, settings),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "traycer-local":
        result = ingest_traycer_local(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            database_glob=_local_path(args.source, args.session_glob, settings),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "cursor-local":
        result = ingest_cursor_local(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            database_glob=_local_path(args.source, args.session_glob, settings),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "opencode-local":
        result = ingest_opencode_local(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            source_database=Path(_local_path(args.source, args.session_glob, settings)),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "cursor-csv":
        result = ingest_cursor_csv(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            import_path=Path(_local_path(args.source, args.session_glob, settings)),
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "backfill" and args.source == "cursor-admin":
        end = _parse_utc(args.end, datetime.now(UTC))
        start = _parse_utc(args.start, end - timedelta(hours=2))
        result = ingest_cursor_admin(
            database_path=settings.database_path,
            pricing=PricingEngine.load(settings.pricing_path),
            api_key=settings.cursor_api_key,
            start=start,
            end=end,
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "failed" else 0
    if args.command == "subscription":
        initialize(settings.database_path)
        with connect(settings.database_path) as connection:
            if args.subscription_command == "add":
                subscription_id = add_subscription(
                    connection,
                    tool_key=args.tool_key,
                    name=args.name,
                    amount_usd=args.amount_usd,
                    cadence=args.cadence,
                    start_date=args.start_date,
                    end_date=args.end_date,
                )
                materialize_subscription_days(
                    connection,
                    start=date.fromisoformat(args.start_date),
                    end=date.today() + timedelta(days=40),
                )
                print(json.dumps({"id": subscription_id, "status": "created"}))
                return 0
            rows = [dict(row) for row in connection.execute("SELECT * FROM subscriptions ORDER BY id")]
            print(json.dumps(rows, indent=2))
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
