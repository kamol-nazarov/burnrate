# Providers

This matrix describes the shipped sources, with the Unreleased compatibility changes below taking precedence over older format details. Every source distinguishes usage, quota, reported charges, credentials and mutation risk.

## Unreleased provider compatibility repairs

Format evidence is pinned to [Tokscale a3209ff](https://github.com/junhoyeo/tokscale/tree/a3209ff03da1b71262a4dd97bff854c07ca548b3) and [Token Monitor e07aeb4](https://github.com/Javis603/token-monitor/tree/e07aeb449353a6f9f0260292beacc27b643550c0). The latter manifest identifies Tokscale base 4.15.1. MIT notices are retained in `NOTICE`. No reference binaries, personal caches, providers or cache producers were executed during implementation. The CLI originator also appears in [official Codex extraction fixtures](https://github.com/openai/codex/blob/6924ce636b2186948f2332ac9460b5871b50aa2f/codex-rs/state/src/extract.rs).

| Source | Implemented compatibility | Identity, capability and coverage limits |
| --- | --- | --- |
| OpenCode | v1 `message`; v2 `session_message` SQL role/type; legacy message JSON; approved `opencode*.db`; labeled cumulative fallback | Provider/message identity, historical model/time, later revisions. Fine usage consumes proven coarse coverage once in either import order; uncovered remainder stays coarse. Ambiguous provider scope is reported. |
| Codex | Known CLI/Desktop originators; approved active/archive roots; cumulative, per-request and total-only rollout evidence | Repeated totals do not add last usage twice. Old IDs map through exact aliases; inherited fork history requires child-turn evidence. Total-only stays unsplit. Complete contiguous counter chains refine accepted coarse intervals without adding another baseline. Ambiguous overlapping fragments are refused, preserving accepted history. |
| Claude Code | `CLAUDE_CONFIG_DIR`, projects/transcripts and approved subagents | Message/request/provider identity, later increases/decreases and partial-to-complete updates. Omitted optional fields do not erase known components. No quota inferred from transcripts. |
| Grok / Traycer | Unified log, selected updates/signals, Traycer projections | Historical model is session/process-generation scoped, not global. Native usage within the persisted unified interval is suppressed; signal remainder stays unsplit. Native totals are refused when unmapped Traycer Grok history already exists, even if Traycer is disabled. Traycer uses logical chat/event IDs and old path aliases. Custom profiles, including `GROK_HOME`, retain overlap conflicts. |
| ZCode | Legacy/modern `model_usage`; selected `.zcode/projects` transcripts | Computed totals select evidenced component conventions. No text estimates or current-model guesses. Shared usage IDs reconcile copies; cross-format overlaps without shared IDs are refused in either import order. Missing billing scope is reported. |
| Cursor local | Existing SDK-agent store | Stable agent/run identity with old aliases; incompatible stores fail visibly. The existing pre-2026-09-02 authority restriction remains. This is not complete IDE/CLI coverage. |
| Cursor CSV / JSON | Native CSV cache-write columns and selected `usageEventsDisplay` JSON | Actual request ID plus matching explicit user/team scope reconciles Admin/export/CSV copies and preserves old IDs. Other observations remain source-local. `Cost`/`totalCents` are reference values; only charged fields become reported charges. Unknown cache fields stay incomplete. |
| Antigravity | Manual populated Tokscale sync artifacts, or existing experimental RPC | The inspected producer writes persisted usage metadata; caches are not self-populating or guaranteed complete. Response IDs reconcile cache/RPC copies. Missing producer/cache/runtime is a prerequisite issue. Rows without response identity are reported and skipped. No producer execution, new authentication flow or TLS exception. |
| OpenAI / Anthropic Admin | Existing official usage/cost APIs and vault bindings | Bounded pages, cursor/schema/deadline failures. Seven-day revisit of completed UTC hours; closed-day charge buckets remain separate. Delays/corrections beyond that window remain possible. |
| Cursor Admin | Existing documented team API; numeric-string/ISO timestamps; pagination | Fresh reporting observations update stable identities. Repeated/mismatched/exhausted pages fail visibly. Team admin key remains required; reference cost is not an extra charge. |
| OpenRouter | Existing management credits endpoint | Balance-only. Deposits, top-ups and credits never become token events. Usage still comes from supported harness evidence. |

All managed local bindings remain **usage-only**, including new paths. Existing narrow bindings are not widened by discovery. Unknown models remain usage evidence; missing pricing does not imply a disconnected source. Unsupported schemas, source failures and ambiguous copies are reported rather than converted to zero-use success.

Official contracts checked for reporting changes: [OpenAI Usage](https://platform.openai.com/docs/api-reference/usage), [Anthropic Usage and Cost](https://platform.claude.com/docs/en/manage-claude/usage-cost-api), and [Cursor Admin](https://prod.cursor.com/docs/account/teams/admin-api). Each reporting loop has a 100-page and 20-second deadline, with requests limited to the remaining budget up to ten seconds. No immediate retry is made for authentication, throttling or bad pages. Quota backoff does not shorten a longer provider Retry-After. An access test is not evidence of complete billing history.

### Optional Claude status-line snapshot

The [official schema](https://code.claude.com/docs/en/statusline) reports `rate_limits.five_hour` and `rate_limits.seven_day`, with `used_percentage` and epoch-second `resets_at`. Older versions or sessions may omit them. Context-window utilization is never substituted for quota.

If desired, explicitly enable `BURNRATE_ENABLE_CLAUDE_STATUSLINE=1` in both processes and configure Claude's documented status-line command to invoke the installed Python executable with:

```text
-m spend_app.claude_statusline --database "C:\path\to\BURNRATE\spend.sqlite3"
```

Use the actual BURNRATE database path. The helper never opens the database: bounded stdin is reduced to allowlisted metadata and atomically written to a database-scoped file in the adjacent BURNRATE `snapshots` folder. It emits no display text. Preserve/back up an existing status-line configuration; the helper neither edits settings nor wraps commands. Leaving an existing status line unchanged and omitting this bridge is supported.

Snapshots expire after 15 minutes and at provider reset. Producer and collector must use the same Claude profile override; the profile is hashed and account identity is not claimed. Managed Claude usage-only bindings still suppress quota, even with this flag enabled. Missing/stale/unsupported snapshots show unavailable, not zero. No native credential is read.

### Upgrade and verification limits

Automatic defaults that do not exist are reported as not detected/skipped;
installing a source later allows the next normal cycle to discover it. A moved
saved binding or missing explicit environment path remains a configured-source
failure. Permission, schema and unexpected I/O failures are not ordinary absence.
Other sources and requested summary warming can still progress.

OpenCode result aggregation preserves accepted/written counts, partial pricing,
quarantine counts and unpriced model lists. Counts describe child import attempts,
not a newly inferred count of unique historical events. ZCode reads an optional
session directory only when available and unambiguous, retaining just its final
project label; absent/unmatched session metadata does not drop valid token rows.
Grok checks file generation and consumed-prefix anchors before parsing appended
complete lines. Traycer uses projection versions within the file/database/binding
scope so another chat's update does not force all projections to be parsed again.
Incomplete report exceptions retain already-read pages for explicit handling;
collection still reports failure and preserves previously accepted history rather
than treating a missing cursor as complete coverage.

The R1–R5 corrections retain explicit Codex request/counter equivalence across
files and replay, including old IDs. Equal amounts or timestamps alone do not
establish that equivalence. OpenCode now persists verified message start times
alongside its existing granular ledger, so completion time is not substituted
for start during reverse reconciliation. Older records without sufficient start
evidence report ambiguous overlap and defer the cumulative checkpoint, including
its logical/path progress, until source replay provides the missing evidence.

Claude field values retain their individual revisions across files, batches
and saved state. Omissions preserve known fields and one-hour cache writes;
explicit zeros and downward corrections remain effective. Equal-authority
conflicts and invalid duration totals remain incomplete rather than being
resolved by maximum counts. ZCode transcript totals (`total`, `totalTokens`,
`total_tokens`) constrain component attribution: a contradictory 15-token
report remains 15 unclassified tokens, while an evidenced additive 21-token
report remains 21. These changes use compatible optional metadata in `app_meta`,
not a new SQL schema. The older implementation test results remain historical;
the corrections have their own focused fake-boundary regression evidence.

There is no SQLite schema migration. Versioned identity/allocation metadata uses existing `app_meta` and ingest transactions. Exact-ID repairs can update or remove proven duplicate priced/unpriced rows. Before release, back up and validate real SQLite atomicity/recovery; older code ignores the ledgers and can reintroduce duplicates if restarted on repaired data.

Evidence here is fake-boundary unit evidence only. Relocated stores, WAL/rotation/junction behavior, scheduler concurrency, OS vault and snapshot permissions, installed dependencies/assets, browser focus, package contents and live provider scopes remain unverified. Unknown identity/account/format combinations remain explicit coverage gaps. No frontend assets were changed or generated for these backend repairs.

BURNRATE is not affiliated with these vendors. Interfaces change; experimental lanes can fail independently.

**Requesty is not connected** and is not a rate authority. Direct xAI API usage is not ingested.

Claude OAuth credential refresh is **opt-in** (`BURNRATE_CLAUDE_OAUTH_REFRESH=1`). All other local readers are read-only.

Paths below use Windows conventions. `%USERPROFILE%` is `~`. `%APPDATA%` is typically `%USERPROFILE%\AppData\Roaming`. `%LOCALAPPDATA%` is typically `%USERPROFILE%\AppData\Local`.

## Stability labels

| Label | Meaning |
| --- | --- |
| **Supported local** | Observed official local files; read-only; used in default ingest |
| **Experimental** | Undocumented or unofficial interface; may break without a BURNRATE release |
| **Opt-in official** | Vendor admin/usage API; skipped until you set a key |
| **Opt-in import** | Operator drops a file; not scheduled |
| **Not connected** | No adapter, no host, no SQLite rows |

## Compact matrix

| Source | Local files (patterns) | Network host | Credential | Interface | SQLite | Semantics | Mutation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Codex usage | `%USERPROFILE%\.codex\sessions\**\*.jsonl` | none | none | Official local Codex Desktop telemetry (observed JSONL) | `usage_events` `source=codex_local` `tool_key=codex`; `sessions` | Tokens exact; spend exact if YAML `exactness: exact` | Read-only. Idle Codex UI is not live. |
| Codex quota | newest verified `limit_id=codex` weekly snapshot in the configured sessions root (tail 2 MB, 40 newest files, 5,000-entry traversal budget) | none | none | Observed local telemetry | `quotas` `provider_key=codex` `limit_key=weekly` `source=codex_local_telemetry` unit `pct` | Observed main-pool % and reset; unavailable without verified identity, after reset, or after six hours | Read-only |
| Codex live | `%USERPROFILE%\.codex\state_5.sqlite`, `thread_history_1.sqlite` | none | none | Observed local projection DBs | `agent_runs` `id=codex:<thread>` | Live only for in-progress turns | SQLite `mode=ro` |
| Claude Code usage | `%USERPROFILE%\.claude\projects\**\*.jsonl` | none | none | Official local Claude Code transcripts | `usage_events` `source=claude_local` `tool_key=claude-code`; `sessions` | Tokens exact (incl. 5m/1h cache writes); spend exact | Read-only. Full JSONL lines are parsed; **prompts are not persisted**. |
| Claude quota (Desktop) | `%APPDATA%\Claude\plan-usage-history.json` or `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\plan-usage-history.json` | none | none | Observed Claude Desktop cache (compact % only) | `quotas` `source=claude_desktop_history` 5h + weekly unit `pct` | Exact % if sample ≤45 min old; **resets unavailable** (not invented) | Read-only |
| Claude quota (Traycer) | Traycer `chat.db` + `%USERPROFILE%\.traycer\cli\bin\traycer.exe` | none (local process) | Traycer epic/agent env from newest chat | **Experimental** `traycer.exe agent profile-rate-limits claude --profile ambient` | `quotas` `source=traycer_profile` | Exact % + reset if CLI returns `available` | Spawns CLI (`CREATE_NO_WINDOW`, 20 s, 15 min breaker). No Traycer file writes. |
| Claude quota (OAuth) | `%USERPROFILE%\.claude\.credentials.json` | `platform.claude.com` (refresh); `api.anthropic.com` (`/api/oauth/usage`) | Claude Code OAuth access + refresh | **Experimental / undocumented.** **Off unless** `BURNRATE_CLAUDE_OAUTH_REFRESH=1` | `quotas` `source=claude_oauth_usage` | Exact utilization + `resets_at` | **Writes provider credentials** on refresh (atomic replace) |
| Claude live | tail of project JSONL (~1 MiB) | none | none | Inferred from event envelopes | `agent_runs` `id=claude:<session>` | Live only while last assistant event is non-terminal. Idle editor is not live. Message text is not stored. | Read-only |
| Cursor local history | `%USERPROFILE%\.cursor\projects\**\sdk-agent-store\*\index.db` | none | none | Observed local agent-store | `usage_events` `source=cursor_local` only if `occurred_at < 2026-09-02T00:00:00Z` | Tokens exact; spend derived | SQLite `mode=ro` |
| Cursor usage service | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` (`cursorAuth/accessToken`) | `api2.cursor.sh` `POST …/DashboardService/GetFilteredUsageEvents` | Local Cursor bearer | **Experimental / undocumented** DashboardService | `usage_events` `source=cursor_usage_service` from 2026-09-02 on | Tokens exact; `cost_usd` from charged cents; computed spend derived | Token sent only to Cursor. Does not write `state.vscdb`. |
| Cursor quota | same `state.vscdb` | `api2.cursor.sh` POSTs: `GetCurrentPeriodUsage`, `GetPlanInfo`, `GetHardLimit` | same bearer | **Experimental / undocumented** | `quotas` Cursor Models + Other Models `source=cursor_usage_service` unit `pct`. Included-value dollars are **read but not persisted**. | Exact % from service; 15 min active / 1 h idle | Read token; no file write |
| Cursor live | same `state.vscdb` `composerHeaders` + `cursorDiskKV` | none | none | Observed local Composer state | `agent_runs` `id=cursor:<composerId>` | Live only if status is `generating` / `running` / `in_progress` and not archived | SQLite `mode=ro` |
| Cursor CSV | checkout `data/imports/cursor/*.csv`, or `CURSOR_IMPORT_PATH` | none | none | Official export drop (CLI `backfill cursor-csv`) | `usage_events` `source=cursor_csv` | Tokens as exported; spend derived | Read-only CSVs. Not scheduled. Installed runs should set `CURSOR_IMPORT_PATH` (for example `%LOCALAPPDATA%\BURNRATE\imports\cursor`). |
| Cursor admin | none | `api.cursor.com` `POST /teams/filtered-usage-events` | `CURSOR_API_KEY` Basic `key:` | Official Admin API | `usage_events` `source=cursor_admin` | Tokens exact; `cost_usd` from `chargedCents`; computed derived | No local file write. Skipped unless key set. |
| OpenCode usage | `%USERPROFILE%\.local\share\opencode\opencode.db` | none | none | Observed local session aggregates | `usage_events` `source=opencode_local` `tool_key=opencode`; skips `providerID=traycer-openrouter` | **Session-level** tokens (not per-turn); spend derived | SQLite `mode=ro` |
| Z.AI quota (OpenCode + ZCode) | OpenCode `auth.json` `zai-coding-plan.key`, else ZCode `%USERPROFILE%\.zcode\v2\config.json` | `api.z.ai` `GET /api/monitor/usage/quota/limit` | Coding Plan API key (`Authorization` header, no `Bearer` prefix) | Documented as official Z.AI Coding Plan quota | `quotas` `provider_key=opencode` 5h + weekly unit `credits` `source=zai_quota_endpoint` | Exact used/limit/%/reset. One card for both harnesses. | Key not persisted. Files read-only. |
| ZCode usage | `%USERPROFILE%\.zcode\cli\db\db.sqlite` `model_usage` ⨝ `session` | none | none | Official ZCode metrics DB | `usage_events` `source=zcode_local` `tool_key=zcode`; only `builtin:zai-coding-plan` / `builtin:bigmodel-coding-plan`, `status=completed` | Per-request tokens exact; spend derived. Message/part/I/O tables never queried. | SQLite `mode=ro` |
| ZCode live | same DB `turn_usage` | none | none | Official metrics | `agent_runs` `id=zcode:<session>:<turn>` | Live only for `status=running` and `completed_at` NULL | SQLite `mode=ro` |
| Grok Build usage | `%USERPROFILE%\.grok\logs\unified.jsonl` (`shell.turn.inference_done`) | none | none | Observed grok CLI log | `usage_events` `source=grok_local` `tool_key=grok` `model_key=supergrok:…`; `cost_usd=NULL` | Tokens exact; spend derived. Authoritative vs Traycer Grok from first log timestamp. | Read-only; byte-offset, reread on truncate. |
| Grok Build quota | same log `billing: fetched credits config` (tail 1 MiB); fallback Traycer CLI `profile-rate-limits grok` | none by BURNRATE if the local snapshot is current | none local; Traycer env on fallback | Local snapshot observed; Traycer path **experimental** | `quotas` `source=grok_local_billing` or `traycer_profile` weekly unit `pct` | Exact % + reset. Empty new week → exact 0. Snapshot older than period → unavailable (not reused). | Read-only log. Traycer CLI spawn on fallback only. |
| Grok Build live | `%USERPROFILE%\.grok\active_sessions.json` + `%USERPROFILE%\.grok\sessions\**\<sid>\summary.json` | none | none | Observed CLI registry | `agent_runs` `id=grok:<sid>` | Live only if pid still running **and** a file in the session dir is newer than 5 min. Idle grok shell is not live. | Read-only; pid probe is `OpenProcess` / `os.kill(0)` |
| Direct xAI | none | none | none | **No adapter** | none | Coverage target only so diagnostics can show “no telemetry.” Quota unavailable by design. | None |
| Antigravity usage | `%APPDATA%\Antigravity\logs\main.log` (port + CSRF) | `https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/{GetAllCascadeTrajectories,GetCascadeTrajectory}` | ephemeral localhost CSRF (`x-codeium-csrf-token`) | **Experimental** undocumented localhost gRPC-Web (`verify=False`) | `usage_events` `source=antigravity_local` `tool_key=antigravity`; tokens only | Tokens exact from generator usage; spend derived. Trajectory fetched then reduced; **prompt/tool/file content is not persisted** but may transit the RPC response. | CSRF never persisted. TLS verify disabled on localhost. |
| Antigravity quota | same log | localhost `RetrieveUserQuotaSummary` | same CSRF | **Experimental** localhost RPC | `quotas` gemini/3p 5h+weekly `source=antigravity_local_rpc` unit `pct` | Exact remainingFraction → usedPct | Same as usage |
| Antigravity live | none extra | localhost trajectory summaries | CSRF | **Experimental** | `agent_runs` `id=antigravity:<cascade>` | Live only for `CASCADE_RUN_STATUS_BUSY` / `RUNNING` | Same RPC |
| OpenRouter usage | Traycer `chat.db` projections with `harnessId=openrouter` | none | none | Observed Traycer structured usage | `usage_events` `source=traycer_local` `tool_key=openrouter` `model_key=openrouter:…` | Tokens classified (subset vs additive); incomplete → coverage gap, not `$0`. Computed derived; promo YAML rows close on `effective_to`. | SQLite `mode=ro` |
| OpenRouter quota | none | `openrouter.ai` `GET /api/v1/credits` | `OPENROUTER_MANAGEMENT_KEY` (dedicated; inference key not reused) | Official credits API | `quotas` `limit_key=balance` unit `usd` `is_payg=1` `source=openrouter_credits_api`; remaining USD in `used` | Exact remaining = total_credits − total_usage. Unavailable without key. | Key not persisted. No file write. |
| Traycer usage | `%USERPROFILE%\.traycer\host\epic-state\**\chat\chat.db` | none | none | Observed chat projections | `usage_events` `source=traycer_local` only harnesses `grok` and `openrouter`. Grok rows skipped when `occurred_at >= grok_local.coverage_start` | See OpenRouter / historical Grok. Claude/Codex/Cursor via Traycer are **not** ingested here. | SQLite `mode=ro` |
| Traycer activity | same `chat.db` + `traycer.exe agent list` | none | Traycer epic/agent env | **Experimental** CLI, only while a turn looks live | `agent_runs` `id=traycer:<chatId>` `live` or `no_data` | Live = `turn.started` and CLI `active=true` (15 s cache). If CLI missing, fallback `lifecycle.state=active` within 90 s. `turn.stopped` without usage → `no_data`, not live. | Spawns CLI (12 s) only on potential activity. No Traycer file writes. |
| Requesty | none | none | none | **Not connected; forbidden** | none | Must not appear in `pricing/*.yaml` | None |
| OpenAI admin | none | `api.openai.com` `GET /v1/organization/usage/completions` and `/organization/costs` | `OPENAI_ADMIN_KEY` Bearer | Official Usage + Costs APIs | `usage_events` `source=openai_admin` `tool_key=codex`; `provider_cost_buckets` | Tokens exact; cost buckets exact USD; computed spend exact if YAML exact | Skip with no key. Errors redacted. |
| Anthropic admin | none | `api.anthropic.com` `GET /v1/organizations/usage_report/messages` and `/v1/organizations/cost_report` | `ANTHROPIC_ADMIN_KEY` `x-api-key`; `anthropic-version: 2023-06-01` | Official org usage/cost | `usage_events` `source=anthropic_admin` `tool_key=claude-code`; cost buckets | Tokens exact (uncached + cache read, 5m/1h writes); cost cents → USD exact | Skip with no key. Does not touch `.credentials.json`. |

HTTPS timeouts are 15–30 seconds depending on the client. Credential-bearing `httpx` clients use `trust_env=False` and `follow_redirects=False`.

## Double-count rules

| Pair | Rule |
| --- | --- |
| Codex Desktop vs Traycer Codex | Local ingest keeps `originator = "Codex Desktop"` only. Traycer-launched Codex is not in `traycer_local.INGESTED_HARNESSES`. |
| OpenCode vs Traycer OpenRouter | OpenCode skips `providerID=traycer-openrouter`. Per-turn authority is Traycer. |
| Grok Build vs Traycer Grok | Native `unified.jsonl` wins from the earlier of the current log-head timestamp and any already-persisted `grok_local` `occurred_at`. Traycer Grok rows on/after that instant are skipped, so CLI log rotation cannot reopen history grok_local already stored. |
| Cursor Admin vs DashboardService ingest | If `CURSOR_API_KEY` is set, experimental `cursor_usage_service` ingest is skipped. Cursor **quota** still uses DashboardService independently of the admin key. |
| Cursor SDK-agent vs usage service | Cutover `2026-09-02T00:00:00Z`: `cursor_local` is historical only; `cursor_usage_service` owns current events. |
| Cursor CSV vs other Cursor sources | Operator import; same `raw_id` upsert is idempotent, but CSV is not a substitute for the usage service. |
| OpenCode vs ZCode quota | One Z.AI Coding Plan card (`provider_key=opencode`). Two harnesses, one quota. |
| Reasoning ⊂ output | OpenCode (and any schema that says so) displays reasoning as detail; it is not added to processed total again. |
| Direct xAI vs Grok Build | Direct xAI is not ingested. SuperGrok uses `supergrok:` model keys and derived xAI list rates. |

## Network allowlist (opt-in or experimental)

| Host | Paths | Who |
| --- | --- | --- |
| `https://api.openai.com` | `/v1/organization/usage/completions`, `/v1/organization/costs` | OpenAI admin |
| `https://api.anthropic.com` | `/v1/organizations/usage_report/messages`, `/v1/organizations/cost_report`; experimental `/api/oauth/usage` | Anthropic admin; Claude OAuth |
| `https://platform.claude.com` | `/v1/oauth/token` | Claude OAuth refresh (opt-in) |
| `https://api.cursor.com` | `/teams/filtered-usage-events` | Cursor admin |
| `https://api2.cursor.sh` | `/aiserver.v1.DashboardService/{GetFilteredUsageEvents,GetCurrentPeriodUsage,GetPlanInfo,GetHardLimit}` | Cursor usage/quota **experimental** |
| `https://openrouter.ai` | `/api/v1/credits` | OpenRouter management key |
| `https://api.z.ai` | `/api/monitor/usage/quota/limit` | Z.AI Coding Plan |
| `https://127.0.0.1:<port>` | `/exa.language_server_pb.LanguageServerService/{GetAllCascadeTrajectories,GetCascadeTrajectory,RetrieveUserQuotaSummary}` | Antigravity **experimental** |

No arbitrary URL can be supplied to a credential-bearing client. There is no `GET /api/v1/models` live fetch; OpenRouter prices are transcribed YAML.

## Per-source notes

### Codex Desktop — supported local

Scheduler glob: `%USERPROFILE%\.codex\sessions\**\*.jsonl`.

Usage fields from `event_msg` / `token_count` / `last_token_usage`: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, optional `reasoning_output_tokens`. File signature cache `(size, mtime_ns)` skips unchanged JSONL.

Quota: first window with `window_minutes >= 7*24*60` becomes “Codex weekly window.” Only weekly is persisted.

Live: `inprogress`/`running`, `completed_at` NULL, not archived, updated within 6 hours.

Invoice-exact: yes (`EXACT_USAGE_SOURCES` + `pricing/openai.yaml`).

Codex capacity uses only explicitly identified `codex` quota-pool observations with
one validated 10,080-minute window, in either window position. Model-specific
pools (including `codex_bengalfox`), unknown/missing IDs and credit-only updates
cannot establish general Codex capacity. Percentages are used percentages;
a new genuine zero or downward correction is accepted. Conflicting observations
at the same timestamp are unavailable rather than guessed.

The registry's `CODEX_HOME` override controls the sessions root. Scans remain
confined to that root and skip links/junctions. Budget exhaustion does not cause
a broader scan. A profile directory is a filesystem scope, not proof of account
identity; switching accounts within it cannot be independently authenticated
from this metadata. No provider or credential read is performed.

Source observation time is retained separately from polling time in versioned
`app_meta` metadata, atomically with quota persistence. Rereading or touching a
file does not refresh it. Snapshots older than six hours or past their reported
reset become unavailable; no replacement reset/zero is invented. Previously
persisted values without main-pool provenance stay unavailable until the next
valid poll. Existing quota and measured usage history is preserved; no SQL
schema migration is required. Older binaries ignore this metadata.

Managed local connections still grant usage only. Disabled or usage-only Codex
bindings suppress quota, including old persisted readings, without falling back
to another profile. Local quota permission for managed bindings is not offered
in this version. Missing telemetry does not require a paid turn to finish setup.

### Claude Code — supported local (OAuth quota experimental)

Three quota collectors, in order: Desktop history, Traycer ambient profile, OAuth usage (opt-in).

OAuth is the only path that can mutate a provider file. Refresh uses `POST https://platform.claude.com/v1/oauth/token` with `grant_type=refresh_token`, then `os.replace` of a temp file next to `.credentials.json`. The opt-in client presents as Claude Code’s public OAuth client (not a BURNRATE-issued client id). Public default does **not** refresh unless `BURNRATE_CLAUDE_OAUTH_REFRESH=1`.

Live-session rule: a trailing `user` event without a terminal `assistant` `stop_reason` in `{end_turn, stop_sequence, max_tokens, refusal}`. An open editor with no in-flight turn does not qualify.

### Cursor — mixed

Four ingest paths, one quota path, one activity path. `api.cursor.com` is official. `api2.cursor.sh` DashboardService is undocumented → **experimental**. If `CURSOR_API_KEY` is set, the experimental DashboardService ingest is skipped so official admin events are not double-counted with `cursor_usage_service`.

Included plan dollars (`usedUsd`/`limitUsd`) are parsed and **dropped** by the quota product contract; they are not persisted as a capacity card.

CSV mapping (headers matched tolerantly):

| Normalized CSV column | Usage field |
| --- | --- |
| date / timestamp | `occurred_at` |
| model | `cursor:<model>` |
| input without cache write tokens | fresh input (stored input = fresh + cache read) |
| cache read tokens | `cached_input_tokens` |
| cache write tokens | `cache_write_tokens` |
| output tokens | `output_tokens` |
| cost usd / charged cents | `cost_usd` |
| event id | `raw_id` `cursor-csv:<id>` |

### OpenCode and ZCode — supported local

OpenCode is session-row aggregates (`tokens_*`, `cost`). A long session updates one `raw_id` and can move `occurred_at` with `time_updated`.

ZCode is one `model_usage` row per completed Coding Plan request. Message/part/I/O retention tables are never queried.

Spend is derived published GLM rates (`pricing/zai.yaml`), not credit invoices.

### Grok Build — experimental local (log format)

The grok CLI log is observed, not a documented stable schema. Direct xAI (`api.x.ai`) has no client.

Live sessions require both a live pid and session-dir activity within 5 minutes.

### Antigravity — experimental localhost RPC

Port and CSRF are scraped from the tail of `%APPDATA%\Antigravity\logs\main.log` (`Local: https://127.0.0.1:<port>/` and `--csrf_token`). `httpx` uses `verify=False` and `trust_env=False` on that localhost HTTPS.

Usage RPC uses `trajectoryVerbosity = 2` (`PROD_UI`). Adapter keeps model, timing, opaque ids, and numeric usage. Full trajectory JSON is not written to SQLite. Privacy residual: content may be present in the localhost response before reduction.

Spend is derived from `pricing/google.yaml` (Google published Gemini API rates). That is not an Antigravity bill. Official quota CLI docs exist; runtime uses the undocumented language-server RPC.

### OpenRouter — Traycer usage + official credits

Usage is Traycer-only (no OpenRouter SDK ingest). Quota is `GET https://openrouter.ai/api/v1/credits` with `OPENROUTER_MANAGEMENT_KEY`. Remaining funds are stored in `quotas.used` with unit `usd` and `is_payg=1`. No allowance is invented.

### Traycer — local store + experimental CLI

CLI invocations:

```text
traycer.exe --json --quiet --no-progress --no-bootstrap agent profile-rate-limits <claude|grok> --profile ambient
traycer.exe --json --quiet --no-progress --no-bootstrap agent list
```

`TRAYCER_EPIC_ID` / `TRAYCER_AGENT_ID` come from the newest `chat_projection` row. Failure opens a 15-minute circuit breaker. That spawn is **not** a live session.

### Admin APIs — opt-in official

Empty keys skip and record `ingest_runs.status=skipped` with a named reason. Scheduler still *calls* the OpenAI and Anthropic ingest functions (they return skipped); Cursor admin is not invoked unless `CURSOR_API_KEY` is set.

Window: last 2 hours, 15-minute job. Cursor documents an hourly Admin API; overlap is the mitigation.

`public_error()` redacts `bearer`, `basic`, `sk-…`, and `x-api-key` shapes before persistence.

## Live-session exclusions

Opening an editor or idle shell is never enough:

- Codex: completed or archived threads ignored
- Grok: dead pid or session files older than 5 minutes ignored
- Cursor: non-generating Composer ignored
- Claude: terminal `stop_reason` clears live
- Traycer: `turn.completed` is not live; `no_data` is diagnostics-only
- ZCode: only `turn_usage.status=running`
- Antigravity: only BUSY/RUNNING summaries

## What is retained vs discarded

Persisted identity is opaque ids, model keys, project directory names, token counts, and optional reported USD. See [privacy.md](privacy.md) for the column list.

Content that may enter process memory and is not stored: Claude JSONL lines, Traycer `projection_json`, Antigravity `GetCascadeTrajectory` PROD_UI payload, Cursor Composer JSON in `state.vscdb`.
