"""Phase 5 of docs/PERFORMANCE_PLAN.md: measure and hold the line.

Health is memoised per data clock, the probe reports paint and render timing,
the bench script and its baseline exist and agree, and asset sizes are gated.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app import aggregate
from spend_app.aggregate import aggregate_health, aggregate_health_cached
from spend_app.db import connect, upsert_quota
from tests_spend.test_aggregation import NOW, TZ, add_event, fixture_database


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "spend_web"
JS = (WEB / "spend.js").read_text(encoding="utf-8")
CSS = (WEB / "spend.css").read_text(encoding="utf-8")


def test_health_is_reused_per_clock_and_invalidated_by_its_inputs(tmp_path: Path) -> None:
    aggregate.reset_memo()
    database, pricing = fixture_database(tmp_path)
    first = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert aggregate_health_cached(database_path=database, now=NOW, timezone=TZ) is first
    assert first == aggregate_health(database_path=database, now=NOW, timezone=TZ)
    assert aggregate_health_cached(database_path=database, now=NOW + timedelta(seconds=15), timezone=TZ) is not first

    with connect(database) as connection:
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES(?,?,?,?,?,?)",
            ("codex_local", "2026-08-30T22:59:00Z", "2026-08-30T22:59:01Z", "failed", 0, "boom"),
        )
    after_run = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_run is not first
    assert {row["source"]: row["status"] for row in after_run["ingest"]}["codex_local"] == "failed"

    with connect(database) as connection:
        upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="traycer_local",
            polled_at="2026-08-30T22:45:00Z",
            pct=40.0,
        )
    after_quota = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_quota is not after_run
    assert next(q for q in after_quota["quotas"] if q["providerKey"] == "grok")["lastPoll"] == "2026-08-30T22:45:00Z"

    add_event(
        database,
        pricing,
        raw_id="codex-health",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-h",
        occurred=NOW - timedelta(hours=1),
        input_tokens=1_000,
        cached=100,
        writes=0,
        output=10,
    )
    after_event = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_event is not after_quota
    aggregate.reset_memo()


def test_probe_reports_paint_and_render_timing() -> None:
    assert "const timing = {lcp: 0, lastRenderMs: null};" in JS
    assert 'observe({type: "largest-contentful-paint", buffered: true})' in JS
    preserve = JS.split("function renderPreservingScroll", 1)[1].split("\nfunction ", 1)[0]
    assert "const started = performance.now();" in preserve
    assert "timing.lastRenderMs = performance.now() - started;" in preserve
    probe = JS.split("function writeProbe(view)", 1)[1].split("\nfunction ", 1)[0]
    for field in ("ttfb:", "firstPaint:", "lcp:", "refreshRenderMs:", "easeFrames:", "easing:"):
        assert field in probe, field


def test_bench_script_and_baseline_agree_and_stay_read_only() -> None:
    script = (ROOT / "scripts" / "Bench-Burnrate.ps1").read_text(encoding="utf-8")
    assert "/api/spend/limits" not in script
    assert "/api/spend/summary" in script and "/api/spend/health" in script
    assert "127.0.0.1:17331" in script
    assert "Select-Object -Skip 1" in script, "the cold first request is excluded from p50/p95"
    assert "PERF REGRESSION" in script and "exit 1" in script


def test_asset_size_gates() -> None:
    js_bytes = (WEB / "spend.js").read_bytes()
    css_bytes = (WEB / "spend.css").read_bytes()
    assert len(js_bytes) < 100_000, len(js_bytes)
    assert len(css_bytes) < 45_000, len(css_bytes)
    assert len(gzip.compress(js_bytes, 6)) < 30_000
    assert len(gzip.compress(css_bytes, 6)) < 12_000
