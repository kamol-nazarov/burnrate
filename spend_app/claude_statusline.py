"""Optional provider-emitted quota snapshot bridge. No usage DB or credentials."""
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 131072
MAX_AGE = 900


def scope_id(root):
    return hashlib.sha256(os.path.normcase(str(Path(root).expanduser().absolute())).casefold().encode()).hexdigest()


def reduce_payload(payload, *, scope, now):
    rates = payload.get("rate_limits") if isinstance(payload, dict) else None
    windows = {}
    if isinstance(rates, dict):
        for name in ("five_hour", "seven_day"):
            row = rates.get(name)
            if not isinstance(row, dict):
                continue
            pct, reset = row.get("used_percentage"), row.get("resets_at")
            if (type(pct) not in (int, float) or not math.isfinite(pct) or not 0 <= pct <= 100
                    or type(reset) not in (int, float) or not math.isfinite(reset)
                    or not now < reset <= now + 8 * 86400):
                continue
            windows[name] = {"pct": pct, "reset": reset}
    return {"version": 1, "scope": scope, "observed": now, "windows": windows}


def snapshot_path(database):
    return Path(database).absolute().parent / "snapshots" / ("claude-statusline-" + scope_id(database)[:16] + ".json")


def atomic_write(path, snapshot):
    parent = path.parent
    if parent.is_symlink() or parent.is_junction():
        raise ValueError("Snapshot directory cannot be a link.")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=parent, prefix=".claude-statusline-", suffix=".tmp", delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def collect_snapshot(snapshot, *, scope, now):
    unavailable = {"status": "unavailable", "detail": "Optional Claude status-line snapshot is missing, stale, expired or belongs to another profile.", "windows": []}
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or snapshot.get("scope") != scope:
        return unavailable
    observed = snapshot.get("observed")
    if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= now - observed <= MAX_AGE:
        return unavailable
    raw = snapshot.get("windows")
    raw = raw if isinstance(raw, dict) else {}
    # Revalidate even BURNRATE-owned snapshots; files are an untrusted boundary.
    valid = reduce_payload({"rate_limits": {key: {"used_percentage": row.get("pct"), "resets_at": row.get("reset")} for key, row in raw.items() if isinstance(row, dict)}}, scope=scope, now=now)
    windows = [{"key": "5h" if key == "five_hour" else "weekly", "usedPct": row["pct"],
                "resetsAt": datetime.fromtimestamp(row["reset"], UTC).isoformat()} for key, row in valid["windows"].items()]
    return {"status": "exact" if windows else "unavailable", "detail": "Provider-reported local-profile snapshot; account identity is not established.", "windows": windows}


def read_snapshot(database):
    from spend_app.integration_policy import require
    require("claude_statusline_snapshot")
    root = os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    path = snapshot_path(database)
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
            raise ValueError("Snapshot links are not supported.")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        snapshot = json.loads(raw) if len(raw) <= MAX_BYTES else None
    except (OSError, ValueError):
        snapshot = None
    return collect_snapshot(snapshot, scope=scope_id(root), now=datetime.now(UTC).timestamp())


def main():
    import argparse
    import sys
    from spend_app.integration_policy import require
    require("claude_statusline_snapshot")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="BURNRATE database path; this command never opens the database")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Status-line input exceeds the metadata limit.")
    payload = json.loads(raw)
    scope = scope_id(os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    snapshot = reduce_payload(payload, scope=scope, now=datetime.now(UTC).timestamp())
    del payload, raw
    atomic_write(snapshot_path(args.database), snapshot)


if __name__ == "__main__":
    main()
