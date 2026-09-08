"""Selected Cursor account export metadata; no cache producer or credentials."""
from dataclasses import replace

from spend_app.adapters.common import stable_id
from spend_app.adapters.cursor_admin import parse_events
from spend_app.adapters.opencode_schema import count


def parse_export(payload, scope_hint="unknown", *, observations=False):
    if not isinstance(payload, dict) or not isinstance(payload.get("usageEventsDisplay"), list):
        raise ValueError("Expected a supported Cursor usageEventsDisplay export.")
    rows, issues, positions = [], [], {}
    scope = payload.get("accountId") or payload.get("userId") or scope_hint
    if scope == "unknown" or scope == "usage":
        issues.append("cursor_export_account_scope_unavailable")
    for event in payload["usageEventsDisplay"]:
        if not isinstance(event, dict):
            issues.append("invalid_cursor_export_event")
            continue
        usage = event.get("tokenUsage")
        if not isinstance(usage, dict):
            continue
        normalized = {}
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
            value = usage.get(key)
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            normalized[key] = count(value)
        if normalized["inputTokens"] is None or normalized["outputTokens"] is None:
            issues.append("invalid_cursor_export_tokens")
            continue
        complete = all(value is not None for value in normalized.values())
        clean = {**event, "tokenUsage": {key: value or 0 for key, value in normalized.items()}}
        parsed = parse_events({"usageEvents": [clean]})
        if not parsed:
            issues.append("invalid_cursor_export_time")
            continue
        row = parsed[0]
        logical = (scope, event.get("conversationId"), row.occurred_at.isoformat())
        ordinal = positions.get(logical, 0)
        positions[logical] = ordinal + 1
        # An export without a request ID cannot assert a cross-feed identity.
        raw_id = stable_id("cursor-export", scope, event["id"]) if event.get("id") else stable_id("cursor-export-observation", *logical, ordinal)
        from spend_app.adapters.event_identity import Observation, cursor_identity
        shared = cursor_identity(event.get("id"), event.get("userEmail") or payload.get("userEmail") or event.get("userId") or payload.get("userId"), event.get("teamId") or payload.get("teamId"))
        reduced = replace(row, source="cursor_csv", raw_id=shared or raw_id, telemetry_complete=complete)
        aliases = (f"cursor-admin:{event['id']}", f"cursor-csv:{event['id']}") if shared else ()
        observation = Observation(reduced, aliases, row.occurred_at.timestamp(), "cursor-export", components=(("cursor_shared", True),) if shared else ())
        rows.append(observation if observations else reduced)
    return rows, issues
