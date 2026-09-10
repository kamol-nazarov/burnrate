# BURNRATE 0.3.0-beta.5

Action Center v1 adds an **Attention** button with Current and History views.
Its three rules use existing recorded evidence: fresh eligible quota usage,
configured or monitored usage-source failures, and missing applicable model rates.
Quota thresholds default to 80% and 95% and can be changed in the panel.

Stable incidents retain acknowledgment, one-hour or 24-hour snooze, escalation
and recurrence history across restarts. History retains the newest 100 closed
episodes; expiry, disable, rebind and evidenced recovery have distinct outcomes.
Missing or stale evidence stays visibly unavailable. Actions open existing
capacity, connection or pricing diagnostics.

This is in-app attention only: no desktop notifications, external delivery,
automatic fixes, price fetching or new provider access. Current persisted quota
provenance supports Codex; other quota lanes remain unassessed without adequate
source observation, scope and window evidence. Large pricing scans can remain
awaiting evidence until a bounded sweep establishes complete coverage.

The upgrade uses versioned Action Center state in the existing application
database, with no schema migration. Existing usage, plans and connection settings
are preserved. The new alert preferences default enabled without granting
collection permission. Acknowledgment does not resolve an underlying condition.

This beta also shares source-reason handling, removes obsolete period-helper
fallbacks, and makes dashboard startup independent of the compatibility bootstrap.
See [Action Center behavior and limits](docs/action-center.md).
