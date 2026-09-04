# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is licensed under Apache-2.0.

## [Unreleased]

## [0.1.0-beta.1] - 2026-09-04

Initial public beta of BURNRATE: a local-first Windows dashboard for AI
coding-agent token usage, cache efficiency, subscription capacity, and
published-rate cost equivalents. BURNRATE reads harness-local files and
optional read-only provider APIs. It does not sit on the inference path, does
not require a BURNRATE account, and does not ship Grafana.

### Added

- Installable `burnrate` CLI on CPython 3.12 with `init`, `doctor`, `serve`,
  `subscription add`, and `subscription list`.
- FastAPI + SQLite dashboard bound to `127.0.0.1:17331` by default.
- Empty subscription list on a fresh database. Users add their own plan
  amounts; proration is not provider billing.
- Local, read-only adapters for Codex, Claude Code, OpenCode, and ZCode
  files/SQLite stores. Prompts and responses are not retained.
- Optional, opt-in provider admin and quota readers. Missing credentials skip
  with a named reason and never render as `$0`.
- Experimental adapters for undocumented interfaces, isolated so one failure
  cannot zero out other providers: Cursor usage service, Anthropic OAuth
  usage, Antigravity localhost RPC, Traycer readers, and Grok Build log
  scrape.
- Effective-dated `pricing/*.yaml` cards with `exact`, `derived`, unpriced,
  incomplete, and unavailable kept distinct.
- System/UI font stack. Google Fonts are not bundled and are not loaded from a
  CDN.
- Windows helpers that start, stop, status, install, and uninstall the
  dashboard only. Uninstall does not delete the user database unless
  `-PurgeData` is passed.
- Community files: `LICENSE` (Apache-2.0), `NOTICE`, `SECURITY.md`,
  `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`.

### Security

- Default listener is localhost only.
- Credentials are not logged, not returned by the API, and not copied into
  SQLite.
- Claude OAuth credential-file refresh is off unless an explicit opt-in
  environment variable is set.
- Report vulnerabilities privately; see `SECURITY.md`. Do not file public
  issues with secrets.

### Notes

- This is an unreleased public beta. Experimental provider interfaces may
  change without a major version bump.
- Grafana, Prometheus, and the OpenTelemetry Collector are not part of
  BURNRATE and are not distributed with this beta.
- Licensed under Apache-2.0.
