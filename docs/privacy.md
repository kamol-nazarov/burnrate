# Privacy

BURNRATE is local-first. The default install makes **no provider network calls**, binds only `127.0.0.1`, and stores usage metadata — not conversation content.

This page is the disclosure. Pair it with [providers.md](providers.md) for hosts and file patterns.

## What SQLite stores

Column-level, in the BURNRATE database only:

| Kind | Fields |
| --- | --- |
| Identity | `source`, `tool_key`, `model_key`, opaque `session_id` / `raw_id`, project **name** (folder basename, not a full home path) |
| Live cards | `agent_runs.name` may copy a harness session/chat **title** (not the prompt body). Titles appear on `/api/spend/summary` activity rows. |
| Time | `occurred_at`, `ingested_at`, quota `resets_at`, session start/end |
| Tokens | `input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `cache_write_1h_tokens`, `output_tokens`, optional `reasoning_tokens`, `unclassified_tokens` |
| Money | optional provider-reported `cost_usd`, YAML `computed_cost_usd`, `is_exact`, subscription amounts you entered |
| Coverage | `telemetry_complete`, priced vs unpriced flags, gap issue codes |
| Quota | `used`, `allowance`, `unit`, `pct`, `is_payg`, NULL when unknown |
| Operations | ingest `status`, redacted `error`, event counts |

## What is never stored

- Prompts, responses, reasoning text, diffs, attachments
- Tool names with arguments or results
- File contents, source code, clipboard
- Environment variables, `.env` values, API keys, OAuth refresh tokens, CSRF tokens
- Raw admin API bodies, trajectory JSON, Composer documents
- Credential-shaped strings in logs (`bearer`, `basic`, `sk-…`, `x-api-key` are stripped before `ingest_runs.error` is written)

Some adapters parse a larger record in memory (a Claude JSONL line, a Traycer projection, an Antigravity PROD_UI trajectory, a Cursor Composer blob) and then keep only the metadata above. The extra content is discarded.

## Local file access

Harness stores are read-only:

- SQLite: `file:<path>?mode=ro` plus `PRAGMA query_only=ON`
- JSONL / logs / CSV: open for read (`r` / `rb`)
- No writes to Codex, Claude transcripts, Cursor `state.vscdb`, OpenCode, ZCode, Grok logs, Traycer `chat.db`, or Antigravity logs

**Exception (opt-in):** if `BURNRATE_CLAUDE_OAUTH_REFRESH=1`, the Claude OAuth fallback may atomically replace `%USERPROFILE%\.claude\.credentials.json` when the access token is missing, expired, or the usage endpoint returns 401. Default is off. Unrelated JSON keys are preserved. A lock plus compare-before-replace tries not to clobber a newer Claude writer. This still rotates the refresh token when Anthropic issues a new one.

Traycer CLI (`profile-rate-limits`, `agent list`) is a local process spawn, not a file write. BURNRATE captures stdout JSON and does not feed prompts. Side effects inside `traycer.exe` are outside BURNRATE’s control; the spawn is **experimental**.

## Network

Default: none.

Opt-in destinations are HTTPS only, host-allowlisted, timed out, and not inference completions:

| When | Host | Credential |
| --- | --- | --- |
| `OPENAI_ADMIN_KEY` set | `api.openai.com` | Bearer admin key |
| `ANTHROPIC_ADMIN_KEY` set | `api.anthropic.com` | `x-api-key` |
| `CURSOR_API_KEY` set | `api.cursor.com` | Basic `key:` |
| Cursor usage / quota (experimental) | `api2.cursor.sh` | local Cursor bearer from `state.vscdb` |
| `OPENROUTER_MANAGEMENT_KEY` set | `openrouter.ai` | dedicated management key |
| Z.AI Coding Plan key found on disk | `api.z.ai` | Coding Plan API key |
| `BURNRATE_CLAUDE_OAUTH_REFRESH=1` | `platform.claude.com`, `api.anthropic.com` | Claude Code OAuth tokens |
| Antigravity running (experimental) | `https://127.0.0.1:<port>/` | ephemeral localhost CSRF |

Empty admin keys skip with a named reason, exit 0, and never log the key. Credential-bearing HTTP clients set `trust_env=False` so `HTTP_PROXY` cannot redirect secrets.

BURNRATE does not post prompts or chat completions to a model endpoint.

## Bind and browser

- Default listener: `127.0.0.1:17331`. Not `0.0.0.0`.
- No third-party analytics, no Google Fonts CDN, no browser calls to provider APIs.
- CSP and the usual security headers are on. The dashboard reads `/api/spend/summary` and `/api/spend/health` only — persisted SQLite, not live provider probes.

Optional Tailscale Serve is an operator choice and is never enabled by BURNRATE. Funnel is not a product feature. See [tailscale.md](tailscale.md).

## Credentials

| Rule | Detail |
| --- | --- |
| In memory only | Keys are read from the environment or from the provider’s own local store |
| Never in SQLite | Not in `usage_events`, `quotas`, or `ingest_runs` |
| Never in API JSON | Including `/api/spend/limits` error strings (redacted) |
| Never in doctor output | Paths may be generic; secrets are not echoed |
| OpenRouter | Inference key ≠ `OPENROUTER_MANAGEMENT_KEY` |
| Cursor | Admin key ≠ the signed-in DashboardService bearer |
| Claude | Desktop history and Traycer quota are preferred; OAuth refresh is opt-in |

`GET /api/spend/limits` can fire every quota collector, including experimental Claude refresh and Traycer CLI. Keep it off any public reverse proxy.

## How to wipe

1. Stop the dashboard (`burnrate` process, or `scripts/stop-burnrate.ps1` / `scripts/uninstall-burnrate.ps1`).
2. Delete the data directory (`%LOCALAPPDATA%\BURNRATE`, or the repo-relative DB in a checkout).
3. Uninstall does **not** delete `*.db` unless you pass `-PurgeData`.

Deleting BURNRATE’s database does not delete Codex, Claude, Cursor, or other harness history. Those files were only read.
