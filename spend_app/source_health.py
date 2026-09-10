"""Ingest failure lifecycle (Release A contract, task A03).

A source health attempt starts BEFORE parsing can fail, so a crash is always
visible as a failed/partial run instead of a silently missing source.
Record-level parsing problems are isolated: one malformed line quarantines
itself and lets neighboring valid records through, reports sanitized counts
and locations (opaque file reference + line number; never raw payloads,
prompts or secret-shaped text), and never marks a source healthy merely
because exceptions were swallowed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from spend_app.db import connect, initialize
from spend_app.ingest import IngestRun
from spend_app.source_evidence import sanitize_reason as sanitize_reason


@dataclass
class RecordOutcome:
    status: str  # "ok" | "quarantined" | "skipped"
    reason: str | None = None
    location: str | None = None

    @property
    def failed(self) -> bool:
        return self.status != "ok"


@dataclass
class SourceHealth:
    """Per-run tallies reported honestly: nothing is silently dropped."""

    parsed: int = 0
    quarantined: int = 0
    skipped: int = 0
    reasons: list[str] = field(default_factory=list)

    def note(self, outcome: RecordOutcome) -> None:
        if outcome.status == "ok":
            self.parsed += 1
            return
        if outcome.status == "quarantined":
            self.quarantined += 1
        else:
            self.skipped += 1
        detail = " · ".join(part for part in (outcome.reason, outcome.location) if part)
        if detail and detail not in self.reasons and len(self.reasons) < 8:
            self.reasons.append(detail)

    def note_ok(self) -> None:
        self.parsed += 1

    @property
    def partial(self) -> bool:
        return self.quarantined > 0 or self.skipped > 0

    def summary_reason(self) -> str | None:
        if not self.partial:
            return None
        parts = []
        if self.quarantined:
            parts.append(f"{self.quarantined} quarantined record(s)")
        if self.skipped:
            parts.append(f"{self.skipped} skipped record(s)")
        text = "; ".join(parts)
        if self.reasons:
            text += f" — first: {self.reasons[0]}"
        return text


def quarantine(reason: object, location: str | None = None) -> RecordOutcome:
    return RecordOutcome(
        status="quarantined", reason=sanitize_reason(reason), location=location
    )


def skipped(reason: object, location: str | None = None) -> RecordOutcome:
    return RecordOutcome(
        status="skipped", reason=sanitize_reason(reason), location=location
    )


def ok() -> RecordOutcome:
    return RecordOutcome(status="ok")


def parse_jsonl_record(
    raw: bytes | str,
    *,
    line_number: int,
    location: str,
    health: SourceHealth,
) -> tuple[dict | None, RecordOutcome]:
    """Isolate one JSONL line: syntax errors quarantine, never abort.

    A final unfinished line (no trailing newline, invalid JSON) is reported
    as skipped and retriable — the writer's next flush completes it — not
    permanent data loss.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    where = f"{location}:{line_number}"
    try:
        record = json.loads(text)
    except ValueError as exc:
        health.note(quarantine(f"invalid JSON syntax ({exc.msg})", where))
        return None, RecordOutcome(status="quarantined")
    if not isinstance(record, dict):
        health.note(quarantine("JSON value is not an object", where))
        return None, RecordOutcome(status="quarantined")
    health.note(ok())
    return record, ok()


@dataclass
class AttemptResult:
    status: str
    error: str | None
    health: SourceHealth
    written: int = 0


@contextmanager
def source_attempt(database_path: Path, source: str) -> Iterator[list[AttemptResult]]:
    """Open the health run before parsing starts; close it whatever happens.

    Usage::

        with source_attempt(db, "codex_local") as outcomes:
            ...parse and persist...
            outcomes.append(close_attempt(health, written=..., unpriced_models=...))

    The run row is committed immediately so a crash mid-parse is visible as a
    stuck-then-failed run. The caller appends one ``AttemptResult`` on the
    success path; if an exception escapes first, a failed run with a
    sanitized reason is recorded and the exception propagates.
    """
    outcomes: list[AttemptResult] = []
    initialize(database_path)
    with connect(database_path) as connection:
        run = IngestRun.start(connection, source)
        run_id = run.id
    try:
        yield outcomes
    except Exception as exc:
        with connect(database_path) as connection:
            IngestRun(connection, source, run_id).finish(
                status="failed", error=sanitize_reason(exc)
            )
        raise
    with connect(database_path) as connection:
        run = IngestRun(connection, source, run_id)
        if not outcomes:
            run.finish(
                status="failed", error=sanitize_reason("ingest produced no result")
            )
        else:
            result = outcomes[0]
            run.events_written = result.written
            run.finish(status=result.status, error=result.error)


def close_attempt(
    health: SourceHealth,
    *,
    written: int,
    unpriced_models: list[str] | None = None,
) -> AttemptResult:
    """Fold parser health into a terminal run status.

    A source is never healthy merely because exceptions were swallowed: any
    quarantine/skip marks the run ``partial`` and carries the sanitized
    reasons; unpriced models keep the historical ``partial`` convention.
    """
    unpriced = sorted(unpriced_models or [])
    error = health.summary_reason()
    if unpriced:
        text = f"Unpriced models: {', '.join(unpriced)}"
        error = f"{text}; {error}" if error else text
    if health.quarantined and not health.skipped and not error:
        error = f"quarantined records: {health.quarantined}"
    status = "partial" if (error or health.partial or unpriced) else "success"
    return AttemptResult(status=status, error=error, health=health, written=written)
