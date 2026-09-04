"""Grok Build (xAI ``grok`` CLI) local usage adapter.

Per-turn token usage is read from ``~/.grok/logs/unified.jsonl``: every
``shell.turn.inference_done`` line carries ``prompt_tokens``,
``cached_prompt_tokens``, ``completion_tokens`` and ``reasoning_tokens`` for
one model call, keyed by the session id (``sid``). The model comes from the
most recent ``model changed`` line for that session (subagents inherit the
last model seen) and the project from ``session created``. Session update
files under ``~/.grok/sessions`` carry only goal totals, so the log is the
only per-call source on disk.

The log is append-only but the CLI truncates it (the current file starts on
the day of the last rotation), so the reader keeps a byte offset and rereads
from the start whenever the file shrinks. Row ids are stable, so a reread
never double counts.

SuperGrok subscription usage has no metered price; rows carry no
``cost_usd`` and are valued at xAI's published API rates (derived).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import number, optional_number, parse_iso_time
from spend_app.pricing import PricingEngine


SOURCE = "grok_local"
TOOL_KEY = "grok"
MODEL_PREFIX = "supergrok:"
DEFAULT_MODEL = "grok-4.6"
TURN_MESSAGE = "shell.turn.inference_done"
MODEL_MESSAGE = "model changed"
SESSION_MESSAGE = "session created"

# Reader state per log path: byte offset already persisted plus the
# session -> model / project maps learned from lines before that offset.
_STATE: dict[str, dict] = {}


def reset_state() -> None:
    _STATE.clear()


def canonical_model(model: str | None) -> str:
    return MODEL_PREFIX + (str(model or DEFAULT_MODEL).strip().lower() or DEFAULT_MODEL)


def _fresh_state() -> dict:
    return {"offset": 0, "models": {}, "cwds": {}, "last_model": None}


def parse_log(path: Path, state: dict | None = None) -> tuple[list[UsageRow], dict]:
    """Return usage rows appended since ``state['offset']`` and the new state.

    The returned state is only valid once the rows have been persisted;
    callers keep the old state when persistence fails so the lines are read
    again next cycle.
    """
    state = dict(state or _fresh_state())
    state["models"] = dict(state.get("models") or {})
    state["cwds"] = dict(state.get("cwds") or {})
    rows: list[UsageRow] = []
    try:
        size = path.stat().st_size
    except OSError:
        return rows, state
    if size < int(state.get("offset") or 0):
        # Truncated or rotated: start over; stable ids make the reread safe.
        state = _fresh_state()
    with path.open("rb") as handle:
        handle.seek(int(state["offset"]))
        chunk = handle.read()
    # Only consume whole lines; a partially written last line waits.
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return rows, state
    consumed = chunk[: cut + 1]
    state["offset"] = int(state["offset"]) + len(consumed)
    for raw in consumed.split(b"\n"):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("msg")
        sid = record.get("sid")
        ctx = record.get("ctx") if isinstance(record.get("ctx"), dict) else {}
        if message == MODEL_MESSAGE:
            model = ctx.get("model")
            if isinstance(model, str) and model.strip():
                state["last_model"] = model.strip()
                if sid:
                    state["models"][str(sid)] = model.strip()
            continue
        if message == SESSION_MESSAGE:
            cwd = ctx.get("cwd")
            if sid and isinstance(cwd, str) and cwd.strip():
                state["cwds"][str(sid)] = cwd
            continue
        if message != TURN_MESSAGE or not sid:
            continue
        occurred_at = parse_iso_time(record.get("ts"))
        if occurred_at is None:
            continue
        prompt = number(ctx.get("prompt_tokens"))
        cached = min(number(ctx.get("cached_prompt_tokens")), prompt)
        completion = number(ctx.get("completion_tokens"))
        if prompt + completion <= 0:
            continue
        session_id = str(sid)
        model = state["models"].get(session_id) or state.get("last_model")
        if not model:
            continue
        cwd = state["cwds"].get(session_id)
        rows.append(
            UsageRow(
                source=SOURCE,
                tool_key=TOOL_KEY,
                model_key=canonical_model(model),
                occurred_at=occurred_at,
                session_id=session_id,
                project=Path(cwd).name if cwd else None,
                input_tokens=prompt,
                cached_input_tokens=cached,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=completion,
                reasoning_tokens=optional_number(ctx.get("reasoning_tokens")),
                cost_usd=None,
                raw_id=stable_id("grok-local", session_id, record.get("ts"), ctx.get("loop_index")),
            )
        )
    return rows, state


def coverage_start(path: Path) -> datetime | None:
    """Timestamp of the first record in the CLI log, or None without a log.

    Traycer launches the same ``grok`` CLI, which logs every call here, so the
    log is the authority for Grok usage from this instant on; the Traycer
    projection keeps only the history from before the log began.
    """
    try:
        with path.open("rb") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        record = json.loads(first.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return parse_iso_time(record.get("ts")) if isinstance(record, dict) else None


def ingest(*, database_path: Path, pricing: PricingEngine, log_path: Path) -> dict:
    key = str(log_path)
    files = int(log_path.is_file())
    previous = _STATE.get(key)
    usage_rows, next_state = parse_log(log_path, previous) if files else ([], previous or _fresh_state())
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    _STATE[key] = next_state
    return {**result, "files": files, "rows": len(usage_rows)}

