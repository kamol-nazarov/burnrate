# Security Policy

## Supported versions

Security fixes are accepted for the current public beta line.

| Version | Supported |
| --- | --- |
| 0.1.x-beta | Yes |

## Reporting a vulnerability

Report vulnerabilities **privately**. Do not file public GitHub issues with secrets, credentials, tokens, session files, `.env` contents, database dumps, real telemetry, or exploit details.

Once this project is published on GitHub, **open a private GitHub security advisory on the published repository**. That is the intake channel. There is no separate public-issue requirement, and there is no placeholder mailbox.

Please include:

- A short description of the issue and who it affects
- Reproduction steps that do **not** need live provider credentials
- Affected version or commit, if known
- Whether any secret may already have been exposed (**do not paste the secret**)

You should receive an acknowledgement after maintainers see the advisory. Please give maintainers time to investigate and ship a fix before any public disclosure.

## Scope

In scope: BURNRATE application code, packaged frontend, helper scripts, tests, and docs that ship with this repository.

Out of scope:

- Provider outages, provider billing disputes, and third-party product bugs
- Issues that appear only after an operator binds BURNRATE to a non-localhost address
- Reports that depend on someone else's credentials or session files

## Defaults we treat as security-relevant

- The default listener is `127.0.0.1`.
- Prompts, responses, diffs, and tool payloads must not be stored.
- Credentials must not be logged, returned by the API, or copied into SQLite.
- Provider network calls and credential refresh are opt-in, not default-on.
