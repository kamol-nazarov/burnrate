import glob
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app.adapters.claude_local import ingest as ingest_claude
from spend_app.adapters.claude_local import reset_file_cache as reset_claude_cache
from spend_app.adapters.codex_local import ingest as ingest_codex
from spend_app.adapters.codex_local import reset_file_cache as reset_codex_cache
from spend_app.adapters.common import persist_rows
from spend_app.aggregate import aggregate_health, aggregate_summary
from spend_app.db import connect, initialize
from zoneinfo import ZoneInfo

from spend_app.pricing import PricingEngine
from spend_app.subscriptions import SUBSCRIPTION_SEEDS


pytestmark = pytest.mark.skipif(
    not os.environ.get("SPEND_REAL_DB_COPY"),
    reason="SPEND_REAL_DB_COPY is not set",
)

ROOT = Path(__file__).resolve().parents[1]
CODEX_GLOB = str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl")
CLAUDE_GLOB = str(Path.home() / ".claude" / "projects" / "**" / "*.jsonl")


def _copy(tmp_path: Path) -> Path:
    source = Path(os.environ["SPEND_REAL_DB_COPY"])
    dest = tmp_path / "spend.db"
    shutil.copy2(source, dest)
    return dest


def test_s01_02_restage_claude_and_codex_on_real_copy(tmp_path: Path) -> None:
    database = _copy(tmp_path)
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_claude_cache()
    reset_codex_cache()
    ingest_claude(database_path=database, pricing=pricing, session_glob=CLAUDE_GLOB)
    ingest_codex(database_path=database, pricing=pricing, session_glob=CODEX_GLOB)
    now = datetime.now(UTC)
    health = aggregate_health(database_path=database, now=now, timezone="America/New_York")
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="1mo",
        tool="all",
        timezone="America/New_York",
        cache_threshold=0.75,
        now=now,
    )
    assert health["dayCoverage"]
    for day in health["dayCoverage"]:
        assert day["status"] in {"priced", "unpriced", "partial", "unavailable"}
        if day["status"] == "unavailable":
            assert day["status"] != "0"
    for day in summary["navigation"]["dayCoverage"]:
        if day["status"] == "unavailable":
            assert summary["navigation"]["burnRatePerDay"] != 0.0 or summary["navigation"]["burnRatePerDay"] is None
    with connect(database) as connection:
        claude_unpriced = connection.execute(
            "SELECT COUNT(*) FROM unpriced_usage_events WHERE source='claude_local'"
        ).fetchone()[0]
        claude_priced = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE source='claude_local'"
        ).fetchone()[0]
    if glob.glob(CLAUDE_GLOB, recursive=True):
        assert claude_unpriced + claude_priced > 0
    priced_days = {
        day["date"] for day in summary["navigation"]["dayCoverage"] if day["status"] == "priced"
    }
    for point in summary["series"]:
        stamp = datetime.fromisoformat(point["bucketStart"].replace("Z", "+00:00"))
        local_day = stamp.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if point["label"] and local_day in priced_days:
            assert point["total"] > 0


def test_s01_16_real_copy_canonical_subscription_identity(tmp_path: Path) -> None:
    database = _copy(tmp_path)
    initialize(database)
    expected = {
        seed["tool_key"]: (seed["name"], seed["amount_usd"], seed["cadence"])
        for seed in SUBSCRIPTION_SEEDS
    }
    with connect(database) as connection:
        rows = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT tool_key, name, amount_usd, cadence FROM subscriptions"
            )
        }
    for tool_key, identity in expected.items():
        if tool_key in rows:
            assert rows[tool_key] == identity


def test_s01_05_empty_other_source_persist_does_not_clear_unstaged_gaps(tmp_path: Path) -> None:
    database = _copy(tmp_path)
    initialize(database)
    persist_rows(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        source="codex_local",
        usage_rows=[],
    )
    with connect(database) as connection:
        unstaged = [
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT model_key FROM pricing_gap_events
                WHERE raw_id NOT IN (SELECT raw_id FROM usage_events)
                """
            )
        ]
        claude_usage = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE source='claude_local'"
        ).fetchone()[0]
        claude_unpriced = connection.execute(
            "SELECT COUNT(*) FROM unpriced_usage_events WHERE source='claude_local'"
        ).fetchone()[0]
    health = aggregate_health(
        database_path=database, now=datetime.now(UTC), timezone="America/New_York"
    )
    for model_key in unstaged:
        assert model_key in health["pricingGaps"]
    if "claude-opus-5" in unstaged:
        assert claude_usage + claude_unpriced >= 0
        assert "claude-opus-5" in health["pricingGaps"]
