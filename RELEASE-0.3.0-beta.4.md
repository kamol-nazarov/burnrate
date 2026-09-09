# BURNRATE 0.3.0-beta.4

Plans & Value compares **This month** or **Last month** accrued configured
expense with recorded API-equivalent usage over the same interval.

- Calendar-aware Decimal accrual respects effective plan history and local dates.
- Shared tool associations, including OpenCode/ZCode, avoid multiplying usage.
  Unassigned usage stays separate from configured plans.
- Qualified multiples preserve partial, unknown, no-history and zero-cost states.
  Reference value is not ROI, cash saved, or a cancellation recommendation.
- Pricing coverage describes recorded usage, not complete account capture.
  Collection evidence is scoped to relevant sources and retains the latest
  attempt, prior success, age, safe reason and historical-capture limitations.
- Explanations render known exclusions and source evidence as readable text.
  Connection help preserves plan drafts and restores focus to a valid opener.

## Upgrade and limits

No SQL schema migration is introduced. Back up the existing SQLite database
consistently before upgrading; retain the prior runtime and a compatible backup.
The packaged dialog bundle and its manifest must be upgraded together.

Configured tool association is an assumption, not proof of provider account
identity. Invoice charges are not added to reference value, unknown pricing is
not invented, and a successful current poll does not prove historical completeness.
Existing provider coverage, permissions and quota-selection behavior are preserved.
No new credentials, consent, provider access or network exposure is required.

This is a GitHub beta prerelease, not a stable release or PyPI publication.
