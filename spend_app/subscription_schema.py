"""Schema 10: stable plans with inclusive, nonoverlapping effective terms."""
from datetime import datetime, UTC
from pathlib import Path
import sqlite3
import uuid


def upgrade_database(path: Path, initialize_base):
    from spend_app.db import backup_database, connect
    path = Path(path)
    version = 0
    if path.is_file():
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as source:
            try:
                version = int(source.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0])
                columns = {row[1] for row in source.execute("PRAGMA table_info(subscriptions)")}
                if version >= 10 and "plan_id" in columns:
                    return
            except (sqlite3.Error, TypeError):
                pass
        if version:
            target = path.parent / "backups" / ("pre-subscription-terms-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".db")
            try:
                backup_database(path, target)
                with sqlite3.connect(target) as backup:
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Invalid backup")
            except Exception as exc:
                raise RuntimeError("Subscription migration backup failed; database was not upgraded") from exc
    if version < 9:
        initialize_base(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS subscription_plans(id INTEGER PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1, end_date TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS subscription_mutations(request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL)")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
        if "plan_id" not in columns:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN plan_id INTEGER REFERENCES subscription_plans(id)")
        # Every existing row is one real plan; do not invent older revisions or
        # collapse legitimate plans that happen to use the same tool.
        for row in connection.execute("SELECT id,end_date FROM subscriptions WHERE plan_id IS NULL").fetchall():
            connection.execute("INSERT OR IGNORE INTO subscription_plans(id,end_date) VALUES(?,?)", (row[0],row[1]))
            connection.execute("UPDATE subscriptions SET plan_id=id WHERE id=?", (row[0],))
        connection.execute("CREATE INDEX IF NOT EXISTS idx_subscription_terms_plan ON subscriptions(plan_id,start_date)")
        connection.execute("UPDATE app_meta SET value='10' WHERE key='schema_version'")
