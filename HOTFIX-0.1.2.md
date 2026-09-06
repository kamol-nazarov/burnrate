# BURNRATE 0.1.2-beta.1 hotfix — 2026-09-06

This patch repairs frontend request ownership, refresh recovery, and navigation.

The private and public repositories ship identical dashboard HTML, CSS,
JavaScript, request-state helper, and favicon. Backend configuration and provider
availability can still produce different data without changing the shared UI.

## Fixed

- Automatic summary refresh resumes after navigating away from an in-flight request.
- Each request owns its pending/loading lifecycle. Older responses cannot clear newer state.
- Failed range changes keep mismatched charts and figures unavailable, with Retry recovery.
- Detail, Back, and Home respect the latest selected range and entity identity.
- Late successes, failures, cancellations, and scroll callbacks cannot undo navigation.
- Summary, detail, and diagnostics polling do not overlap a slow request for the same active resource.
- Health loading stays independent of summary rendering and survives subsequent summary refreshes.
- Matching cached data remains reusable and honestly labeled stale. Startup prefetch is consumed once.

## Scope and validation

No pricing-rate, provider-routing, subscription, database-schema, or backend-cache changes.
The request helper uses the existing content-hash asset delivery mechanism.

Regression coverage executes production JavaScript, renderers, and DOM in Chrome
with synthetic fixtures, deferred responses, cancellation, and controlled polling ticks.
The public reviewed baseline failed 19 of 29 race scenarios; all 29 pass with this patch.
Existing viewport, snapshot, Python, syntax, and redacted-secret checks are also exercised.

Reload an already-open dashboard tab to load the updated frontend. Deployments must
include request-state.js alongside spend.js. Runtime restart requirements depend
on the hosting process; changing a Git branch alone does not restart a running app.
