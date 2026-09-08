"""Bounded source inspection. Never returns source content or discovers credentials."""
import csv
import fnmatch
import json
import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

APPROVED_ROOT = ContextVar("connection_root", default=None)
APPROVED_REVISION = ContextVar("connection_revision", default=0)
APPROVED_PATTERNS = ContextVar("connection_patterns", default=None)


class LocationError(ValueError):
    pass


class SampleLimit(LocationError):
    pass


def normalize(raw, meta):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 2048:
        raise LocationError("Enter a local folder or file on the computer running BURNRATE.")
    raw = raw.strip()
    if (raw.startswith(("\\\\", "//")) or "://" in raw or
            re.search(r'[\x00-\x1f*?\[\]<>|]', raw) or "fakepath" in raw.lower() or
            "$" in raw or (":" in raw[2:]) or "%" in raw):
        raise LocationError("Use a local path, not a URL, network/device path, command or wildcard.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LocationError("Use an absolute path or ~/ home path.")
    if path.name.lower() == meta.home_name.lower() and meta.child:
        path = path / meta.child
    try:
        path = path.resolve(strict=True)
    except PermissionError:
        raise LocationError("Permission denied. Grant this user read access.") from None
    except OSError:
        raise LocationError("Location is missing or moved. Choose its current location.") from None
    if str(path).startswith(("\\\\", "//")):
        raise LocationError("Network locations are not supported.")
    if identity(path) in {identity(Path(path.anchor)), identity(Path.home())}:
        raise LocationError("Select the harness folder, not a whole drive or user home.")
    if path.is_file():
        allowed = {"sqlite": {".db", ".sqlite"}, "grok": {".jsonl", ".json"}, "zcode": {".db", ".sqlite", ".jsonl"}, "opencode": {".db", ".sqlite", ".json"}, "csv": {".csv", ".json"}, "jsonl": {".jsonl"}}[meta.shape]
        if path.suffix.lower() not in allowed:
            raise LocationError("Choose the expected database, usage log or CSV file for this source.")
    if not meta.suffix and meta.shape not in {"opencode", "zcode", "grok"} and not path.is_file():
        raise LocationError("This source requires its database or log file, not a home folder.")
    if meta.suffix and not (path.is_file() or path.is_dir()):
        raise LocationError("Expected a readable folder or source file.")
    return str(path)


def identity(path):
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def confined(path, root=None):
    root = root or APPROVED_ROOT.get()
    if not root:
        return Path(path)
    base = Path(root)
    resolved = Path(path).resolve(strict=True)
    if identity(base.resolve(strict=True)) != identity(base):
        raise LocationError("The saved location changed. Recheck it before collecting.")
    if resolved != base and (base.is_file() or not resolved.is_relative_to(base)):
        raise LocationError("A source link points outside the approved location.")
    return resolved


@contextmanager
def approved(root, revision=0, patterns=None):
    token = APPROVED_ROOT.set(root)
    revision_token = APPROVED_REVISION.set(revision)
    patterns_token = APPROVED_PATTERNS.set(patterns)
    try:
        yield
    finally:
        APPROVED_ROOT.reset(token)
        APPROVED_REVISION.reset(revision_token)
        APPROVED_PATTERNS.reset(patterns_token)


def cache_identity(database, path, parser_version="0.3.0"):
    return (identity(Path(database).resolve()), identity(Path(path).resolve()), APPROVED_REVISION.get(), parser_version)


def file_signature(path, stat=None):
    path = confined(path)
    stat = stat or path.stat()
    with path.open("rb") as stream:
        first = stream.read(4096)
        stream.seek(max(0, stat.st_size - 4096))
        last = stream.read(4096)
    return (stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0), getattr(stat, "st_ctime_ns", 0), hashlib.blake2b(first + last, digest_size=16).hexdigest())


def matches_parts(parts, pattern, *, prefix=False):
    """Fixed provider patterns; unlike fnmatch('*'), a segment cannot cross '/'."""
    if not parts:
        return prefix or not pattern or all(p == "**" for p in pattern)
    if not pattern:
        return False
    if pattern[0] == "**":
        return matches_parts(parts, pattern[1:], prefix=prefix) or matches_parts(parts[1:], pattern, prefix=prefix)
    return fnmatch.fnmatchcase(parts[0], pattern[0]) and matches_parts(parts[1:], pattern[1:], prefix=prefix)


def source_files(root, suffix, budget=100000):
    base = Path(root)
    confined(base, root)
    if base.is_file():
        yield base
        return
    visited = 0
    patterns = [p.split("/") for p in ((suffix,) if isinstance(suffix, str) else suffix)]
    stack = [base]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > budget:
                    raise SampleLimit("Inspection limit reached; the sample does not establish whether usage history is present.")
                # Never traverse links/junctions. Canonical files are checked again at open.
                if entry.is_symlink() or Path(entry.path).is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    relative = Path(entry.path).relative_to(base).parts
                    if any(matches_parts(relative, pattern, prefix=True) for pattern in patterns):
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    relative = Path(entry.path).relative_to(base).as_posix()
                    if any(matches_parts(relative.split("/"), pattern) for pattern in patterns):
                        yield confined(entry.path, root)


def opencode_files(root, budget=100000):
    root = Path(root)
    patterns = ("**/*.json",) if root.name == "message" else ("message/**/*.json",) if root.name == "storage" else ("opencode*.db", "storage/message/**/*.json")
    return source_files(root, patterns, budget)


def adapter_files(pattern):
    root = APPROVED_ROOT.get()
    if not root:
        import glob
        return glob.glob(pattern, recursive=True)
    relative = str(pattern).replace("\\", "/")[len(str(root).replace("\\", "/")):].lstrip("/")
    return [str(p) for p in source_files(root, APPROVED_PATTERNS.get() or relative or "*")]


def transcript_patterns(source, root, fallback="**/*.jsonl"):
    root = Path(root)
    names = ("sessions", "archived_sessions") if source == "codex_local" else ("projects", "transcripts")
    home_name = ".codex" if source == "codex_local" else ".claude"
    broader = root.name == home_name
    if not broader:
        for name in names:
            child = root / name
            if not child.is_symlink() and not child.is_junction() and child.is_dir():
                broader = True
    return [name + "/**/*.jsonl" for name in names] if broader else [fallback]


class Inspector:
    def normalize(self, raw, meta):
        return normalize(raw, meta)

    def inspect(self, source, path, meta):
        if source == "opencode_local":
            from spend_app.adapters.opencode_schema import inspect_source
            return inspect_source(path)
        if source == "antigravity_local":
            from spend_app.adapters.antigravity_cache import files, read_file
            try:
                found, usable, issues = 0, False, []
                for file in files(path, budget=256):
                    rows, errors = read_file(file, limit=16)
                    found += 1
                    usable |= bool(rows)
                    issues.extend(errors)
                    if found == 3:
                        break
                if not found:
                    raise LocationError("No populated Antigravity sync artifacts. Create the cache with its separate producer first.")
                return {"state": "readable", "usableSample": usable, "detail": "Populated cache only; completeness depends on its separate producer." + (" Some sampled records have unavailable identity or invalid metadata." if issues else "")}
            except OSError:
                raise LocationError("Antigravity cache is unavailable or unreadable.") from None
        try:
            patterns = transcript_patterns(source, path, meta.suffix or "*") if source in {"codex_local", "claude_local"} else [meta.suffix or "*"]
            if source == "zcode_local":
                patterns = ["cli/db/db.sqlite", "projects/**/*.jsonl"] if Path(path).name == ".zcode" else ["**/*.jsonl"]
            found = 0
            usable = False
            if source == "grok_local":
                from spend_app.adapters.grok_native import files
                candidates = files(path, budget=256)
            else:
                candidates = source_files(path, patterns, budget=256)
            for file in candidates:
                if found >= 3:
                    break
                found += 1
                if meta.shape == "sqlite" or source == "zcode_local" and file.suffix.lower() != ".jsonl":
                    with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                        db.execute("PRAGMA query_only=ON")
                        db.set_progress_handler(lambda: 1, 10000)
                        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{meta.table}")')}
                        if source == "zcode_local":
                            from spend_app.adapters.zcode_schema import select_rows, parse_row
                            try:
                                records, modern = select_rows(db, limit=3)
                            except ValueError:
                                raise LocationError("The database has an incompatible ZCode schema.") from None
                            usable |= any(parse_row(record, modern)[0] is not None for record in records)
                            continue
                        if not set(meta.columns) <= columns:
                            raise LocationError("The database has an incompatible source schema.")
                        # Metadata-only columns; no prompts, projections or credential documents.
                        column = meta.columns[0]
                        predicate = ""
                        if source == "opencode_local":
                            predicate = " WHERE COALESCE(tokens_input,0)+COALESCE(tokens_output,0)>0"
                        elif source == "cursor_local":
                            predicate = " WHERE usage_json IS NOT NULL"
                        elif source == "zcode_local":
                            predicate = " WHERE status='completed' AND COALESCE(input_tokens,0)+COALESCE(output_tokens,0)>0"
                        usable |= db.execute(f'SELECT "{column}" FROM "{meta.table}"{predicate} LIMIT 1').fetchone() is not None
                else:
                    with file.open("rb") as handle:
                        sample = handle.read(131072)
                    if source == "grok_local" and file.name == "signals.json":
                        from spend_app.adapters.grok_native import signal_row
                        from datetime import datetime, UTC
                        try:
                            usable |= signal_row(json.loads(sample), file.parent.name, datetime.now(UTC)).unclassified_tokens > 0
                        except ValueError:
                            raise LocationError("Incompatible Grok signal metadata.") from None
                        continue
                    if meta.shape == "csv" and file.suffix.lower() == ".json":
                        from spend_app.adapters.cursor_export import parse_export
                        if file.stat().st_size > len(sample):
                            return {"state": "readable", "usableSample": None, "detail": "Export exceeds the bounded verification sample. Full schema and usage remain unverified until collection."}
                        try:
                            rows, errors = parse_export(json.loads(sample))
                        except ValueError:
                            raise LocationError("Expected a supported Cursor account export.") from None
                        usable |= bool(rows)
                        continue
                    if meta.shape == "csv":
                        header = next(csv.reader(sample.decode("utf-8-sig").splitlines()), [])
                        if not header:
                            continue
                        from spend_app.adapters.cursor_csv import resolve_columns
                        if not {"timestamp", "model"} <= resolve_columns(header).keys():
                            raise LocationError("Expected a Cursor usage CSV export.")
                        usable |= len(sample.splitlines()) > 1
                    else:
                        recognized = not sample.strip()
                        for line in sample.splitlines()[:64]:
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(row, dict):
                                continue
                            kind = row.get("type")
                            if source == "codex_local":
                                recognized |= kind in {"session_meta", "event_msg", "response_item", "turn_context"}
                                usable |= kind == "event_msg" and isinstance(row.get("payload"), dict) and row["payload"].get("type") == "token_count"
                            elif source == "claude_local":
                                recognized |= kind in {"user", "assistant", "summary", "system", "progress", "file-history-snapshot"}
                                usable |= kind == "assistant" and isinstance(row.get("message"), dict) and bool(row["message"].get("usage"))
                            elif source == "zcode_local":
                                from spend_app.adapters.zcode_transcript import reduce_records
                                recognized |= row.get("role") in {"user", "assistant", "system"}
                                usable |= bool(reduce_records([row])[0])
                            elif source == "grok_local":
                                from spend_app.adapters.grok_native import reduce_updates, signal_row
                                from spend_app.adapters.grok_records import reduce_records
                                recognized |= "msg" in row and "sid" in row or isinstance(row.get("params"), dict)
                                usable |= bool(reduce_updates([row])[0] or reduce_records([row])[0])
                                if file.name == "signals.json":
                                    from datetime import datetime, UTC
                                    recognized = "totalTokens" in row
                                    if recognized:
                                        try:
                                            usable |= signal_row(row, file.parent.name, datetime.now(UTC)).unclassified_tokens > 0
                                        except ValueError:
                                            raise LocationError("Incompatible Grok signal metadata.") from None
                        if not recognized:
                            raise LocationError("The sample is not a supported usage log.")
                if found >= 3:
                    break
            return {"state": "readable", "usableSample": bool(usable), "patterns": patterns, "detail": ("Verified readable." if usable else "Verified readable; no usable history in the bounded sample. You can connect and wait for activity.") + (" Approved subroots: " + ", ".join(p.split("/")[0] for p in patterns) if len(patterns) > 1 else "")}
        except SampleLimit:
            return {"state": "readable", "usableSample": None, "sampleLimited": True, "detail": "The bounded sample was exhausted. History and full source shape remain unverified; collection will report its own result."}
        except PermissionError:
            raise LocationError("Permission denied. Grant this user read access.") from None
        except (OSError, sqlite3.Error):
            raise LocationError("Source unavailable or incompatible. Check its location and permissions.") from None
