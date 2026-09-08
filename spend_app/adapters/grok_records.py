"""Observed Grok unified-log metadata, with process-generation model scope."""
from pathlib import Path

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_iso_time
from spend_app.adapters.opencode_schema import count


def reduce_records(records, state=None):
    state = state or {}
    models = dict(state.get("models", {}))
    cwds = dict(state.get("cwds", {}))
    generations = dict(state.get("generations", {}))
    rows, issues = [], []
    for record in records:
        if not isinstance(record, dict):
            issues.append("malformed_grok_metadata")
            continue
        message = record.get("msg")
        ctx = record.get("ctx") if isinstance(record.get("ctx"), dict) else {}
        pid, sid = str(record.get("pid", "")), str(record.get("sid") or "")
        if message == "AuthManager::new":
            generations[pid] = str(record.get("ts"))
        generation = generations.get(pid, "unknown")
        scope = stable_id("grok-process-session", pid, generation, sid)
        if message in {"model changed", "model catalog: notifying clients", "backend_search:model switch"}:
            model = ctx.get("model") or ctx.get("new_model") or ctx.get("current_model_id")
            if sid and isinstance(model, str) and model.strip():
                models[scope] = model.strip()
        child = ctx.get("subagent_id")
        child_model = ctx.get("effective_model") or ctx.get("effective_model_raw")
        if child and isinstance(child_model, str):
            models[stable_id("grok-process-session", pid, generation, str(child))] = child_model
        if message == "session created" and sid and isinstance(ctx.get("cwd"), str):
            cwds[scope] = ctx["cwd"]
        if message != "shell.turn.inference_done" or not sid:
            continue
        stamp = parse_iso_time(record.get("ts"))
        values = [count(ctx.get(key)) for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")]
        if stamp is None or any(v is None for v in values) or values[2] > values[0]:
            issues.append("invalid_grok_usage_metadata")
            continue
        prompt, output, cached = values
        if prompt + output == 0:
            continue
        model = models.get(scope)
        if not model:
            issues.append("historical_model_unavailable")
        reasoning = count(ctx.get("reasoning_tokens"))
        if reasoning is not None and reasoning > output:
            reasoning = None
            issues.append("invalid_reasoning_detail")
        rows.append(UsageRow(
            source="grok_local", tool_key="grok", model_key="supergrok:" + (model or "unknown").lower(),
            occurred_at=stamp, session_id=sid, project=Path(cwds[scope]).name if scope in cwds else None,
            input_tokens=prompt, cached_input_tokens=cached, cache_write_tokens=0,
            cache_write_1h_tokens=0, output_tokens=output, reasoning_tokens=reasoning, cost_usd=None,
            raw_id=stable_id("grok-local", sid, record.get("ts"), ctx.get("loop_index"))))
    return rows, {"models": models, "cwds": cwds, "generations": generations}, issues
