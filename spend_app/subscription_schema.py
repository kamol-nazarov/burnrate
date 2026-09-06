"""Schema 10: stable plans with inclusive, nonoverlapping effective terms."""

import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def upgrade_database(path: Path, initialize_base):
    from spend_app.db import backup_database, connect

    path = Path(path)
    version = 0
    unknown_version = False
    existing = path.is_file() and path.stat().st_size > 0
    if path.is_file():
        with closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as source:
            try:
                version = int(
                    source.execute(
                        "SELECT value FROM app_meta WHERE key='schema_version'"
                    ).fetchone()[0]
                )
                if version > 10:
                    raise RuntimeError(
                        "Database uses a newer schema; upgrade this application before opening it"
                    )
                columns = {
                    row[1] for row in source.execute("PRAGMA table_info(subscriptions)")
                }
                daily_columns = {
                    row[1]
                    for row in source.execute(
                        "PRAGMA table_info(subscription_daily_costs)"
                    )
                }
                if (
                    version == 10
                    and "plan_id" in columns
                    and "cost_decimal" in daily_columns
                ):
                    return
            except (sqlite3.Error, TypeError, ValueError):
                unknown_version = existing
                tables = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                # Preserve the documented pre-version activity-only upgrade,
                # but never initialize an unrelated database in place.
                if tables - {"sqlite_sequence"} == {"quotas", "agent_runs"}:
                    unknown_version = False
        if existing:
            target = (
                path.parent
                / "backups"
                / (
                    "pre-subscription-terms-"
                    + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                    + "-"
                    + uuid.uuid4().hex
                    + ".db"
                )
            )
            try:
                backup_database(path, target)
                with closing(sqlite3.connect(target)) as backup:
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Invalid backup")
            except Exception as exc:
                raise RuntimeError(
                    "Subscription migration backup failed; database was not upgraded"
                ) from exc
        if unknown_version:
            raise RuntimeError(
                "Existing database schema could not be identified; backup created and upgrade stopped safely"
            )
    if version < 9:
        initialize_base(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS subscription_plans(id INTEGER PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1, end_date TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS subscription_mutations(request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL)"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")
        }
        if "plan_id" not in columns:
            connection.execute(
                "ALTER TABLE subscriptions ADD COLUMN plan_id INTEGER REFERENCES subscription_plans(id)"
            )
        # Every existing row is one real plan; do not invent older revisions or
        # collapse legitimate plans that happen to use the same tool.
        for row in connection.execute(
            "SELECT id,end_date FROM subscriptions WHERE plan_id IS NULL"
        ).fetchall():
            connection.execute(
                "INSERT OR IGNORE INTO subscription_plans(id,end_date) VALUES(?,?)",
                (row[0], row[1]),
            )
            connection.execute(
                "UPDATE subscriptions SET plan_id=id WHERE id=?", (row[0],)
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscription_terms_plan ON subscriptions(plan_id,start_date)"
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(subscription_daily_costs)")
        }
        if "cost_decimal" not in columns:
            connection.execute(
                "ALTER TABLE subscription_daily_costs ADD COLUMN cost_decimal TEXT"
            )
        bounds = connection.execute(
            "SELECT MIN(date),MAX(date) FROM subscription_daily_costs"
        ).fetchone()
        if bounds[0]:
            from datetime import date

            from spend_app.subscriptions import materialize_subscription_days

            materialize_subscription_days(
                connection,
                start=date.fromisoformat(bounds[0]),
                end=date.fromisoformat(bounds[1]),
            )
        connection.execute("UPDATE app_meta SET value='10' WHERE key='schema_version'")
