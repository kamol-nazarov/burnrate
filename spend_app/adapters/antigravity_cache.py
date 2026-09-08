"""User-selected Tokscale Antigravity sync artifacts; never invokes a producer."""
import json
from pathlib import Path

from spend_app.adapters.common import UsageRow, stable_id, persist_rows
from spend_app.adapters.local_common import parse_millis
from spend_app.adapters.opencode_schema import count
from spend_app.connection_paths import source_files, confined


def reduce_records(records):
    models, rows, issues = {}, {}, []
    for record in records:
        if not isinstance(record, dict):
            issues.append("invalid_antigravity_cache_metadata")
            continue
        sid = record.get("sessionId")
        if record.get("type") == "session_meta" and sid and record.get("modelId"):
            models[sid] = record["modelId"]
        if record.get("type") != "usage":
            continue
        stamp = parse_millis(record.get("timestamp"))
        response = record.get("responseId")
        if not sid or not stamp or not isinstance(response, str) or not response:
            # The producer can emit retries without response IDs. Neither a
            # position nor matching counts proves their identity against RPC.
            issues.append("antigravity_response_identity_or_time_unavailable")
            continue
        values = [count(record.get(key)) for key in ("input", "output", "cacheRead", "cacheWrite")]
        if any(value is None for value in values):
            issues.append("invalid_antigravity_token_metadata")
            continue
        inp, out, cached, writes = values
        reason = count(record.get("reasoning"))
        if reason is not None and reason > out:
            issues.append("invalid_antigravity_reasoning_detail")
            reason = None
        from spend_app.adapters.antigravity_local import canonical_model
        raw_id = stable_id("antigravity-local", response)
        row = UsageRow("antigravity_local", "antigravity", canonical_model(record.get("modelId") or models.get(sid)),
                       stamp, sid, None, inp + cached, cached, writes, 0, out, reason, None, raw_id)
        previous = rows.get(raw_id)
        if previous and previous != row:
            issues.append("conflicting_antigravity_response_copies")
            continue
        rows[raw_id] = row
    return list(rows.values()), issues


def files(root, budget=100000):
    from spend_app.connection_paths import source_files
    root = Path(root)
    return source_files(root, ("sessions/*.jsonl",) if root.name == "antigravity-cache" else ("*.jsonl",), budget)


def read_file(path, *, limit=None):
    records, issues = [], []
    with confined(path).open("rb") as stream:
        index, remaining = 0, 131072
        while limit is None or index < limit:
            size = 131073 if limit is None else min(131073, remaining)
            if size <= 0:
                break
            line = stream.readline(size)
            if not line:
                break
            index += 1
            remaining -= len(line)
            if len(line) > 131072 or not line.endswith(b"\n") and limit is not None and remaining <= 0:
                issues.append("oversized_antigravity_metadata")
                break
            try:
                records.append(json.loads(line))
            except ValueError:
                issues.append("invalid_antigravity_cache_metadata")
    rows, parsed_issues = reduce_records(records)
    return rows, issues + parsed_issues


def ingest(*, database_path, pricing, import_path):
    rows, issues, found = [], [], 0
    for file in files(import_path):
        found += 1
        try:
            parsed, errors = read_file(file)
            rows.extend(parsed)
            issues.extend(errors)
        except OSError:
            issues.append("antigravity_cache_unreadable")
    if not found:
        raise ValueError("Antigravity cache is not populated. Its separate producer must create supported history first.")
    from spend_app.adapters.event_identity import Observation, reconcile
    observations = [Observation(row, (), row.occurred_at.timestamp(), "antigravity-cache") for row in rows]
    result = persist_rows(database_path=database_path, pricing=pricing, source="antigravity_local", usage_rows=rows,
                          prepare=lambda connection, _: reconcile(connection, observations, issues), issues=issues)
    return {**result, "files": found, "issues": sorted(set(issues))}
