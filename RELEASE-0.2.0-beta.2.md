# BURNRATE 0.2.0-beta.2

Python package version: 0.2.0b2. Released 2026-09-07.

## Fixed

- Usage value excludes configured subscription accrual. Partial pricing retains
  a labeled known subtotal. Provider-reported charges have a separate field.
- Expired or absent rate cards produce partial coverage instead of an API error.
- Fixed-duration windows use elapsed UTC time. Calendar windows and subscription
  materialization use the configured timezone. Heatmap values carry dollar units.
- Session values are no longer rescaled to manufacture an accounting remainder.
- Malformed Codex and Claude records are isolated from neighboring valid records.
- OpenCode cumulative sessions retain a first-observation baseline and subsequently
  emit observed deltas, with progress and usage committed transactionally.
- Chart keyboard focus, timezone labels, and status announcements are improved.

## Added

- `burnrate pricing-refresh --provider <provider> --snapshot <file>` drafts a
  review from captured metadata. It never applies new prices automatically.
- `burnrate pricing-audit` reports approaching card expiry.

## Upgrade and privacy

Initialization upgrades existing databases to schema 11 with a consistent backup
and transaction. Numeric UTC microsecond timestamps preserve event ordering.
Existing plan identities, terms, and usage remain in the database.
Keep the backup before upgrading; an older runtime may not support schema 11.

Native credential reuse now needs explicit consent:
`BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE=1`,
`BURNRATE_ENABLE_CURSOR_USAGE_SERVICE=1`, or
`BURNRATE_ENABLE_ZAI_QUOTA=1`. These lanes default to disabled.
Claude credential refresh additionally requires the existing
`BURNRATE_CLAUDE_OAUTH_REFRESH=1` setting.
Permitted native HTTP requests identify as BURNRATE.
Local Claude quota snapshots remain usable without native-credential consent.

The limits endpoint reads persisted snapshots without triggering live probes.
An optional `BURNRATE_ACCESS_TOKEN` requires bearer authentication for API reads;
the dashboard does not provide a token-entry interface.

Configured plan amounts are calendar-prorated attribution, not verified payments.
Ending a plan in BURNRATE does not cancel the provider subscription.

## Validation

- 564 non-browser tests passed; six optional tests skipped.
- 39 required browser tests passed, including all 29 request-race scenarios;
  no browser tests skipped.
- A clean Python 3.12 wheel installation passed initialization, read-only doctor,
  all six local assets, safe API reads, and shutdown.
- Synthetic fixtures were used throughout validation.

## Limitations

Experimental provider interfaces can fail independently. Missing pricing and
quota data remain unavailable. OpenCode pre-observation history has no invented
per-turn time distribution. The Claude status-line bridge is not included.
