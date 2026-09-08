# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is licensed under Apache-2.0.

## [Unreleased]

## [0.3.0-beta.2] - 2026-09-08

- Treat absent automatic harness defaults as not detected, while missing
  configured locations and real access/schema errors remain visible failures.
- Preserve OpenCode child-result counters, unpriced models and partial status
  across cumulative and mixed-format collection; restore optional ZCode project labels.
- Restore incremental Grok append reads and per-projection Traycer caching;
  correct duplicate-removal accounting and retain contradictory Codex totals
  as incomplete evidence rather than discarding them.

- Correct Codex request/counter identity transitions across files and replay;
  retain explicit-request equivalence and existing historical aliases.
- Persist OpenCode message start evidence for reverse cumulative reconciliation;
  defer ambiguous older checkpoints without advancing their progress.
- Merge Claude partial usage by field revision across files and persisted state,
  preserving omitted one-hour writes and exposing conflicting evidence.
- Conserve ZCode transcript totals; contradictory or missing component splits
  retain a trustworthy reported total as unclassified usage.

- Repair OpenCode v1/v2/legacy message selection, scoped cumulative remainder
  handling and unsupported-schema reporting without changing source event IDs.
- Support evidenced Codex CLI/Desktop roots and counter forms, Claude message
  revisions, Grok process/session metadata and native totals, and ZCode metadata
  alternatives. Preserve old IDs through exact alias records where provable.
- Add manual populated Antigravity cache and selected Cursor JSON export input;
  neither starts cache producers nor borrows their credentials.
- Bound admin reporting pagination, retain closed-hour/day reporting windows,
  revisit seven days for delayed reports, and honor longer quota Retry-After.
- Add an optional, explicitly enabled Claude status-line snapshot bridge using
  provider percentages only. Managed local connections remain usage-only.
- Retain third-party MIT notices and document ambiguous identity/coverage limits
  and the separately authorized release checks still required.

## [0.3.0-beta.1] - 2026-09-08

- Add an optional Connect harness wizard with bounded local discovery, manual
  verification, revisioned bindings, per-source retries and connection lifecycle.
- Resolve managed locations and credentials on collection cycles; preserve
  measured history when disabled and keep current-binding health separate.
- Add explicit documented provider API setup using Windows Credential Manager,
  with protected requests and no automatic native-credential borrowing.
- Redesign subscription management as a list and step wizard; add navbar and
  panel entry points and a read-only harness/integration status dialog.

## [0.2.0-beta.3] - 2026-09-07

- Fix summary crashes when a heatmap event has unavailable pricing. Unknown
  values are excluded from dollar totals; known values, including zero, remain.

## [0.2.0-beta.2] - 2026-09-07

- Separate usage value, configured plan accrual, and provider-reported charges.
- Preserve UTC ordering through schema 11 and improve malformed-record isolation.
- Require explicit consent for native credential usage and serve persisted limits on GET.
- Correct heatmap units, partial-value labels, and keyboard interactions.
- See [release notes and upgrade guidance](RELEASE-0.2.0-beta.2.md).

## [0.2.0-beta.1] - 2026-09-06

- Added effective-dated browser subscription management, historical correction previews,
  edit conflicts, idempotent retries and backup-gated schema 10 migration.
- Added optional onboarding and persisted actionable source diagnostics.
- Required browser/race execution, isolated wheel smoke and deterministic LF assets.
- Preserved request ownership, provider isolation and read-only doctor.

See [complete release notes](RELEASE-0.2.0.md).

## [0.1.2-beta.1] - 2026-09-06

- Fix stuck refresh state and overlapping detail/diagnostics polling.
- Guard request success, failure, cancellation, cleanup, and delayed scroll restoration.
- Keep selected-range validity independent of loading; recover through Retry.
- Preserve current navigation and range on Back/Home, with matching-cache reuse.
- Keep health requests independent of summary rendering and retain startup prefetch.
- Add deterministic browser regressions and helper-asset integrity checks.

See [hotfix patch notes](HOTFIX-0.1.2.md).

## [0.1.1-beta.1] - 2026-09-06

### Fixed

- Reject stale or mismatched time-range snapshots and show a loading chart until the selected range arrives.
- Render summaries without waiting for diagnostics; reuse startup prefetch and prevent overlapping automatic summary refreshes.
- Reuse historical event pricing with bounded caches that invalidate on event or pricing changes.
- Add the official GPT-6 Astra Standard API rate card and display name, including the long-context threshold.

This patch retains the public beta designation and does not change provider routing or database schema.

## [0.1.0-beta.1] - 2026-09-04

Initial public beta of BURNRATE: a local-first Windows dashboard for AI
coding-agent token usage, cache efficiency, subscription capacity, and
published-rate cost equivalents. BURNRATE reads harness-local files and
optional read-only provider APIs. It does not sit on the inference path, does
not require a BURNRATE account, and does not ship Grafana.

### Added

- Installable `burnrate` CLI on CPython 3.12 with `init`, `doctor`, `serve`,
  `subscription add`, and `subscription list`.
- FastAPI + SQLite dashboard bound to `127.0.0.1:17331` by default.
- Empty subscription list on a fresh database. Users add their own plan
  amounts; proration is not provider billing.
- Local, read-only adapters for Codex, Claude Code, OpenCode, and ZCode
  files/SQLite stores. Prompts and responses are not retained.
- Optional, opt-in provider admin and quota readers. Missing credentials skip
  with a named reason and never render as `$0`.
- Experimental adapters for undocumented interfaces, isolated so one failure
  cannot zero out other providers: Cursor usage service, Anthropic OAuth
  usage, Antigravity localhost RPC, Traycer readers, and Grok Build log
  scrape.
- Effective-dated `pricing/*.yaml` cards with `exact`, `derived`, unpriced,
  incomplete, and unavailable kept distinct.
- System/UI font stack. Google Fonts are not bundled and are not loaded from a
  CDN.
- Windows helpers that start, stop, status, install, and uninstall the
  dashboard only. Uninstall does not delete the user database unless
  `-PurgeData` is passed.
- Community files: `LICENSE` (Apache-2.0), `NOTICE`, `SECURITY.md`,
  `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`.

### Security

- Default listener is localhost only.
- Credentials are not logged, not returned by the API, and not copied into
  SQLite.
- Claude OAuth credential-file refresh is off unless an explicit opt-in
  environment variable is set.
- Report vulnerabilities privately; see `SECURITY.md`. Do not file public
  issues with secrets.

### Notes

- This is an unreleased public beta. Experimental provider interfaces may
  change without a major version bump.
- Grafana, Prometheus, and the OpenTelemetry Collector are not part of
  BURNRATE and are not distributed with this beta.
- Licensed under Apache-2.0.
