from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
APP_DIR_NAME = "BURNRATE"

LOCAL_INGEST_INTERVAL_SECONDS = 15
ADMIN_INGEST_INTERVAL_MINUTES = 15
# Backward-compatible name for callers that only configure remote/admin cadence.
INGEST_INTERVAL_MINUTES = ADMIN_INGEST_INTERVAL_MINUTES
# Quota job tick. Each lane decides whether it is due on a tick (see
# spend_app.quotas.LANE_CADENCE); external endpoints are never called on
# every tick.
QUOTA_POLL_SECONDS = 15
ACTIVITY_POLL_SECONDS = 4


def local_app_data() -> Path:
    configured = os.getenv("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def default_app_dir() -> Path:
    return local_app_data() / APP_DIR_NAME


def default_database_path() -> Path:
    override = os.getenv("SPEND_DATABASE_PATH")
    if override:
        return Path(override)
    if os.getenv("BURNRATE_DEV") == "1":
        return ROOT / "data" / "spend" / "spend.db"
    return default_app_dir() / "spend.db"


def default_logs_path() -> Path:
    if os.getenv("BURNRATE_DEV") == "1":
        return ROOT / "logs"
    return default_app_dir() / "logs"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    pricing_path: Path
    cursor_import_path: Path
    anthropic_admin_key: str | None
    openai_admin_key: str | None
    cursor_api_key: str | None
    timezone: str
    cache_hit_threshold: float
    over_routing_token_ceiling: int
    local_ingest_interval_seconds: int = LOCAL_INGEST_INTERVAL_SECONDS
    admin_ingest_interval_minutes: int = ADMIN_INGEST_INTERVAL_MINUTES
    quota_poll_seconds: int = QUOTA_POLL_SECONDS
    activity_poll_seconds: int = ACTIVITY_POLL_SECONDS
    logs_path: Path = field(default_factory=default_logs_path)


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or ROOT / ".env", override=False)
    return Settings(
        database_path=default_database_path(),
        pricing_path=Path(os.getenv("SPEND_PRICING_PATH", ROOT / "pricing")),
        cursor_import_path=Path(
            os.getenv("CURSOR_IMPORT_PATH", ROOT / "data" / "imports" / "cursor")
        ),
        anthropic_admin_key=os.getenv("ANTHROPIC_ADMIN_KEY") or None,
        openai_admin_key=os.getenv("OPENAI_ADMIN_KEY") or None,
        cursor_api_key=os.getenv("CURSOR_API_KEY") or None,
        timezone=os.getenv("SPEND_TIMEZONE", "UTC"),
        cache_hit_threshold=float(os.getenv("CACHE_HIT_THRESHOLD", "0.75")),
        over_routing_token_ceiling=int(os.getenv("OVER_ROUTING_TOKEN_CEILING", "40000")),
        local_ingest_interval_seconds=int(
            os.getenv(
                "SPEND_LOCAL_INGEST_INTERVAL_SECONDS",
                str(LOCAL_INGEST_INTERVAL_SECONDS),
            )
        ),
        admin_ingest_interval_minutes=int(
            os.getenv(
                "SPEND_ADMIN_INGEST_INTERVAL_MINUTES",
                os.getenv(
                    "SPEND_INGEST_INTERVAL_MINUTES",
                    str(ADMIN_INGEST_INTERVAL_MINUTES),
                ),
            )
        ),
        quota_poll_seconds=int(os.getenv("SPEND_QUOTA_POLL_SECONDS", str(QUOTA_POLL_SECONDS))),
        activity_poll_seconds=int(
            os.getenv("SPEND_ACTIVITY_POLL_SECONDS", str(ACTIVITY_POLL_SECONDS))
        ),
        logs_path=default_logs_path(),
    )
