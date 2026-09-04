"""Shared builders for the BURNRATE build-prompt acceptance gates.

This module is collected by pytest. Its tests only prove the frozen contract
fixture and clock helper — product assertions live in the sibling gate files.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import (
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_agent_run,
    upsert_quota,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "build_prompt_contract.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
TZ = "America/New_York"
CENTS = Decimal("0.01")
EXACT_TOOLS = set(CONTRACT["exactTools"])
HTML_PATH = ROOT / "spend_web" / "index.html"


class FrozenDateTime(datetime):
    """`datetime.now(UTC)` always returns the frozen acceptance clock."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


def sources() -> tuple[str, str, str]:
    html = (ROOT / "spend_web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")
    js = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")
    return html, css, js


def visual_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def money(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP))


def cents(value: object) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def make_settings(database: Path) -> Settings:
    return Settings(
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


def freeze_clock(monkeypatch) -> None:
    monkeypatch.setattr("spend_app.aggregate.datetime", FrozenDateTime)
    monkeypatch.setattr("spend_app.api.datetime", FrozenDateTime, raising=False)


def add_event(
    database: Path,
    pricing: PricingEngine,
    *,
    raw_id: str,
    tool: str,
    model: str,
    session: str | None,
    occurred: datetime,
    input_tokens: int,
    cached: int,
    writes: int,
    output: int,
    source: str | None = None,
    cost_usd: float | None = None,
    writes_1h: int = 0,
) -> None:
    computed = pricing.compute(
        model_key=model,
        occurred_at=occurred,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=writes,
        cache_write_1h_tokens=writes_1h,
        output_tokens=output,
    )
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source=source or f"{tool}_local",
                tool_key=tool,
                model_key=model,
                occurred_at=iso(occurred),
                session_id=session,
                project="build-prompt",
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                cache_write_tokens=writes,
                cache_write_1h_tokens=writes_1h,
                output_tokens=output,
                reasoning_tokens=0,
                cost_usd=cost_usd,
                computed_cost_usd=float(computed),
                raw_id=raw_id,
                ingested_at="2026-08-30T22:59:00Z",
                is_exact=tool in EXACT_TOOLS,
            ),
        )


def _quota(
    connection,
    *,
    provider: str,
    limit: str,
    label: str,
    pct: float | None,
    used: float | None,
    allowance: float | None,
    unit: str,
    resets: str | None,
    source: str,
    is_payg: bool = False,
) -> None:
    upsert_quota(
        connection,
        provider_key=provider,
        limit_key=limit,
        label=label,
        unit=unit,
        source=source,
        polled_at="2026-08-30T22:50:00Z",
        used=used,
        allowance=allowance,
        pct=pct,
        resets_at=resets,
        is_payg=is_payg,
    )


def _capacity_rows(connection) -> None:
    _quota(
        connection,
        provider="claude-code",
        limit="weekly",
        label="Weekly · all models",
        pct=43.0,
        used=43.0,
        allowance=100.0,
        unit="pct",
        resets="2026-09-02T16:59:00Z",
        source="read-only provider quota",
    )
    _quota(
        connection,
        provider="claude-code",
        limit="5h",
        label="5-hour session",
        pct=9.0,
        used=9.0,
        allowance=100.0,
        unit="pct",
        resets="2026-08-31T02:00:00Z",
        source="rolling window",
    )
    _quota(
        connection,
        provider="grok",
        limit="weekly",
        label="Weekly",
        pct=35.0,
        used=35.0,
        allowance=100.0,
        unit="pct",
        resets="2026-09-06T00:00:00Z",
        source="traycer_local",
    )
    _quota(
        connection,
        provider="codex",
        limit="weekly",
        label="Weekly",
        pct=23.0,
        used=23.0,
        allowance=100.0,
        unit="pct",
        resets="2026-09-06T02:25:00Z",
        source="codex_local",
    )
    _quota(
        connection,
        provider="cursor",
        limit="cursor_models",
        label="Cursor models",
        pct=7.3,
        used=7.3,
        allowance=100.0,
        unit="pct",
        resets="2026-09-16T03:44:00Z",
        source="authenticated read-only usage service",
    )
    _quota(
        connection,
        provider="cursor",
        limit="other_models",
        label="Other models — Cursor usage service omitted the Other models percentage.",
        pct=None,
        used=None,
        allowance=None,
        unit="unavailable",
        resets=None,
        source="cursor_usage_service",
    )
    _quota(
        connection,
        provider="opencode",
        limit="5h",
        label="5-hour credits",
        pct=0.0,
        used=0.0,
        allowance=30000.0,
        unit="credits",
        resets="2026-08-31T02:00:00Z",
        source="official_coding_plan",
    )
    _quota(
        connection,
        provider="opencode",
        limit="weekly",
        label="Weekly credits",
        pct=0.4,
        used=571.0,
        allowance=146000.0,
        unit="credits",
        resets="2026-09-06T02:49:00Z",
        source="official_coding_plan",
    )
    _quota(
        connection,
        provider="openrouter",
        limit="payg",
        label="Metered API usage",
        pct=None,
        used=4.18,
        allowance=None,
        unit="usd",
        resets=None,
        source="per-token rates · no quota to exhaust",
        is_payg=True,
    )


def _activity_and_ingest(connection) -> None:
    upsert_agent_run(
        connection,
        id="run-live",
        name="Northwind invoicing",
        model_key="claude-opus-5",
        state="LIVE",
        started_at="2026-08-30T20:00:00Z",
        last_seen_at="2026-08-30T22:59:00Z",
    )
    upsert_agent_run(
        connection,
        id="run-idle",
        name="Inventory weekly close",
        model_key="grok-4.6",
        state="NO DATA",
        started_at="2026-08-30T18:00:00Z",
        last_seen_at="2026-08-30T18:10:00Z",
    )
    connection.execute(
        "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES(?,?,?,?,?)",
        ("codex_local", "2026-08-30T22:58:00Z", "2026-08-30T22:59:00Z", "success", 4),
    )


def complete_world(tmp_path: Path) -> tuple[Path, PricingEngine]:
    """Fully priced world: consistency identities must hold to the cent."""
    database = tmp_path / "complete.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    add_event(
        database,
        pricing,
        raw_id="codex-session-a",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-a",
        occurred=datetime(2026, 8, 30, 22, 55, tzinfo=UTC),
        input_tokens=100_000,
        cached=20_000,
        writes=10_000,
        output=5_000,
    )
    add_event(
        database,
        pricing,
        raw_id="codex-session-b",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-b",
        occurred=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        input_tokens=100_000,
        cached=90_000,
        writes=0,
        output=2_000,
    )
    add_event(
        database,
        pricing,
        raw_id="codex-yesterday",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-yday",
        occurred=datetime(2026, 8, 30, 2, 0, tzinfo=UTC),
        input_tokens=50_000,
        cached=40_000,
        writes=0,
        output=1_000,
    )
    add_event(
        database,
        pricing,
        raw_id="claude-today",
        tool="claude-code",
        model="claude-opus-5",
        session="claude-1",
        occurred=datetime(2026, 8, 30, 18, 0, tzinfo=UTC),
        input_tokens=80_000,
        cached=70_000,
        writes=5_000,
        output=4_000,
        writes_1h=5_000,
    )
    add_event(
        database,
        pricing,
        raw_id="cursor-low-cache",
        tool="cursor",
        model="cursor:grok-4.6",
        session="cursor-1",
        occurred=datetime(2026, 8, 30, 16, 0, tzinfo=UTC),
        input_tokens=200_000,
        cached=40_000,
        writes=0,
        output=10_000,
    )
    add_event(
        database,
        pricing,
        raw_id="grok-week",
        tool="grok",
        model="supergrok:grok-4.6",
        session="grok-1",
        occurred=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        input_tokens=2_000,
        cached=1_800,
        writes=0,
        output=100,
    )
    add_event(
        database,
        pricing,
        raw_id="openrouter-today",
        tool="openrouter",
        model="openrouter:glm-5.3-flash",
        session="or-1",
        occurred=datetime(2026, 8, 30, 20, 0, tzinfo=UTC),
        input_tokens=8_000,
        cached=1_000,
        writes=0,
        output=500,
        cost_usd=4.18,
    )
    with connect(database) as connection:
        _capacity_rows(connection)
        _activity_and_ingest(connection)
        add_subscription(
            connection,
            tool_key="codex",
            name="Example Codex plan",
            amount_usd=20.0,
            cadence="monthly",
            start_date="2026-08-01",
            end_date=None,
        )
    return database, pricing


def gap_world(tmp_path: Path) -> tuple[Path, PricingEngine]:
    """Same as complete_world plus an unpriced model that must stay `None`, never $0."""
    database, pricing = complete_world(tmp_path)
    with connect(database) as connection:
        upsert_unpriced_event(
            connection,
            UnpricedUsageEvent(
                source="opencode_local",
                tool_key="opencode",
                model_key="opencode:unlisted-model",
                occurred_at="2026-08-30T16:00:00Z",
                session_id="opencode-1",
                project="build-prompt",
                input_tokens=1_000,
                cached_input_tokens=900,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=50,
                reasoning_tokens=0,
                unclassified_tokens=0,
                telemetry_complete=True,
                cost_usd=None,
                raw_id="opencode-unlisted",
                ingested_at="2026-08-30T22:59:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,occurrences,sample_raw_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                "opencode:unlisted-model",
                "opencode_local",
                "2026-08-30T16:00:00Z",
                "2026-08-30T16:00:00Z",
                1,
                "opencode-unlisted",
            ),
        )
    return database, pricing


def client_for(database: Path, monkeypatch) -> TestClient:
    freeze_clock(monkeypatch)
    return TestClient(create_app(make_settings(database), enable_scheduler=False))


def require_keys(payload: dict, keys: list[str], *, where: str) -> None:
    missing = [key for key in keys if key not in payload]
    assert not missing, f"{where} missing keys {missing}; have {sorted(payload)}"


def media_blocks(css: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in re.finditer(r"@media\s*(\([^)]+\))\s*\{", css):
        query = match.group(1)
        start = match.end()
        depth = 1
        index = start
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        blocks[query] = css[start : index - 1]
    return blocks


def keyframes(css: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"@keyframes\s+([A-Za-z0-9_-]+)\s*\{", css):
        name = match.group(1)
        start = match.end()
        depth = 1
        index = start
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        found[name] = css[start : index - 1]
    return found


def usd_js(value: float) -> str:
    """Mirror the attached file's money format: two decimals below 1000, whole dollars at/above."""
    if value >= 1000:
        return "$" + f"{round(value):,}"
    return f"${value:.2f}"


def test_build_prompt_contract_lists_the_twelve_windows() -> None:
    keys = [row["key"] for row in CONTRACT["windows"]]
    assert keys == ["15m", "30m", "1h", "3h", "6h", "12h", "1d", "1w", "1mo", "mtd", "ytd", "all"]
    by_key = {row["key"]: row["buckets"] for row in CONTRACT["windows"]}
    assert by_key["15m"] == 15
    assert by_key["1d"] == 24
    assert by_key["1w"] == 28
    assert by_key["ytd"] == 32


def test_build_prompt_visual_html_is_readable() -> None:
    text = visual_html()
    html, css, js = sources()
    assert HTML_PATH.is_file()
    assert "Downloads" not in str(HTML_PATH)
    assert "BURNRATE" in text
    assert 'src="/spend.js' in text
    assert "function renderOverview" in js
    packaged = (html + css + js).replace(" ", "").lower()
    assert "animation-fill-mode:both" not in packaged
    assert "animation-fill-mode: both" not in text
    assert "animation-fill-mode:both" not in text.replace(" ", "")
