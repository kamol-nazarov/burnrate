"""Claude message/request revisions, without upstream max-token heuristics.

Identity/layout evidence: Tokscale claudecode.rs at a3209ff and Token Monitor
Claude paths.js at e07aeb4. MIT notices are retained in NOTICE.
"""
from dataclasses import replace
from pathlib import Path
from datetime import UTC, datetime

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.event_identity import Observation
from spend_app.adapters.opencode_schema import count
from spend_app.source_health import quarantine
from spend_app.timeutil import parse_utc

PARSER_VERSION = "claude-message-3"
FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def merge_observations(group, *, record=None, existing=None, issues=None):
    """Merge reported fields, retaining their own revision rather than file age."""
    issues = issues if issues is not None else []
    record = record or {}
    values = dict(record.get("components", {}))
    stored_rank = record.get("rank", [0, 0, 0])
    fallback_rank = (stored_rank[0], stored_rank[2] if len(stored_rank) > 2 else 0)
    ranks = {k: tuple(v) for k, v in record.get("field_ranks", {}).items()}
    conflicts = {k: tuple(v) for k, v in record.get("field_conflicts", {}).items()}
    if existing and existing.get("telemetry_complete", True):
        seed = dict(input_tokens=existing["input_tokens"]-existing["cached_input_tokens"],
                    cache_read_input_tokens=existing["cached_input_tokens"],
                    cache_creation_input_tokens=existing["cache_write_tokens"], output_tokens=existing["output_tokens"])
        # A legacy positive duration is evidence. A legacy default zero is not.
        if existing.get("cache_write_1h_tokens", 0) > 0:
            seed["ephemeral_1h_input_tokens"] = existing["cache_write_1h_tokens"]
        for name, value in seed.items():
            if name not in conflicts:
                values.setdefault(name, value)
    for name in values:
        ranks.setdefault(name, fallback_rank)
    for item in sorted(group, key=lambda o: (o.revision, o.authority)):
        item_ranks = dict(item.field_ranks)
        for name, conflict_rank in item.field_conflicts:
            rank = tuple(conflict_rank)
            if rank >= ranks.get(name, (-1, -1)):
                conflicts[name] = ranks[name] = rank
                values.pop(name, None)
        for name, value in item.components:
            rank = tuple(item_ranks.get(name, (item.revision, item.authority)))
            old_rank = ranks.get(name, (-1, -1))
            if rank < old_rank or name in conflicts and rank <= conflicts[name]:
                continue
            if rank == old_rank and name in values and values[name] != value:
                conflicts[name] = rank
                values.pop(name, None)
            else:
                values[name] = value
                conflicts.pop(name, None)
            ranks[name] = rank
    if conflicts:
        issues.append("claude_equal_authority_field_conflict")
    latest = max(group, key=lambda o: (o.revision, o.authority))
    row = latest.row
    write = values.get("cache_creation_input_tokens")
    five, hour = values.get("ephemeral_5m_input_tokens"), values.get("ephemeral_1h_input_tokens")
    if write is None and five is not None and hour is not None and "cache_creation_input_tokens" not in conflicts:
        write = five + hour
    complete = all(name in values for name in ("input_tokens", "cache_read_input_tokens", "output_tokens")) and write is not None and not conflicts
    invalid_duration = write is not None and (hour is not None and hour > write or five is not None and five > write or
        hour is not None and five is not None and hour + five != write)
    if invalid_duration:
        issues.append("claude_cache_duration_conflict")
        complete = False
        hour = None
    inp = values.get("input_tokens", 0) + values.get("cache_read_input_tokens", 0)
    output = values.get("output_tokens", 0)
    model = next((o.row.model_key for o in sorted(group, key=lambda o: (o.revision, o.authority), reverse=True) if o.row.model_key != "unknown"), existing.get("model_key", "unknown") if existing else "unknown")
    stamps = [o.row.occurred_at for o in group]
    if existing:
        from spend_app.adapters.local_common import parse_iso_time
        old_time = parse_iso_time(existing["occurred_at"])
        if old_time:
            stamps.append(old_time)
    row = replace(row, model_key=model, occurred_at=min(stamps), input_tokens=inp,
                  cached_input_tokens=values.get("cache_read_input_tokens", 0), cache_write_tokens=write or 0,
                  cache_write_1h_tokens=hour or 0, output_tokens=output, telemetry_complete=complete,
                  unclassified_tokens=max(0, values.get("total_tokens", 0)-inp-(write or 0)-output) if not complete else 0)
    aliases = tuple(dict.fromkeys(alias for o in group for alias in o.aliases))
    return replace(latest, row=row, aliases=aliases, revision=max(latest.revision, stored_rank[0]),
                   components=tuple(values.items()), field_ranks=tuple(ranks.items()), field_conflicts=tuple(conflicts.items()))


def reduce_records(records, health, fallback_session):
    observations, known, known_scope = {}, {}, {}
    session = {}
    for outer in records:
        message = outer.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict) or outer.get("type") not in {None, "assistant"}:
            continue
        if message.get("model") == "<synthetic>":
            continue
        message_id = message.get("id")
        request_id = outer.get("requestId")
        if not isinstance(message_id, str) or not message_id:
            message_id = None
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        legacy_message = message_id or outer.get("uuid")
        if not message_id and not request_id and not isinstance(legacy_message, str):
            health.note(quarantine("claude_request_identity_missing"))
            continue
        identity = (message_id, request_id or legacy_message)
        provider = outer.get("providerId") or outer.get("provider_id") or message.get("providerId") or message.get("provider_id")
        if not isinstance(provider, str) or not provider:
            provider = known_scope.get(identity, "native")
        known_scope[identity] = provider
        key = stable_id("claude-message", provider, message_id or "request", request_id or legacy_message)
        previous = observations.get(key)
        when = parse_utc(str(outer.get("timestamp") or ""))
        if when is None and previous:
            when = datetime.fromtimestamp(previous.revision, UTC)
        if when is None:
            health.note(quarantine("claude_usage_time_missing"))
            continue
        if previous and when.timestamp() < previous.revision:
            health.note(quarantine("claude_older_usage_revision"))
        usage = message["usage"]
        values = dict(known.get(key, {}))
        field_ranks = dict(previous.field_ranks) if previous else {}
        field_rank = (when.timestamp(), int(bool(message.get("stop_reason"))))
        for name in FIELDS:
            value = count(usage.get(name))
            if value is not None:
                values[name] = value
                field_ranks[name] = field_rank
        cache = usage.get("cache_creation")
        if isinstance(cache, dict):
            for name in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
                value = count(cache.get(name))
                if value is not None:
                    values[name] = value
                    field_ranks[name] = field_rank
        write = values.get("cache_creation_input_tokens")
        five, hour = values.get("ephemeral_5m_input_tokens"), values.get("ephemeral_1h_input_tokens")
        if write is None and five is not None and hour is not None:
            write = five + hour
            values["cache_creation_input_tokens"] = write
            field_ranks["cache_creation_input_tokens"] = field_rank
        complete = all(values.get(name) is not None for name in ("input_tokens", "cache_read_input_tokens", "output_tokens")) and write is not None
        if hour is not None and write is not None and hour > write:
            hour = None
            complete = False
            health.note(quarantine("claude_cache_duration_conflict"))
        if cache is not None and not isinstance(cache, dict):
            health.note(quarantine("claude_optional_cache_metadata_invalid"))
        details = usage.get("output_tokens_details")
        reasoning = count(details.get("thinking_tokens")) if isinstance(details, dict) else None
        if reasoning is None and previous:
            reasoning = previous.row.reasoning_tokens
        model = message.get("model")
        if not isinstance(model, str) or not model:
            model = previous.row.model_key if previous else "unknown"
        sid = outer.get("sessionId")
        sid = sid if isinstance(sid, str) and sid else fallback_session
        cwd = outer.get("cwd")
        project = Path(cwd).name if isinstance(cwd, str) else previous.row.project if previous else None
        aliases = list(previous.aliases if previous else ())
        if legacy_message:
            aliases.append(f"claude-local:{sid}:{legacy_message}")
        total = count(usage.get("total_tokens"))
        if total is not None:
            values["total_tokens"] = total
            field_ranks["total_tokens"] = field_rank
        inp = values.get("input_tokens", 0) + values.get("cache_read_input_tokens", 0)
        output = values.get("output_tokens", 0)
        unknown = max(0, (total or 0) - inp - (write or 0) - output) if not complete else 0
        row = UsageRow(source="claude_local", tool_key="claude-code", model_key=model,
                       occurred_at=min(when, previous.row.occurred_at) if previous else when, session_id=sid, project=project,
                       input_tokens=inp, cached_input_tokens=values.get("cache_read_input_tokens", 0),
                       cache_write_tokens=write or 0, cache_write_1h_tokens=hour or 0, output_tokens=output,
                       reasoning_tokens=reasoning, cost_usd=None, raw_id=key, unclassified_tokens=unknown, telemetry_complete=complete)
        incoming = Observation(row, tuple(dict.fromkeys(aliases)), when.timestamp(), "claude_message", int(bool(message.get("stop_reason"))), tuple(values.items()), tuple(field_ranks.items()))
        errors = []
        observations[key] = merge_observations(([previous] if previous else []) + [incoming], issues=errors)
        for error in errors:
            health.note(quarantine(error))
        known[key] = dict(observations[key].components)
        health.note_ok()
        if not session:
            session = {"id": sid, "project": project, "started_at": when, "model_key": model}
    return session, list(observations.values())
