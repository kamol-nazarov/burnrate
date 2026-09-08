"""Shared OpenCode schema selection and metadata reduction.

Format evidence: junhoyeo/tokscale a3209ff, sessions/opencode_schema.rs
and opencode.rs (MIT, Junho Yeo). See NOTICE. No fingerprint-based dedup:
distinct message identities must survive equal timestamps/token counts.
"""
import json
import math
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_millis, positive_cost

PARSER_VERSION = "opencode-message-v1"
LEGACY_COLUMNS = frozenset(("id", "project_id", "directory", "path", "model", "cost", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write", "time_updated"))


class UnsupportedSchema(ValueError):
    pass


def recognize(columns):
    formats = []
    v2 = set(columns.get("session_message", ()))
    if {"id", "session_id", "data"} <= v2 and {"type", "role"} & v2:
        formats.append("v2")
    if {"id", "session_id", "data"} <= set(columns.get("message", ())):
        formats.append("v1")
    if formats:
        if LEGACY_COLUMNS <= set(columns.get("session", ())):
            formats.append("cumulative")
        return tuple(formats)
    if LEGACY_COLUMNS <= set(columns.get("session", ())):
        return ("cumulative",)
    raise UnsupportedSchema("Unsupported OpenCode database schema: incompatible message/session columns; usage was not read.")


def schema_columns(connection):
    return {name: {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
            for name in ("session_message", "message", "session")}


def count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


@dataclass(frozen=True)
class Message:
    row: UsageRow
    provider: str
    message_id: str
    revision: float
    format: str
    started_at: object = None


def parse_message(data, *, row_id=None, session_id=None, role=None, format="json", updated_at=None):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    selected_role = role if format == "v2" else data.get("role")
    if selected_role != "assistant":
        return None
    tokens = data.get("tokens")
    time = data.get("time")
    if not isinstance(tokens, dict) or not isinstance(time, dict):
        return None
    message_id = data.get("id") or row_id
    session = data.get("sessionID") or session_id
    if not isinstance(message_id, str) or not message_id or not isinstance(session, str) or not session:
        return None
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    model_id = data.get("modelID") or model.get("id") or "unknown"
    provider = data.get("providerID") or model.get("providerID") or "unknown"
    if not isinstance(model_id, str) or not isinstance(provider, str):
        return None
    if provider == "traycer-openrouter":
        return None
    when = parse_millis(time.get("completed") or time.get("created"))
    if when is None:
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    values = [count(tokens.get("input")), count(cache.get("read")), count(cache.get("write")), count(tokens.get("output"))]
    total = count(tokens.get("total"))
    if not any(value is not None for value in values) and total is None:
        return None
    fresh, read, write, output = (value or 0 for value in values)
    complete = all(value is not None for value in values)
    # OpenCode's output already includes reasoning; retain reasoning as detail.
    reasoning = count(tokens.get("reasoning"))
    unknown = max(0, (total or 0) - fresh - read - write - output) if not complete else 0
    path = data.get("path")
    project = Path(path["root"]).name if isinstance(path, dict) and isinstance(path.get("root"), str) else None
    row = UsageRow(source="opencode_local", tool_key="opencode", model_key="opencode:" + model_id.strip().lower(), occurred_at=when,
                   session_id=session, project=project, input_tokens=fresh + read, cached_input_tokens=read,
                   cache_write_tokens=write, cache_write_1h_tokens=0, output_tokens=output, reasoning_tokens=reasoning,
                   cost_usd=positive_cost(data.get("cost")), raw_id=stable_id("opencode-message", provider, message_id),
                   unclassified_tokens=unknown, telemetry_complete=complete)
    updated = updated_at or time.get("updated") or time.get("completed") or time.get("created")
    revision = float(updated) if isinstance(updated, (int, float)) and math.isfinite(updated) else when.timestamp() * 1000
    return Message(row, provider, message_id, revision, format, parse_millis(time.get("created")))


def merge_messages(messages):
    chosen = {}
    ambiguous = 0
    for message in messages:
        key = message.row.raw_id
        previous = chosen.get(key)
        rank = (message.revision, message.row.telemetry_complete, message.format == "v2")
        old_rank = (previous.revision, previous.row.telemetry_complete, previous.format == "v2") if previous else None
        if not previous or rank > old_rank:
            chosen[key] = message
        elif rank == old_rank and replace(message.row, project=None, session_id=None) != replace(previous.row, project=None, session_id=None):
            # Equal-authority conflicting copies cannot be resolved by max(tokens).
            ambiguous += 1
    return sorted(chosen.values(), key=lambda m: (m.row.occurred_at, m.row.raw_id)), ambiguous


def read_messages(connection, columns, limit=None):
    formats = recognize(columns)
    messages, malformed = [], 0
    for format in formats:
        if format == "cumulative":
            continue
        table = "session_message" if format == "v2" else "message"
        role = ('"type"' if "type" in columns[table] else '"role"') if format == "v2" else "NULL"
        # Project only usage metadata, never prompts, message parts or source text.
        projection = "CASE WHEN json_valid(data) THEN json_object(" + ",".join(
            "'" + field + "',json_extract(data,'$." + field + "')" for field in ("id", "sessionID", "role", "modelID", "providerID", "model", "tokens", "time", "cost")) + ") ELSE NULL END"
        updated = "time_updated" if "time_updated" in columns[table] else "NULL"
        sql = f'SELECT id,session_id,{role},{updated},{projection} FROM "{table}"'
        if limit is not None:
            sql += " LIMIT ?"
        for row_id, session, role_value, updated_at, payload in connection.execute(sql, (limit,) if limit is not None else ()):
            message = parse_message(payload, row_id=row_id, session_id=session, role=role_value, format=format, updated_at=updated_at)
            if message:
                messages.append(message)
            elif payload is None:
                malformed += 1
            else:
                decoded = json.loads(payload)
                model = decoded.get("model") if isinstance(decoded.get("model"), dict) else {}
                provider = decoded.get("providerID") or model.get("providerID")
                effective_role = role_value if format == "v2" else decoded.get("role")
                if effective_role == "assistant" and decoded.get("tokens") is not None and provider != "traycer-openrouter":
                    malformed += 1
    return messages, malformed


def inspect_source(path):
    from spend_app.connection_paths import LocationError, SampleLimit, opencode_files
    usable, formats, seen = False, set(), 0
    try:
        for file in opencode_files(path, budget=256):
            seen += 1
            if file.suffix.lower() == ".json":
                with file.open("rb") as stream:
                    data = stream.read(131073)
                if len(data) > 131072:
                    raise SampleLimit()
                try:
                    payload = json.loads(data)
                except ValueError:
                    raise LocationError("Malformed OpenCode message JSON.") from None
                if not isinstance(payload, dict) or payload.get("role") not in {"assistant", "user"}:
                    raise LocationError("Unsupported OpenCode message JSON shape.")
                formats.add("json")
                usable |= parse_message(payload, row_id=file.stem, session_id=file.parent.name) is not None
            else:
                with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                    db.execute("PRAGMA query_only=ON")
                    db.set_progress_handler(lambda: 1, 10000)
                    columns = schema_columns(db)
                    selected = recognize(columns)
                    formats.update(selected)
                    if "cumulative" in selected:
                        usable |= db.execute("SELECT id FROM session WHERE COALESCE(tokens_input,0)+COALESCE(tokens_output,0)>0 LIMIT 1").fetchone() is not None
                    if selected != ("cumulative",):
                        messages, _bad = read_messages(db, columns, limit=3)
                        usable |= bool(messages)
            if seen >= 3:
                break
        return {"state": "readable", "usableSample": usable, "formats": sorted(formats), "detail": "OpenCode schema verified; usage metadata found." if usable else "Readable OpenCode location; no usable history in the bounded sample. Collection has not run yet."}
    except SampleLimit:
        return {"state": "readable", "usableSample": None, "sampleLimited": True, "formats": sorted(formats), "detail": "OpenCode inspection budget reached; history remains unverified, not empty or incompatible."}
    except UnsupportedSchema as exc:
        raise LocationError(str(exc)) from None
    except PermissionError:
        raise LocationError("Permission denied reading OpenCode metadata.") from None
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            return {"state": "readable", "usableSample": None, "sampleLimited": True, "detail": "OpenCode schema inspection budget reached; history remains unverified."}
        raise LocationError("OpenCode metadata is locked or unavailable. Recheck after the writer completes.") from None
    except (OSError, sqlite3.Error):
        raise LocationError("OpenCode metadata could not be read.") from None
