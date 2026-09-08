# BURNRATE 0.3.0-beta.3

This beta corrects Codex subscription-capacity readings that could show a
model-specific pool's zero instead of the main Codex quota.

- Select the newest explicitly identified main Codex weekly observation,
  regardless of primary/secondary window position. Alternate and unidentified
  pools are excluded. Genuine zero and downward corrections remain valid.
- Keep source observation time separate from polling. Snapshots older than
  six hours or past their reported reset are unavailable, without inventing
  a new percentage or reset. Touching a file does not renew freshness.
- Honor CODEX_HOME and confinement. Managed usage-only or disabled connections
  do not grant quota access or trigger a default-profile fallback.
- Keep measured token history, prices, costs and reconciliation unchanged.

## Upgrade and coverage

No SQL schema migration is required. Versioned quota provenance is stored in
existing app_meta atomically with quota persistence. Old unverified readings
remain unavailable until a valid main-pool poll. Back up the database before
upgrading; older binaries ignore the provenance checks and retain the old
quota-selection behavior.

Local telemetry remains a bounded sample: at most 5,000 directory entries,
40 recent session files and 2 MB per file. A profile path does not prove account
identity. Missing IDs, expired observations and unsupported managed quota
permissions remain unavailable; no paid turn is required to finish setup.
No credential access, provider permissions or network exposure is added.

This is a beta prerelease. Existing provider coverage limits from beta.2
continue to apply. Quota percentages are separate from usage value, configured
subscription cost and actual reported charges.
