"""Revisioned connection service; one durable binding per registry source.

The existing app_meta table holds non-secret, versioned JSON. No new schema,
event identity, database, worker, or scheduler is introduced.
"""
import copy
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from spend_app.connection_paths import Inspector, LocationError, approved, identity
from spend_app.db import connect
from spend_app.providers import REGISTRY

KEY = "connections.v1"
_LOCKS = {}
_LOCK_GUARD = threading.Lock()


def lock_for(path):
    key = identity(Path(path).absolute())
    with _LOCK_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def now():
    return datetime.now(UTC).isoformat()


def empty_state():
    return {"version": 1, "bindings": {}, "requests": {}, "legacy": [], "cleanup": []}


def decode(raw):
    state = json.loads(raw) if raw is not None else empty_state()
    if not isinstance(state, dict) or state.get("version") != 1:
        raise LocationError("Connection settings require a newer BURNRATE version.")
    if not isinstance(state.get("bindings"), dict) or not isinstance(state.get("requests"), dict) or not isinstance(state.get("legacy"), list) or not isinstance(state.get("cleanup"), list):
        raise LocationError("Connection settings are corrupted; restore or repair them before collecting.")
    for binding in state["bindings"].values():
        if not isinstance(binding, dict) or type(binding.get("enabled")) is not bool or type(binding.get("revision")) is not int:
            raise LocationError("Connection binding metadata is corrupted; defaults were not enabled.")
    return state


class Store:
    def __init__(self, path):
        self.path = path

    def read(self):
        # Status reads do not create directories, switch WAL modes or write.
        from contextlib import closing
        try:
            Path(self.path).stat()
        except FileNotFoundError:
            return empty_state()
        with closing(sqlite3.connect(Path(self.path).absolute().as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone():
                return empty_state()
            row = db.execute("SELECT value FROM app_meta WHERE key=?", (KEY,)).fetchone()
            return decode(row[0] if row else None)

    @contextmanager
    def transaction(self):
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM app_meta WHERE key=?", (KEY,)).fetchone()
            state = decode(row[0] if row else None)
            yield state
            db.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(state, ensure_ascii=False)))

    def bootstrap(self):
        # Called by normal application initialization, never by GET/discovery.
        with connect(self.path) as db:
            if db.execute("SELECT 1 FROM app_meta WHERE key=?", (KEY,)).fetchone():
                return
            state = empty_state()
            state["legacy"] = [r[0] for r in db.execute("SELECT DISTINCT source FROM ingest_runs WHERE status IN ('success','partial')")]
            db.execute("INSERT OR IGNORE INTO app_meta(key,value) VALUES(?,?)", (KEY, json.dumps(state)))


class Conflict(LocationError):
    pass


def source_spec(source):
    spec = REGISTRY.get(source)
    if not spec or not spec.connection:
        raise LocationError("Choose a supported local source or documented provider API.")
    return spec


def default_location(spec, settings):
    meta = spec.connection
    if spec.key == "cursor_csv":
        return str(settings.cursor_import_path)
    value = str(meta.default())
    if meta.suffix:
        value = value.replace("\\", "/").removesuffix("/" + meta.suffix)
    return value


def external_key(spec, settings):
    name = spec.connection.credential_env
    attribute = {"OPENAI_ADMIN_KEY": "openai_admin_key", "ANTHROPIC_ADMIN_KEY": "anthropic_admin_key", "CURSOR_API_KEY": "cursor_api_key"}.get(name)
    return os.getenv(name) or (getattr(settings, attribute, None) if attribute else None) if name else None


def external(spec, settings):
    return bool(external_key(spec, settings) or (spec.key == "cursor_csv" and os.getenv("CURSOR_IMPORT_PATH")) or (spec.connection.location_env and os.getenv(spec.connection.location_env)))


def location_conflict(spec, location):
    name = spec.connection.location_env
    raw = os.getenv(name) if name else None
    if not raw:
        return False
    base = Path(raw).expanduser()
    if spec.key == "opencode_local":
        base /= "opencode"
    base = base.resolve()
    chosen = Path(location).resolve()
    return not (chosen == base or chosen.is_relative_to(base))


def eligible(spec, state):
    binding = state["bindings"].get(spec.key)
    if binding:
        return binding["enabled"]
    if spec.key == "cursor_usage_service":
        from spend_app.integration_policy import authorize
        if authorize("cursor_usage_service")[0]:
            return True
    return spec.enabled_by_default or spec.key in state.get("legacy", [])


class Service:
    def __init__(self, settings, store=None, inspector=None, vault=None, probe=None):
        self.settings = settings
        self.store = store or Store(settings.database_path)
        self.inspector = inspector or Inspector()
        from spend_app.connection_secrets import Vault, test_provider
        self.vault = vault or Vault(settings.database_path)
        self.probe = probe or test_provider
        self.lock = lock_for(settings.database_path)

    def list(self):
        state = self.store.read()
        rows = []
        for spec in REGISTRY:
            meta = spec.connection
            if not meta:
                if spec.kind == "local":
                    rows.append({"source": spec.key, "name": spec.key.replace("_", " "), "kind": "external", "experimental": spec.stability == "experimental", "revision": 0, "location": "", "capabilities": sorted(spec.capabilities), "capabilityNote": "This existing local-service integration has no supported custom-file contract. Configure it outside this wizard.", "credentialHelp": "", "externallyManaged": True, "state": "externally_managed", "detail": "The wizard does not borrow native credentials, call private services to discover sources, or configure this integration.", "lastVerification": None, "lastImport": None, "importRevision": None})
                continue
            binding = state["bindings"].get(spec.key)
            monitored = eligible(spec, state) and meta.shape != "api"
            row = {"source": spec.key, "name": meta.name, "kind": meta.shape,
                   "experimental": spec.stability == "experimental", "revision": 0,
                   "location": "" if meta.shape == "api" else default_location(spec, self.settings),
                   "capabilities": ["balance"] if spec.key == "openrouter" else (["usage", "provider charges"] if meta.shape == "api" else ["usage"]),
                   "capabilityNote": "Managed local connections collect usage only; activity and quota from other default profiles are suppressed. Pricing availability is separate.",
                   "credentialHelp": meta.credential_help,
                   "externallyManaged": external(spec, self.settings),
                   "state": "already_monitored" if monitored else "not_connected",
                   "detail": "Existing collection settings." if monitored else "Choose a location or provider to verify and connect.",
                   "lastVerification": None, "lastImport": None, "importRevision": None}
            if binding:
                row.update({k: v for k, v in binding.items() if k != "credentialRef"})
                row["credentialConfigured"] = bool(binding.get("credentialRef"))
            if row["externallyManaged"]:
                row.update(state="externally_managed", detail="Configured outside BURNRATE. Change that explicit setting before managing this source here.")
            if binding and meta.location_env:
                row["externallyManaged"] = False
                row["externalSetting"] = meta.location_env if os.getenv(meta.location_env) else None
            rows.append(row)
        return {"connections": rows, "vaultAvailable": self.vault.available(), "computerScope": "Locations are on the computer running BURNRATE."}

    def verify(self, body):
        spec = source_spec(body.get("source"))
        if spec.connection.shape == "api":
            raise LocationError("Use Test and connect for a provider API.")
        path = self.inspector.normalize(body.get("location"), spec.connection)
        return {"source": spec.key, "location": path, **self.inspector.inspect(spec.key, path, spec.connection), "lastVerification": now()}

    def discover(self):
        rows = self.list()["connections"]
        for row in rows:
            if row["kind"] in {"api", "external"}:
                continue
            spec = source_spec(row["source"])
            if spec.connection.manual_only:
                row["candidates"] = []
                row["detail"] = "Select an already populated supported cache manually. BURNRATE does not run or enroll its producer."
                continue
            # Default and managed location may differ: expose both, never guess.
            candidates = list({identity(path): path for path in [default_location(spec, self.settings), row["location"]]}.values())
            row["candidates"] = []
            for path in candidates:
                try:
                    result = self.verify({"source": spec.key, "location": path})
                except LocationError as exc:
                    result = {"location": path, "state": "needs_attention", "detail": str(exc)}
                row["candidates"].append(result)
        return {"connections": [r for r in rows if r["kind"] not in {"api", "external"} and not source_spec(r["source"]).connection.manual_only]}

    def mutate(self, body):
        if not isinstance(body, dict):
            raise LocationError("Expected a connection object.")
        spec = source_spec(body.get("source"))
        op = body.get("operation", "connect")
        if op not in {"connect", "disable", "reconnect", "recheck"}:
            raise LocationError("Unsupported connection operation.")
        request_id = body.get("requestId")
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise LocationError("A request identifier is required.") from None
        revision = body.get("revision")
        if type(revision) is not int or revision < 0:
            raise LocationError("A current binding revision is required.")
        # Never hash/store a secret. Retries identify the original operation;
        # replacement requires a NEW request identifier.
        public_body = {k: body.get(k) for k in ("source", "operation", "revision", "location", "mode")}
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()
        new_ref = None
        with self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]
            previous = state["bindings"].get(spec.key)
            if (previous or {}).get("revision", 0) != revision:
                raise Conflict("Connection changed. Reload before saving.")
            if external(spec, self.settings) and not spec.connection.location_env:
                raise Conflict("This source has an external override. Change it outside BURNRATE first; no settings were rewritten.")
            if body.get("mode", "manual") not in {"auto", "manual", "api"}:
                raise LocationError("Choose auto, manual or API mode.")
            if spec.connection.shape == "api" and body.get("mode") == "auto":
                raise LocationError("Provider APIs cannot be automatically connected.")
            if op == "disable":
                record = copy.deepcopy(previous or {"source": spec.key, "location": default_location(spec, self.settings) if spec.connection.shape != "api" else "", "mode": "manual"})
                record.update(enabled=False, state="disabled", detail="Disconnected. Existing measured history is preserved.", credentialRef=None)
            elif spec.connection.shape == "api":
                if not self.vault.available():
                    raise LocationError("Secure Windows credential storage is unavailable. Local connections still work.")
                key = body.get("secret")
                if not key and previous and previous.get("credentialRef"):
                    key = self.vault.get(previous["credentialRef"])
                if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key):
                    raise LocationError("Enter the required provider key.")
                self.probe(spec.key, key)
                new_ref = "burnrate-" + uuid.uuid4().hex
                # Journal the owned reference before vault I/O. A process exit
                # between vault and metadata commit leaves a cleanup entry.
                with self.store.transaction() as pending:
                    pending["cleanup"].append(new_ref)
                try:
                    self.vault.put(key, new_ref)
                except Exception:
                    self._cleanup_ref(new_ref)
                    raise
                record = {"source": spec.key, "location": "", "mode": "api", "enabled": True,
                          "credentialRef": new_ref, "state": "awaiting_ingest", "detail": "Credential verified; awaiting scheduled collection.", "lastVerification": now(), "capabilities": ["balance"] if spec.key == "openrouter" else ["usage", "provider charges"]}
            else:
                location = body.get("location") or (previous or {}).get("location")
                if op == "recheck" and previous:
                    location = previous["location"]
                try:
                    checked = self.verify({"source": spec.key, "location": location})
                except LocationError as exc:
                    if op != "recheck" or not previous:
                        raise
                    with self.store.transaction() as current:
                        record = current["bindings"].get(spec.key)
                        if not record or record["revision"] != revision:
                            raise Conflict("Connection changed. Reload before rechecking.")
                        record.update(state="needs_attention" if record["enabled"] else "disabled", detail=str(exc), lastVerification=now())
                    raise
                if location_conflict(spec, checked["location"]):
                    raise Conflict("Selected location conflicts with " + spec.connection.location_env + ". Resolve the external setting first.")
                record = {**checked, "mode": body.get("mode", "manual"), "enabled": True,
                          "state": "awaiting_ingest", "detail": "Saved, awaiting first import." if checked["usableSample"] else checked["detail"], "capabilities": ["usage"]}
                if op == "recheck" and previous and not previous["enabled"]:
                    record.update(enabled=False, state="disabled", detail="Location verified; connection remains disabled. Use Reconnect to collect again.")
                # No new authority engine: custom Grok/Traycer combinations cannot
                # reuse the existing default-log cutoff safely.
                other = "traycer_local" if spec.key == "grok_local" else "grok_local"
                if spec.key in {"grok_local", "traycer_local"}:
                    peer = REGISTRY.get(other)
                    peer_binding = state["bindings"].get(other)
                    custom = identity(record["location"]) != identity(default_location(spec, self.settings))
                    peer_custom = peer_binding and identity(peer_binding["location"]) != identity(default_location(peer, self.settings))
                    # An environment-selected Grok profile is not evidence that
                    # the default Traycer store belongs to that profile.
                    custom = custom or bool(os.getenv("GROK_HOME"))
                    if eligible(peer, state) and (custom or peer_custom):
                        raise Conflict("Custom Grok and Traycer locations cannot be monitored together. Disable the overlapping source first.")
            record.update(revision=revision + 1, bindingId=spec.key, lastImport=None, importRevision=None)
            try:
                with self.store.transaction() as current:
                    if current["bindings"].get(spec.key, {}).get("revision", 0) != revision:
                        raise Conflict("Connection changed while verifying. Reload before saving.")
                    current["bindings"][spec.key] = record
                    if new_ref:
                        current["cleanup"] = [r for r in current["cleanup"] if r != new_ref]
                    public = {k: v for k, v in record.items() if k != "credentialRef"}
                    result = {"connection": public, "requestId": request_id}
                    current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
                    old_ref = (previous or {}).get("credentialRef")
                    if old_ref and old_ref != new_ref:
                        current["cleanup"].append(old_ref)
            except Exception:
                if new_ref:
                    self._cleanup_ref(new_ref)
                raise
            self.cleanup()
            return result

    def _cleanup_ref(self, ref):
        try:
            self.vault.delete(ref)
        except Exception:
            with self.store.transaction() as state:
                if ref not in state["cleanup"]:
                    state["cleanup"].append(ref)

    def cleanup(self):
        for ref in self.store.read().get("cleanup", []):
            try:
                self.vault.delete(ref)
            except Exception:
                continue
            with self.store.transaction() as state:
                state["cleanup"] = [r for r in state["cleanup"] if r != ref]

    def completion(self, source, revision, result):
        with self.store.transaction() as state:
            record = state["bindings"].get(source)
            if not record or not record["enabled"] or record["revision"] != revision:
                return
            status = result.get("status")
            if status in {"success", "partial"}:
                accepted = result.get("eventsAccepted", result.get("eventsSeen", 0))
                if not accepted and result.get("issues"):
                    record.update(state="needs_attention", detail="Source metadata was read, but usage could not be accepted safely: " + ", ".join(result["issues"]))
                    return
                seen = accepted > 0 or record["state"] == "receiving_usage"
                record.update(lastImport=now(), importRevision=revision,
                              state=("balance_available" if source == "openrouter" else "receiving_usage") if (seen or source == "openrouter") else "waiting_activity",
                              detail=("Balance updated; usage events are a separate connection." if source == "openrouter" else "Import completed. Missing pricing and quota do not disconnect usage.") if (seen or source == "openrouter") else "Readable; waiting for usage. No paid turn is required to finish setup.")
                if result.get("issues"):
                    record["detail"] += " Some records need attention: " + "; ".join(str(issue).replace("_", " ") for issue in sorted(set(result["issues"]))[:3]) + ". Review the provider compatibility guidance before changing the source."
            else:
                record.update(state="needs_attention", detail="Import unavailable. Recheck location, permissions or provider access; collection will retry on its normal cadence.")


def managed_job(settings, pricing, spec, window=None):
    """Resolve at execution time, not when the scheduler was constructed."""
    service = Service(settings)
    state = service.store.read()
    binding = state["bindings"].get(spec.key)
    if binding and not binding["enabled"]:
        return service, binding, None
    if not eligible(spec, state) and not (spec.connection and external(spec, settings)):
        return service, None, None
    if spec.key in {"grok_local", "traycer_local"}:
        peers = [REGISTRY.get(key) for key in ("grok_local", "traycer_local")]
        custom = bool(os.getenv("GROK_HOME")) or any(
            state["bindings"].get(peer.key) and identity(state["bindings"][peer.key]["location"]) != identity(default_location(peer, settings))
            for peer in peers)
        if custom and all(eligible(peer, state) for peer in peers):
            raise Conflict("Selected Grok/Traycer profiles have no proven shared authority. Disable one overlapping source.")
    if spec.key == "traycer_local" or binding and spec.connection and spec.connection.shape != "api":
        kwargs = {"database_path": settings.database_path, "pricing": pricing}
        if spec.key == "traycer_local":
            from spend_app.providers import default_grok_log, default_traycer_glob, grok_coverage_start
            kwargs["database_glob"] = default_traycer_glob()
            kwargs["grok_covered_from"] = grok_coverage_start(default_grok_log(), settings.database_path) if eligible(REGISTRY.get("grok_local"), state) else None
    else:
        kwargs = spec.build_ingest_kwargs(settings, pricing, **(window or {}))
    if binding and spec.connection:
        meta = spec.connection
        if not binding["enabled"] and not external(spec, settings):
            return service, binding, None
        if not external(spec, settings) or meta.location_env:
            if meta.shape == "api":
                kwargs[meta.argument] = service.vault.get(binding["credentialRef"])
            else:
                path = service.inspector.normalize(binding["location"], meta)
                if location_conflict(spec, path):
                    raise Conflict("Saved location conflicts with " + meta.location_env + "; collection was not redirected.")
                if identity(path) != identity(binding["location"]):
                    raise LocationError("Saved location moved. Recheck before collecting.")
                service.inspector.inspect(spec.key, path, meta)
                if meta.suffix and Path(path).is_dir() and meta.argument != "import_path":
                    kwargs[meta.argument] = str(Path(path) / meta.suffix)
                else:
                    kwargs[meta.argument] = Path(path) if meta.argument not in {"session_glob", "database_glob"} else path
    if spec.key == "cursor_usage_service":
        admin = state["bindings"].get("cursor_admin")
        if admin and admin["enabled"]:
            return service, binding, None
    if spec.skip_if and not binding and spec.skip_if(settings):
        return service, binding, None
    return service, binding, kwargs


def execute(settings, pricing, spec, ingest, window=None):
    # Saves serialize with the existing job, so a successful save cannot be
    # followed by an obsolete collector starting or reporting healthy.
    with lock_for(settings.database_path):
        service = Service(settings)
        service.cleanup()
        binding = service.store.read()["bindings"].get(spec.key)
        try:
            service, binding, kwargs = managed_job(settings, pricing, spec, window)
            if kwargs is None:
                return {"status": "skipped", "eventsSeen": 0}
            root = binding["location"] if binding and spec.connection.shape != "api" else None
            if not binding and spec.connection and spec.connection.shape != "api" and not spec.connection.manual_only:
                root = service.inspector.normalize(default_location(spec, settings), spec.connection)
            with approved(root, binding["revision"] if binding else 0, binding.get("patterns") if binding else None):
                result = ingest(**kwargs)
        except Exception:
            if binding:
                service.completion(spec.key, binding["revision"], {"status": "failed"})
                raise LocationError("Connected source import failed. Recheck its location, permissions or provider access.") from None
            raise
        if binding:
            service.completion(spec.key, binding["revision"], result)
        return result


def serialize_collection(function):
    from functools import wraps
    @wraps(function)
    def wrapped(database_path, *args, **kwargs):
        with lock_for(database_path):
            return function(database_path, *args, **kwargs)
    return wrapped
