# Action Center v1

**Attention** opens a reopenable in-app panel. Current includes unresolved,
acknowledged, snoozed, below-threshold quota windows, and awaiting-evidence items.
History contains the newest 100 closed episodes with their actual closure reasons.
The badge counts distinct actionable, unacknowledged, unsnoozed items backed by
usable evidence. Evaluation failure or lag is visible; zero is not a claim of
complete source coverage.

A failed reader marks that rule's evidence as awaiting; independently usable
evidence from other rules can still need attention. A lost evaluator or persistence
failure suppresses the badge until evaluation is usable again.

There are exactly three rules. All start enabled; this grants no provider access
and enables no external notifications.

## Rules and evidence

- **Quota:** used utilization at configurable integer thresholds (defaults 80/95;
  `1 <= lower < higher <= 100`). One pool/window has one card, warning then high.
  The first observation says “Currently,” not “Just crossed.” Newer decreases,
  including zero, replace the current reading. A dip below the threshold keeps
  that window's episode and reached/acknowledged tiers, with no badge obligation.
  Reset changes without an established boundary await fresh evidence; expiry is
  labeled expired, not fixed.
- **Source failure:** a persisted terminal failure of an explicitly enabled
  binding or an eligible, previously monitored legacy source. Never-installed
  defaults, inactivity, intentional disable, and cadence skips do not trigger.
  A later usable same-revision import can recover connectivity even with zero
  new rows. Managed pricing-only partial imports can recover when their persisted
  completion state/revision proves acceptance; ambiguous legacy partial imports
  remain awaiting evidence. Schema, access and authentication failures are distinct.
- **Missing rate:** canonical measured unpriced rows with complete classification
  and no conflicting coverage/identity record, checked against the already loaded
  event-time pricing engine. API/admin traffic, incomplete token classification
  and unresolved identities are not mislabeled as missing prices. Counts are
  records/tokens, never invented dollars. A known price domain and exact model key
  can group contributing sources; unqualified unknown domains remain source-specific.

Only Codex currently persists the required source-observation/scope/window proof
for quota alerts. Its released main-pool validation and six-hour source-age mask
are reused, including configured-path scope identity. No telemetry files are
discovered or read by Action Center. Other quota lanes lacking persisted source
observations remain unassessed; poll timestamps, remaining balances, credits and
context utilization do not substitute for quota evidence. The pure normalization
contract supports remaining-to-used conversion only with an explicit proven meaning.
Managed usage-only bindings do not gain quota eligibility.

Current/History actions open existing capacity, connection-status or pricing-gap
diagnostics. Pricing actions carry the available model context. There is no
automatic reconnect, retry, scan, pricing edit, reset-credit use or repair.

## Attention and episodes

Acknowledgment suppresses the current reached tier without fixing the condition.
A higher quota tier can need attention again. Cosmetic text and count changes do
not reset acknowledgment. Snooze covers the entire incident, including escalation,
for one or 24 elapsed UTC hours; expiry is not recovery. Unsnooze removes that
suppression. Closing the panel does neither.

Repeated observations and restarts retain stable subject/episode identities.
True source/pricing recurrence after later proven recovery can start another
episode. Quota episodes retain reached-tier history within the same window.
Rule disable, source disable, rebind, window expiry, recovery and removed/superseded
pricing evidence have distinct outcomes. Re-enabling a rule cannot turn an old
snapshot into a new crossing. Missing reads preserve uncertainty rather than
silently resolving items.

## Storage and evaluation

One coalesced 30-second job (`max_instances=1`) reads persisted application data.
It does not run collectors. The existing SQLite `app_meta` table holds versioned
`attention.v1` state, minimal replay watermarks, a safe evaluator-error marker,
and hashed retry receipts. No table/schema migration or second database is added.
Older binaries ignore these namespaced records.

Evaluator transitions and user mutations read current state inside a
`BEGIN IMMEDIATE` transaction. A stale expected revision conflicts rather than
overwriting an acknowledgment. Retry receipts have one effect; a lost snooze
response cannot extend the deadline on retry. Failed transactions retain incident
state/progress. A separate safe failure marker, or read-time lag if storage itself
is unavailable, exposes evaluation failure.

Quota reads seek only the latest declared windows. Source metadata is batched.
Pricing uses primary-key batches of at most 500 rows with indexed coverage/gap
joins; new IDs are processed incrementally and full revalidation is periodic.
Unchanged completed scans can be cached for five minutes. Counts during an
incomplete scan are marked partial. Missing-rate closure requires a completed
sweep at the active pricing revision with stable ingest evidence; busy large
histories can remain awaiting evaluation. Future-only rates cannot close a
historical gap. No usage rows are repriced or modified.

Display history is pruned to 100; unresolved incidents and replay watermarks are
not pruned to meet that limit. Hashed retry receipts are retained separately.
The state blob has a two-megabyte safety limit: exhaustion reports evaluation
failure instead of silently discarding unresolved episodes. Large-state/storage
maintenance is not an automatic Action Center operation.

## API and UI contract

- `GET /api/attention` is read-only. `detail=false` returns badge/evaluator metadata.
  Opening the panel does not evaluate or repair state.
- `POST /api/attention` accepts acknowledge, snooze, unsnooze and preferences.
  It requires a canonical UUID retry ID and current expected revision, and reuses
  the existing bearer, exact Host/Origin, JSON, non-simple-header and size protections.
- Responses distinguish `observedAt`, `evaluatedAt`, `asOf`, first detection,
  acknowledgment, snooze, closure/recovery evidence, severity, eligibility,
  `reasonCode`, and a fixed `actionType`. The backend owns counts and clock logic.
- The badge uses the existing dashboard cadence, throttled to 30 seconds and
  paused when hidden. No new sockets or per-card timers are added. Stale responses
  after tabs, close/reopen and mutations are discarded. Failed writes preserve drafts.
- Dates use the configured timezone. Native buttons/dialog/details provide
  keyboard behavior; actual DOM openers are separate from incident data. No
  notification permission, toast delivery, sound, email or webhook is requested.

Authoritative frontend files generate all shipped code and checksums through the
existing non-browser pipeline. Dashboard/request code is compiled in one scope;
the request bootstrap still precedes dashboard execution. Existing cross-script
DOM helpers and browser-race bindings are explicit. Node-only formatter exports
remain in source tests, and all production modules count toward unchanged budgets.

## Verification limits

Action Center consumes the existing `load_source_health` boundary. Its reason
handling reuses the shared pure `source_evidence` helper through that loader;
incident eligibility and recovery remain Action Center decisions. The feature
ships in beta.5 with no database schema migration.

Unit tests use guarded imports, fake query rows, transactions, clocks, scheduler
registration and DOM/HTTP boundaries. A shared fixture is produced through the
production loader/reducer/store projection and used by the actual renderer tests.
Fake transaction checks are not real SQLite rollback or cross-process evidence.
Real persistence/concurrency, installed-package and scheduler runtime acceptance
remain release-stage checks. Existing independent CI/browser/security/package
gates remain enabled; no local browser or live-provider test is needed or authorized
for this feature candidate. This branch does not deploy or change beta.4 permissions.
