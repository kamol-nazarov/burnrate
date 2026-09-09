"""Persisted evidence only: bounded reads and atomic attention-only transactions."""
import copy
import hashlib
import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from spend_app import codex_quota
from spend_app.attention_rules import initial, iso, stamp, identity, reduce, mutate, snapshot
from spend_app.diagnostics import collection_stale_after
from spend_app.connections import eligible as usage_eligible
from spend_app.plans_value_store import load_source_health
from spend_app.pricing import UnpricedModelError
from spend_app.providers import REGISTRY
from spend_app.quotas import REQUIRED_LIMITS, PROVIDER_SOURCES

KEY = "attention.v1"
BATCH = 500
MAX_STATE_BYTES = 2_000_000


class Conflict(ValueError):
    pass


def safe_label(value):
    text = str(value or "")
    if len(text) > 120 or re.search(r"(?i)(?:[a-z]:[/\\]|://|@|sk-|bearer|gh[pousr]_)", text):
        return "Unidentified evidence"
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/()+-]*", text) else "Unidentified evidence"


def meta(connection, key, default=None):
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else copy.deepcopy(default)


def fact(family, subject, scope, now, observed=None, **changes):
    return {"key": identity(family, subject), "family": family, "scope": str(scope),
            "title": safe_label(subject), "observedAt": observed, "usableUntil": iso(now + timedelta(seconds=90)),
            "expiresAt": None, "scopeConfirmed":False, "eligibility": "awaiting", "condition": "awaiting", "severity": 1,
            "reasonCode": "awaiting_evidence", "evidence": {}, "actionType": {"quota":"capacity", "source":"connections", "pricing":"pricing"}[family], **changes}


def quota_fact(row, provenance, now, *, eligible_scope=True):
    provider, limit = row["provider_key"], row["limit_key"]
    pool = provenance.get("poolId") or "unknown"
    item = fact("quota", f"{provider}:{limit}:{pool}", provenance.get("scope", "unknown"), now,
                provenance.get("observedAt"), title=f"{safe_label(provider)} {safe_label(limit)} quota")
    value = row.get("pct")
    meaning = provenance.get("meaning", "used" if provider == "codex" and pool == "codex" else None)
    observed, reset = stamp(item["observedAt"]), stamp(row.get("resets_at"))
    valid_number = type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 100
    if not eligible_scope or not provenance.get("scope") or pool == "unknown" or row.get("unit") != "pct" or not valid_number or meaning not in ("used", "remaining"):
        item["reasonCode"] = "not_eligible"
        return item
    if not observed or not reset or observed > now or reset <= observed:
        return item
    used = 100 - value if meaning == "remaining" else value
    until = min(reset, observed + timedelta(seconds=codex_quota.MAX_AGE_SECONDS))
    item.update(condition="breach", eligibility="eligible" if now < until else "awaiting",
                scopeConfirmed=True,
                expiresAt=iso(reset), usableUntil=iso(until), reasonCode="quota_high",
                evidence={"usedPercent":used, "pool":safe_label(pool), "provider":safe_label(provider), "window":safe_label(limit)})
    return item


def quota_facts(connection, config, now):
    # Each subquery seeks a declared provider/window in the existing unique index.
    pairs = [(provider, key) for provider, limits in REQUIRED_LIMITS.items() for key, _label in limits]
    query = " UNION ALL ".join("SELECT * FROM (SELECT * FROM quotas WHERE provider_key=? AND limit_key=? ORDER BY polled_at DESC LIMIT 1)" for _ in pairs)
    rows = [dict(r) for r in connection.execute(query, tuple(x for pair in pairs for x in pair))]
    provenance = meta(connection, codex_quota.KEY, {})
    # Reuse the released Codex scope/pool/freshness mask, restricted to these latest rows.
    # Its scope helper resolves configured path identity only; it does not discover/read telemetry.
    masked = codex_quota.read_rows(connection, now=now, rows=rows)
    output = []
    for row in masked:
        provider = row["provider_key"]
        binding = config.get("bindings", {}).get("codex_local") if provider == "codex" else None
        eligible = provider == "codex" and row["source"] == PROVIDER_SOURCES["codex"] and not binding
        proof = provenance if provider == "codex" else {}
        item = quota_fact(row, proof, now, eligible_scope=eligible)
        if binding:
            item.update(eligibility="disabled" if binding.get("enabled") is False else "awaiting",
                        scope="binding:"+str(binding.get("revision")), scopeConfirmed=True)
        output.append(item)
    return output


def source_facts(connection, config, now, health=None):
    out = []
    for row in (load_source_health(connection, as_of=now) if health is None else health):
        source = row["source"]
        spec = REGISTRY.get(source)
        if not spec or "usage" not in spec.capabilities or "admin" in spec.capabilities:
            continue
        binding = config.get("bindings", {}).get(source)
        legacy = source in config.get("legacy", []) or usage_eligible(spec, config) and row["lastSuccessAt"] is not None
        scope = str(binding["revision"]) if binding else "legacy"
        item = fact("source", source, scope, now, row["lastAttemptAt"], title=row["sourceLabel"] + " collection",
                    scopeConfirmed=bool(binding),
                    evidence={"source":source, "lastSuccessAt":row["lastSuccessAt"], "detail":row["reason"]})
        if binding and not binding.get("enabled"):
            item.update(eligibility="disabled", reasonCode="source_disabled")
        elif not binding and not legacy:
            item["reasonCode"] = "not_eligible"
        else:
            when = stamp(row["lastAttemptAt"])
            verified = stamp(binding.get("lastVerification")) if binding else None
            if binding and binding.get("importRevision") == binding["revision"] and binding.get("state") in ("receiving_usage", "waiting_activity"):
                when = stamp(binding.get("lastImport"))
                item.update(condition="clear", reasonCode="recovered", observedAt=iso(when) if when else None)
            elif row["status"] == "failed" and (not binding or binding.get("state") == "needs_attention" and verified and when and when >= verified):
                code = {"Access to local usage metadata was denied.":"source_permission",
                        "The source schema is incompatible with the supported format.":"source_schema",
                        "Source authentication failed.":"source_auth"}.get(row["reason"], "source_failed")
                item.update(condition="breach", reasonCode=code)
            elif not binding and row["status"] == "success":
                item.update(condition="clear", reasonCode="recovered")
            if when:
                item.update(eligibility="eligible", usableUntil=iso(when + timedelta(seconds=collection_stale_after(source))))
        out.append(item)
    return out


def pricing_revision(pricing):
    return hashlib.sha256(repr(pricing.prices).encode()).hexdigest()


def pricing_facts(connection, pricing, state, now, health=None):
    """Bounded primary-key sweeps; complete unchanged scans are cached five minutes."""
    revision = pricing_revision(pricing)
    health = load_source_health(connection, as_of=now) if health is None else health
    evidence_stamp = identity(*[(row['source'],row['status'],row['lastAttemptAt'],row['lastSuccessAt']) for row in health])
    previous = state.get("pricingScan") or {}
    high = connection.execute("SELECT COALESCE(MAX(id),0) FROM unpriced_usage_events").fetchone()[0]
    recent_full = stamp(previous.get("fullCheckedAt")) and now < stamp(previous["fullCheckedAt"]) + timedelta(minutes=5)
    cached = previous.get("authoritative") and previous.get("revision") == revision and previous.get("high") == high and recent_full and previous.get('evidenceStamp') == evidence_stamp
    scan = copy.deepcopy(previous)
    if not cached:
        if scan.get("revision") != revision or not scan or scan.get("complete") and (not recent_full or high < scan["high"] or not scan.get('authoritative') or scan.get('evidenceStamp') != evidence_stamp and high == scan['high']):
            revision_at = scan.get("revisionAt") if scan.get("revision") == revision else iso(now)
            scan = {"revision":revision, "revisionAt":revision_at, "cursor":0, "groups":{}, "complete":False, "full":True, "evidenceStamp":evidence_stamp}
        elif scan.get("complete"):
            scan.update(complete=False, full=False)
        scan["high"] = high
        rows = list(connection.execute(
            "SELECT u.id,u.source,u.tool_key,u.model_key,u.occurred_at,u.ingested_at,u.input_tokens,u.cached_input_tokens,u.cache_write_tokens,u.cache_write_1h_tokens,u.output_tokens,u.unclassified_tokens,u.telemetry_complete,u.cost_usd,c.issue AS coverage_issue,p.model_key AS gap_model FROM unpriced_usage_events u LEFT JOIN coverage_gap_events c ON c.raw_id=u.raw_id LEFT JOIN pricing_gap_events p ON p.raw_id=u.raw_id WHERE u.id>? AND u.id<=? ORDER BY u.id LIMIT ?",
            (scan["cursor"], high, BATCH)))
        for raw in rows:
            row = dict(raw)
            scan["cursor"] = row["id"]
            source, model = row["source"], row["model_key"]
            spec = REGISTRY.get(source)
            when = stamp(row["occurred_at"])
            if not spec or "usage" not in spec.capabilities or "admin" in spec.capabilities or row.get("coverage_issue") or row.get("gap_model") not in (None,model) or not row["telemetry_complete"] or row["unclassified_tokens"] or not when or not stamp(row["ingested_at"]) or safe_label(model) != model or model.lower().startswith(("unknown", "unresolved")):
                continue
            if row["cached_input_tokens"] > row["input_tokens"] or row.get("cache_write_1h_tokens",0) > row["cache_write_tokens"]:
                continue
            tokens = row["input_tokens"] + row["cache_write_tokens"] + row["output_tokens"]
            if tokens <= 0:
                continue
            owners = {price.provider for price in getattr(pricing,"_index",{}).get(model,())}
            domain = next(iter(owners)) if len(owners)==1 else model.split(":",1)[0] if ":" in model else source
            key = identity(domain, model)
            group = scan["groups"].setdefault(key, {"sources":[], "model":model, "domain":domain, "tokens":0,
                                                  "from":row["occurred_at"], "to":row["occurred_at"], "observedAt":row["ingested_at"], "missing":0})
            group["observedAt"] = max(group["observedAt"], row["ingested_at"], key=stamp)
            group["checkedAt"] = iso(now)
            try:
                pricing.resolve(model, when)
            except UnpricedModelError:
                if not group["missing"]:
                    group["from"] = group["to"] = row["occurred_at"]
                group["missing"] += 1
                group["tokens"] += tokens
                group["from"] = min(group["from"], row["occurred_at"], key=stamp)
                group["to"] = max(group["to"], row["occurred_at"], key=stamp)
                group["sources"] = sorted(set(group["sources"]) | {source})
        scan["complete"] = len(rows) < BATCH or scan["cursor"] >= high
        scan["authoritative"] = scan["complete"] and scan["evidenceStamp"] == evidence_stamp
        if scan["complete"]:
            scan["completedAt"] = iso(now)
            if scan["full"]:
                scan["fullCheckedAt"] = iso(now)
    out = []
    for key, group in scan["groups"].items():
        if not group["missing"] and not scan["complete"]:
            continue
        checked = stamp(group["checkedAt"])
        item = fact("pricing", key, key, now, max(group["observedAt"],scan["revisionAt"],key=stamp), title=group["model"] + " pricing",
                    eligibility="eligible", condition="breach" if group["missing"] else "clear",
                    reasonCode="missing_rate" if group["missing"] else "pricing_restored",
                    usableUntil=iso(checked + timedelta(minutes=10)),
                    evidence={"model":group["model"], "rateDomain":group["domain"], "sources":group["sources"], "records":group["missing"],
                              "tokens":group["tokens"], "from":group["from"], "to":group["to"], "countsComplete":scan.get("authoritative",False), "pricingRevision":revision})
        if not group["missing"]:
            item["observedAt"] = scan["completedAt"]
            if not scan.get('authoritative'):
                item.update(eligibility='awaiting',condition='awaiting',reasonCode='awaiting_evidence')
        out.append(item)
    return out, scan


class Repository:
    def __init__(self, path):
        self.path = Path(path).absolute()

    @contextmanager
    def transaction(self, write=False):
        connection = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def load_state(connection):
    state = meta(connection, KEY, initial())
    if not isinstance(state, dict) or state.get("schemaVersion") != 1 or not isinstance(state.get("current"), dict) or not isinstance(state.get("history"), list):
        raise ValueError("Unsupported attention state")
    return state


def save(connection, state):
    state["revision"] += 1
    encoded = json.dumps(state, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_STATE_BYTES:
        raise ValueError("Attention state capacity reached; unresolved items retained")
    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, encoded))


class Service:
    def __init__(self, repository, pricing, timezone, clock=None):
        self.repository, self.pricing, self.timezone = repository, pricing, timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    def view(self, connection, state, now, detail=True):
        state = copy.deepcopy(state)
        if meta(connection, KEY + ".error"):
            state["evaluation"] = {**state["evaluation"], "status":"error", "reasonCode":"persistence_failed"}
        return snapshot(state, now, self.timezone, detail=detail)

    def read(self, detail=True):
        with self.repository.transaction() as connection:
            return self.view(connection, load_state(connection), self.clock(), detail)

    def update(self, body):
        op = body.get("operation")
        allowed = {"operation", "requestId", "expectedRevision"} | ({"preferences"} if op == "preferences" else {"id", "hours"} if op == "snooze" else {"id"})
        if set(body) - allowed or type(body.get("expectedRevision")) is not int or body["expectedRevision"] < 0:
            raise ValueError("Invalid attention request")
        if str(UUID(body.get("requestId", ""))) != body["requestId"]:
            raise ValueError("Use a canonical UUID request identifier")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        receipt = KEY + ".request." + identity(body["requestId"])
        now = self.clock()
        with self.repository.transaction(write=True) as connection:
            state = load_state(connection)
            previous = meta(connection, receipt)
            if previous is not None:
                if previous != digest:
                    raise Conflict("Retry identifier reused for a different request")
                return self.view(connection, state, now)
            if state["revision"] != body["expectedRevision"]:
                raise Conflict("Attention changed; refresh before retrying")
            state = mutate(state, body, now)
            save(connection, state)
            connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?)", (receipt, json.dumps(digest)))
            return self.view(connection, state, now)

    def evaluate(self):
        now = self.clock()
        try:
            with self.repository.transaction(write=True) as connection:
                state = load_state(connection)
                try:
                    config = meta(connection, "connections.v1")
                    if not isinstance(config, dict) or not isinstance(config.get("bindings"), dict) or not isinstance(config.get("legacy"), list):
                        raise ValueError("Connection evidence unavailable")
                except Exception:
                    config = None
                facts, complete, failures = [], [], []
                scan = state.get("pricingScan", {})
                try:
                    health = load_source_health(connection, as_of=now) if state['preferences']['source'] or state['preferences']['pricing'] else []
                except Exception:
                    health = None
                for family, loader in (("quota", quota_facts), ("source", source_facts), ("pricing", pricing_facts)):
                    if not state["preferences"][family]:
                        continue
                    try:
                        if family != "pricing" and config is None:
                            raise ValueError("Connection evidence unavailable")
                        if family in ('source','pricing') and health is None:
                            raise ValueError('Source snapshot unavailable')
                        if family == "pricing":
                            loaded, scan = loader(connection, self.pricing, state, now, health=health)
                        elif family == 'source':
                            loaded = loader(connection, config, now, health=health)
                        else:
                            loaded = loader(connection, config, now)
                        facts.extend(loaded)
                        complete.append(family)
                    except Exception:
                        failures.append(family)
                result = reduce(state, facts, now, complete=complete, failures=failures, pricing_complete=scan.get("authoritative", False))
                result['evaluation']['eligibility'] = {
                    family:{'enabled':state['preferences'][family],
                            'eligible':sum(f['family']==family and f['eligibility']=='eligible' and stamp(f.get('observedAt')) is not None and stamp(f['observedAt'])<=now and stamp(f['usableUntil'])>now for f in facts),
                            'observedSubjects':sum(f['family']==family for f in facts)}
                    for family in ('quota','source','pricing')}
                result["pricingScan"] = scan
                save(connection, result)
                connection.execute("DELETE FROM app_meta WHERE key=?", (KEY + ".error",))
        except Exception:
            logging.getLogger(__name__).warning("Attention evaluation could not persist; existing progress retained")
            try:
                with self.repository.transaction(write=True) as connection:
                    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY + ".error", json.dumps({"at":iso(now),"reasonCode":"persistence_failed"})))
            except Exception:
                pass  # Read-time lag still exposes loss of successful evaluation if storage is unavailable.
            return False
        return True


def register(scheduler, settings, pricing):
    service = Service(Repository(settings.database_path), pricing, settings.timezone)
    scheduler.add_job(service.evaluate, "interval", seconds=30, id="attention", coalesce=True, max_instances=1)
