# Plans & Value

Plans & Value is a read-only comparison report. It places **configured plan
expense** next to **API-equivalent reference usage** over the same calendar
interval so you can see how measured consumption at published rates compares to
the fixed prices you configured.

It is not an invoice, payment ledger, ROI calculator, or profit report. It does
not cancel, downgrade, or re-route subscriptions. Configured terms still live in
[Configured subscriptions](subscriptions.md); this view only compares them to
recorded usage.

## Periods

Every report is anchored to a server-side `asOf` instant (aware UTC) and the
configured IANA timezone (`settings.timezone`). Periods are half-open UTC
intervals `[start_utc, end_utc)`.

| Period key | UI label | Interval |
| --- | --- | --- |
| `this_month` | This month | Local month-start (`00:00` on the 1st) through `asOf` |
| `last_month` | Last month | Previous local month-start through current local month-start (the full prior calendar month) |

**This month** is month-to-date up to `asOf`, not a projected full month.
**Last month** is the entire previous calendar month in the configured zone —
not “the same number of days as so far this month,” and not truncated by
`asOf`.

The UI shows the resolved local date span and timezone (for example
`Sep 1, 2026 – Sep 8, 2026 (America/New_York)`).

## Configured cost

Configured cost is calendar proration of your plan terms over the **exact**
comparable interval used for usage — the same `[start_utc, end_utc)`. Mixing
month-to-date usage with a full-month plan price is intentionally rejected.

Accrual rules:

- Each local calendar day that intersects the window contributes
  `daily_rate × (overlap_seconds / day_duration_seconds)`.
- Local days can be 23, 24, or 25 hours across DST transitions; the fraction
  uses the day’s true length, never a fixed 24 hours.
- Cadences follow the same calendar math as subscription management:
  - **monthly** — `amount / days in that calendar month`
  - **quarterly** — `amount / days in that calendar quarter` (sum of the three
    months)
  - **annual** — `amount / 366` in a leap year, otherwise `/ 365`
- Money uses `Decimal`. Intermediate daily pieces stay exact; rounding is for
  display only.
- Term `start_date` is inclusive (local midnight of that date). Inclusive
  `end_date` (last active day) becomes the next local midnight as the exclusive
  UTC end. Open-ended terms have no end bound.
- Plans that intersect the period are included, including ended plans whose
  last-active span still overlaps. Future-only scheduled terms do not accrue
  outside their effective windows.

Example: a $300 September monthly plan with `asOf` at local midnight on
September 9 accrues eight complete days → \(8 × (300 / 30) = \$80\), not $300.

## Tool grouping and shared telemetry

Comparison rows are **groups**, not always one row per logical plan.

- Tool associations follow the same keys as plan management (`codex`,
  `claude-code`, `cursor`, `grok`, `opencode`, `zcode`, `openrouter`, `xai`,
  `antigravity`, `custom`).
- **OpenCode and ZCode** share the canonical Z.AI association (`opencode`). One
  configured Coding Plan is not counted twice because two harnesses recorded
  usage.
- When multiple plans share inseparable telemetry (same tool key or alias
  group), the report shows **one group**:
  - Group configured cost = sum of contributing plan accruals over the period
  - Group usage = each eligible event counted **once**
  - Child plans may list their own accrued costs; they do not each claim the
    full group usage or repeat the multiple
- Usage is matched to the **union** of active intervals for the group. Overlapping
  plans (A: Sep 1–5, B: Sep 4–10 → union Sep 1–10) must not multiply events.
- Usage outside every group’s active intervals, or for a tool with no configured
  plan, appears under **Unassigned / no configured plan**.

## Reference value

Reference value is the API-equivalent dollar amount of measured tokens priced at
**published / documented rates effective at event time**.

Included:

- Events with a defensible computed reference amount (stored
  `computed_cost_usd` or components priced via the packaging `PricingEngine`)
- Genuine measured zeros when a priced model produced zero-value usage

Excluded from the numerator (and explained when relevant):

- Configured plan expense
- Provider-reported invoice / bucket charges used as a fallback “rate”
- Admin or API-only charge streams such as `openai_admin`, `anthropic_admin`,
  and `provider_cost_buckets` — those are not subscription reference value
- Events that cannot be priced at a documented rate (unpriced models,
  incomplete token classes)

Coverage states for a group:

| Situation | Display |
| --- | --- |
| Fully priced usable records | Known subtotal (may be $0 when measured zero) |
| Some priced + some unpriced / incomplete | Partial known subtotal; lower-bound labeling |
| Records exist but nothing is defensibly priced | Unavailable amount; tokens and named gaps retained |
| No usable records in scope | “No recorded usage” — not “measured zero consumption” |

## Reference-value multiple

\[
\text{multiple} = \frac{\text{reference value}}{\text{positive configured accrued group cost}}
\]

Interpretation: how recorded consumption at API-equivalent rates compares to the
fixed plan price accrued over the **same** interval. It is **not** ROI, cash
saved, profit, or a cancel/downgrade instruction.

Availability:

| Condition | Result |
| --- | --- |
| Complete positive numerator and complete positive denominator | Exact ratio (for example `30.00×`) |
| Positive priced subset over a complete positive denominator | Qualified **lower bound** (for example `≥ 30.00×`) |
| Zero / missing denominator, no priced value, or incompatible scope | Unavailable — structured reason, never `∞` or a fake `0×` |

## Pricing coverage vs collection evidence

These are separate signals and must not be collapsed.

- **Pricing coverage** answers: of the usage that *was recorded* for this
  group, how much could be valued at documented rates? Complete vs partial
  (named unpriced models / incomplete telemetry) vs unavailable.
- **Collection evidence** answers: does ingest / source health suggest the
  history for this window was actually captured? Fresh vs stale / limited /
  failing sources.

Fully priced recorded rows with stale or partial collection **do not** claim
complete historical coverage. Unknown models do not hide other priced rows.
Pricing coverage never invents dollars for gaps in collection.

Collection evidence is scoped to relevant configured or contributing sources for
the group's tool association, including shared OpenCode/ZCode sources. Unrelated
failures do not change that group's cost, value, multiple, or collection status.
Unattributed source issues remain separate.

The report keeps the latest attempt separate from the last successful import.
Freshness is calculated at the request's captured `asOf`; absent dates cannot
establish recent health. Disabled, missing, never-observed, stale, partial, and
failed collection are distinct. For **Last month**, collection status is a current
snapshot, not proof that the previous month's history was completely captured.
“Why this number?” shows relevant source dates and safe reasons alongside known
record exclusions; an excluded invoice charge is not an unpriced-token gap.

Row and footer **Check connection** actions open existing connection help while
keeping the parent view and plan draft intact. Closing help restores focus to the
clicked button, or to the active dialog heading if that button is no longer usable.

## “Why this number?”

Each comparison row can expand a component breakdown. Typical contents:

- Exact period bounds and the applicable active intervals
- Contributing plans and the configured-cost formula (cadence, daily rates,
  overlap fractions)
- Included tools / sources and why they were associated (including OpenCode /
  ZCode sharing)
- Reference-rate basis, unpriced models, and incomplete telemetry
- Freshness and historical-coverage limits from source health
- The multiple formula, or the reason no ratio is available

Links from the report point back to plan management and connection /
diagnostics screens when you need to fix terms or collection.

## Known limitations and verification boundaries

Plans & Value assumes:

- Configured terms and tool associations you entered are the expense side of the
  comparison
- Local and permitted ingest produced the usage events under comparison
- Documented YAML rates at event time are a fair API-equivalent counterfactual

It does **not** prove:

- That you paid (or were refunded) the configured amount
- Account-level billing attribution or multi-seat invoice truth
- That unused capacity should be cancelled or that high multiples are “savings”
- Completeness of provider history you never collected
- That admin API charges equal personal subscription value

Verification stays inside the comparable interval, Decimal calendar accrual, and
the priced subset that was actually recorded. Treat the multiple as a labeled
comparison figure with stated coverage — not a bank or provider statement.
