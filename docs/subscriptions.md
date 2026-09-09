# Configured subscriptions

Open **/mo plans** in the navbar or **Manage** beside Fixed costs. The Fixed
costs panel stays expanded. The dialog opens to a list of every plan, with an
active-term monthly-equivalent total, prices, dates and proportional term bars.
Adding plans is optional and requires no provider credentials.

## Plans & Value

Configured terms are the expense side of the
[Plans & Value](plans-value.md) comparison report. That view accrues the same
calendar-prorated costs over **This month** (month-to-date through `asOf`) or
**Last month** (the full previous calendar month) in `settings.timezone`, then
places them beside API-equivalent reference usage for the identical interval.

Use subscription management to add, schedule, correct, or end plans. Use Plans
& Value only to read the comparison: reference-value multiples, pricing
coverage, and collection evidence. Editing a plan refreshes the report; ending
a plan here still does not cancel the provider subscription. See
[Plans & Value](plans-value.md) for period math, OpenCode/ZCode grouping, and
what the multiple does and does not mean.

**Add a plan** walks through **Tool → Price → Dates**. Choose a tool and name,
enter its price and Month/Quarter/Year cadence, then review its dates before
saving. The price step shows cents-rounded daily and monthly equivalents and
the current total plus the proposed monthly equivalent. OpenCode and ZCode use
the shared Z.AI tool option. Duplicate tool/name combinations are allowed.

Click a row's **Edit** button to reveal **Change price**, **Record end**, and
**View history**. Change price skips Tool and starts with the latest term's
price, with the effective date defaulting to the server's current date. Record
end shows only the inclusive last-active date and does not cancel the provider's
subscription. History shows each term and a **Correct** action; **Correct a
term…** also allows selection of the historical term.

Corrections use Price and Dates, followed by the existing server-generated
before/after preview and affected-day count. Check the confirmation box before
**Confirm correction**. Name and tool corrections remain available in the
expandable section of the Price step. Cancel/Back stays within the dialog;
Close returns focus to the button that opened it.

Amounts are configured cost attribution, not verified payments, provider refunds,
or cash saved. Monthly equivalents are reference figures. Calendar proration uses
the number of days in the calendar month, quarter, or year, including leap years.
Selected-window partial days use the configured timezone and that local day's length.
Money calculations use Decimal; display values are rounded to cents.

Every logical plan has its own ID and version, and a series of nonoverlapping terms.
Dates shown as **last active** are inclusive. A new term beginning September 15
closes its predecessor on September 14. A plan ending September 20 includes
September 20 and excludes later days. Ending a plan does **not** cancel it with
its provider. Future scheduled terms remain in history if the plan ends earlier.
Separate plans can use the same tool. OpenCode and ZCode can share one Z.AI plan;
this does not imply per-account usage attribution.

**Change price** preserves earlier terms. To repair a historical typo,
choose **Correct**, inspect the affected-period preview, and
explicitly confirm it. A stale editor receives a conflict; reload history before
retrying; close and reopen the dialog to reload history. Failed saves keep form
fields and their retry identity. Successful saves return to the list and reload
history. Use **Add a plan** again to intentionally add a second identical plan.
Permanent deletion is unavailable.

Schema 10 preserves existing subscription IDs and inclusive end dates without
inventing revisions. Before upgrading an existing database, BURNRATE creates and
checks a SQLite online backup in the database's adjacent backups folder. A backup
failure prevents the upgrade. Affected daily costs are reconciled transactionally.

Existing CLI add/list commands remain available. Advanced automation can use
`burnrate subscription apply --json '…'` with the same validated operations as
the dashboard. Logical plan IDs and term IDs are distinct after revisions.

Browser writes require JSON, the BURNRATE request header, the same Origin,
and an allowlisted Host. Loopback hosts are allowed by default. If an existing
installation is intentionally accessed through another hostname, configure
`BURNRATE_ALLOWED_HOSTS` as a comma-separated list of those exact hostnames.
This does not create a listener, enable a tunnel, or change provider routing.

For an explicitly configured TLS-terminating loopback proxy, also set
`BURNRATE_ALLOWED_ORIGINS` to the exact external origin (scheme and hostname,
and port if non-default). Hosts must still appear in `BURNRATE_ALLOWED_HOSTS`.
The application does not blindly trust forwarded headers.

The daily materialized table is a reconciled cache. `cost_decimal` preserves the
Decimal daily result; the legacy `cost_usd` REAL field remains for compatibility.
Canonical historical calculations use the effective terms with Decimal.
