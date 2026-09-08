"""Atomic, exact-ID compatibility for repaired event parsers."""
from dataclasses import dataclass, replace

from spend_app.adapters.common import stable_id
from spend_app.adapters.usage_repair import delete_event, read_meta, write_meta


@dataclass(frozen=True)
class Observation:
    row: object
    aliases: tuple
    revision: float
    format: str
    authority: int = 0
    components: tuple = ()
    field_ranks: tuple = ()
    field_conflicts: tuple = ()


def stored_event(connection, raw_id):
    for table in ("usage_events", "unpriced_usage_events"):
        row = connection.execute(f"SELECT * FROM {table} WHERE raw_id=?", (raw_id,)).fetchone()
        if row:
            return dict(row)
    return None


def reconcile(connection, observations, issues=None):
    issues = issues if issues is not None else []
    linked = []
    for observation in observations:
        raw_id = observation.row.raw_id
        if observation.row.source == "codex_local" and raw_id.startswith("codex-counter:"):
            owner = read_meta(connection, "usage.alias.v1:" + stable_id("codex_local", raw_id), None)
            if isinstance(owner, str) and owner.startswith("codex-request:"):
                observation = replace(observation, row=replace(observation.row, raw_id=owner),
                    aliases=observation.aliases + (raw_id,),
                    components=observation.components + (("request_equivalent_counter", raw_id),))
        linked.append(observation)
    observations = guard_counter_fragments(connection, linked, issues)
    grouped = {}
    claude_groups = {}
    for observation in observations:
        canonical = observation.row.raw_id
        if observation.row.source == "claude_local":
            claude_groups.setdefault(canonical, []).append(observation)
            continue
        previous = grouped.get(canonical)
        aliases = tuple(dict.fromkeys((previous.aliases if previous else ()) + observation.aliases))
        rank = (observation.revision, observation.row.telemetry_complete, observation.authority)
        old_rank = (previous.revision, previous.row.telemetry_complete, previous.authority) if previous else None
        if previous and rank == old_rank and replace(observation.row, project=None, session_id=None) != replace(previous.row, project=None, session_id=None):
            issues.append("conflicting_equal_revision_copies")
        if not previous or rank > old_rank or rank == old_rank and observation.row.source == "codex_local" and dict(observation.components).get("request_equivalent_counter"):
            grouped[canonical] = replace(observation, aliases=aliases)
        else:
            grouped[canonical] = replace(previous, aliases=aliases)
    from spend_app.adapters.claude_records import merge_observations
    grouped.update({key: merge_observations(group, issues=issues) for key, group in claude_groups.items()})
    rows = []
    for canonical, observation in grouped.items():
        source = observation.row.source
        shared_cursor = dict(observation.components).get("cursor_shared", False)
        namespace = "cursor-shared" if shared_cursor else source
        allowed_sources = {"cursor_admin", "cursor_csv"} if shared_cursor else {source}
        key = "usage.identity.v1:" + stable_id(namespace, canonical)
        record = read_meta(connection, key, None)
        if source == "claude_local":
            observation = merge_observations([observation], record=record,
                existing=stored_event(connection, record["raw_id"] if record else canonical), issues=issues)
        rank = [observation.revision, int(observation.row.telemetry_complete), observation.authority]
        if record and rank < record["rank"]:
            if shared_cursor:
                existing = stored_event(connection, record["raw_id"])
                if existing:
                    from spend_app.adapters.opencode_granular import row_from_record
                    rows.append(row_from_record(existing))
                    continue
            issues.append("older_usage_revision_ignored")
            continue
        target = record["raw_id"] if record else canonical
        found = []
        for alias in dict.fromkeys((canonical,) + observation.aliases):
            owner_key = "usage.alias.v1:" + stable_id(namespace, alias)
            owner = read_meta(connection, owner_key, None)
            if owner is not None and owner != canonical:
                proven_counter = source == "codex_local" and dict(observation.components).get("request_equivalent_counter") == alias
                if not (proven_counter and owner == alias):
                    if proven_counter:
                        issues.append("codex_counter_request_ownership_conflict")
                    continue
                old_identity = read_meta(connection, "usage.identity.v1:" + stable_id(source, owner), None)
                if old_identity:
                    old_target = old_identity["raw_id"]
                    old_event = stored_event(connection, old_target)
                    if old_event and old_event["source"] == source:
                        found.append(old_target)
            existing = stored_event(connection, alias)
            if existing and existing["source"] in allowed_sources:
                found.append(alias)
            write_meta(connection, owner_key, canonical)
        if not record and found:
            # Keep a stored 0.3.0 ID instead of minting another accepted row.
            target = found[0]
        for alias in found:
            if alias != target:
                delete_event(connection, alias)
        row = replace(observation.row, raw_id=target)
        existing = stored_event(connection, target)
        if shared_cursor and existing and existing["source"] == "cursor_admin" and source == "cursor_csv":
            from spend_app.adapters.opencode_granular import row_from_record
            row = row_from_record(existing)
        rows.append(row)
        components = dict(record.get("components", {})) if record else {}
        components.update(dict(observation.components))
        if source == "claude_local":
            components = dict(observation.components)
        write_meta(connection, key, {"raw_id": target, "rank": rank, **({"components": components} if components else {}),
            **({"field_ranks": dict(observation.field_ranks), "field_conflicts": dict(observation.field_conflicts)} if source == "claude_local" else {})})
    return rows


def cursor_identity(event_id, user=None, team=None):
    """Shared only with an actual request ID and explicit matching scope."""
    if not event_id or not (user or team):
        return None
    return stable_id("cursor-reported", str(team or ""), str(user or ""), str(event_id))


def guard_counter_fragments(connection, observations, issues):
    """Do not layer truncated cumulative prefixes over accepted intervals.

    Complete copied rollouts have identical adjacent intervals. When a fragment
    starts inside another accepted interval, counter identity proves overlap
    but not its historical per-model split. Preserve accepted history and make
    this limitation explicit instead of fabricating a granular allocation.
    """
    observations = list(observations)
    states, accepted = {}, []
    grouped = {}
    for observation in observations:
        scope = dict(observation.components).get("counter_scope")
        if scope:
            grouped.setdefault("codex.counter-ranges.v1:" + scope, []).append(observation)
    replacements, ignored = {}, set()
    for key, group in grouped.items():
        ranges = states.setdefault(key, read_meta(connection, key, {}))
        for item in group:
            details = dict(item.components)
            alias = details.get("request_equivalent_counter")
            if alias in ranges and ranges[alias] == [details["counter_start"], details["counter_end"]]:
                ranges[item.row.raw_id] = ranges.pop(alias)
        for old_id, span in list(ranges.items()):
            cursor, chain = span[0], []
            while cursor < span[1]:
                choices = {}
                for observation in group:
                    details = dict(observation.components)
                    interval = [details["counter_start"], details["counter_end"]]
                    if interval == span or interval[0] != cursor or interval[1] > span[1]:
                        continue
                    previous = choices.get(interval[1])
                    if previous is None or observation.revision > previous.revision:
                        choices[interval[1]] = observation
                if len(choices) != 1:
                    break
                cursor, observation = next(iter(choices.items()))
                chain.append(observation)
            if cursor != span[1] or len(chain) < 2:
                continue
            identity_key = "usage.identity.v1:" + stable_id("codex_local", old_id)
            record = read_meta(connection, identity_key, None)
            stored = stored_event(connection, record["raw_id"] if record else old_id)
            if stored is None:
                continue
            processed = lambda row: row.input_tokens + row.output_tokens + row.cache_write_tokens + row.unclassified_tokens
            if (sum(processed(item.row) for item in chain) != span[1] - span[0]
                    or any(chain[i].row.occurred_at > chain[i+1].row.occurred_at for i in range(len(chain)-1))
                    or stored.get("telemetry_complete", True) and not all(item.row.telemetry_complete for item in chain)):
                continue
            # The complete chain refines one proven counter interval. Its final
            # endpoint keeps the old canonical/0.3.0 ID; preceding endpoints
            # supply the uncovered pieces, never another baseline on top.
            del ranges[old_id]
            for item in chain:
                details = dict(item.components)
                ranges[item.row.raw_id] = [details["counter_start"], details["counter_end"]]
                rank = read_meta(connection, "usage.identity.v1:" + stable_id("codex_local", item.row.raw_id), None)
                replacements[id(item)] = replace(item, revision=max(item.revision, rank["rank"][0] if rank else item.revision),
                                                 authority=details["counter_start"])
            for item in group:
                details = dict(item.components)
                if [details["counter_start"], details["counter_end"]] == span:
                    ignored.add(id(item))
    observations = [replacements.get(id(item), item) for item in observations if id(item) not in ignored]
    observations.sort(key=lambda item: (dict(item.components).get("counter_scope", ""), dict(item.components).get("counter_end", 0), -dict(item.components).get("counter_start", 0)))
    for observation in observations:
        components = dict(observation.components)
        scope = components.get("counter_scope")
        if not scope:
            accepted.append(observation)
            continue
        key = "codex.counter-ranges.v1:" + scope
        ranges = states[key]
        start, end = components["counter_start"], components["counter_end"]
        if start == 0 and not ranges and not components.get("counter_reset"):
            from spend_app.adapters.local_common import parse_iso_time
            earlier_legacy = False
            for table in ("usage_events", "unpriced_usage_events"):
                for old in connection.execute(f"SELECT * FROM {table} WHERE source=? AND session_id=?", ("codex_local", observation.row.session_id)):
                    when = parse_iso_time(old["occurred_at"])
                    if old["raw_id"].startswith("codex-local:") and when and when < observation.row.occurred_at:
                        earlier_legacy = True
            if earlier_legacy:
                issues.append("codex_legacy_fragment_requires_full_replay")
                continue
        interval = [start, end]
        overlap = any(max(start, known[0]) < min(end, known[1]) and interval != known for known in ranges.values())
        if overlap:
            issues.append("codex_overlapping_counter_fragment_requires_complete_history")
            continue
        ranges[observation.row.raw_id] = interval
        accepted.append(observation)
    for key, ranges in states.items():
        write_meta(connection, key, ranges)
    return accepted
