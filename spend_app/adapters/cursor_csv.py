"""CSV drop ingestion for Cursor usage exports.

Files dropped into ``data/imports/cursor`` (``CURSOR_IMPORT_PATH``) are
Cursor dashboard/Admin usage exports. The documented column mapping:

| Normalized CSV column            | UsageRow field                                |
|----------------------------------|-----------------------------------------------|
| date / timestamp                 | occurred_at (ISO-8601 or epoch ms)            |
| user / user email / user id      | identity only (stable raw IDs)                |
| model                            | model_key ``cursor:<model>``                  |
| agent id / session id            | session_id                                    |
| input without cache write tokens | fresh input (stored input = fresh + cache read)|
| input with cache write tokens    | fresh input fallback (= with − cache write)   |
| cache read tokens                | cached_input_tokens                           |
| cache write tokens               | cache_write_tokens                            |
| output tokens                    | output_tokens                                 |
| cost usd                         | cost_usd                                      |
| cost cents / charged cents       | cost_usd = cents / 100                        |
| event id / id                    | raw_id ``cursor-csv:<event id>``              |

Headers are matched tolerantly (case, whitespace, punctuation). Rows carry
content-derived stable raw IDs, so re-dropping or re-exporting the same
data is an idempotent upsert, never a duplicate.
"""

from __future__ import annotations

import csv
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, skipped_result, stable_id
from spend_app.adapters.cursor_local import canonical_model
from spend_app.adapters.local_common import parse_iso_time
from spend_app.pricing import PricingEngine


SOURCE = "cursor_csv"

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("date", "timestamp", "time", "occurred at"),
    "event_id": ("event id", "usage event id", "id", "request id"),
    "user": ("user", "user email", "email", "user id"),
    "agent": ("agent id", "agent", "session id", "session", "cloud agent id"),
    "model": ("model", "model name", "model key"),
    "input_with_cache_write": (
        "input with cache write tokens",
        "input with cache write",
        "input tokens with cache write",
    ),
    "input_without_cache_write": (
        "input w o cache write",
        "input without cache write tokens",
        "input without cache write",
        "input tokens without cache write",
        "fresh input tokens",
        "input tokens",
        "input",
    ),
    "cache_read": (
        "cache read tokens",
        "cache read",
        "cached input tokens",
        "cache read input tokens",
    ),
    "cache_write": (
        "input w cache write",
        "cache write tokens",
        "cache write",
        "cache creation input tokens",
        "cache creation tokens",
    ),
    "output": ("output tokens", "output"),
    "cost_usd": ("cost usd", "cost to you", "charged cost usd"),
    "cost_cents": ("cost cents", "charged cents", "cents", "charged cost cents"),
}


def normalize_header(header: str) -> str:
    letters = "".join(ch if ch.isalnum() else " " for ch in header.lower())
    return " ".join(letters.split())


def resolve_columns(fieldnames: object) -> dict[str, str]:
    fields: dict[str, str] = {}
    for header in fieldnames or ():
        normalized = normalize_header(str(header))
        for field, aliases in FIELD_ALIASES.items():
            if normalized in aliases and field not in fields:
                fields[field] = str(header)
                break
    return fields


def _cell(row: dict[str, object], fields: dict[str, str], field: str) -> str:
    header = fields.get(field)
    if header is None:
        return ""
    value = row.get(header)
    return value.strip() if isinstance(value, str) else ""


def _count(value: str) -> int:
    if not value:
        return 0
    try:
        numeric = float(value.replace(",", ""))
        return int(numeric) if math.isfinite(numeric) and numeric >= 0 and numeric.is_integer() else 0
    except (ValueError, OverflowError):
        return 0


def _optional_float(value: str) -> float | None:
    if not value:
        return None
    try:
        numeric = float(value.removeprefix("$").replace(",", ""))
        return numeric if math.isfinite(numeric) else None
    except (ValueError, OverflowError):
        return None


def _timestamp(value: str) -> datetime | None:
    stripped = value.strip()
    if re.fullmatch(r"\d{10,13}", stripped):
        seconds = int(stripped)
        if seconds > 10**12:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, UTC)
    return parse_iso_time(stripped)


def _cost(row: dict[str, object], fields: dict[str, str]) -> float | None:
    usd = _optional_float(_cell(row, fields, "cost_usd"))
    if usd is not None:
        return usd
    cents = _optional_float(_cell(row, fields, "cost_cents"))
    if cents is not None:
        return cents / 100
    return None


def parse_csv(path: Path, *, observations=False) -> list[UsageRow]:
    rows: list[UsageRow] = []
    from spend_app.connection_paths import confined
    with confined(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = resolve_columns(reader.fieldnames)
        if "timestamp" not in fields or "model" not in fields:
            raise ValueError("Expected a supported Cursor CSV usage export.")
        positions = {}
        for row in reader:
            occurred_at = _timestamp(_cell(row, fields, "timestamp"))
            if occurred_at is None:
                continue
            model = _cell(row, fields, "model") or "unknown"
            user = _cell(row, fields, "user")
            agent = _cell(row, fields, "agent")
            event_id = _cell(row, fields, "event_id")
            cache_read = _count(_cell(row, fields, "cache_read"))
            cache_write = _count(_cell(row, fields, "cache_write"))
            without_write = _count(_cell(row, fields, "input_without_cache_write"))
            with_write = _count(_cell(row, fields, "input_with_cache_write"))
            if _cell(row, fields, "input_without_cache_write") != "":
                fresh = without_write
            else:
                fresh = max(0, with_write - cache_write)
            output = _count(_cell(row, fields, "output"))
            cost_usd = _cost(row, fields)
            raw_id = (
                f"cursor-csv:{event_id}"
                if event_id
                else stable_id(
                    "cursor-csv",
                    occurred_at.isoformat(),
                    user,
                    canonical_model(model),
                    agent,
                    fresh,
                    cache_read,
                    cache_write,
                    output,
                    cost_usd,
                )
            )
            parsed = UsageRow(
                    source=SOURCE,
                    tool_key="cursor",
                    model_key=canonical_model(model),
                    occurred_at=occurred_at,
                    session_id=agent or None,
                    project=None,
                    input_tokens=fresh + cache_read,
                    cached_input_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    cache_write_1h_tokens=0,
                    output_tokens=output,
                    reasoning_tokens=None,
                    cost_usd=cost_usd,
                    raw_id=raw_id,
                    telemetry_complete=all(_cell(row, fields, field) != "" for field in ("input_without_cache_write", "cache_read", "cache_write", "output")),
                )
            from dataclasses import replace
            from spend_app.adapters.event_identity import Observation, cursor_identity
            legacy_fields = {key: header for key, header in fields.items() if normalize_header(header) not in {"input w o cache write", "input w cache write", "cost to you"}}
            if "cost_usd" not in legacy_fields:
                legacy_fields.update({"cost_usd": header for header in row if normalize_header(header) == "cost"})
            def old_count(field):
                try:
                    return max(0, int(float(_cell(row, legacy_fields, field))))
                except (ValueError, OverflowError):
                    return 0
            old_write = old_count("cache_write")
            old_fresh = old_count("input_without_cache_write") or max(0, old_count("input_with_cache_write") - old_write)
            try:
                old_cost = float(_cell(row, legacy_fields, "cost_usd"))
            except ValueError:
                try:
                    old_cost = float(_cell(row, legacy_fields, "cost_cents")) / 100
                except ValueError:
                    old_cost = None
            legacy = f"cursor-csv:{event_id}" if event_id else stable_id("cursor-csv", occurred_at.isoformat(), user, canonical_model(model), agent,
                        old_fresh, old_count("cache_read"), old_write, old_count("output"), old_cost)
            identity = (occurred_at.isoformat(), user, agent, canonical_model(model))
            ordinal = positions.get(identity, 0)
            positions[identity] = ordinal + 1
            canonical = f"cursor-csv:{event_id}" if event_id else stable_id("cursor-csv-observation", *identity, ordinal)
            shared = cursor_identity(event_id, user)
            aliases = (f"cursor-admin:{event_id}", legacy) if shared else (legacy,)
            observation = Observation(replace(parsed, raw_id=shared or canonical), aliases, occurred_at.timestamp(), "cursor-csv", components=(("cursor_shared", True),) if shared else ())
            rows.append(observation if observations else observation.row)
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine, import_path: Path) -> dict:
    import_path = Path(import_path)
    if not (import_path.is_dir() or import_path.is_file()):
        from spend_app.adapters.common import failed_result
        return failed_result(database_path=database_path, source=SOURCE, reason="Cursor export location is missing or moved. Recheck the configured path.")
    usage_rows, observations, issues = [], [], []
    files = 0
    from spend_app.connection_paths import adapter_files, confined
    paths = [import_path] if import_path.is_file() else [Path(name) for name in sorted(adapter_files(str(import_path / "*.csv")))]
    for path in paths:
        try:
            if path.suffix.lower() == ".json":
                import json
                from spend_app.adapters.cursor_export import parse_export
                with confined(path).open("r", encoding="utf-8-sig") as stream:
                    parsed, errors = parse_export(json.load(stream), path.stem.removeprefix("usage."), observations=True)
                observations.extend(parsed)
                usage_rows.extend(o.row for o in parsed)
                issues.extend(errors)
            else:
                parsed = parse_csv(path, observations=True)
                observations.extend(parsed)
                usage_rows.extend(o.row for o in parsed)
            files += 1
        except (OSError, ValueError):
            issues.append("cursor_export_unavailable_or_incompatible")
    from spend_app.adapters.event_identity import Observation, reconcile
    if not paths:
        return skipped_result(database_path=database_path, source=SOURCE, reason="The readable Cursor export folder contains no exports yet.")
    result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE,
                          usage_rows=usage_rows, issues=issues,
                          prepare=lambda connection, _: reconcile(connection, observations, issues))
    return {**result, "files": files, "issues": sorted(set(issues))}
