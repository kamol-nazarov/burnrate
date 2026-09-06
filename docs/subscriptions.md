# Configured subscriptions

Use **Manage subscriptions** on the dashboard to add plans, schedule changes,
inspect term history, record an ending, or explicitly correct an entry.
Adding plans is optional and requires no provider credentials.

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

**Schedule a new price** preserves earlier terms. To repair a historical typo,
choose **Correct historical entry**, inspect the affected-period preview, and
explicitly confirm it. A stale editor receives a conflict; reload history before
retrying. Failed saves keep form fields and their retry identity. Use **Add another
plan** to intentionally add a second identical plan. Permanent deletion is unavailable.

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
