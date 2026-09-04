from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal


SUBSCRIPTION_SEED_VERSION_KEY = "subscription_seed_version"
SUBSCRIPTION_SEED_VERSION = "1"

LEGACY_ZAI_SEED_TOOL_KEY = "opencode"
LEGACY_ZAI_SEED_NAME = "Z.AI - $400 for 3 months"
LEGACY_ZAI_SEED_START_DATE = "2026-08-01"
LEGACY_ZAI_PLAN_AMOUNT_USD = 400.0
LEGACY_ZAI_LEGACY_CADENCE = "monthly"
LEGACY_ZAI_TARGET_CADENCE = "quarterly"
LEGACY_ZAI_LEGACY_AMOUNT_USD = float(Decimal(str(LEGACY_ZAI_PLAN_AMOUNT_USD)) / Decimal(3))
LEGACY_ZAI_AMOUNT_TOLERANCE = 1e-6

# Public onboarding inserts zero subscription rows. Users add plans via CLI.
# migrate_legacy_* still rewrites already-populated identities; do not re-seed.
SUBSCRIPTION_SEEDS: tuple[dict, ...] = ()

SUPPORTED_CADENCES = ("monthly", "quarterly", "annual")
EDITABLE_SUBSCRIPTION_FIELDS = (
    "tool_key",
    "name",
    "amount_usd",
    "cadence",
    "start_date",
    "end_date",
)


def daily_cost(amount_usd: float, cadence: str, day: date) -> Decimal:
    amount = Decimal(str(amount_usd))
    if cadence == "monthly":
        return amount / Decimal(calendar.monthrange(day.year, day.month)[1])
    if cadence == "quarterly":
        first_month = 3 * ((day.month - 1) // 3) + 1
        days = sum(
            calendar.monthrange(day.year, first_month + offset)[1] for offset in range(3)
        )
        return amount / Decimal(days)
    if cadence == "annual":
        return amount / Decimal(366 if calendar.isleap(day.year) else 365)
    raise ValueError(f"unsupported subscription cadence: {cadence}")


def _require_cadence(cadence: str) -> str:
    if cadence not in SUPPORTED_CADENCES:
        raise ValueError(f"unsupported subscription cadence: {cadence}")
    return cadence


LEGACY_IDENTITY_PATTERNS = (
    {
        "tool_key": "codex",
        "legacy_name": "Codex - $200 per month",
        "name": "Codex",
        "amount_usd": 200.0,
        "cadence": "monthly",
        "legacy_amounts": (200.0,),
        "legacy_cadences": ("monthly",),
    },
    {
        "tool_key": "claude-code",
        "legacy_name": "Claude - $200 per month",
        "name": "Claude Code",
        "amount_usd": 200.0,
        "cadence": "monthly",
        "legacy_amounts": (200.0,),
        "legacy_cadences": ("monthly",),
    },
    {
        "tool_key": "grok",
        "legacy_name": "SuperGrok - $300 per month",
        "name": "SuperGrok",
        "amount_usd": 300.0,
        "cadence": "monthly",
        "legacy_amounts": (300.0,),
        "legacy_cadences": ("monthly",),
    },
    {
        "tool_key": "cursor",
        "legacy_name": "Cursor - free",
        "name": "Cursor",
        "amount_usd": 0.0,
        "cadence": "monthly",
        "legacy_amounts": (0.0,),
        "legacy_cadences": ("monthly",),
    },
    {
        "tool_key": "opencode",
        "legacy_name": "Z.AI - $400 for 3 months",
        "name": "Z.AI Coding Plan",
        "amount_usd": LEGACY_ZAI_PLAN_AMOUNT_USD,
        "cadence": "quarterly",
        "legacy_amounts": (LEGACY_ZAI_PLAN_AMOUNT_USD,),
        "legacy_cadences": ("quarterly",),
    },
)


def _amount_matches(value: float, allowed: tuple[float, ...]) -> bool:
    return any(abs(value - candidate) <= LEGACY_ZAI_AMOUNT_TOLERANCE for candidate in allowed)


def migrate_legacy_subscription_identities(connection) -> int:
    migrated = 0
    for pattern in LEGACY_IDENTITY_PATTERNS:
        rows = connection.execute(
            "SELECT id, amount_usd, cadence FROM subscriptions WHERE tool_key=? AND name=?",
            (pattern["tool_key"], pattern["legacy_name"]),
        ).fetchall()
        for row in rows:
            if not _amount_matches(float(row["amount_usd"]), pattern["legacy_amounts"]):
                continue
            if row["cadence"] not in pattern["legacy_cadences"]:
                continue
            connection.execute(
                "UPDATE subscriptions SET name=?, amount_usd=?, cadence=? WHERE id=?",
                (pattern["name"], pattern["amount_usd"], pattern["cadence"], row["id"]),
            )
            migrated += 1
    return migrated


def migrate_legacy_zai_seed(connection) -> int:
    candidates = connection.execute(
        "SELECT id, amount_usd FROM subscriptions "
        "WHERE tool_key=? AND name=? AND cadence=? AND start_date=?",
        (
            LEGACY_ZAI_SEED_TOOL_KEY,
            LEGACY_ZAI_SEED_NAME,
            LEGACY_ZAI_LEGACY_CADENCE,
            LEGACY_ZAI_SEED_START_DATE,
        ),
    ).fetchall()
    migrated = 0
    for row in candidates:
        amount = float(row["amount_usd"])
        if abs(amount - LEGACY_ZAI_LEGACY_AMOUNT_USD) > LEGACY_ZAI_AMOUNT_TOLERANCE:
            continue
        connection.execute(
            "UPDATE subscriptions SET amount_usd=?, cadence=? WHERE id=?",
            (LEGACY_ZAI_PLAN_AMOUNT_USD, LEGACY_ZAI_TARGET_CADENCE, row["id"]),
        )
        migrated += 1
    return migrated


def seed_subscriptions(connection, *, today: str | None = None) -> int:
    """Stamp the seed version so initialize stays idempotent; insert no catalog rows."""
    stamped = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (SUBSCRIPTION_SEED_VERSION_KEY,)
    ).fetchone()
    if stamped is not None:
        return 0
    start_date = today or datetime.now(UTC).date().isoformat()
    inserted = 0
    for seed in SUBSCRIPTION_SEEDS:
        present = connection.execute(
            "SELECT 1 FROM subscriptions WHERE tool_key=?", (seed["tool_key"],)
        ).fetchone()
        if present is not None:
            continue
        connection.execute(
            "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date,end_date) "
            "VALUES(?,?,?,?,?,NULL)",
            (seed["tool_key"], seed["name"], seed["amount_usd"], seed["cadence"], start_date),
        )
        inserted += 1
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SUBSCRIPTION_SEED_VERSION_KEY, SUBSCRIPTION_SEED_VERSION),
    )
    return inserted


def materialize_subscription_days(connection, *, start: date, end: date) -> int:
    written = 0
    subscriptions = connection.execute("SELECT * FROM subscriptions").fetchall()
    day = start
    while day <= end:
        for subscription in subscriptions:
            active_start = date.fromisoformat(subscription["start_date"])
            active_end = date.fromisoformat(subscription["end_date"]) if subscription["end_date"] else None
            if day < active_start or (active_end and day > active_end):
                continue
            raw_id = f"subscription:{subscription['id']}:{day.isoformat()}"
            existed = connection.execute(
                "SELECT 1 FROM subscription_daily_costs WHERE raw_id=?", (raw_id,)
            ).fetchone() is not None
            connection.execute(
                """
                INSERT INTO subscription_daily_costs(subscription_id,tool_key,date,cost_usd,raw_id)
                VALUES(?,?,?,?,?)
                ON CONFLICT(raw_id) DO UPDATE SET
                    tool_key=excluded.tool_key,
                    cost_usd=excluded.cost_usd
                """,
                (
                    subscription["id"],
                    subscription["tool_key"],
                    day.isoformat(),
                    float(daily_cost(subscription["amount_usd"], subscription["cadence"], day)),
                    raw_id,
                ),
            )
            if not existed:
                written += 1
        day += timedelta(days=1)
    return written


def add_subscription(
    connection,
    *,
    tool_key: str,
    name: str,
    amount_usd: float,
    cadence: str,
    start_date: str,
    end_date: str | None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO subscriptions(tool_key,name,amount_usd,cadence,start_date,end_date) VALUES(?,?,?,?,?,?)",
        (tool_key, name, amount_usd, _require_cadence(cadence), start_date, end_date),
    )
    return int(cursor.lastrowid)


def update_subscription(connection, subscription_id: int, **fields) -> bool:
    unknown = set(fields) - set(EDITABLE_SUBSCRIPTION_FIELDS)
    if unknown:
        raise TypeError(f"unsupported subscription fields: {sorted(unknown)}")
    if "cadence" in fields:
        _require_cadence(fields["cadence"])
    present = connection.execute(
        "SELECT 1 FROM subscriptions WHERE id=?", (subscription_id,)
    ).fetchone()
    if present is None:
        return False
    if not fields:
        return True
    assignments = ",".join(f"{column}=?" for column in fields)
    connection.execute(
        f"UPDATE subscriptions SET {assignments} WHERE id=?",
        (*fields.values(), subscription_id),
    )
    return True


def list_subscriptions(connection):
    return connection.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
