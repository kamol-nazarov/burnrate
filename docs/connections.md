# Connect harness

Open **Connect harness** from the dashboard. Setup is optional and can be
closed and reopened. **Manage subscriptions** remains a separate screen.

1. Choose **Auto-detect (recommended)** or **Choose manually**.
2. Review the source, location, capabilities and bounded verification result.
3. Select **Connect selected** or **Connect** to save the binding.
4. Open **Connection status** to see the current binding revision and import status.

Auto-detect runs only when requested. It checks known local locations; it does
not scan drives, read credentials, launch harness commands or test providers.
When both the default and a previously saved location exist, select the one to
use. Each selected source saves independently. Retry leaves successful items
alone and reuses the uncertain item's request identifier.

Locations belong to the **computer running BURNRATE**, not the browser's
computer. Enter an absolute local path or a `~/` home-relative path. Spaces
and Unicode are supported. Uploads, URLs, network shares, device paths and
user-defined wildcards are not supported. Resolved locations are stored;
linked children outside the approved source are not followed.

## Local capabilities

| Source | Accepted location examples | Managed capabilities |
| --- | --- | --- |
| Codex | `~/.codex`, its `sessions` or `archived_sessions` folder, or a session `.jsonl` | Usage |
| Claude Code | `~/.claude`, its `projects` or `transcripts` folder, or a transcript `.jsonl` | Usage |
| Cursor local (experimental) | `~/.cursor`, its `projects` folder, or an `index.db` run store | Usage |
| Traycer (experimental) | `~/.traycer`, its `host/epic-state` folder, or a `chat.db` | Usage |
| OpenCode | `~/.local/share/opencode`, `opencode*.db`, or `storage/message` JSON | Usage |
| ZCode | `~/.zcode`, `cli/db/db.sqlite`, or `projects` transcripts | Usage |
| Grok Build (experimental) | `~/.grok`, unified log, or session `updates.jsonl` / `signals.json` | Usage |
| Cursor export | A CSV folder, `.csv` file, or selected `usage*.json` account export | Usage |

The existing activity/quota readers do not have a custom-file contract.
Managed local bindings therefore grant **usage only** and suppress those
default-profile readers. A readable transcript never proves quota access.
Antigravity's experimental RPC remains externally configured. A separate manual
location on that source can replace RPC with a populated Tokscale sync cache
(`antigravity-cache/sessions` or one `.jsonl`). This path never runs the producer,
reads its credentials, or appears in Auto-detect. Missing artifacts are a
prerequisite issue. Native credential/private-service integrations remain
externally managed; this wizard does not offer new logins for them.
Existing unmanaged collection and consent settings remain in effect.

Custom Grok/Traycer locations cannot be activated together without a safe
existing cutoff rule. The wizard explains the conflict instead of guessing
which source is authoritative. This version does not support multiple accounts
per source; existing session/event identities and mirror suppression remain
the authority for deduplication. Moving an OpenCode store preserves the existing
logical session watermark while keeping physical-location progress separate.

## Verification and connection status

Verification checks existence, read access, expected schema/shape and a small
metadata sample (at most three files and 256 directory entries). It does not
import all history or return file contents. For very broad trees, select a
narrower source folder. An empty readable source can be connected without
running paid inference.

- **Already monitored / externally managed:** existing default collection or
  an explicit environment setting; not a claim that this location was verified.
- **Verified readable:** a bounded inspection passed, possibly without usable history.
- **Saved, awaiting first ingest:** saved settings are waiting for the existing cadence.
- **Receiving usage:** this binding revision actually imported usage, including
  unpriced usage. Pricing and quota remain separate.
- **Waiting for activity:** an import completed without usable usage yet.
- **Needs attention:** recheck location, permissions, schema or provider access.
- **Disabled:** future collection through the managed binding is stopped.

Use **Recheck**, **Change location**, **Disable**, or **Reconnect** in Connection
status. Changing a location verifies the replacement before saving it. A failed
replacement retains the previous binding. A failed recheck marks that existing
binding as needing attention. Rescan proposes candidates without applying them.

Saves take effect on the next eligible existing collection cycle; they do not
create a new worker or restart the server. A collection already in progress
finishes before a save is accepted. Old revisions cannot report a new binding
as receiving. Disconnect preserves measured history and does not uninstall a
harness, cancel a plan, revoke external keys or remove source files.

Explicit environment credentials and `CURSOR_IMPORT_PATH` remain externally
managed. The wizard does not rewrite `.env`. Resolve an external override in
its original configuration before managing that source here.

## Documented provider APIs

Choose **Add provider API** separately. These are read-only reporting
integrations, not consumer subscription logins.

| Connection | Credential requirement | Capability |
| --- | --- | --- |
| OpenAI Admin | Organization Admin API key with organization usage and cost access | API usage and reported charges |
| Anthropic Admin | Claude Console organization Admin API key with usage/cost reporting access | API usage and reported charges |
| Cursor Admin | Team Admin API key created by a team admin in dashboard settings | Team usage and reported cost fields |
| OpenRouter | Management key with credits access | Account balance only; no usage-event coverage |

See the official [OpenAI administration reference](https://platform.openai.com/docs/api-reference/admin-api-keys),
[Anthropic usage/cost requirements](https://platform.claude.com/docs/en/manage-claude/usage-cost-api),
[Cursor Admin API](https://prod.cursor.com/docs/account/teams/admin-api), and
[OpenRouter credits requirements](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits).

**Test and connect** explicitly makes at most two bounded requests to the fixed
documented reporting endpoints, testing usage and cost access separately where
applicable. It never runs inference, follows redirects, paginates history or
automatically retries authentication/rate-limit failures. Scheduled collection
continues to use the existing adapters and cadence.

Keys are entered in password fields and stored only in Windows Credential
Manager through the explicit keyring Windows backend. SQLite holds opaque
BURNRATE-owned references, not keys. No key is placed in `.env`, browser
storage, URLs or status responses. Secret fields clear after save or close;
nonsecret location drafts survive recoverable errors. Replace a credential in
Connection status. Disconnect deletes only BURNRATE-owned credentials; failed
cleanup is recorded for retry without enabling a broken binding.

If secure storage is unavailable, API persistence is unavailable and the UI
explains why. Local connections continue to work. The wizard never borrows
Claude/Cursor sessions, refreshes another app's token or enables experimental
native-credential consent flags.

## Access and development notes

Connection settings and probes use the existing API bearer authentication and
same-origin protections. If prompted, enter the configured **BURNRATE access
token** in the dialog; it remains only in memory until close. Saving uses JSON,
a non-simple header, exact origin/host validation and a 16 KiB payload limit.
Closing a dialog cannot undo a submitted save; reopening reloads persisted state.

Nonsecret binding records and retry receipts use a versioned entry in the
existing `app_meta` table. There is no schema migration or additional database.
Older application versions do not honor these bindings; do not downgrade a
managed installation without reviewing the prior automatic collection settings.

This implementation has focused unit evidence with fake boundaries. Real
Windows vault behavior, installed assets, browser focus/layout, live provider
scopes, SQLite failure recovery and scheduler concurrency require separate
authorized release validation. Do not treat these units as runtime validation.

## Compatibility repair notes (Unreleased)

New explicit Codex/Claude home selections include only their known active and
archive/transcript subroots. Existing saved sessions/projects paths stay narrow;
rescan never widens them. `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `GROK_HOME` and
`XDG_DATA_HOME` are visible overrides. A selected child within an explicit root
is permitted; a conflicting profile is rejected without rewriting `.env`.
Grok/Traycer overlap is also rejected when `GROK_HOME` selects another profile.

A bounded sample can report that its limit was exhausted. This means history
or complete shape is unverified, not zero usage. Successful usage with an
unpriced model stays connected. Parser issues and ambiguous copies appear in
source health; a cycle that accepted no valid evidence cannot establish health
for a new binding. Historical data is preserved when a connection is disabled.

Cursor local keeps the existing pre-2026-09-02 SDK authority restriction; it
is not complete IDE/CLI coverage. Use a supported account export or Admin feed
for later coverage. Plain CSV `Cost` and JSON `totalCents` are reference values;
only explicit charged fields become reported charges. Exports without proven
shared request/account identities cannot establish cross-feed deduplication.
Do not combine overlapping account exports and Admin history without matching
provider event IDs and scope; no numeric-only matching is performed.

Admin polling revisits seven days of completed UTC hours. Daily charge requests
cover closed UTC days, separately from token events. This permits bounded late
corrections but does not promise complete historical/billing coverage. Malformed,
repeated or exhausted pages fail visibly and preserve previously accepted data.
A successful Test and connect still tests access only.

See [the provider repair matrix](providers.md#unreleased-provider-compatibility-repairs)
for precise format, identity and prerequisite limits.
