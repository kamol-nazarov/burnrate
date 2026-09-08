"""Bounded source inspection. Never returns source content or discovers credentials."""
import csv
import fnmatch
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

APPROVED_ROOT = ContextVar("connection_root", default=None)
APPROVED_REVISION = ContextVar("connection_revision", default=0)


class LocationError(ValueError):
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
        allowed = {"sqlite": {".db", ".sqlite"}, "csv": {".csv"}, "jsonl": {".jsonl"}}[meta.shape]
        if path.suffix.lower() not in allowed:
            raise LocationError("Choose the expected database, usage log or CSV file for this source.")
    if not meta.suffix and not path.is_file():
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
def approved(root, revision=0):
    token = APPROVED_ROOT.set(root)
    revision_token = APPROVED_REVISION.set(revision)
    try:
        yield
    finally:
        APPROVED_ROOT.reset(token)
        APPROVED_REVISION.reset(revision_token)


def cache_identity(database, path):
    return (identity(Path(database).resolve()), identity(Path(path).resolve()), APPROVED_REVISION.get())


def source_files(root, suffix, budget=100000):
    base = Path(root)
    confined(base, root)
    if base.is_file():
        yield base
        return
    visited = 0
    stack = [base]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > budget:
                    raise LocationError("Inspection limit reached. Select a narrower source folder.")
                # Never traverse links/junctions. Canonical files are checked again at open.
                if entry.is_symlink() or Path(entry.path).is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    relative = Path(entry.path).relative_to(base).as_posix()
                    patterns = (suffix, suffix.removeprefix("**/"))
                    if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                        yield confined(entry.path, root)


def adapter_files(pattern):
    root = APPROVED_ROOT.get()
    if not root:
        import glob
        return glob.glob(pattern, recursive=True)
    relative = str(pattern).replace("\\", "/")[len(str(root).replace("\\", "/")):].lstrip("/")
    return [str(p) for p in source_files(root, relative or "*")]


class Inspector:
    def normalize(self, raw, meta):
        return normalize(raw, meta)

    def inspect(self, source, path, meta):
        try:
            found = 0
            usable = False
            for file in source_files(path, meta.suffix or "*", budget=256):
                found += 1
                if meta.shape == "sqlite":
                    with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                        db.execute("PRAGMA query_only=ON")
                        db.set_progress_handler(lambda: 1, 10000)
                        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{meta.table}")')}
                        if not set(meta.columns) <= columns:
                            raise LocationError("The database has an incompatible source schema.")
                        if source == "zcode_local":
                            session_columns = {r[1] for r in db.execute('PRAGMA table_info("session")')}
                            if not {"id", "directory"} <= session_columns:
                                raise LocationError("The database has an incompatible session schema.")
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
                            elif source == "grok_local":
                                recognized |= "msg" in row and "sid" in row
                                usable |= "usage" in row or "tokens" in row
                        if not recognized:
                            raise LocationError("The sample is not a supported usage log.")
                if found >= 3:
                    break
            return {"state": "readable", "usableSample": bool(usable), "detail": "Verified readable." if usable else "Verified readable; no usable history in the bounded sample. You can connect and wait for activity."}
        except PermissionError:
            raise LocationError("Permission denied. Grant this user read access.") from None
        except (OSError, sqlite3.Error):
            raise LocationError("Source unavailable or incompatible. Check its location and permissions.") from None
