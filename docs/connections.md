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
| Codex | `~/.codex`, its `sessions` folder, or a session `.jsonl` | Usage |
| Claude Code | `~/.claude`, its `projects` folder, or a transcript `.jsonl` | Usage |
| Cursor local (experimental) | `~/.cursor`, its `projects` folder, or an `index.db` run store | Usage |
| Traycer (experimental) | `~/.traycer`, its `host/epic-state` folder, or a `chat.db` | Usage |
| OpenCode | `~/.local/share/opencode` or its `opencode.db` | Usage |
| ZCode | `~/.zcode` or `cli/db/db.sqlite` | Usage |
| Grok Build (experimental) | `~/.grok` or `logs/unified.jsonl` | Usage |
| Cursor CSV | A local export folder or `.csv` usage export | Usage |

The existing activity/quota readers do not have a custom-file contract.
Managed local bindings therefore grant **usage only** and suppress those
default-profile readers. A readable transcript never proves quota access.
Antigravity's local service and native credential/private-service integrations
remain externally managed; this wizard does not offer them as new connections.
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
