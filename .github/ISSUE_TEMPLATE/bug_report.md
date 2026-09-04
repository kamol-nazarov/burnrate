---
name: Bug report
about: Report a BURNRATE defect using synthetic or redacted evidence only
title: ""
labels: []
---

## Privacy (required)

Do **not** paste any of the following into this issue:

- `.env` files or environment dumps
- Provider API keys, OAuth tokens, cookies, or session files
- Prompts, completions, tool calls, or other model content
- Telemetry dumps, session JSONL, SQLite databases, or log files from a live install
- Real project names, account emails, Tailscale hostnames, or personal filesystem paths

Describe the problem with synthetic fixtures or redacted screenshots. If a secret was exposed, rotate it and use private vulnerability reporting instead of a public issue.

## What happened

## What you expected

## Steps to reproduce (synthetic data only)

1.
2.
3.

## Environment

- BURNRATE version:
- Python version (expect 3.12):
- OS:
- Install method (`pip`, checkout, scheduled task):

## Provider / data notes

- Which providers were enabled (names only, no keys):
- Was this reproduced with in-repo `tests_spend/fixtures/` or another synthetic dataset?

## Logs (redacted)

Paste only status text after secrets have been stripped. No `.env`, no tokens, no prompt bodies.
