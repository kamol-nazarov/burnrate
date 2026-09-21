# BURNRATE 0.3.0-beta.7

This reliability beta prevents an old Grok Build billing snapshot from being
shown as a current quota percentage. A local Grok Build or experimental Traycer
quota observation must be no more than 15 minutes old before BURNRATE shows
its percentage. A fresh empty weekly period remains an exact 0%; an older
snapshot is shown as unavailable until a current observation is available.

When OpenCode usage records show a Grok model, the Grok Build capacity card now
states that this activity was observed separately. OpenCode does not expose a
Grok Build allowance or account linkage, so that activity never supplies or
changes the Grok Build percentage.
