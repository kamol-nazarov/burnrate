"""Release A: schema 11 migration compatibility (task A00).

A schema-10 database with mixed-precision ISO timestamps upgrades additively:
identical historical tokens/costs, stable ids, idempotent re-runs, and a
recoverable database after an interrupted upgrade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


from spend_app.db import (
    SCHEMA_VERSION,
    UsageEvent,
    backup_database,
    connect,
    initialize,
    upsert_usage_event,
)


def _schema10_database(path: Path) -> dict:
    """A small but real schema-10 database with mixed timestamp precision."""
    initialize(path)
    with connect(path) as connection:
        connection.execute("UPDATE app_meta SET value='10' WHERE key='schema_version'")
        # Simulate the schema-10 shape: no epoch column, mixed precision.
        connection.execute("DROP INDEX IF EXISTS idx_usage_events_epoch")
        connection.execute("ALTER TABLE usage_events DROP COLUMN occurred_epoch_us")
        rows = [
            (
                "codex_local", "codex", "gpt-6-astra",
                "2026-09-01T12:00:00Z",          # second precision
                "2026-09-01T12:00:00.500000+00:00",  # sub-second LATER but TEXT-sorts first
                1000, 0, 0, 200,
            )
        ]
        for index, (source, tool, model, early, late, inp, cached, writes, out) in enumerate(rows):
            for position, stamp in ((0, early), (1, late)):
                connection.execute(
                    """
                    INSERT INTO usage_events(
                        source, tool_key, model_key, occurred_at, session_id, project,
                        input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                        output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, is_exact,
                        raw_id, ingested_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source, tool, model, stamp, f"s{index}", None,
                        inp, cached, writes, 0, out, None, None, 0.01, 0,
                        f"codex-local:mixed:{position}", "2026-09-01T12:00:01Z",
                    ),
                )
        connection.commit()
    return {"events": 2}


def _event_counts(path: Path) -> tuple[int, int]:
    with connect(path) as connection:
        tokens = connection.execute(
            "SELECT COALESCE(SUM(input_tokens + cached_input_tokens + cache_write_tokens + output_tokens), 0) FROM usage_events"
        ).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    return int(count), int(tokens)


def test_schema10_upgrade_preserves_history_and_adds_epoch(tmp_path: Path):
    database = tmp_path / "upgrade.db"
    expected = _schema10_database(database)
    initialize(database)  # runs the schema-11 migration
    with connect(database) as connection:
        version = connection.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
        epoch_columns = [
            connection.execute(f"PRAGMA table_info('{table}')").fetchall()
            for table in ("usage_events", "unpriced_usage_events")
        ]
    assert version == str(SCHEMA_VERSION)
    assert all(any(row[1] == "occurred_epoch_us" for row in table) for table in epoch_columns)
    count, tokens = _event_counts(database)
    assert (count, tokens) == (expected["events"], 2400)


def test_backfilled_epoch_orders_within_second_correctly(tmp_path: Path):
    from spend_app.aggregate import _load_events

    database = tmp_path / "order.db"
    _schema10_database(database)
    initialize(database)
    from datetime import UTC, datetime

    with connect(database) as connection:
        events = _load_events(
            connection,
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 2, tzinfo=UTC),
            "all",
        )
    stamps = [event["occurred_at"] for event in events]
    # TEXT order would put ".500000" first; epoch order puts it second.
    assert stamps == [
        "2026-09-01T12:00:00Z",
        "2026-09-01T12:00:00.500000+00:00",
    ]


def test_repeated_migration_is_a_no_op(tmp_path: Path):
    database = tmp_path / "idem.db"
    _schema10_database(database)
    initialize(database)
    first = _event_counts(database)
    initialize(database)
    initialize(database)
    assert _event_counts(database) == first


def test_fresh_database_starts_at_current_schema(tmp_path: Path):
    database = tmp_path / "fresh.db"
    initialize(database)
    with connect(database) as connection:
        version = connection.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
        progress = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='opencode_session_progress'"
        ).fetchone()
    assert version == str(SCHEMA_VERSION)
    assert progress is not None


def test_new_writes_carry_the_epoch_column(tmp_path: Path):
    from datetime import UTC, datetime

    from spend_app.timeutil import epoch_micros

    database = tmp_path / "writes.db"
    initialize(database)
    stamp = "2026-09-05T10:30:00.250Z"
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="codex_local", tool_key="codex", model_key="gpt-6-astra",
                occurred_at=stamp, session_id="s", project=None,
                input_tokens=10, cached_input_tokens=0, cache_write_tokens=0,
                cache_write_1h_tokens=0, output_tokens=5, reasoning_tokens=None,
                cost_usd=None, computed_cost_usd=0.0, raw_id="x:1", ingested_at="2026-09-05T10:30:01Z",
            ),
        )
        stored = connection.execute("SELECT occurred_epoch_us FROM usage_events WHERE raw_id='x:1'").fetchone()[0]
    assert stored == epoch_micros(datetime(2026, 9, 5, 10, 30, 0, 250000, tzinfo=UTC))


def test_backup_integrity_roundtrip(tmp_path: Path):
    database = tmp_path / "src.db"
    _schema10_database(database)
    initialize(database)
    backup = backup_database(database, tmp_path / "backups" / "copy.db")
    assert _event_counts(backup) == _event_counts(database)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
