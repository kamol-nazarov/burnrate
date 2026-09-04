"""Per-source ingest run records.

Adapters persist rows through ``spend_app.adapters.common``. The scheduler
iterates ``spend_app.providers.REGISTRY`` and wraps each adapter so one
failure cannot abort the rest of the cycle.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from spend_app.db import utc_now


@dataclass
class IngestRun:
    connection: sqlite3.Connection
    source: str
    id: int
    events_written: int = 0

    @classmethod
    def start(cls, connection: sqlite3.Connection, source: str) -> "IngestRun":
        cursor = connection.execute(
            "INSERT INTO ingest_runs(source, started_at, status, events_written) VALUES(?, ?, 'running', 0)",
            (source, utc_now()),
        )
        return cls(connection=connection, source=source, id=int(cursor.lastrowid))

    def finish(self, *, status: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE ingest_runs SET finished_at=?, status=?, events_written=?, error=? WHERE id=?",
            (utc_now(), status, self.events_written, error, self.id),
        )
