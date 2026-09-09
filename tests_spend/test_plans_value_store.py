"""Unit tests for Plans & Value read-only store (Case 11 + bounded queries)."""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.db import (
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.plan_service import apply_mutation
from spend_app.pricing import PricingEngine
from spend_app.timeutil import epoch_micros, iso_utc
from tests_spend.test_api import make_settings

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 8)
AS_OF = datetime(2026, 9, 9, 4, 0, 0, tzinfo=UTC)  # Sep 9 00:00 America/New_York


def _ensure_plans_value_module() -> types.ModuleType:
    """Prefer the real valuation module; stub only if it is unavailable."""
    try:
        import spend_app.plans_value as real  # noqa: WPS433

        return real
    except ImportError:
        pass

    module = types.ModuleType("spend_app.plans_value")

    def assemble_group_valuation(
        group, events, unpriced_events, source_health, *, pricing=None
    ):
        priced = sum(Decimal(str(e.get("computed_cost_usd") or 0)) for e in events)
        return {
            "groupId": getattr(group, "group_id", None) or group["group_id"],
            "name": getattr(group, "name", None) or group["name"],
            "configuredCostUsd": str(
                getattr(group, "accrued_cost", group.get("accrued_cost"))
            ),
            "usageValueUsd": str(priced) if events or unpriced_events else None,
            "events": len(events),
            "unpricedTokens": sum(
                int(e.get("input_tokens") or 0) for e in (unpriced_events or [])
            ),
            "sourceHealth": source_health,
        }

    def assemble_unassigned_summary(unassigned_events, unassigned_unpriced, *, pricing=None):
        return {
            "usageValueUsd": None
            if not unassigned_events
            else str(
                sum(Decimal(str(e.get("computed_cost_usd") or 0)) for e in unassigned_events)
            ),
            "events": len(unassigned_events),
        }

    def assemble_plans_value_report(
        period_key,
        as_of,
        zone,
        groups_data,
        unassigned_data,
        sources_data,
        from_utc,
        to_utc,
    ):
        return {
            "schemaVersion": 1,
            "period": period_key,
            "timezone": getattr(zone, "key", str(zone)),
            "generatedAt": iso_utc(as_of),
            "fromUtc": iso_utc(from_utc),
            "toUtc": iso_utc(to_utc),
            "groups": groups_data,
            "unassigned": unassigned_data,
            "sources": sources_data,
        }

    module.assemble_group_valuation = assemble_group_valuation
    module.assemble_unassigned_summary = assemble_unassigned_summary
    module.assemble_plans_value_report = assemble_plans_value_report
    sys.modules["spend_app.plans_value"] = module
    return module


_ensure_plans_value_module()

from spend_app import plans_value_store as store  # noqa: E402


def _correct_amount(db: Path, *, amount_usd: str) -> None:
    """Historical correction that changes configured amount (Case 11)."""
    with connect(db) as connection:
        plan = connection.execute(
            "SELECT id, version FROM subscription_plans ORDER BY id LIMIT 1"
        ).fetchone()
        term = connection.execute(
            "SELECT * FROM subscriptions WHERE plan_id=? ORDER BY start_date LIMIT 1",
            (plan["id"],),
        ).fetchone()
    values = {
        "name": term["name"],
        "tool_key": term["tool_key"],
        "amount_usd": amount_usd,
        "cadence": term["cadence"],
        "start_date": term["start_date"],
        "end_date": term["end_date"],
    }
    args = {
        "plan_id": plan["id"],
        "expected_version": plan["version"],
        "term_id": term["id"],
        "values": values,
    }
    preview = _mutate(db, "correct", preview=True, **args)
    _mutate(
        db,
        "correct",
        confirmed=True,
        preview_token=preview["preview_token"],
        **args,
    )


class CountingConnection:
    """Proxy that records SQL text for bounded-query assertions."""

    def __init__(self, connection):
        self._connection = connection
        self.statements: list[str] = []

    def execute(self, sql, parameters=()):
        self.statements.append(" ".join(str(sql).split()))
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _usage_selects(statements: list[str]) -> list[str]:
    return [
        sql
        for sql in statements
        if "FROM usage_events" in sql or "FROM unpriced_usage_events" in sql
    ]


def _plan_values(**changes):
    return {
        "name": "Codex Plus",
        "tool_key": "codex",
        "amount_usd": "300.00",
        "cadence": "monthly",
        "start_date": "2026-09-01",
        "end_date": None,
    } | changes


def _mutate(db: Path, operation: str, **fields):
    body = dict(operation=operation, request_id=uuid.uuid4().hex, **fields)
    with connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return apply_mutation(connection, body, TODAY)


def _stamp(moment: datetime) -> str:
    return iso_utc(moment)


def _add_priced(
    connection,
    *,
    raw_id: str,
    occurred: datetime,
    tool_key: str = "codex",
    source: str = "codex_local",
    computed: float = 12.5,
    model_key: str = "gpt-5",
) -> None:
    upsert_usage_event(
        connection,
        UsageEvent(
            source=source,
            tool_key=tool_key,
            model_key=model_key,
            occurred_at=_stamp(occurred),
            session_id="s1",
            project="p1",
            input_tokens=100,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=20,
            reasoning_tokens=None,
            cost_usd=None,
            computed_cost_usd=computed,
            raw_id=raw_id,
            ingested_at=_stamp(occurred),
            is_exact=True,
        ),
    )


def _add_unpriced(
    connection,
    *,
    raw_id: str,
    occurred: datetime,
    tool_key: str = "codex",
    source: str = "codex_local",
    model_key: str = "mystery-model",
) -> None:
    upsert_unpriced_event(
        connection,
        UnpricedUsageEvent(
            source=source,
            tool_key=tool_key,
            model_key=model_key,
            occurred_at=_stamp(occurred),
            session_id="s1",
            project="p1",
            input_tokens=1000,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=0,
            reasoning_tokens=None,
            unclassified_tokens=0,
            telemetry_complete=True,
            cost_usd=None,
            raw_id=raw_id,
            ingested_at=_stamp(occurred),
        ),
    )


@pytest.fixture
def pricing() -> PricingEngine:
    return PricingEngine.load(ROOT / "pricing")


@pytest.fixture(autouse=True)
def _clear_cache():
    store.clear_plans_value_cache()
    yield
    store.clear_plans_value_cache()


@pytest.fixture
def db_with_plan(tmp_path: Path) -> Path:
    database = tmp_path / "plans_value.db"
    initialize(database)
    _mutate(database, "add", values=_plan_values())
    occurred = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
    with connect(database) as connection:
        _add_priced(connection, raw_id="priced-1", occurred=occurred, computed=2400.0)
        _add_unpriced(connection, raw_id="unpriced-1", occurred=occurred)
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) "
            "VALUES(?,?,?,?,?)",
            ("codex_local", _stamp(occurred), _stamp(occurred), "success", 2),
        )
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) "
            "VALUES(?,?,?,?,?,?)",
            (
                "openai_admin",
                _stamp(occurred),
                _stamp(occurred),
                "skipped",
                0,
                "OPENAI_ADMIN_KEY is not configured",
            ),
        )
    return database


def test_fetch_report_wires_period_groups_and_sources(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    with connect(db_with_plan) as connection:
        report = store.fetch_plans_value_report(
            connection, settings, pricing, "this_month", as_of=AS_OF
        )
    assert report["period"] == "this_month"
    assert report["timezone"] == "America/New_York"
    assert report["fromUtc"]
    assert report["toUtc"]
    assert report["groups"]
    assert report["groups"][0]["groupId"] == "tool:codex"
    assert Decimal(report["groups"][0]["configuredCostUsd"]) > 0
    assert report["groups"][0]["events"] == 2  # 1 priced + 1 unpriced
    assert Decimal(report["groups"][0]["usageValueUsd"]) == Decimal("2400")
    assert any(row["source"] == "codex_local" for row in report["sources"])


def test_admin_sources_excluded_from_subscription_usage(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    admin_moment = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    with connect(db_with_plan) as connection:
        _add_priced(
            connection,
            raw_id="admin-priced",
            occurred=admin_moment,
            source="openai_admin",
            tool_key="codex",
            computed=999.0,
        )
        _add_priced(
            connection,
            raw_id="anthropic-admin-priced",
            occurred=admin_moment,
            source="anthropic_admin",
            tool_key="claude-code",
            computed=888.0,
        )
        store.clear_plans_value_cache()
        report = store.fetch_plans_value_report(
            connection, settings, pricing, "this_month", as_of=AS_OF
        )
    # Admin rows excluded; fixture remains 1 priced + 1 unpriced.
    assert report["groups"][0]["events"] == 2
    assert Decimal(report["groups"][0]["usageValueUsd"]) == Decimal("2400")


def test_usage_queries_are_bounded_not_per_plan(tmp_path: Path, pricing):
    database = tmp_path / "many_plans.db"
    initialize(database)
    for index in range(5):
        _mutate(
            database,
            "add",
            values=_plan_values(
                name=f"Plan {index}",
                tool_key="codex" if index < 3 else "cursor",
                amount_usd=f"{100 + index}.00",
            ),
        )
    base = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    with connect(database) as connection:
        for day in range(1, 9):
            moment = base.replace(day=day)
            _add_priced(
                connection,
                raw_id=f"e-{day}",
                occurred=moment,
                tool_key="codex" if day % 2 else "cursor",
                computed=10.0 * day,
            )
        settings = make_settings(database)
        counted = CountingConnection(connection)
        store.clear_plans_value_cache()
        store.fetch_plans_value_report(
            counted, settings, pricing, "this_month", as_of=AS_OF
        )
        usage_sql = _usage_selects(counted.statements)
        # Fingerprint: 2 aggregations; load: 2 SELECTs. Never one per plan/day.
        assert len(usage_sql) <= 4
        assert len(usage_sql) >= 2
        assert all("occurred_epoch_us" in sql for sql in usage_sql)
        assert all("openai_admin" in sql for sql in usage_sql)
        # No per-day loop markers / plan_id filters on usage tables.
        assert not any("plan_id" in sql.lower() for sql in usage_sql)
        assert not any("date(" in sql.lower() for sql in usage_sql)


def test_case_11_plan_edit_invalidates_cache_without_restart(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    first = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    second = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    assert first == second
    assert first is second  # memoized object

    assert first["groups"][0]["groupId"] == "tool:codex"
    before_cost = Decimal(first["groups"][0]["configuredCostUsd"])

    _correct_amount(db_with_plan, amount_usd="600.00")

    refreshed = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    after_cost = Decimal(refreshed["groups"][0]["configuredCostUsd"])
    assert after_cost == before_cost * 2
    assert refreshed is not first


def test_case_11_database_caches_stay_isolated(tmp_path: Path, pricing):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    for path, amount in ((db_a, "300.00"), (db_b, "100.00")):
        initialize(path)
        _mutate(path, "add", values=_plan_values(amount_usd=amount, name=path.stem))
        with connect(path) as connection:
            _add_priced(
                connection,
                raw_id=f"{path.stem}-e",
                occurred=datetime(2026, 9, 4, 12, tzinfo=UTC),
                computed=50.0,
            )

    report_a = store.get_plans_value(
        db_a, make_settings(db_a), pricing, "this_month", as_of=AS_OF
    )
    report_b = store.get_plans_value(
        db_b, make_settings(db_b), pricing, "this_month", as_of=AS_OF
    )
    assert report_a is not report_b
    assert Decimal(report_a["groups"][0]["configuredCostUsd"]) != Decimal(
        report_b["groups"][0]["configuredCostUsd"]
    )
    # Touching B must not poison A's memoized payload.
    _correct_amount(db_b, amount_usd="900.00")
    again_a = store.get_plans_value(
        db_a, make_settings(db_a), pricing, "this_month", as_of=AS_OF
    )
    assert again_a is report_a
    again_b = store.get_plans_value(
        db_b, make_settings(db_b), pricing, "this_month", as_of=AS_OF
    )
    assert Decimal(again_b["groups"][0]["configuredCostUsd"]) > Decimal(
        report_b["groups"][0]["configuredCostUsd"]
    )


def test_last_month_period_and_ingest_fingerprint(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    # Seed August usage so last_month is non-empty.
    august = datetime(2026, 8, 15, 12, tzinfo=UTC)
    with connect(db_with_plan) as connection:
        _add_priced(
            connection, raw_id="aug-1", occurred=august, computed=33.0
        )
        # Need an August-active plan term for grouping — extend via schedule/add.
    _mutate(
        db_with_plan,
        "add",
        values=_plan_values(
            name="August Codex",
            start_date="2026-08-01",
            end_date="2026-08-31",
            amount_usd="310.00",
        ),
    )
    store.clear_plans_value_cache()
    last_month = store.get_plans_value(
        db_with_plan, settings, pricing, "last_month", as_of=AS_OF
    )
    this_month = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    assert last_month["period"] == "last_month"
    assert this_month["period"] == "this_month"
    assert last_month["fromUtc"] != this_month["fromUtc"]
    assert last_month is not this_month


def test_ingest_change_busts_cache(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    first = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    with connect(db_with_plan) as connection:
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) "
            "VALUES(?,?,?,?,?)",
            (
                "codex_local",
                _stamp(AS_OF),
                _stamp(AS_OF),
                "success",
                99,
            ),
        )
    second = store.get_plans_value(
        db_with_plan, settings, pricing, "this_month", as_of=AS_OF
    )
    assert second is not first
    codex = next(row for row in second["sources"] if row["source"] == "codex_local")
    assert codex["eventsWritten"] == 99


def test_load_helpers_exclude_admin_and_respect_bounds(db_with_plan: Path):
    start = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    end = AS_OF
    outside = datetime(2026, 8, 20, 12, tzinfo=UTC)
    with connect(db_with_plan) as connection:
        _add_priced(
            connection,
            raw_id="outside",
            occurred=outside,
            computed=1.0,
        )
        _add_priced(
            connection,
            raw_id="admin-in-window",
            occurred=datetime(2026, 9, 2, 12, tzinfo=UTC),
            source="openai_admin",
            computed=50.0,
        )
        priced = store.load_priced_usage_events(connection, start, end)
        unpriced = store.load_unpriced_usage_events(connection, start, end)
    assert all(start <= event["occurred_at"] < end for event in priced)
    assert all(event["source"] not in store.ADMIN_USAGE_SOURCES for event in priced)
    assert all(event["source"] not in store.ADMIN_USAGE_SOURCES for event in unpriced)
    assert {event["raw_id"] for event in priced} == {"priced-1"}
    assert epoch_micros(priced[0]["occurred_at"]) >= epoch_micros(start)


def test_unsupported_period_raises(db_with_plan: Path, pricing):
    settings = make_settings(db_with_plan)
    with connect(db_with_plan) as connection:
        with pytest.raises(ValueError, match="unsupported period"):
            store.fetch_plans_value_report(
                connection, settings, pricing, "ytd", as_of=AS_OF
            )
