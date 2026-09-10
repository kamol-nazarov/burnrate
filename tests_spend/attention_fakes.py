"""In-memory transaction/query boundaries; never an SQLite fixture or running app."""
import copy
import json
import threading
import os
import sys
from types import SimpleNamespace
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

def denied(*_args, **_kwargs):
    raise AssertionError("Unexpected filesystem/provider/SQLite/vault/subprocess boundary")

_open = Path.open
def import_metadata_only(path, *args, **kwargs):
    # Distribution metadata is an import prerequisite, not a harness/data boundary.
    if path.name in {"METADATA", "entry_points.txt"} and os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(sys.prefix + os.sep)):
        return _open(path, *args, **kwargs)
    return denied(path)

# These guards apply before the entire transitive production import chain.
_vault_module = sys.modules.get("keyring.backends.Windows")
sys.modules["keyring.backends.Windows"] = SimpleNamespace(WinVaultKeyring=denied)
try:
    with patch("sqlite3.connect", denied), patch("pathlib.Path.home", return_value=Path("C:/attention-fixture")), patch("pathlib.Path.open", import_metadata_only), patch("subprocess.Popen", denied), patch("socket.socket.connect", denied):
        from spend_app.attention_store import Service, KEY
        from spend_app.pricing import UnpricedModelError
        from spend_app import attention_api
finally:
    if _vault_module is None:
        sys.modules.pop("keyring.backends.Windows",None)
    else:
        sys.modules["keyring.backends.Windows"] = _vault_module

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
SCOPE = "a" * 64

class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

class Rows(list):
    def fetchone(self):
        return self[0] if self else None

class Prices:
    prices = ()
    _index = {}
    restored_from = None
    def resolve(self, model, when):
        if self.restored_from is not None and when >= self.restored_from:
            return object()
        raise UnpricedModelError("Synthetic missing applicable rate")

class MemoryRepository:
    def __init__(self):
        self.meta = {"connections.v1": {"legacy":["codex_local", "cursor_local"], "bindings":{}}}
        self.quotas, self.attempts, self.successes, self.gaps = [], [], [], []
        self.lock = threading.RLock()
        self.statements = []
        self.fail_commit = False
        self.fail_reads = set()
        self.before_write = None

    @contextmanager
    def transaction(self, write=False):
        if write and self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        with self.lock:
            staged = copy.deepcopy(self.meta)
            repository = self
            class Connection:
                def execute(self, sql, parameters=()):
                    sql = " ".join(sql.split())
                    repository.statements.append((sql, parameters, write))
                    if any(f in sql for f in repository.fail_reads):
                        raise RuntimeError("Injected read failure")
                    if sql.startswith("SELECT value FROM app_meta"):
                        return Rows([Row(value=json.dumps(staged[parameters[0]]))]) if parameters[0] in staged else Rows()
                    if sql.startswith("SELECT key,value FROM app_meta"):
                        return Rows([(key,json.dumps(staged[key])) for key in parameters if key in staged])
                    if sql.startswith("INSERT INTO app_meta"):
                        assert write and parameters[0].startswith(KEY)
                        staged[parameters[0]] = json.loads(parameters[1])
                        return Rows()
                    if sql.startswith("DELETE FROM app_meta"):
                        assert write and parameters == (KEY + '.error',)
                        staged.pop(parameters[0],None)
                        return Rows()
                    if "FROM quotas" in sql:
                        return Rows(map(Row,repository.quotas))
                    if "MAX(id)" in sql and "GROUP BY source" in sql:
                        return Rows(map(Row,repository.attempts))
                    if "MAX(finished_at)" in sql:
                        return Rows(map(Row,repository.successes))
                    if "FROM unpriced_usage_events" in sql:
                        if "MAX(id)" in sql:return Rows([(max((r["id"] for r in repository.gaps), default=0),)])
                        return Rows(Row(row) for row in repository.gaps if row["id"] > parameters[0] and row["id"] <= parameters[1])[:parameters[2]]
                    raise AssertionError("Unexpected SQL: " + sql)
            yield Connection()
            if write:
                if self.fail_commit:
                    raise RuntimeError("Injected transaction failure")
                self.meta = staged

def set_quota(repo, pct, when=NOW, reset="2026-09-17T12:00:00Z", **proof):
    observed = when.isoformat().replace("+00:00", "Z")
    repo.quotas = [{"provider_key":"codex", "limit_key":"weekly", "source":"codex_local_telemetry", "pct":pct, "unit":"pct", "used":None, "allowance":None, "resets_at":reset, "polled_at":observed}]
    repo.meta["codex.quota-observation.v1"] = {"poolId":"codex", "scope":SCOPE, "pct":pct, "resetsAt":reset, "observedAt":observed, "polledAt":observed, **proof}

def attempt(source="cursor_local", status="failed", when=NOW, reason="redacted cursor failure"):
    t = when.isoformat().replace("+00:00", "Z")
    return {"source":source, "status":status, "started_at":t, "finished_at":t, "events_written":0, "error":reason}

def gap(id=1, model="vendor:missing", source="codex_local", **overrides):
    return {"id":id, "source":source, "tool_key":"codex", "model_key":model, "occurred_at":"2026-09-01T12:00:00Z", "ingested_at":"2026-09-10T11:59:00Z", "input_tokens":1000, "cached_input_tokens":200, "cache_write_tokens":0, "cache_write_1h_tokens":0, "output_tokens":100, "unclassified_tokens":0, "telemetry_complete":1, "cost_usd":None, "coverage_issue":None, "gap_model":None, **overrides}
