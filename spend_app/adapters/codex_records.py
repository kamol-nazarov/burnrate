"""Codex rollout reducer. Schema/reset/fork evidence: pinned Tokscale 4.15.1.

Known CLI originator codex_cli_rs also appears in OpenAI codex state/extract.rs
at 6924ce636b2186948f2332ac9460b5871b50aa2f. See NOTICE.
"""
import re
from dataclasses import dataclass, replace

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.event_identity import Observation
from spend_app.adapters.opencode_schema import count
from spend_app.source_health import quarantine
from spend_app.timeutil import parse_utc

PARSER_VERSION = "codex-rollout-3"
ORIGINATORS = frozenset({"Codex Desktop", "codex_cli_rs", "codex_exec"})
KINDS = frozenset({"cli", "exec", "vscode"})


def known_source(payload):
    origin = payload.get("originator")
    source = payload.get("source")
    if origin:
        return origin in ORIGINATORS
    if isinstance(source, str):
        return source in KINDS
    spawn = source.get("subagent", {}).get("thread_spawn") if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else None
    return isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str)


def parent_id(payload):
    if isinstance(payload.get("forked_from_id"), str):
        return payload["forked_from_id"]
    source = payload.get("source")
    agent = source.get("subagent") if isinstance(source, dict) else None
    spawn = agent.get("thread_spawn") if isinstance(agent, dict) else None
    return spawn.get("parent_thread_id") if isinstance(spawn, dict) else None


def uuid_millis(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-7[0-9a-fA-F]{3}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return None
    return int(value.replace("-", "")[:12], 16)


def model_from(payload):
    info = payload.get("model_info")
    for value in (payload.get("model"), payload.get("model_name"), info.get("slug") if isinstance(info, dict) else None):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def usage_vector(raw):
    if not isinstance(raw, dict):
        return None
    inp, out = count(raw.get("input_tokens")), count(raw.get("output_tokens"))
    cache = count(raw.get("cached_input_tokens", raw.get("cache_read_input_tokens")))
    write = count(raw.get("cache_write_input_tokens", 0))
    total = count(raw.get("total_tokens"))
    if inp is None and out is None and total is None:
        return None
    if inp is not None and cache is not None and cache > inp:
        return None
    complete = all(value is not None for value in (inp, out, cache, write))
    values = (inp or 0, cache or 0, write or 0, out or 0)
    measured = total if total is not None else (values[0] + values[2] + values[3])
    if measured < values[0] + values[2] + values[3]:
        return None
    if measured != values[0] + values[2] + values[3]:
        complete = False
    return {"values": values, "known": tuple(value is not None for value in (inp, cache, write, out)), "total": measured, "complete": complete, "reasoning": count(raw.get("reasoning_output_tokens"))}


def reduce_records(records, health, fallback_session):
    session, observations, aliases = {}, {}, {}
    replay_session = None
    replaying = False
    current_model = None
    legacy_model = None
    legacy_session = None
    turn = None
    own_turns = set()
    previous = None
    accounted = (0, 0, 0, 0)
    accounted_total = 0
    latest_time = None
    epoch = "initial"
    legacy_index = 0
    at_timestamp = {}
    previous_key = None
    for outer in records:
        payload = outer.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = outer.get("type")
        when = parse_utc(str(outer.get("timestamp") or payload.get("timestamp") or ""))
        if kind == "session_meta":
            sid = payload.get("id") or payload.get("session_id") or fallback_session
            if not isinstance(sid, str) or not sid:
                health.note(quarantine("codex_session_identity_missing"))
                continue
            if not session:
                session = {"id": sid, "started_at": when, "project": None, "originator": payload.get("originator"), "supported_origin": known_source(payload), "forked_from_id": parent_id(payload), "thread_source": payload.get("thread_source")}
                cwd = payload.get("cwd")
                if isinstance(cwd, str):
                    from pathlib import Path
                    session["project"] = Path(cwd).name
                replaying = bool(session["forked_from_id"])
                current_model = model_from(payload)
            replay_session = sid
            if when is not None:
                legacy_session = sid
            continue
        if not session or not session["supported_origin"]:
            continue
        event = payload.get("type")
        if kind == "event_msg" and event == "task_started":
            tid = payload.get("turn_id")
            root_ms = uuid_millis(session["id"])
            turn_ms = uuid_millis(tid)
            start = payload.get("started_at")
            valid_start = isinstance(start, (int, float)) and session["started_at"] and start >= session["started_at"].timestamp()
            if not replaying or (root_ms is not None and turn_ms is not None and turn_ms >= root_ms) or valid_start:
                own_turns.add(tid)
            continue
        if kind == "turn_context":
            if payload.get("model"):
                legacy_model = str(payload["model"])
            turn = payload.get("turn_id") or turn
            if replaying:
                own = turn in own_turns
                root_ms, turn_ms = uuid_millis(session["id"]), uuid_millis(turn)
                if session.get("thread_source") == "user" and root_ms is not None and turn_ms is not None and turn_ms > root_ms:
                    own = True
                if own:
                    replaying = False
                    replay_session = session["id"]
                    current_model = None
            current_model = model_from(payload) or current_model
            continue
        if kind != "event_msg" or event != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict) or when is None:
            health.note(quarantine("codex_token_metadata_invalid"))
            continue
        current_model = model_from(payload) or model_from(info) or current_model
        last = usage_vector(info.get("last_token_usage"))
        cumulative = usage_vector(info.get("total_token_usage"))
        request = payload.get("request_id") or info.get("request_id")
        request = request if isinstance(request, str) and request else None
        legacy = None
        from spend_app.adapters.local_common import number as legacy_number
        old_last = info.get("last_token_usage")
        if isinstance(old_last, dict) and legacy_model and legacy_session and legacy_number(old_last.get("cached_input_tokens")) <= legacy_number(old_last.get("input_tokens")):
            # Preserve the index progression of the 0.3.0 parser, including
            # repeated snapshots that the new reducer no longer emits.
            legacy = f"codex-local:{legacy_session}:{outer.get('timestamp')}:{legacy_index}"
            legacy_index += 1
        if replaying:
            previous = cumulative or previous
            session["inherited_skipped"] = session.get("inherited_skipped", 0) + 1
            continue
        if latest_time and when < latest_time:
            health.note(quarantine("codex_stale_snapshot"))
            if legacy and previous_key:
                aliases.setdefault(previous_key, []).append(legacy)
            continue
        if not last and not cumulative:
            health.note(quarantine("codex_usage_components_unavailable"))
            continue
        model = current_model or "unknown"
        if model == "unknown":
            health.note(quarantine("codex_historical_model_unavailable"))
        selected = last
        form = "last"
        if cumulative:
            base = previous or {"values": accounted, "known": (True, True, True, True), "total": accounted_total, "complete": True, "reasoning": 0}
            delta_total = cumulative["total"] - base["total"]
            if delta_total < 0:
                # An evidenced hard reset starts with last == the new totals.
                # Otherwise retain the established counter, never append a guess.
                if last and last["complete"] and cumulative["complete"] and last["values"] == cumulative["values"] and last["total"] == cumulative["total"]:
                    epoch = stable_id(session["id"], turn, outer.get("timestamp"), "reset")
                    base = {"values": (0, 0, 0, 0), "known": (True, True, True, True), "total": 0, "complete": True, "reasoning": 0}
                    delta_total = cumulative["total"]
                else:
                    health.note(quarantine("codex_counter_regression_ignored"))
                    if legacy and previous_key:
                        aliases.setdefault(previous_key, []).append(legacy)
                    continue
            key = stable_id("codex-counter", session["id"], epoch, cumulative["total"])
            if delta_total == 0:
                if legacy and previous_key:
                    aliases.setdefault(previous_key, []).append(legacy)
                # A single coarse total can acquire its component breakdown.
                if key in observations and not observations[key].row.telemetry_complete and cumulative["complete"] and accounted_total == cumulative["total"] and len(observations) == 1:
                    row = observations[key].row
                    values = cumulative["values"]
                    observations[key] = replace(observations[key], row=replace(row, input_tokens=values[0], cached_input_tokens=values[1], cache_write_tokens=values[2], output_tokens=values[3], unclassified_tokens=0, telemetry_complete=True))
                previous = cumulative
                continue
            values = tuple(value - prior for value, prior in zip(cumulative["values"], base["values"]))
            complete = cumulative["complete"] and base["complete"] and min(values) >= 0
            if complete:
                reasoning = None if cumulative["reasoning"] is None or base["reasoning"] is None or cumulative["reasoning"] < base["reasoning"] else cumulative["reasoning"]-base["reasoning"]
                selected = {"values": values, "total": delta_total, "complete": True, "reasoning": reasoning}
            elif last and last["total"] == delta_total:
                selected = last
            else:
                known = cumulative["known"]
                prior_known = base.get("known", (True, True, True, True))
                partial = tuple(value if known[i] and prior_known[i] and value >= 0 else 0 for i, value in enumerate(values))
                selected = {"values": partial, "total": delta_total, "complete": False, "reasoning": None}
            previous = cumulative
            form = "cumulative_delta"
        else:
            stamp = when.isoformat()
            index = at_timestamp.get((turn, stamp), 0)
            at_timestamp[(turn, stamp)] = index + 1
            key = stable_id("codex-request", session["id"], request or turn, None if request else stamp, None if request else index)
        counter_alias = None
        if request and form == "cumulative_delta" and last:
            if selected["total"] == last["total"] and selected["values"] == last["values"]:
                # An explicit request plus its matching per-request counter
                # delta proves the representation transition, not amounts alone.
                counter_alias = key
                key = stable_id("codex-request", session["id"], request, None, None)
                aliases.setdefault(key, []).append(counter_alias)
            elif base["total"] == 0:
                health.note(quarantine("codex_request_counter_interval_ambiguous"))
                selected, form = last, "last"
                key = stable_id("codex-request", session["id"], request, None, None)
        values = selected["values"]
        if values[1] > values[0]:
            values = (values[0], 0, values[2], values[3])
            selected = {**selected, "complete": False}
        unknown = max(0, selected["total"]-values[0]-values[2]-values[3]) if not selected["complete"] else 0
        row = UsageRow(source="codex_local", tool_key="codex", model_key=model, occurred_at=when, session_id=session["id"], project=session["project"], input_tokens=values[0], cached_input_tokens=values[1], cache_write_tokens=values[2], cache_write_1h_tokens=0, output_tokens=values[3], reasoning_tokens=selected["reasoning"], cost_usd=None, raw_id=key, telemetry_complete=selected["complete"], unclassified_tokens=unknown)
        components = (("counter_scope", stable_id(session["id"], epoch)), ("counter_start", base["total"]), ("counter_end", cumulative["total"]), ("counter_reset", epoch != "initial")) if form == "cumulative_delta" else ()
        if counter_alias:
            components += (("request_equivalent_counter", counter_alias),)
        earlier = observations.get(key)
        old_values = (earlier.row.input_tokens, earlier.row.cached_input_tokens, earlier.row.cache_write_tokens, earlier.row.output_tokens) if earlier else (0, 0, 0, 0)
        old_total = earlier.row.input_tokens + earlier.row.cache_write_tokens + earlier.row.output_tokens + earlier.row.unclassified_tokens if earlier else 0
        if earlier and old_values == values and old_total == selected["total"]:
            components = tuple({**dict(earlier.components), **dict(components)}.items())
        observations[key] = Observation(row, (), when.timestamp(), form, components=components)
        if legacy:
            aliases.setdefault(key, []).append(legacy)
        accounted = tuple(a+b-old for a,b,old in zip(accounted, values, old_values))
        accounted_total += selected["total"] - old_total
        latest_time = when
        previous_key = key
        session["started_at"] = session["started_at"] or when
        health.note_ok()
    return session, [replace(value, aliases=tuple(aliases.get(key, ()))) for key, value in observations.items()]
