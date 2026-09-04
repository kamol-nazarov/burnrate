# Contributing to BURNRATE

Thanks for helping improve a local-first dashboard for AI coding-agent usage.
Please read this file and the [Code of Conduct](CODE_OF_CONDUCT.md) before
opening a pull request.

This project is licensed under Apache-2.0. By contributing, you agree that
your contribution is licensed under Apache-2.0.

## Development setup

- CPython 3.12 on Windows is the supported contributor environment. CI is
  Windows + Python 3.12.
- Create a virtual environment, install the project in editable/dev mode, and
  copy `.env.example` to `.env` with empty optional keys.
- Run the suite from the repository root:

  ```text
  python -m pip install -e ".[dev]"
  python -m pytest
  ```

- Public CLI commands are `burnrate init`, `burnrate doctor`, `burnrate serve`,
  `burnrate subscription add`, and `burnrate subscription list`.
- Default listener is `127.0.0.1:17331`. Do not bind `0.0.0.0` in tests or
  examples.

Application code lives in `spend_app/`, the dashboard in `spend_web/`, tests in
`tests_spend/`, and effective-dated price cards in `pricing/`.

## Privacy

BURNRATE stores usage metadata only. Contributions must not introduce real
telemetry, prompts, responses, diffs, tool payloads, credentials, hostnames,
Tailscale names, or personal filesystem paths.

- Do not commit `.env`, provider credential files, SQLite databases, or session
  transcripts.
- Tests, fixtures, screenshots, and docs use synthetic Northwind-style names
  and round numbers. Replace recorded usage with fixtures before opening a PR.
- Never log credentials, never return them from the API, and never copy them
  into SQLite.
- Local adapters stay read-only (`mode=ro` for SQLite, no writes to harness
  files). Do not refresh third-party OAuth tokens unless the operator sets an
  explicit opt-in environment variable; the public default is off.
- Opening an editor or shell is not a live session. Active-session signals
  need an explicit running state or a documented recent-activity field.

If you find a secret or a real prompt in the tree, do **not** file a public
issue. Follow [SECURITY.md](SECURITY.md).

## Measurement integrity

Missing data must stay missing. Do not estimate a measured value.

- Preserve token classes: fresh input, cache read, cache write, output, and
  reasoning when the source distinguishes them. Do not conflate them.
- Do not double-count reasoning tokens when the source already includes them
  in output.
- `exact` requires an invoice-authoritative usage source **and** an
  `exactness: exact` price card. Published-rate math is `derived` / equivalent,
  never a bill. Subscription proration is not provider billing.
- Incomplete token components must not invent a cache rate. Cache savings is
  null/partial when any cached event is unpriced or incomplete.
- Unavailable, unknown, unpriced, and `$0` are distinct. Missing quotas and
  missing spend render as unavailable, never as zero.
- Keep raw provider/harness identity on stored events. Display aliases must
  not rewrite raw rows.
- Cross-provider duplicate suppression must be deterministic and tested.

## Provider compatibility

BURNRATE reads local files and optional documented APIs. It does not sit on
the inference path.

- Do not proxy, reroute, or rewrite provider requests.
- Do not add provider logos, official brand glyphs, or “official” / “certified”
  language.
- Prefer documented local files and official HTTPS hosts. Allowlist hosts on
  credential-bearing HTTP clients; set `trust_env=False`.
- A new adapter should fail independently. Core aggregation must not grow a
  new provider conditional for each source.
- PAYG providers must not invent an allowance.

## Experimental labels

Undocumented or unofficial interfaces stay **experimental** in code, docs, and
the provider matrix until there is a documented, stable contract.

Treat at least these as experimental unless that contract exists:

- Cursor `api2.cursor.sh` usage service
- Anthropic OAuth usage snapshots
- Antigravity localhost RPC
- Traycer CLI / chat-database readers that are not a documented public API
- Grok Build log scrape

Experimental providers may break without a major version bump. Label them, isolate
their failures, and do not let an experimental outage look like zero usage.

## Tests are hermetic

- No live network except `127.0.0.1`.
- No live provider credentials.
- No reads of `%USERPROFILE%` provider trees unless a test opts into a
  `tmp_path` home.
- No Downloads HTML, wall-clock dependence, or “this machine” observations.
- Clock, filesystem, and HTTP boundaries are injected.
- Cover success, skip/unavailable, partial/unpriced, and idempotent re-ingest.

`python -m pytest` from the repo root should collect only this project’s suite.

## Adding a provider

Keep ingest, pricing, capacity, and tests local. Do not validate against a live
provider.

1. Add `spend_app/adapters/<name>.py` with a stable `SOURCE`, an `ingest(...)`
   that returns persisted rows or a skipped/unavailable result, and
   content-derived `raw_id`s. Do not retain prompt or response bodies.
2. Register the adapter with the provider framework (ingest / quota / activity /
   admin capabilities). Scheduler jobs should iterate the registry. Do not add
   one-off provider branches in aggregation.
3. Add `pricing/<provider>.yaml` with official `https://` `source_url` values
   and file-level `exactness` of `exact` or `derived`. Third-party aggregators
   are not pricing authority.
4. If the source is invoice-authoritative, mark it as an exact usage source so
   `is_exact` can only be true when both the source and the card are exact.
5. Register display metadata. Quota collectors belong with the provider spec.
   PAYG sources set `isPayg` and must not invent an allowance.
6. Add an offline fixture under `tests_spend/fixtures/` and tests for success,
   skip/unavailable, partial coverage, and idempotent re-ingest.

## Adding an official effective-dated price revision

Pricing files live in `pricing/*.yaml`. Each price row needs `model_key`,
per-MTok rates (`input`, `cached_input`, `cache_write`, `output`),
`effective_from`, and an `https://` `source_url`. Optional fields include
`aliases`, `effective_to`, long-context multipliers, and cache-write variants.

Revisions for the same `model_key` must not overlap: close the previous card
with `effective_to` equal to the next `effective_from`. `effective_to` is
exclusive. Events before the first `effective_from` stay unpriced; do not
back-date.

Official hosts used by tests include `developers.openai.com`,
`platform.claude.com`, `cursor.com`, `docs.x.ai`, `docs.z.ai`,
`openrouter.ai`, and `ai.google.dev`. Do not cite unofficial resellers.

`exactness: exact` is billed/invoice math. `derived` is published-rate
attribution of included-plan usage and must not be presented as a metered
invoice. When a model is unpriced, store the event as unpriced/partial; do not
invent spend.

## Pull requests

- Keep diffs focused. Do not mix formatting-only churn with behavior changes.
- Update tests and experimental/exact/derived labels in the same change.
- Redact `burnrate doctor` output if you paste it; never paste `.env`.
- Point to `SECURITY.md` rather than discussing a secret in the PR.

Questions about product behavior belong in issues **without** secrets. License
publication is not decided by a PR; it waits for the copyright holder.
