# BURNRATE 0.2.0-beta.1 — 2026-09-06

## Added

- Manage configured subscriptions from the dashboard: add plans, inspect active,
  scheduled and ended terms, schedule price/cadence changes, record inclusive
  last-active dates, and preview/confirm historical corrections.
- Optional, dismissible setup guidance with a reopen button, timezone information,
  and a link to subscription management. Returning users with history are not
  forced through setup.
- Actionable source diagnostics showing the latest attempt, last success,
  freshness, available measurements, redacted reasons and next steps.

## Fixed

- Required CI browser validation now includes all 29 request-ownership regressions,
  existing viewport/snapshot checks, and subscription/onboarding workflows.
  Missing runtime or skipped required tests fail the release check.
- LF checkout rules make web asset byte counts deterministic on Windows.
- Daily configured-cost caches reconcile obsolete dates after changes and retain
  exact Decimal text alongside the legacy REAL compatibility field.
- Latest source failures remain visible even when an earlier attempt succeeded.
- Migration backup connections explicitly close, including on Windows.

## Changed

- Plans have stable logical IDs, edit versions and nonoverlapping effective terms.
  Distinct real plans may use the same tool; shared OpenCode/ZCode plans count once.
- Browser writes have stale-editor conflicts and duplicate-retry protection.
  Existing CLI add/list remain available; validated advanced changes share the
  same service through subscription apply.
- Historical calculations and forecasts respect effective dates. The core
  JavaScript budget remains 30 KB compressed; the new product functionality has
  a 6 KB allowance, with all production scripts included in a 36 KB total gate.

## Upgrade / migration

Schema 10 preserves existing plan IDs, rates, cadence and inclusive end dates
without inventing earlier revisions. Existing databases require a verified
SQLite online backup before migration. Unrecognized schemas stop safely.

Proration remains calendar-based: each month, quarter or year uses its actual
day count. A plan ending September 20 includes that day and excludes later days.
**Ending a configured plan does not cancel the subscription with its provider.**
Configured cost is not a verified payment/refund calculation. Monthly equivalents
are references, and API-equivalent usage value is not cash saved.

Include all packaged frontend assets when upgrading and reload existing tabs.
See [subscription semantics and configuration](docs/subscriptions.md).

## Privacy / security

No inference proxy, provider routing change, new public listener, or provider-secret
form is introduced. Browser mutations require allowlisted Host, same intended
Origin, JSON and a request header. Explicitly configured TLS-terminating origins
are supported without trusting arbitrary forwarding headers.

Setup and normal diagnostics do not enable integrations, refresh credentials or
run paid/admin probes. Local detection is limited to known metadata locations.
Only setup dismissal/completion preferences are saved in browser storage.

## Known limitations

- Windows/Python 3.12 remains the supported platform.
- Configured costs are not provider billing or account-level attribution.
- Historical correction previews state their covered date interval; open-ended
  future days are not included in that preview.
- Local presence detection is cached for the runtime; restart after installing a
  new harness to refresh presence. Persisted usage attempts continue refreshing.
- Permanent deletion of plan history is not available.
- Undocumented provider integrations remain experimental and isolated.
