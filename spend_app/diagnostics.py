"""Safe source guidance from persisted attempts and bounded metadata presence."""

import re
from datetime import datetime
from pathlib import Path

LOCAL_PATHS = {
    "codex_local": (".codex", "sessions"),
    "claude_local": (".claude", "projects"),
    "cursor_local": (".cursor", "projects"),
    "opencode_local": (".local", "share", "opencode", "opencode.db"),
    "zcode_local": (".zcode", "cli", "db", "db.sqlite"),
    "grok_local": (".grok", "logs"),
    "traycer_local": (".traycer", "host"),
    "antigravity_local": ("AppData", "Roaming", "Antigravity"),
}
SETTINGS = {
    "openai_admin": "OPENAI_ADMIN_KEY",
    "anthropic_admin": "ANTHROPIC_ADMIN_KEY",
    "cursor_admin": "CURSOR_API_KEY",
    "openrouter": "OPENROUTER_MANAGEMENT_KEY",
}
EXPERIMENTAL = {
    "cursor_local",
    "cursor_usage_service",
    "grok_local",
    "traycer_local",
    "antigravity_local",
}
HELP_URL = "https://github.com/kamol-nazarov/burnrate/blob/main/docs/providers.md"


def detect_local_sources(home: Path):
    found = {}
    for source, parts in LOCAL_PATHS.items():
        try:
            found[source] = home.joinpath(*parts).exists()
        except OSError:
            found[source] = None
    return found


def safe_reason(raw, source=""):
    text = str(raw or "").lower()
    if not text:
        return None
    if "not configured" in text or "disabled" in text or "credential missing" in text:
        return (
            SETTINGS.get(source, "Optional integration configuration")
            + " is missing or disabled."
        )
    if "permission" in text or "access denied" in text or "permissionerror" in text:
        return "Access to local usage metadata was denied."
    if "429" in text or "throttl" in text:
        return "The provider limited usage-status requests. Wait for the existing retry cadence."
    if "unpriced" in text or "pricing" in text:
        return "Some model pricing is unavailable; measured usage remains available."
    return "The latest source attempt reported a problem; other sources continue independently."


def source_reports(connection, now, detected=None):
    detected = detected or {}
    sources = set(LOCAL_PATHS) | set(SETTINGS) | {"cursor_usage_service", "cursor_csv"}
    sources.update(
        row[0] for row in connection.execute("SELECT DISTINCT source FROM ingest_runs")
    )
    reports = []
    for source in sorted(sources):
        latest = connection.execute(
            "SELECT started_at,finished_at,status,error FROM ingest_runs WHERE source=? ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        success = connection.execute(
            "SELECT MAX(finished_at) FROM ingest_runs WHERE source=? AND status IN ('success','partial')",
            (source,),
        ).fetchone()[0]
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM usage_events WHERE source=?), (SELECT COUNT(*) FROM unpriced_usage_events WHERE source=?)",
            (source, source),
        ).fetchone()
        total = sum(counts)
        age = None
        if success:
            age = max(0, (now - datetime.fromisoformat(success)).total_seconds())
        status = latest["status"] if latest else "never"
        reason = safe_reason(latest["error"], source) if latest else None
        if status == "failed":
            state = "failed"
        elif status == "skipped":
            state = "configuration_missing"
        elif status == "partial":
            state = "partial"
        elif total == 0 and detected.get(source) is False:
            state = "not_detected"
        elif total == 0 and detected.get(source) is True:
            state = "detected_without_history"
        elif not latest:
            state = "not_checked"
        elif age is None or age > (3600 if source in SETTINGS else 180):
            state = "stale"
        else:
            state = "healthy"
        action = "Complete a supported turn and recheck."
        if state == "not_detected":
            action = "Use the supported harness so its local usage store is created, then recheck."
        elif state == "configuration_missing":
            action = (
                "If you want this optional integration, configure "
                + SETTINGS.get(source, "its documented opt-in setting")
                + ". No credential is required for the dashboard."
            )
        elif reason and "denied" in reason:
            action = "Run BURNRATE as the user who owns the usage store and grant that user read access."
        elif state == "failed":
            action = "Check the integration documentation and retry after resolving the source issue. Other sources remain independent."
        elif state == "stale":
            action = "Check that the harness has completed a turn and the existing local ingestion process is running."
        elif state == "healthy" and total:
            action = "No action needed for usage. Quota availability and pricing are reported separately."
        models = [
            row[0]
            for row in connection.execute(
                "SELECT model_key FROM pricing_gaps WHERE source=?", (source,)
            )
        ]
        models = [
            m
            if re.fullmatch(r"[A-Za-z0-9_./:\[\]-]{1,100}", m or "")
            and not m.startswith("/")
            else "unidentified model"
            for m in models
        ]
        reports.append(
            {
                "source": source,
                "state": state,
                "lastAttempt": (latest["finished_at"] or latest["started_at"])
                if latest
                else None,
                "lastSuccess": success,
                "freshnessSeconds": age,
                "measurements": (
                    ["input/output tokens", "cache components"]
                    if counts[0]
                    else ["partial token totals"]
                    if total
                    else []
                ),
                "reason": reason,
                "nextAction": action,
                "pricingMissing": models,
                "experimental": source in EXPERIMENTAL,
                "documentation": HELP_URL,
            }
        )
    return reports
