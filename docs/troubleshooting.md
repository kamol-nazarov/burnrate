# Troubleshooting

## `burnrate doctor`

```powershell
burnrate doctor
```

Doctor is read-only. It does not ingest, does not migrate, does not create `spend.db`, does not call provider admin APIs, and does not print secrets. Run `burnrate init` first.

Typical checks:

| Check | Pass means |
| --- | --- |
| Python | Interpreter is CPython 3.12 |
| Imports | Runtime packages (`fastapi`, `uvicorn`, `yaml`, …) import |
| tzdata | IANA timezone data loads for `SPEND_TIMEZONE` |
| Database | `spend.db` already exists and `PRAGMA integrity_check` is `ok` (opened read-only) |
| Pricing | Packaged `pricing/*.yaml` cards load |
| Web assets | Packaged `spend_web` HTML/CSS/JS/SVG are present |
| Bind | Default listener is `127.0.0.1:17331` (not a public interface). Doctor does not probe whether the port is free |

Treat doctor output as support data. Redact any remaining absolute paths before pasting it into a public issue.

## Empty dashboard after install

Expected. Local ingest needs harness history (Codex sessions, Claude transcripts, and so on). Until those files exist, KPIs stay `—` and activity is idle. That is not a failed install.

Generate some completed-turn usage in a supported harness, wait about 15 seconds, and refresh. Opening an editor without a turn does not create events.

## `skipped` vs `failed` vs `unavailable` vs `partial`

| Status | Meaning | What to do |
| --- | --- | --- |
| `skipped` | Job did not run; named reason (usually an empty admin key) | Optional: set the key, or ignore |
| `failed` | Job ran and errored; message is redacted | Check that the host is reachable and the key is the *management* key, not an inference key |
| `unavailable` | No trustworthy sample | Missing file, idle quota, or experimental interface down. UI shows `—`, never `$0` |
| `partial` | Some events priced, some not | Look at unpriced models; add a dated price card or wait for telemetry |
| `success` | Events written or quota stored | — |

Admin keys are optional. An empty `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` / `CURSOR_API_KEY` / `OPENROUTER_MANAGEMENT_KEY` is a skip, not a misconfiguration.

## Advanced backfill

`burnrate backfill <source>` is an advanced CLI. It is not required for the five-minute install. Sources:

`codex-local`, `claude-local`, `traycer-local`, `cursor-local`, `cursor-csv`, `opencode-local`, `openai-admin`, `anthropic-admin`, `cursor-admin`.

`cursor-csv` is CLI-only (not scheduled). A source that errors writes a redacted reason and uses exit `2` on `failed`. `partial` writes a reason and exits `0`. Empty keys `skip` and exit `0`. Missing keys are skips, not failures.

## Port 17331 already in use

Another BURNRATE process owns `127.0.0.1:17331`.

```powershell
# if the Windows helpers are installed
.\scripts\status-burnrate.ps1
.\scripts\stop-burnrate.ps1
```

Or stop the `BURNRATE Dashboard` scheduled task / the `burnrate serve` process, then start again. Do not pick a random port: the public default is `17331` so it does not collide with other local stacks.

Confirm liveness with:

```powershell
Invoke-RestMethod http://127.0.0.1:17331/healthz
```

You should see `status: ok`. The dashboard itself is [http://127.0.0.1:17331](http://127.0.0.1:17331).

## Python is not 3.12

BURNRATE requires CPython 3.12. Recreate the venv with `py -3.12 -m venv .venv` and reinstall. 3.11 and 3.13 are not the supported runtime.

## Pricing YAML failed to load

Doctor and `serve` both load `pricing/*.yaml`. A missing file, a non-HTTPS `source_url`, or an invalid `exactness` fails closed: spend is not invented. Reinstall the package so packaged cards are present, or point `SPEND_PRICING_PATH` at a complete cards directory.

Unpriced models stay in diagnostics. They do not become `$0`.

## Experimental provider missing files

Cursor DashboardService, Antigravity localhost RPC, Traycer CLI, Grok Build log scrape, and Claude OAuth usage are **experimental**. If the editor is not installed, the log is absent, or `traycer.exe` is missing, that lane reports unavailable and the rest of BURNRATE keeps running.

Claude OAuth refresh is off unless `BURNRATE_CLAUDE_OAUTH_REFRESH=1`. Prefer Claude Desktop `plan-usage-history.json` or local transcripts.

## Nothing from OpenRouter / Cursor admin / OpenAI / Anthropic

Those paths need keys. Local Traycer OpenRouter usage still ingests from `chat.db` without `OPENROUTER_MANAGEMENT_KEY`; the key is only the credits balance. Cursor CSV is a drop-folder import, not the Admin API.

Direct xAI and Requesty are not connected. There is nothing to repair there.

## `/api/spend/limits` looks different from the dashboard

The dashboard reads SQLite via `/api/spend/summary` and `/api/spend/health`. `/api/spend/limits` is a live probe of quota collectors. Do not call it casually; it can hit experimental endpoints and, if you opted in, Claude OAuth.

## Backup and restore

The supported live path is SQLite’s online backup (`sqlite3.Connection.backup()`), which copies a consistent snapshot without stopping the WAL writer.

If you copy files by hand, stop BURNRATE first and copy `*.db` together with any `*.db-wal` / `*.db-shm` in the same directory (`%LOCALAPPDATA%\BURNRATE` for installed runs).

Restore by replacing those files while BURNRATE is stopped, then `burnrate doctor`.

Uninstall without `-PurgeData` leaves the database in place.

## What not to do

- Do not curl Grafana, Prometheus, an OpenTelemetry Collector, or a tailnet hostname as a BURNRATE health check.
- Do not paste `.env`, provider JSON, or prompt text into issues.
- Do not set Funnel on the dashboard (see [tailscale.md](tailscale.md)).
- Do not treat `≈` published-rate value as an invoice, or `—` as zero.
