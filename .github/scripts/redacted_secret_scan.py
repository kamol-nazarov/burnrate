#!/usr/bin/env python3
"""Redacted secret scan for CI.

Reports filename, rule ID, and a redacted snippet only. Never prints a full
secret value. Synthetic pytest fixtures under tests_spend/ that match
ASSIGNED_SECRET are allowlisted by path+rule (fixture values are not copied
into this file). Any other match fails the process.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    ".venv-312",
    ".venv-spend",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "data",
    "logs",
    "run",
    "backups",
    "validation",
    "bin",
    "downloads",
    "plugins",
    "work",
    "test-results",
    "playwright-report",
    ".qa-shots",
    ".qa-visual",
}

SKIP_FILES = {".env"}
SKIP_SUFFIXES = {".db", ".db-shm", ".db-wal", ".pyc", ".pyo", ".log"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("AWS_ACCESS_KEY_ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_PAT", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("GITHUB_FINE_GRAINED", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("OPENAI_PROJ_KEY", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("XAI_KEY", re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b")),
    ("OPENROUTER_KEY", re.compile(r"\bsk-or-[A-Za-z0-9_-]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("JWT_LIKE", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("PRIVATE_KEY_HEADER", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("GENERIC_BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._\-+=/]{24,}\b")),
    (
        "ASSIGNED_SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|token|access[_-]?key)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
    (
        "ENV_ASSIGN_NONEMPTY",
        re.compile(
            r"(?m)^(ANTHROPIC_ADMIN_KEY|OPENAI_ADMIN_KEY|CURSOR_API_KEY|"
            r"OPENROUTER_MANAGEMENT_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|XAI_API_KEY)=.+"
        ),
    ),
]


def redact(value: str) -> str:
    if len(value) <= 8:
        return "***REDACTED***"
    return f"{value[:4]}…{value[-2:]} (len={len(value)})"


def iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".venv")]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            if any(name.endswith(suf) for suf in SKIP_SUFFIXES):
                continue
            out.append(Path(dirpath) / name)
    return out


def scan_file(path: Path, root: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    rel = str(path.relative_to(root)).replace("\\", "/")
    for i, line in enumerate(text.splitlines(), 1):
        for rule_id, pattern in RULES:
            for match in pattern.finditer(line):
                findings.append(
                    {
                        "file": rel,
                        "line": i,
                        "rule": rule_id,
                        "redacted": redact(match.group(0)),
                    }
                )
    return findings


def is_allowlisted(item: dict) -> bool:
    rel = item["file"].replace("\\", "/")
    if rel.startswith("tests_spend/") and item["rule"] == "ASSIGNED_SECRET":
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = iter_files(root)
    findings: list[dict] = []
    for path in files:
        findings.extend(scan_file(path, root))

    allowed = [item for item in findings if is_allowlisted(item)]
    blocked = [item for item in findings if not is_allowlisted(item)]

    lines = [
        "# Redacted secret scan",
        "",
        f"- Root: `{root}`",
        f"- Files scanned: {len(files)}",
        f"- Findings: {len(findings)}",
        f"- Allowlisted (tests_spend ASSIGNED_SECRET): {len(allowed)}",
        f"- Blocking: {len(blocked)}",
        "",
    ]
    if not findings:
        lines.append("No pattern matches.")
    else:
        lines.append("| File | Line | Rule | Kind | Redacted |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in findings:
            kind = "allowlisted" if is_allowlisted(item) else "blocking"
            lines.append(
                f"| `{item['file']}` | {item['line']} | `{item['rule']}` | {kind} | `{item['redacted']}` |"
            )

    report = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    print(f"scanned_files={len(files)} findings={len(findings)} blocking={len(blocked)}")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
