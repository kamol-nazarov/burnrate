"""Observed Grok session updates/signals, reduced without conversation content."""
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_millis
from spend_app.adapters.opencode_schema import count
from spend_app.adapters.usage_repair import read_meta, write_meta, delete_event
from spend_app.connection_paths import source_files, confined


def files(root, budget=100000):
    from spend_app.connection_paths import source_files
    root = Path(root)
    patterns = ("logs/unified.jsonl", "sessions/*/*/updates.jsonl", "sessions/*/*/signals.json") if root.name == ".grok" or (root / "logs").is_dir() else ("**/updates.jsonl", "**/signals.json", "unified.jsonl")
    return source_files(root, patterns, budget)


def reduce_updates(records):
    rows, issues, positions = [], [], {}
    for record in records:
        params = record.get("params", {}) if isinstance(record, dict) else {}
        update, meta = params.get("update", {}), params.get("_meta", {})
        if not isinstance(update, dict) or not isinstance(meta, dict):
            continue
        usage, sid = update.get("usage"), params.get("sessionId")
        if not isinstance(usage, dict):
            continue
        stamp = parse_millis(meta.get("agentTimestampMs"))
        if not sid or not stamp:
            issues.append("grok_native_identity_or_time_unavailable")
            continue
        def get(*keys):
            return next((count(usage[key]) for key in keys if key in usage), None)
        inp = get("inputTokens", "input_tokens", "promptTokens")
        out = get("outputTokens", "output_tokens", "completionTokens")
        cached = get("cachedReadTokens", "cacheReadTokens", "cache_read_input_tokens")
        writes = get("cachedWriteTokens", "cacheWriteTokens", "cacheCreationTokens", "cache_creation_input_tokens")
        reasoning = get("reasoningTokens", "thoughtTokens", "thinkingTokens")
        total = get("totalTokens", "total_tokens")
        complete = all(v is not None for v in (inp, out, cached, writes))
        unsplit = 0
        if inp is None or out is None:
            if total is None:
                issues.append("grok_native_usage_unavailable")
                continue
            unsplit, inp, out, cached, writes, reasoning, complete = total, 0, 0, 0, 0, None, False
        cached, writes = cached or 0, writes or 0
        if cached > inp or reasoning is not None and reasoning > out:
            issues.append("invalid_grok_native_components")
            continue
        models = usage.get("modelUsage")
        model = next(iter(models)) if isinstance(models, dict) and len(models) == 1 else "unknown"
        key = (sid, stamp.isoformat(), meta.get("eventId"))
        ordinal = positions.get(key, 0)
        positions[key] = ordinal + 1
        rows.append(UsageRow("grok_local", "grok", "supergrok:" + model.lower(), stamp, sid, None,
                             inp, cached, writes, 0, out, reasoning, None,
                             stable_id("grok-native-usage", *key, ordinal), unsplit, complete))
    return rows, issues


def signal_row(payload, sid, observed_at):
    if not isinstance(payload, dict):
        raise ValueError("invalid_grok_signals")
    before = count(payload.get("totalTokensBeforeCompaction", 0))
    total = count(payload.get("totalTokens"))
    context = count(payload.get("contextTokensUsed"))
    if before is None or total is None:
        raise ValueError("invalid_grok_signals")
    # Observed producer contract: total includes compaction when context is
    # present; otherwise total is the post-compaction segment.
    value = max(total, before + context) if context is not None else before + total
    return UsageRow("grok_local", "grok", "supergrok:unknown", observed_at, sid, None,
                    0, 0, 0, 0, 0, None, None, stable_id("grok-native-total", sid), value, False)


def read_source(root):
    from spend_app.adapters.grok_local import parse_log
    rows, issues, found = [], [], 0
    for file in files(root):
        found += 1
        try:
            if file.name == "unified.jsonl":
                parsed, state = parse_log(file)
                rows.extend(parsed)
                issues.extend(state.get("issues", []))
            elif file.name == "signals.json":
                with confined(file).open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
                rows.append(signal_row(payload, file.parent.name, datetime.fromtimestamp(file.stat().st_mtime, UTC)))
            else:
                records = []
                with confined(file).open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            issues.append("malformed_grok_native_metadata")
                parsed, errors = reduce_updates(records)
                rows.extend(parsed)
                issues.extend(errors)
        except (OSError, ValueError):
            issues.append("grok_native_source_unavailable")
    return rows, issues, found


def reconcile(connection, incoming, issues):
    from spend_app.adapters.opencode_granular import row_from_record
    # A Traycer chat ID is not a Grok session ID. A native cumulative signal
    # has no start boundary with which to prove its otherwise-uncovered share.
    # Disabling Traycer preserves that history; it must not make a new signal
    # safe to layer over it. Preserve existing measurements and report the gap.
    traycer_history = any(connection.execute(
        f"SELECT 1 FROM {table} WHERE source=? AND tool_key=? LIMIT 1", ("traycer_local", "grok")
    ).fetchone() for table in ("usage_events", "unpriced_usage_events"))
    if traycer_history and any(row.raw_id.startswith("grok-native-total:") for row in incoming):
        issues.append("grok_native_total_traycer_history_scope_unproven")
        incoming = [row for row in incoming if not row.raw_id.startswith("grok-native-total:")]
    output = []
    for sid in {row.session_id for row in incoming}:
        key = "grok.native.v1:" + stable_id(sid)
        state = read_meta(connection, key, {})
        existing = {}
        for table in ("usage_events", "unpriced_usage_events"):
            for record in connection.execute(f"SELECT * FROM {table} WHERE source=? AND session_id=?", ("grok_local", sid)):
                row = row_from_record(dict(record))
                existing[row.raw_id] = row
        rows = dict(existing)
        for row in incoming:
            if row.session_id == sid:
                rows[row.raw_id] = row
        unified = [row for row in rows.values() if row.raw_id.startswith("grok-local:")]
        if unified:
            start, end = min(row.occurred_at for row in unified), max(row.occurred_at for row in unified)
            for raw_id, row in list(rows.items()):
                if raw_id.startswith("grok-native-usage:") and start <= row.occurred_at <= end:
                    if raw_id in existing:
                        delete_event(connection, raw_id)
                    del rows[raw_id]
        coarse_id = stable_id("grok-native-total", sid)
        coarse = next((r for r in incoming if r.raw_id == coarse_id), None)
        if coarse and state and coarse.unclassified_tokens < state["total"]:
            issues.append("grok_signal_decrease_without_revision_evidence")
            coarse = None
        if coarse and (not state or coarse.unclassified_tokens > state["total"] and coarse.occurred_at.timestamp() >= state["observed"]):
            state = {"total": coarse.unclassified_tokens, "observed": coarse.occurred_at.timestamp()}
        if state:
            stamp = datetime.fromtimestamp(state["observed"], UTC)
            covered = sum(r.input_tokens + r.output_tokens + r.cache_write_tokens + r.unclassified_tokens
                          for r in rows.values() if r.raw_id != coarse_id and r.occurred_at <= stamp)
            remainder = max(0, state["total"] - covered)
            template = coarse or rows.get(coarse_id)
            if template and remainder:
                rows[coarse_id] = replace(template, unclassified_tokens=remainder, occurred_at=stamp)
            elif coarse_id in rows:
                if coarse_id in existing:
                    delete_event(connection, coarse_id)
                del rows[coarse_id]
            write_meta(connection, key, state)
        output.extend(rows.values())
    return output
