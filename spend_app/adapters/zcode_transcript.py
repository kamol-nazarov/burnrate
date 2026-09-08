"""ZCode transcript metadata. Text/content never estimates usage."""
from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_iso_time, parse_millis
from spend_app.adapters.opencode_schema import count


def reduce_records(records):
    rows, issues, positions = [], [], {}
    for record in records:
        if not isinstance(record, dict) or record.get("role") != "assistant":
            continue
        usage = record.get("usage") or record.get("token_usage")
        if not isinstance(usage, dict):
            continue
        def get(*keys):
            values = {count(usage[key]) for key in keys if key in usage}
            values.discard(None)
            return next(iter(values)) if len(values) == 1 else None
        inp = get("input", "input_tokens", "prompt_tokens", "inputTokens")
        out = get("output", "output_tokens", "completion_tokens", "outputTokens")
        cached = get("cache_read", "input_cache_read", "cache_read_tokens", "cacheReadTokens")
        writes = get("cache_write", "input_cache_creation", "cache_write_tokens", "cacheCreationTokens")
        reason = get("reasoning", "reasoningTokens")
        total = get("total", "totalTokens", "total_tokens")
        if total is None and any(key in usage for key in ("total", "totalTokens", "total_tokens")):
            issues.append("zcode_transcript_reported_total_invalid_or_conflicting")
            continue
        sid = record.get("sessionId")
        timestamp = record.get("timestamp")
        stamp = parse_iso_time(timestamp) if isinstance(timestamp, str) else parse_millis(timestamp)
        if not sid or not stamp:
            issues.append("zcode_transcript_identity_or_time_unavailable")
            continue
        event_id = record.get("usageId") or record.get("id")
        key = (sid, stamp.isoformat())
        ordinal = positions.get(key, 0)
        positions[key] = ordinal + 1
        raw_id = stable_id("zcode-local", record["usageId"]) if record.get("usageId") else stable_id("zcode-transcript", sid, event_id or (stamp.isoformat(), ordinal))
        model = record.get("model") or "unknown"
        complete = all(v is not None for v in (inp, out, cached, writes))
        component_keys = ("input", "input_tokens", "prompt_tokens", "inputTokens", "output", "output_tokens", "completion_tokens", "outputTokens",
                          "cache_read", "input_cache_read", "cache_read_tokens", "cacheReadTokens", "cache_write", "input_cache_creation", "cache_write_tokens", "cacheCreationTokens", "reasoning", "reasoningTokens")
        malformed = any(key in usage and count(usage[key]) is None for key in component_keys)
        additive = sum(v or 0 for v in (inp, out, cached, writes, reason))
        contradictory = total is not None and (not complete or malformed or total != additive)
        if contradictory or inp is None or out is None:
            if total is None:
                issues.append("zcode_transcript_usage_unavailable")
                continue
            if any(key in usage for key in component_keys):
                issues.append("zcode_transcript_component_attribution_unproven")
            inp, out, cached, writes, reason, complete = 0, 0, 0, 0, None, False
            unclassified = total
        else:
            unclassified = 0
            cached, writes = cached or 0, writes or 0
            # Accept the observed disjoint convention only when it does not
            # contradict a reported total. DB inclusive rules are not inferred
            # merely because a transcript uses similarly named aliases.
            inp += cached
            out += reason or 0
            if malformed:
                complete = False
                issues.append("zcode_transcript_component_attribution_unproven")
        rows.append(UsageRow("zcode_local", "zcode", "zcode:" + str(model).lower(), stamp, sid, None,
                             inp, cached, writes, 0, out, reason, None, raw_id, unclassified, complete))
    return rows, issues


def reconcile(connection, observations, issues):
    from spend_app.adapters.event_identity import reconcile as exact_reconcile
    accepted = []
    for sid in {o.row.session_id for o in observations}:
        ids = set()
        for table in ("usage_events", "unpriced_usage_events"):
            ids.update(row["raw_id"] for row in connection.execute(f"SELECT * FROM {table} WHERE source=? AND session_id=?", ("zcode_local", sid)))
        families = {"transcript" if raw.startswith("zcode-transcript:") else "database" for raw in ids}
        for observation in observations:
            row = observation.row
            if row.session_id != sid:
                continue
            family = "transcript" if row.raw_id.startswith("zcode-transcript:") else "database"
            if families and family not in families and row.raw_id not in ids:
                issues.append("zcode_cross_format_identity_unproven")
                continue
            accepted.append(observation)
            families.add(family)
    return exact_reconcile(connection, accepted, issues)
