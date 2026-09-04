from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.config import Settings


pytestmark = pytest.mark.skipif(
    not os.environ.get("SPEND_REAL_DB_COPY"),
    reason="SPEND_REAL_DB_COPY is not set",
)

ROOT = Path(__file__).resolve().parents[1]
TZ = "America/New_York"
SUMMARY_REQUIRED = (
    "window",
    "generatedAt",
    "cadenceSeconds",
    "cadenceMinutes",
    "status",
    "navigation",
    "coverage",
    "totals",
    "capacity",
    "activity",
    "waste",
    "cacheSavings",
    "projected",
    "mix",
    "series",
    "tools",
    "models",
    "subscriptions",
    "heatmap",
)


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "spend.db"
    shutil.copy2(os.environ["SPEND_REAL_DB_COPY"], dest)
    return dest


def _client(database: Path) -> TestClient:
    settings = Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        cursor_import_path=database.parent / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone=TZ,
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    return TestClient(create_app(settings, enable_scheduler=False, now=datetime.now(UTC)))


def test_s03_16_real_copy_payload_truth(tmp_path: Path) -> None:
    database = _copy(tmp_path)
    client = _client(database)
    health = client.get("/api/spend/health").json()
    summary = client.get("/api/spend/summary", params={"window": "1mo"}).json()
    for key in SUMMARY_REQUIRED:
        assert key in summary
    assert "generatedAt" in health
    assert "pricingGaps" in health
    unknown = [model for model in summary["models"] if model["value"] is None]
    for model in unknown:
        assert model["value"] not in {0, 0.0}
        assert model["tokens"] > 0
    if unknown:
        assert summary["totals"]["trackedValue"] is None
    if summary["totals"]["tokens"] > 0:
        assert abs(sum(item["share"] for item in summary["mix"]) - 100) <= 0.1
    for window in ("1d", "1mo"):
        payload = client.get("/api/spend/summary", params={"window": window}).json()
        series_total = sum(point["total"] for point in payload["series"])
        assert int(round(series_total)) == payload["totals"]["tokens"]
        assert len(payload["series"]) == payload["window"]["buckets"]
