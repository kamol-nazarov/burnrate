# BURNRATE

## What BURNRATE is

BURNRATE is a private, local-first dashboard for AI coding-agent token usage, cache efficiency, subscription capacity, and API-equivalent cost.

It reads harness-local files and, if you opt in, a small set of read-only provider admin or quota APIs. It does not sit on the inference path: Codex, Claude Code, Cursor, OpenCode, ZCode, Grok Build, Antigravity, and Traycer keep talking to their providers exactly as they do today. There is no BURNRATE account, no hosted service, and no inference proxy.

The default product is one process: FastAPI + SQLite + a browser dashboard bound to `127.0.0.1:17331`. Grafana, Prometheus, and OpenTelemetry are not part of the install.

- **Windows-first**, CPython 3.12.
- **Localhost-only** by default.
- **Empty credentials are valid.** Local ingest needs no API keys.
- **Missing data stays missing.** Unavailable is rendered `—`, never `$0`.

Version: `0.1.0-beta.1`.

## What the dashboard looks like

The dashboard is a dark, single-page app titled **BURNRATE · AI Cost Intelligence**. After `burnrate serve`, open [http://127.0.0.1:17331](http://127.0.0.1:17331).

A typical overview (synthetic demo data — not a real account):

- **Top nav.** Wordmark, a burn-rate figure, today’s tracked value, and a metering-status chip (live, idle, skipped, or failed).
- **Coverage banner.** Which tools are billed-exact versus published-rate (`≈`). Unpriced usage is named, never filled in.
- **Capacity.** Per-provider meters for “what runs out first” (weekly windows, 5-hour sessions, PAYG remaining credits). Missing quotas stay blank.
- **Activity.** In-progress turns only. Opening an editor or an idle shell does not appear here. Usage totals update when a turn completes.
- **Analysis window.** `15m`, `30m`, `1h`, `3h`, `6h`, `12h`, `1d`, `1w`, `1mo`, `MTD`, `YTD`, `All`.
- **KPI row.** Tracked value, measured tokens, API-equivalent cost per million tokens, cache reuse, session count.
- **Token activity chart.** Stacked by tool or cumulative, with a weekday/hour heatmap below.
- **Recoverable / forecast.** Cache savings versus a no-cache counterfactual, and a labeled forecast of plan cost versus published-rate value.
- **Unit economics.** Cost by model (click through for a tool or model detail page), token mix (cached input / fresh input / output), and any subscriptions you configured.

Public screenshots, when added, always use synthetic Northwind-style names and placeholder figures. They are not captures of a live database.

A fresh install with no harness history shows an empty state, not an error.

## What it measures and does not

**Measures**

- Completed-turn token classes when the harness reports them: fresh input, cache read, cache write (including Anthropic 5-minute / 1-hour writes when distinct), output, and reasoning when it is a separate field.
- Cache hit rate from those measured components: cache-read input ÷ measured total input. Unclassified schemas are excluded from the rate, not estimated.
- Subscription burn you configure yourself (daily proration of the amount you entered).
- Published-rate *equivalents* from effective-dated YAML price cards.
- Capacity snapshots when a quota source exists.

**Does not measure**

- Prompts, responses, diffs, tool payloads, attachments, or source code.
- Inference latency as a spend proxy.
- “You should have used model X” as a fact.
- Spend for traffic with no trustworthy amount.
- Opening an editor, an idle Composer, or an idle shell as a live session.
- Direct xAI API usage or Requesty (neither is connected).

BURNRATE never reroutes completions. Cache behavior of the underlying providers is unchanged.

## Five-minute Windows install

Prerequisites: Windows 10 or 11, [CPython 3.12](https://www.python.org/downloads/), and PowerShell.

From a checkout of this repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install .
burnrate init
burnrate serve
```

Open [http://127.0.0.1:17331](http://127.0.0.1:17331).

`burnrate serve` binds **only** `127.0.0.1:17331`. Empty admin keys are valid: those jobs skip with a named reason and write zero events. Local Codex / Claude Code / OpenCode / ZCode ingest does not need keys.

Optional: copy `.env.example` to `.env` and leave the keys blank. Never commit a populated `.env`.

Installed runs store SQLite and logs under `%LOCALAPPDATA%\BURNRATE`. Set `BURNRATE_DEV=1` to keep data repo-relative for development. A git checkout alone does not change the data directory.

Then run `burnrate doctor` if you want a redacted health report before leaving the dashboard running. See [Troubleshooting](docs/troubleshooting.md).

## Provider compatibility and stability

Official vs experimental is about the *interface BURNRATE uses*, not about whether the product itself is well known. Undocumented endpoints are labeled **experimental** and can fail independently.

| Provider / harness | Interface | Stability | Local files | Network | Tokens / spend |
| --- | --- | --- | --- | --- | --- |
| Codex Desktop | local JSONL + projection SQLite | supported | yes | none | tokens exact; spend exact when the price card is `exact` |
| Claude Code | local JSONL transcripts | supported | yes | none for usage | tokens exact; spend exact when the price card is `exact` |
| OpenCode | local session SQLite | supported | yes | Z.AI quota HTTPS if a Coding Plan key is on disk | session-level tokens; spend derived |
| ZCode | local `model_usage` SQLite | supported | yes | shares the Z.AI quota card | per-request tokens exact; spend derived |
| Cursor signed-in usage | `api2.cursor.sh` DashboardService | **experimental** | bearer from `state.vscdb` | HTTPS to Cursor | tokens exact; spend derived |
| Cursor SDK-agent history | local `index.db` | **experimental** | yes | none | historical cutover only; spend derived |
| Cursor Admin API | `api.cursor.com` | opt-in official | none | HTTPS if `CURSOR_API_KEY` is set | tokens exact; spend derived |
| Cursor CSV export | drop folder | opt-in import | yes | none | as exported; spend derived |
| Grok Build CLI | local `unified.jsonl` | **experimental** | yes | none | tokens exact; spend derived |
| Direct xAI API | — | **not connected** | none | none | unavailable |
| Google Antigravity | localhost language-server RPC | **experimental** | log for port/CSRF | `https://127.0.0.1:<port>/` | tokens exact; spend derived |
| OpenRouter (via Traycer) | Traycer `chat.db` + optional credits API | Traycer path **experimental** | yes | `openrouter.ai` only with a management key | tokens classified; spend derived |
| Traycer CLI quota / activity | `traycer.exe` | **experimental** | `chat.db` + CLI | none (local process) | quota exact if the CLI returns `available` |
| OpenAI admin usage/cost | official Usage + Costs APIs | opt-in official | none | `api.openai.com` if `OPENAI_ADMIN_KEY` is set | exact |
| Anthropic admin usage/cost | official org usage/cost | opt-in official | none | `api.anthropic.com` if `ANTHROPIC_ADMIN_KEY` is set | exact |
| Claude OAuth usage | undocumented `/api/oauth/usage` | **experimental, opt-in** | `~/.claude/.credentials.json` | Anthropic + Claude platform | quota only; off unless `BURNRATE_CLAUDE_OAUTH_REFRESH=1` |
| Requesty | — | **not connected** | none | none | not a rate authority |

Full file patterns, hosts, credentials, mutation risk, and double-count rules: [docs/providers.md](docs/providers.md).

## Privacy and data-flow summary

Default: **no provider network calls** until you enable an admin key or an experimental quota path. The HTTP listener is `127.0.0.1` only.

SQLite stores usage metadata: timestamps, model and provider keys, opaque ids, numeric token classes, priced/unpriced flags, and quota numbers. It does not store prompts, responses, tool arguments, file contents, env vars, or credentials.

Local harness databases are opened `mode=ro` with `PRAGMA query_only=ON`. JSONL and logs are opened for read. The only provider-file write in the product is Claude OAuth token refresh, and that path is **off unless** `BURNRATE_CLAUDE_OAUTH_REFRESH=1`.

Credentials are held in memory, sent only to the matching allowlisted host, never logged, never returned by the API, and never copied into SQLite. OpenRouter inference keys are not reused; balance polling requires `OPENROUTER_MANAGEMENT_KEY`.

The browser loads local CSS/JS only. There is no third-party analytics.

Details: [docs/privacy.md](docs/privacy.md).

## Exact vs derived vs unavailable

| Label | Meaning | UI |
| --- | --- | --- |
| **Exact** | Invoice-authoritative usage source **and** a YAML card with `exactness: exact`. Today that is Codex local / OpenAI admin and Claude local / Anthropic admin. | plain number |
| **Derived** / published-rate | Included-plan attribution at published API rates, or a card marked `exactness: derived`. Not a bill. | `≈` when complete, `≥` when only a priced subset is known |
| **Unavailable** | No trustworthy sample (missing file, skipped key, incomplete telemetry, unpriced model). | `—` — **never `$0`** |
| **Partial** | Some events in the window are priced and some are not. | named as partial |
| **Forecast** | Projection from paced usage. Incomplete pricing is a known-spend floor, not a complete projection. | labeled forecast |

Cache savings is the per-event no-cache counterfactual, not a blended ratio. If any cached event in the window is unpriced or telemetry-incomplete, savings is unavailable rather than silently small.

Reasoning tokens that the harness already includes in output are displayed as detail and are not added twice.

More: [docs/pricing.md](docs/pricing.md).

## Configuration and subscriptions

Optional keys in `.env` (all empty by default):

| Variable | Role |
| --- | --- |
| `OPENAI_ADMIN_KEY` | OpenAI organization usage + cost |
| `ANTHROPIC_ADMIN_KEY` | Anthropic organization usage + cost |
| `CURSOR_API_KEY` | Cursor Admin API |
| `OPENROUTER_MANAGEMENT_KEY` | OpenRouter credits (dedicated; not the inference key) |
| `BURNRATE_CLAUDE_OAUTH_REFRESH` | Set to `1` to allow Claude OAuth token refresh |
| `SPEND_DATABASE_PATH` | SQLite path (default `%LOCALAPPDATA%\BURNRATE`) |
| `SPEND_PRICING_PATH` | YAML cards (packaged default) |
| `CURSOR_IMPORT_PATH` | Cursor CSV drop folder |
| `SPEND_TIMEZONE` | IANA zone for midnight and windows (default `UTC`) |

Fresh `burnrate init` does **not** seed plan prices. Add your own:

```powershell
burnrate subscription add --tool-key codex --name "Codex" --amount-usd 20 --cadence monthly --start-date 2026-09-01
burnrate subscription list
```

`--cadence` is `monthly`, `quarterly`, or `annual`. Daily cost is `amount / days in the period`. That proration is not provider billing and is excluded from API-equivalent fields.

Cursor CSV files dropped into `CURSOR_IMPORT_PATH` are imported on demand (`burnrate backfill cursor-csv` is an advanced command). They are not a live session source.

## Optional Tailscale Serve

BURNRATE never enables Tailscale, Serve, or Funnel. If you already run Tailscale and want the localhost dashboard on your tailnet:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:17331
```

Access URL: `https://<magicdns-name>/` — replace the placeholder with your own MagicDNS name. Funnel (public Internet exposure) is out of scope; see [docs/tailscale.md](docs/tailscale.md).

## Troubleshooting and `burnrate doctor`

```powershell
burnrate doctor
```

Doctor checks CPython 3.12, required imports, timezone data, a read-only SQLite integrity check, packaged pricing YAML, packaged web assets, and that the default bind is loopback. It does not migrate, does not walk provider paths, does not inspect credentials, and does not print secrets. Run `burnrate init` first; doctor will not create `spend.db`.

Common cases:

- Empty dashboard after install — expected until a harness writes usage.
- Admin job `skipped` — empty key; not a failure.
- `unavailable` / `—` — missing telemetry or pricing; not zero.
- Port `17331` already in use — another BURNRATE process is running.
- Experimental provider down — that lane fails independently.

Full legend: [docs/troubleshooting.md](docs/troubleshooting.md).

## Development and test commands

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

`python -m pytest` from the repository root collects only this suite. Tests are hermetic: no live provider credentials, no home-directory harness installs, no Downloads fixtures, no network except `127.0.0.1`.

Optional localhost bench (dev only): `scripts/Bench-Burnrate.ps1` against `http://127.0.0.1:17331`. It prints p50/p95 without a committed baseline file. Do not point it at a tailnet hostname.

See also [CONTRIBUTING.md](CONTRIBUTING.md).

## License and non-affiliation

The project license is **Apache-2.0**. See [LICENSE](LICENSE).

BURNRATE is not affiliated with OpenAI, Anthropic, Cursor, xAI, Google, Z.AI, OpenRouter, Requesty, Traycer, or their related products. Trademarks belong to their owners.

---

Docs: [architecture](docs/architecture.md) · [providers](docs/providers.md) · [privacy](docs/privacy.md) · [pricing](docs/pricing.md) · [Tailscale](docs/tailscale.md) · [troubleshooting](docs/troubleshooting.md)

Community: [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) · [LICENSE](LICENSE)
