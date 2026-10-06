# Capacity forecast and reset alerts

The "What runs out first" panel keeps the reset countdown and adds a pace line
when a quota window has a known reset and fresh samples.

Pace samples are recorded at most once a minute, including polls that do not
change the quota row, and are kept for 48 hours. The burn rate is the percent
change over the last 60 minutes. A flat or empty hour uses the last 24 hours
instead. Idle minutes are not treated as consumption. Fewer than two samples,
a non-positive rate, or a stale window produces no run-dry time. A flat fresh
window is `pace → comfortable`.

Stale means the newest sample for that reset is older than five minutes, or
older than twice the lane's current cadence when that is longer. Cursor's
active poll is therefore stale at 30 minutes. Windows with no reset (OpenRouter
funds, Cursor's model bars, and Claude when only the desktop history file is
available) are left out.

`hours remaining = (100 − percent used) / percent per hour`. Compared with the
reset:

- after the reset: comfortable, and the bar stays neutral
- inside the last 12 hours before the reset: tight, amber bar
- earlier than that: will run dry, red bar, `pace → dry in ~38h (6h before reset)`

Lanes with a projection sort by that time. Lanes without one keep the previous
percent order behind them. OpenRouter stays last.

## Alerts

A 30-second job fires when percent used is at or above the threshold (default
90, overridable per subscription) and the projection is before the reset.
Each quota window and tier fires once per reset. The optional second tier is
97%. Quiet hours default to 22:00–07:00 in `SPEND_TIMEZONE`. During quiet hours
the alert waits, unless the projection is within two hours. A stale window
does not alert.

The suggestion names another subscription that is still comfortable, using that
tool's majority model from the last 24 hours when one model is more than half
the events. Otherwise it says to ease off until the stressed window resets.

Delivery is the overview banner plus one browser notification after the page
is allowed to notify. Dismissing the banner does not arm the same reset again.
Phone push is not part of this version. Action Center is unchanged.
