from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from spend_app.subscriptions import (
    SUBSCRIPTION_SEED_VERSION_KEY,
    migrate_legacy_subscription_identities,
    migrate_legacy_zai_seed,
    seed_subscriptions,
)


SCHEMA_VERSION = 9

EXACT_USAGE_SOURCES = ("codex_local", "openai_admin", "claude_local", "anthropic_admin")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_1h_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    cost_usd REAL,
    computed_cost_usd REAL NOT NULL CHECK (computed_cost_usd >= 0),
    is_exact INTEGER NOT NULL DEFAULT 0 CHECK (is_exact IN (0, 1)),
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_events_occurred_at ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_tool_model ON usage_events(tool_key, model_key);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_project ON usage_events(project);

CREATE TABLE IF NOT EXISTS model_prices (
    model_key TEXT NOT NULL,
    input_per_mtok REAL NOT NULL CHECK (input_per_mtok >= 0),
    cached_input_per_mtok REAL NOT NULL CHECK (cached_input_per_mtok >= 0),
    cache_write_per_mtok REAL NOT NULL CHECK (cache_write_per_mtok >= 0),
    cache_write_1h_per_mtok REAL,
    output_per_mtok REAL NOT NULL CHECK (output_per_mtok >= 0),
    long_context_threshold INTEGER,
    long_input_multiplier REAL NOT NULL DEFAULT 1,
    long_output_multiplier REAL NOT NULL DEFAULT 1,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    source_url TEXT NOT NULL,
    PRIMARY KEY (model_key, effective_from)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
    cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'annual')),
    start_date TEXT NOT NULL,
    end_date TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    tool_key TEXT NOT NULL,
    project TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    model_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'skipped', 'partial')),
    events_written INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_id ON ingest_runs(source, id);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_status_finished ON ingest_runs(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs(started_at);

CREATE TABLE IF NOT EXISTS pricing_gaps (
    model_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    sample_raw_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_gap_events (
    raw_id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_cost_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    starting_at TEXT NOT NULL,
    ending_at TEXT NOT NULL,
    project_id TEXT,
    line_item TEXT,
    model_key TEXT,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provider_cost_buckets_time
ON provider_cost_buckets(starting_at, ending_at);

CREATE TABLE IF NOT EXISTS subscription_daily_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    tool_key TEXT NOT NULL,
    date TEXT NOT NULL,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);

CREATE INDEX IF NOT EXISTS idx_subscription_daily_costs_date
ON subscription_daily_costs(date, tool_key);

CREATE TABLE IF NOT EXISTS unpriced_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER,
    unclassified_tokens INTEGER NOT NULL DEFAULT 0,
    telemetry_complete INTEGER NOT NULL DEFAULT 1,
    cost_usd REAL,
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unpriced_usage_time
ON unpriced_usage_events(occurred_at, tool_key, model_key);

CREATE TABLE IF NOT EXISTS coverage_gap_events (
    raw_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    issue TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_gap_time
ON coverage_gap_events(occurred_at, tool_key, model_key);
"""

ACTIVITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    label TEXT NOT NULL,
    used REAL,
    allowance REAL,
    unit TEXT NOT NULL,
    pct REAL,
    resets_at TEXT,
    source TEXT NOT NULL,
    is_payg BOOLEAN CHECK (is_payg IS NULL OR is_payg IN (0, 1)),
    polled_at TEXT NOT NULL,
    UNIQUE(provider_key, limit_key, polled_at)
);

CREATE INDEX IF NOT EXISTS idx_quotas_provider ON quotas(provider_key, polled_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_key TEXT,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_last_seen ON agent_runs(last_seen_at);
"""


@dataclass(frozen=True)
class UsageEvent:
    source: str
    tool_key: str
    model_key: str
    occurred_at: str
    session_id: str | None
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    cost_usd: float | None
    computed_cost_usd: float
    raw_id: str
    ingested_at: str
    is_exact: bool = False


@dataclass(frozen=True)
class ProviderCostBucket:
    source: str
    starting_at: str
    ending_at: str
    project_id: str | None
    line_item: str | None
    model_key: str | None
    cost_usd: float
    raw_id: str
    ingested_at: str


@dataclass(frozen=True)
class UnpricedUsageEvent:
    source: str
    tool_key: str
    model_key: str
    occurred_at: str
    session_id: str | None
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    unclassified_tokens: int
    telemetry_complete: bool
    cost_usd: float | None
    raw_id: str
    ingested_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def backup_database(src: Path, dst: Path) -> Path:
    """Copy ``src`` to ``dst`` using SQLite's Online Backup API.

    Safe while the source is in WAL mode: pages are read from the live
    connection, so the destination is a consistent standalone database
    without copying ``-wal``/``-shm`` sidecars. Not called by initialize.
    """
    source_path = Path(src)
    dest_path = Path(dst)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path, timeout=30) as source:
        with sqlite3.connect(dest_path, timeout=30) as dest:
            source.backup(dest)
    return dest_path


def rebuild_subscriptions_for_quarterly(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone()
    if row is None or "quarterly" in (row[0] or ""):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE IF EXISTS subscriptions_rebuild")
        connection.execute(
            """
            CREATE TABLE subscriptions_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_key TEXT NOT NULL,
                name TEXT NOT NULL,
                amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
                cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'annual')),
                start_date TEXT NOT NULL,
                end_date TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subscriptions_rebuild(id, tool_key, name, amount_usd, cadence, start_date, end_date)
            SELECT id, tool_key, name, amount_usd, cadence, start_date, end_date FROM subscriptions
            """
        )
        connection.execute("DROP TABLE subscriptions")
        connection.execute("ALTER TABLE subscriptions_rebuild RENAME TO subscriptions")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("subscriptions rebuild produced foreign key violations")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def rebuild_ingest_runs_for_partial(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ingest_runs'"
    ).fetchone()
    if row is None or "partial" in (row[0] or ""):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE IF EXISTS ingest_runs_rebuild")
        connection.execute(
            """
            CREATE TABLE ingest_runs_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'skipped', 'partial')),
                events_written INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ingest_runs_rebuild(id, source, started_at, finished_at, status, events_written, error)
            SELECT id, source, started_at, finished_at, status, events_written, error FROM ingest_runs
            """
        )
        connection.execute("DROP TABLE ingest_runs")
        connection.execute("ALTER TABLE ingest_runs_rebuild RENAME TO ingest_runs")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def ensure_activity_table_shapes(connection: sqlite3.Connection) -> None:
    quota_columns = {row[1] for row in connection.execute("PRAGMA table_info('quotas')")}
    agent_columns = {row[1] for row in connection.execute("PRAGMA table_info('agent_runs')")}
    stale_quotas = bool(quota_columns) and "provider_key" not in quota_columns
    stale_agent_runs = bool(agent_columns) and "run_id" in agent_columns
    if stale_quotas or stale_agent_runs:
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if stale_quotas:
                connection.execute("DROP TABLE quotas")
            if stale_agent_runs:
                connection.execute("DROP TABLE agent_runs")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(ACTIVITY_SCHEMA_SQL)


REQUIRED_SCHEMA_OBJECTS = frozenset(
    {
        "app_meta",
        "usage_events",
        "unpriced_usage_events",
        "model_prices",
        "subscriptions",
        "subscription_daily_costs",
        "sessions",
        "ingest_runs",
        "pricing_gaps",
        "pricing_gap_events",
        "provider_cost_buckets",
        "coverage_gap_events",
        "quotas",
        "agent_runs",
        "idx_ingest_runs_source_id",
        "idx_ingest_runs_status_finished",
        "idx_ingest_runs_started_at",
    }
)

INGEST_RUN_RETENTION_HOURS = 24
INGEST_RUN_KEEP_PER_SOURCE = 200


def schema_is_current(connection: sqlite3.Connection) -> bool:
    """True when the database already carries the current schema and seeds.

    Every scheduler job and poller calls ``initialize`` defensively. Running
    the full migration script each time cost a write transaction every few
    seconds (the schema-version upsert) and contended with ingest writers.
    A current database is recognised by its version stamp, seed stamp and the
    presence of every required table and index, so a partially migrated or
    externally modified file still takes the full path.
    """
    try:
        rows = connection.execute("SELECT key, value FROM app_meta").fetchall()
    except sqlite3.OperationalError:
        return False
    meta = {row[0]: row[1] for row in rows}
    if meta.get("schema_version") != str(SCHEMA_VERSION):
        return False
    if SUBSCRIPTION_SEED_VERSION_KEY not in meta:
        return False
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }
    return REQUIRED_SCHEMA_OBJECTS <= present


def prune_ingest_runs(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    retention_hours: int = INGEST_RUN_RETENTION_HOURS,
    keep_per_source: int = INGEST_RUN_KEEP_PER_SOURCE,
) -> int:
    """Drop finished ingest-run history older than the retention window.

    The 15-second local cadence writes five run rows per cycle (about 29k per
    day). Nothing reads history beyond the latest run per source and the
    latest success, so old rows only slowed those lookups and grew the file.
    The newest ``keep_per_source`` rows of every source are always kept, so
    the latest status and last success survive even for dormant sources.
    """
    stamp = now or utc_now()
    cutoff = (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(hours=retention_hours)
    ).isoformat().replace("+00:00", "Z")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_runs'"
    ).fetchone()
    if table is None:
        return 0
    cursor = connection.execute(
        """
        DELETE FROM ingest_runs
        WHERE finished_at IS NOT NULL
          AND started_at < ?
          AND id NOT IN (
              SELECT r.id FROM ingest_runs AS r
              WHERE r.source = ingest_runs.source
              ORDER BY r.id DESC LIMIT ?
          )
        """,
        (cutoff, int(keep_per_source)),
    )
    return int(cursor.rowcount or 0)


def initialize(path: Path) -> None:
    with connect(path) as connection:
        if schema_is_current(connection):
            # Cheap row-level migrations stay on every call: they only touch
            # the handful of subscription rows and write nothing when there
            # is nothing legacy to rename.
            migrate_legacy_zai_seed(connection)
            migrate_legacy_subscription_identities(connection)
            seed_subscriptions(connection)
            return
        try:
            previous_row = connection.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()
            previous_version = int(previous_row[0]) if previous_row else 0
        except sqlite3.OperationalError:
            previous_version = 0
        connection.executescript(SCHEMA_SQL)
        ensure_activity_table_shapes(connection)
        rebuild_subscriptions_for_quarterly(connection)
        rebuild_ingest_runs_for_partial(connection)
        # The rebuild above recreates ingest_runs without its indexes.
        connection.executescript(
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_id ON ingest_runs(source, id);"
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_status_finished ON ingest_runs(status, finished_at);"
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs(started_at);"
        )
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info('usage_events')")}
        if "cache_write_1h_tokens" not in usage_columns:
            connection.execute(
                "ALTER TABLE usage_events ADD COLUMN cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0"
            )
        price_columns = {row[1] for row in connection.execute("PRAGMA table_info('model_prices')")}
        if "cache_write_1h_per_mtok" not in price_columns:
            connection.execute("ALTER TABLE model_prices ADD COLUMN cache_write_1h_per_mtok REAL")
        unpriced_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('unpriced_usage_events')")
        }
        if "unclassified_tokens" not in unpriced_columns:
            connection.execute(
                "ALTER TABLE unpriced_usage_events ADD COLUMN unclassified_tokens INTEGER NOT NULL DEFAULT 0"
            )
        if "telemetry_complete" not in unpriced_columns:
            connection.execute(
                "ALTER TABLE unpriced_usage_events ADD COLUMN telemetry_complete INTEGER NOT NULL DEFAULT 1"
            )
        if "is_exact" not in usage_columns:
            connection.execute(
                "ALTER TABLE usage_events ADD COLUMN is_exact INTEGER NOT NULL DEFAULT 0"
            )
            placeholders = ",".join("?" for _ in EXACT_USAGE_SOURCES)
            connection.execute(
                f"UPDATE usage_events SET is_exact = CASE WHEN source IN ({placeholders}) "
                "THEN 1 ELSE 0 END",
                EXACT_USAGE_SOURCES,
            )
        if previous_version < 5:
            connection.execute("DELETE FROM pricing_gaps")
            connection.execute("DELETE FROM pricing_gap_events")
        migrate_legacy_zai_seed(connection)
        migrate_legacy_subscription_identities(connection)
        seed_subscriptions(connection)
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def _same_content(existing: sqlite3.Row | None, values: dict) -> bool:
    """True when the stored row already carries these values (ignoring ingested_at)."""
    if existing is None:
        return False
    for column, value in values.items():
        if column == "ingested_at":
            continue
        stored = existing[column]
        if isinstance(value, bool):
            value = int(value)
        if stored == value:
            continue
        if (
            isinstance(stored, (int, float))
            and isinstance(value, (int, float))
            and float(stored) == float(value)
        ):
            continue
        return False
    return True


def upsert_usage_event(connection: sqlite3.Connection, event: UsageEvent) -> bool:
    values = asdict(event)
    columns = tuple(values)
    existing = connection.execute(
        f"SELECT {','.join(columns)} FROM usage_events WHERE raw_id = ?", (event.raw_id,)
    ).fetchone()
    existed = existing is not None
    if _same_content(existing, values):
        # Read-only local stores (Cursor, OpenCode, Traycer) are re-read every
        # cycle; rewriting unchanged rows only churned the WAL and made
        # ingested_at meaningless as a change marker.
        connection.execute("DELETE FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,))
        return False
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO usage_events({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    connection.execute("DELETE FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,))
    return not existed


def upsert_cost_bucket(connection: sqlite3.Connection, bucket: ProviderCostBucket) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM provider_cost_buckets WHERE raw_id = ?", (bucket.raw_id,)
    ).fetchone() is not None
    values = asdict(bucket)
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO provider_cost_buckets({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    return not existed


def upsert_unpriced_event(connection: sqlite3.Connection, event: UnpricedUsageEvent) -> bool:
    values = asdict(event)
    columns = tuple(values)
    existing = connection.execute(
        f"SELECT {','.join(columns)} FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,)
    ).fetchone()
    existed = existing is not None
    if _same_content(existing, values):
        connection.execute("DELETE FROM usage_events WHERE raw_id=?", (event.raw_id,))
        return False
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO unpriced_usage_events({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    connection.execute("DELETE FROM usage_events WHERE raw_id=?", (event.raw_id,))
    return not existed


def upsert_quota(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    limit_key: str,
    label: str,
    unit: str,
    source: str,
    polled_at: str,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
    resets_at: str | None = None,
    is_payg: bool | None = None,
) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM quotas WHERE provider_key=? AND limit_key=? AND polled_at=?",
        (provider_key, limit_key, polled_at),
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO quotas(
            provider_key, limit_key, label, used, allowance, unit, pct, resets_at,
            source, is_payg, polled_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_key, limit_key, polled_at) DO UPDATE SET
            label=excluded.label,
            used=excluded.used,
            allowance=excluded.allowance,
            unit=excluded.unit,
            pct=excluded.pct,
            resets_at=excluded.resets_at,
            source=excluded.source,
            is_payg=excluded.is_payg
        """,
        (
            provider_key,
            limit_key,
            label,
            used,
            allowance,
            unit,
            pct,
            resets_at,
            source,
            is_payg,
            polled_at,
        ),
    )
    return not existed


def upsert_agent_run(
    connection: sqlite3.Connection,
    *,
    id: str,
    name: str,
    model_key: str | None,
    state: str,
    started_at: str,
    last_seen_at: str,
) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM agent_runs WHERE id = ?", (id,)
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO agent_runs(id, name, model_key, state, started_at, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            model_key=excluded.model_key,
            state=excluded.state,
            started_at=excluded.started_at,
            last_seen_at=excluded.last_seen_at
        """,
        (id, name, model_key, state, started_at, last_seen_at),
    )
    return not existed


def record_coverage_gap(
    connection: sqlite3.Connection,
    *,
    raw_id: str,
    source: str,
    tool_key: str,
    model_key: str,
    occurred_at: str,
    issue: str,
) -> None:
    connection.execute(
        """
        INSERT INTO coverage_gap_events(raw_id,source,tool_key,model_key,occurred_at,issue)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(raw_id) DO UPDATE SET
            source=excluded.source,
            tool_key=excluded.tool_key,
            model_key=excluded.model_key,
            occurred_at=excluded.occurred_at,
            issue=excluded.issue
        """,
        (raw_id, source, tool_key, model_key, occurred_at, issue),
    )


def upsert_session(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    tool_key: str,
    project: str | None,
    started_at: str,
    ended_at: str | None,
    model_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO sessions(session_id, tool_key, project, started_at, ended_at, model_key)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            tool_key=excluded.tool_key,
            project=COALESCE(excluded.project, sessions.project),
            started_at=MIN(sessions.started_at, excluded.started_at),
            ended_at=MAX(COALESCE(sessions.ended_at, excluded.ended_at), excluded.ended_at),
            model_key=excluded.model_key
        """,
        (session_id, tool_key, project, started_at, ended_at, model_key),
    )


def record_pricing_gap(
    connection: sqlite3.Connection,
    *,
    model_key: str,
    source: str,
    occurred_at: str,
    raw_id: str,
) -> None:
    inserted = connection.execute(
        "INSERT OR IGNORE INTO pricing_gap_events(raw_id,model_key,source,occurred_at) VALUES(?,?,?,?)",
        (raw_id, model_key, source, occurred_at),
    ).rowcount
    if not inserted:
        return
    connection.execute(
        """
        INSERT INTO pricing_gaps(model_key, source, first_seen_at, last_seen_at, occurrences, sample_raw_id)
        VALUES(?, ?, ?, ?, 1, ?)
        ON CONFLICT(model_key) DO UPDATE SET
            last_seen_at=MAX(pricing_gaps.last_seen_at, excluded.last_seen_at),
            occurrences=pricing_gaps.occurrences + 1,
            sample_raw_id=excluded.sample_raw_id
        """,
        (model_key, source, occurred_at, occurred_at, raw_id),
    )


def sync_model_prices(connection: sqlite3.Connection, prices: object) -> int:
    written = 0
    for price in getattr(prices, "prices", ()):  # PricingEngine without a circular import.
        connection.execute(
            """
            INSERT INTO model_prices(
                model_key, input_per_mtok, cached_input_per_mtok, cache_write_per_mtok,
                cache_write_1h_per_mtok, output_per_mtok, long_context_threshold, long_input_multiplier,
                long_output_multiplier, effective_from, effective_to, source_url
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_key, effective_from) DO UPDATE SET
                input_per_mtok=excluded.input_per_mtok,
                cached_input_per_mtok=excluded.cached_input_per_mtok,
                cache_write_per_mtok=excluded.cache_write_per_mtok,
                cache_write_1h_per_mtok=excluded.cache_write_1h_per_mtok,
                output_per_mtok=excluded.output_per_mtok,
                long_context_threshold=excluded.long_context_threshold,
                long_input_multiplier=excluded.long_input_multiplier,
                long_output_multiplier=excluded.long_output_multiplier,
                effective_to=excluded.effective_to,
                source_url=excluded.source_url
            """,
            (
                price.model_key,
                float(price.input_per_mtok),
                float(price.cached_input_per_mtok),
                float(price.cache_write_per_mtok),
                float(price.cache_write_1h_per_mtok) if price.cache_write_1h_per_mtok else None,
                float(price.output_per_mtok),
                price.long_context_threshold,
                float(price.long_input_multiplier),
                float(price.long_output_multiplier),
                price.effective_from.isoformat(),
                price.effective_to.isoformat() if price.effective_to else None,
                price.source_url,
            ),
        )
        written += 1
    return written
