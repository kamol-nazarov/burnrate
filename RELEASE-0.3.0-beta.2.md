# BURNRATE 0.3.0-beta.2

Python package: `0.3.0b2`. This is a beta prerelease, not a stable-release or
complete-provider-history guarantee.

## Provider compatibility and accounting

- OpenCode supports message v1/v2, legacy JSON and labeled cumulative fallback.
  Proven message starts and allocation state prevent coarse/granular replay from
  adding the same work twice. Missing start evidence defers ambiguous checkpoints.
- Codex supports evidenced CLI/Desktop rollouts and approved archives, retains
  proven request/counter equivalence and old IDs, and distinguishes inherited
  fork history from new child work. Contradictory totals stay incomplete.
- Claude usage merges by message/request and per-field revision across files
  and restarts. Omission preserves known values and one-hour cache writes;
  explicit zeros/corrections and conflicting evidence remain distinct.
- Grok tracks models by session/process generation and reads verified append
  tails. Traycer uses logical identity and per-projection caching. ZCode preserves
  trustworthy totals and optional project labels without inferring a model.
- Selected Cursor exports and populated Antigravity caches use existing source
  scopes. Documented admin reporting has bounded pagination, preserves partial
  results and revisits recent completed periods for delayed reports.

Usage value at published/reference rates, configured subscription accrual and
provider-reported charges are separate. Included reference value is not cash
savings; deposits, allowance and OpenRouter balance are not token events.

## Connections and optional quota

Missing automatic defaults are not detected/skipped; missing configured sources,
permissions and schema errors remain visible. Unpriced usable history still
counts as receiving usage. Other collection and requested summary warming continue.
Saved locations, disabled states, credential references and request ownership
are retained. Managed local bindings continue to grant usage only.

An optional Claude status-line bridge reads provider-reported window percentages
into an atomic BURNRATE-owned snapshot. It requires explicit setup and opt-in;
the upgrade does not install it, change Claude settings or borrow credentials.
Existing native/private integrations retain their existing consent requirements.

## Upgrade and rollback

The application schema remains version 11. Compatibility ledgers add nonsecret
metadata to existing `app_meta`; normal ingest transactions own event/checkpoint
changes together. Keep a consistent SQLite online backup, including live WAL
contents, before upgrade. Never migrate the only rollback copy.

Repaired history can differ where an evidenced duplicate or component error is
corrected. Compare identical fixed input/time scopes; do not blanket-reimport
other profiles. Older code ignores repair ledgers and binding controls, so code
rollback alone is insufficient: preserve a compatible runtime and database backup.

## Explicit limits

Unmatched ZCode formats, native Grok totals over unmapped Traycer history and
Antigravity records without response IDs remain coverage gaps. Ambiguous counter
fragments are not matched by equal numeric values. Cursor SDK history retains its
existing cutoff; scope matching requires real IDs. A populated cache depends on
its separate producer. Reporting delays can exceed the recent revisit window.

Beta acceptance and independent CI do not establish live provider scopes, every
credential-vault failure mode or all concurrency interleavings. See the current
[provider matrix](docs/providers.md) and [connection guide](docs/connections.md).
Upstream attribution is retained in `NOTICE`. No PyPI publication is part of this
GitHub beta release.
