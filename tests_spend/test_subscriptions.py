from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.db import connect, initialize
from spend_app.subscriptions import (
    SUBSCRIPTION_SEEDS,
    add_subscription,
    daily_cost,
    list_subscriptions,
    materialize_subscription_days,
    migrate_legacy_subscription_identities,
    migrate_legacy_zai_seed,
    update_subscription,
)


def test_monthly_proration_respects_days_in_each_month() -> None:
    assert daily_cost(31, "monthly", date(2026, 8, 1)) == Decimal("1")
    assert daily_cost(28, "monthly", date(2026, 2, 1)) == Decimal("1")


def test_quarterly_proration_spreads_over_days_in_quarter() -> None:
    assert daily_cost(400, "quarterly", date(2026, 8, 15)) == Decimal("400") / Decimal(92)
    assert daily_cost(400, "quarterly", date(2026, 2, 15)) == Decimal("400") / Decimal(90)
    assert daily_cost(400, "quarterly", date(2026, 1, 1)) == daily_cost(
        400, "quarterly", date(2026, 3, 31)
    )


def test_materialization_is_idempotent_and_updates_amount(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscriptions")
        subscription_id = add_subscription(
            connection,
            tool_key="cursor",
            name="Cursor Pro",
            amount_usd=31,
            cadence="monthly",
            start_date="2026-08-01",
            end_date=None,
        )
        assert materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 3)
        ) == 3
        assert materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 3)
        ) == 0
        rows = connection.execute(
            "SELECT subscription_id,date,cost_usd FROM subscription_daily_costs ORDER BY date"
        ).fetchall()
    assert len(rows) == 3
    assert all(row[0] == subscription_id and row[2] == 1 for row in rows)


def test_quarterly_subscription_materializes_prorated_days(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscription_daily_costs")
        connection.execute("DELETE FROM subscriptions")
        subscription_id = add_subscription(
            connection,
            tool_key="opencode",
            name="Z.AI Coding Plan",
            amount_usd=400,
            cadence="quarterly",
            start_date="2026-08-01",
            end_date=None,
        )
        assert materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 3)
        ) == 3
        rows = connection.execute(
            "SELECT cost_usd FROM subscription_daily_costs WHERE subscription_id=? ORDER BY date",
            (subscription_id,),
        ).fetchall()
        second_pass = materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 3)
        )
    assert len(rows) == 3
    expected = float(Decimal("400") / Decimal(92))
    assert all(abs(row[0] - expected) < 1e-9 for row in rows)
    assert second_pass == 0


def test_fresh_initialize_has_zero_subscription_rows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        stamp = connection.execute(
            "SELECT value FROM app_meta WHERE key='subscription_seed_version'"
        ).fetchone()[0]
        assert migrate_legacy_zai_seed(connection) == 0
        assert migrate_legacy_subscription_identities(connection) == 0
    assert SUBSCRIPTION_SEEDS == ()
    assert count == 0
    assert stamp == "1"

    with connect(database) as connection:
        add_subscription(
            connection,
            tool_key="codex",
            name="Codex",
            amount_usd=20,
            cadence="monthly",
            start_date="2026-08-01",
            end_date=None,
        )
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
        connection.execute("DELETE FROM subscriptions")
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


def test_recognized_legacy_zai_seed_is_migrated_and_materializes_quarterly(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscription_daily_costs")
        connection.execute("DELETE FROM subscriptions")
        cursor = connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date,end_date) "
            "VALUES('opencode', 'Z.AI - $400 for 3 months', 133.3333333333, 'monthly', "
            "'2026-08-01', NULL)"
        )
        legacy_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO subscription_daily_costs(subscription_id,tool_key,date,cost_usd,raw_id) "
            "VALUES(?,?,?,?,?)",
            (legacy_id, "opencode", "2026-08-01", 4.3, f"subscription:{legacy_id}:2026-08-01"),
        )
    initialize(database)
    initialize(database)
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT id, name, amount_usd, cadence, start_date, end_date FROM subscriptions"
        ).fetchall()
        fk_rows = connection.execute(
            "SELECT subscription_id, cost_usd FROM subscription_daily_costs"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (
            legacy_id,
            "Z.AI Coding Plan",
            400.0,
            "quarterly",
            "2026-08-01",
            None,
        )
        assert [(row[0], round(row[1], 2)) for row in fk_rows] == [(legacy_id, 4.3)]
        written = materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 3)
        )
        daily = connection.execute(
            "SELECT cost_usd FROM subscription_daily_costs WHERE subscription_id=? AND date>? "
            "ORDER BY date",
            (legacy_id, "2026-08-01"),
        ).fetchall()
    assert written == 2
    expected = float(Decimal("400") / Decimal(92))
    assert len(daily) == 2
    assert all(abs(row[0] - expected) < 1e-9 for row in daily)


def test_custom_or_edited_opencode_subscriptions_are_preserved(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscription_daily_costs")
        connection.execute("DELETE FROM subscriptions")
        untouched = [
            (
                "opencode",
                "My ZAI plan",
                133.3333333333,
                "monthly",
                "2026-08-01",
            ),
            (
                "opencode",
                "Z.AI - $400 for 3 months",
                150.0,
                "monthly",
                "2026-08-01",
            ),
            (
                "opencode",
                "Z.AI - $400 for 3 months",
                133.33,
                "monthly",
                "2026-08-01",
            ),
            (
                "opencode",
                "Z.AI - $400 for 3 months",
                133.3333333333,
                "annual",
                "2026-08-01",
            ),
            (
                "opencode",
                "Z.AI - $400 for 3 months",
                133.3333333333,
                "monthly",
                "2026-07-01",
            ),
        ]
        for tool_key, name, amount, cadence, start in untouched:
            connection.execute(
                "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date,end_date) "
                "VALUES(?,?,?,?,?,NULL)",
                (tool_key, name, amount, cadence, start),
            )
        before = connection.execute(
            "SELECT tool_key, name, amount_usd, cadence, start_date, end_date FROM subscriptions "
            "ORDER BY id"
        ).fetchall()
        before_tuples = [tuple(row) for row in before]
    initialize(database)
    with connect(database) as connection:
        after = connection.execute(
            "SELECT tool_key, name, amount_usd, cadence, start_date, end_date FROM subscriptions "
            "ORDER BY id"
        ).fetchall()
        quarterly_count = connection.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE cadence='quarterly'"
        ).fetchone()[0]
    assert [tuple(row) for row in after] == before_tuples
    assert len(after) == 5
    assert quarterly_count == 0


def test_update_subscription_edits_quarterly_amount_and_rematerializes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM subscription_daily_costs")
        connection.execute("DELETE FROM subscriptions")
        subscription_id = add_subscription(
            connection,
            tool_key="opencode",
            name="Z.AI Coding Plan",
            amount_usd=400,
            cadence="quarterly",
            start_date="2026-08-01",
            end_date=None,
        )
        assert materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 1)
        ) == 1
        assert update_subscription(
            connection,
            subscription_id,
            amount_usd=460,
            name="Z.AI Coding Plan Plus",
        )
        rewritten = materialize_subscription_days(
            connection, start=date(2026, 8, 1), end=date(2026, 8, 1)
        )
        row = connection.execute(
            "SELECT name, amount_usd, cadence, cost_usd FROM subscriptions "
            "JOIN subscription_daily_costs ON subscriptions.id = subscription_daily_costs.subscription_id "
            "WHERE subscriptions.id=?",
            (subscription_id,),
        ).fetchone()
        listed = list_subscriptions(connection)
    assert rewritten == 0
    assert row[0] == "Z.AI Coding Plan Plus"
    assert row[1] == 460
    assert row[2] == "quarterly"
    expected = float(Decimal("460") / Decimal(92))
    assert abs(row[3] - expected) < 1e-9
    assert listed[0]["name"] == "Z.AI Coding Plan Plus"


def test_add_subscription_rejects_unsupported_cadence(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        with pytest.raises(ValueError, match="unsupported subscription cadence"):
            add_subscription(
                connection,
                tool_key="codex",
                name="Bad",
                amount_usd=10,
                cadence="weekly",
                start_date="2026-08-01",
                end_date=None,
            )
        assert update_subscription(connection, 999, amount_usd=1) is False
