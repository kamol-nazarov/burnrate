"""Small transactional helpers for parser upgrades, not a new event store."""
import json
from dataclasses import replace
from decimal import Decimal

COUNTS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "unclassified_tokens")


def read_meta(connection, key, default):
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write_meta(connection, key, value):
    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, sort_keys=True)))


def delete_event(connection, raw_id):
    # Call only for an exact proven overlap, inside the ingest transaction.
    for table in ("usage_events", "unpriced_usage_events", "coverage_gap_events", "pricing_gap_events"):
        connection.execute(f"DELETE FROM {table} WHERE raw_id=?", (raw_id,))


def coarse_remainder(coarse, granular, previous):
    """Consume a coarse observation once per granular identity.

    An authoritative correction of an already allocated message does not
    resurrect coarse tokens. Uncovered components and charge remainder stay
    on the original coarse ID and observation time.
    """
    remaining = {name: getattr(coarse, name) for name in COUNTS}
    cost = None if coarse.cost_usd is None else Decimal(str(coarse.cost_usd))
    allocations = dict(previous)
    for message in granular:
        row = message.row
        started = message.started_at or row.occurred_at
        if started > coarse.occurred_at:
            continue
        # Already allocated identities cannot consume a second coarse share
        # after an authoritative revision; the residual must not grow back.
        known = allocations.get(row.raw_id, {})
        # This is an allocation high-water mark, never the accepted event's
        # value: a later authoritative increase can cover more coarse work,
        # while a decrease must not resurrect already replaced coarse work.
        budget = {name: max(known.get("basis", {}).get(name, 0), getattr(row, name)) for name in COUNTS}
        used = known.get("used", {name: 0 for name in COUNTS})
        available = {name: max(0, budget[name] - used[name]) for name in COUNTS}
        before = dict(remaining)
        # Input includes cache reads, so allocate fresh and cached separately.
        fresh = max(0, remaining["input_tokens"] - remaining["cached_input_tokens"])
        fresh -= min(fresh, max(0, available["input_tokens"] - available["cached_input_tokens"]))
        remaining["cached_input_tokens"] -= min(remaining["cached_input_tokens"], available["cached_input_tokens"])
        remaining["input_tokens"] = fresh + remaining["cached_input_tokens"]
        for name in ("cache_write_tokens", "output_tokens", "unclassified_tokens"):
            remaining[name] -= min(remaining[name], available[name])
        cost_used = Decimal(str(known.get("costUsed", "0")))
        cost_basis = known.get("costBasis", None if row.cost_usd is None else str(row.cost_usd))
        if row.cost_usd is not None:
            cost_basis = str(max(Decimal(cost_basis or "0"), Decimal(str(row.cost_usd))))
        if cost is not None and cost_basis is not None:
            take = min(cost, max(Decimal(0), Decimal(cost_basis) - cost_used))
            cost -= take
            cost_used += take
        allocations[row.raw_id] = {"basis": budget, "used": {name: used[name] + before[name] - remaining[name] for name in COUNTS}, "costBasis": cost_basis, "costUsed": str(cost_used)}
    result = replace(coarse, **remaining, cost_usd=None if cost is None else float(cost),
                     cache_write_1h_tokens=min(coarse.cache_write_1h_tokens, remaining["cache_write_tokens"]),
                     reasoning_tokens=coarse.reasoning_tokens if remaining["output_tokens"] == coarse.output_tokens else None)
    return result, allocations
