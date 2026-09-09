"""Pure incident lifecycle. Observation time, evaluation time and user actions differ."""
import copy
import hashlib
from datetime import UTC, datetime, timedelta

FAMILIES = ("quota", "source", "pricing")
DEFAULTS = {"quota": True, "source": True, "pricing": True, "lower": 80, "higher": 95}
REASONS = {
    "quota_high": "Currently at or above the configured quota threshold.",
    "quota_below": "Below the threshold; attention history is retained for this window.",
    "source_failed": "A monitored usage source reported a terminal failure.",
    "source_schema": "The monitored source reported an incompatible schema.",
    "source_permission": "The monitored source could not read its configured location.",
    "source_auth": "The monitored source reported an authentication failure.",
    "missing_rate": "Recorded usage has no applicable event-time rate.",
    "awaiting_evidence": "Awaiting fresh, usable evidence.",
    "window_ambiguous": "Reset metadata changed; the window boundary is not yet established.",
    "window_expired": "The evidenced quota window expired; no healthy replacement was inferred.",
    "recovered": "A later usable import established recovery for the same binding.",
    "pricing_restored": "Applicable pricing covers the recorded event-time evidence.",
    "evidence_removed": "The underlying evidence was removed or superseded.",
    "rule_disabled": "This Action Center rule was disabled.",
    "source_disabled": "The source was intentionally disabled.",
    "source_rebound": "The source binding changed; this episode no longer applies.",
    "not_eligible": "Current source or quota eligibility is not established.",
}


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def identity(*parts):
    return hashlib.sha256("\x00".join(map(str, parts)).encode()).hexdigest()[:32]


def initial():
    return {"schemaVersion": 1, "revision": 0, "preferences": dict(DEFAULTS),
            "current": {}, "history": [], "watermarks": {}, "pricingScan": {},
            "evaluation": {"status": "unavailable", "evaluatedAt": None, "lastAttemptAt": None}}


def preferences(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Provide all Action Center preferences")
    if any(type(values[k]) is not bool for k in FAMILIES):
        raise ValueError("Rule preferences must be booleans")
    if any(type(values[k]) is not int for k in ("lower", "higher")) or not 1 <= values["lower"] < values["higher"] <= 100:
        raise ValueError("Quota thresholds must satisfy 1 <= lower < higher <= 100")
    return dict(values)


def close(state, key, reason, now):
    item = state["current"].pop(key)
    item.update(status="closed", reasonCode=reason, closedAt=iso(now))
    state["history"].append(item)
    mark = state["watermarks"][key]
    mark.update(closedReason=reason, closedAt=iso(now))
    if item["family"] == "quota":
        mark["lastQuota"] = copy.deepcopy(item)
    state["history"] = sorted(state["history"], key=lambda x: (stamp(x["closedAt"]), x["id"]), reverse=True)[:100]


def reduce(state, facts, now, *, complete=(), failures=(), pricing_complete=False):
    """Only complete successful snapshots can prove absence/recovery."""
    result = copy.deepcopy(state)
    current = result["current"]
    marks = result["watermarks"]
    seen = set()
    for key, item in list(current.items()):
        if not result["preferences"][item["family"]]:
            close(result, key, "rule_disabled", now)
        elif item["family"] == "quota" and stamp(item.get("expiresAt")) and stamp(item["expiresAt"]) <= now:
            close(result, key, "window_expired", now)
    for fact in facts:
        key, family = fact["key"], fact["family"]
        seen.add(key)
        mark = marks.setdefault(key, {"serial": 0, "seenAt": None})
        observed = stamp(fact.get("observedAt"))
        if observed and observed > now:
            observed = None  # Invalid future clocks cannot poison durable replay watermarks.
        previous = stamp(mark.get("seenAt"))
        item = current.get(key)
        if item and fact["eligibility"] == "disabled":
            close(result, key, "source_disabled", now)
            item = None
        if item and item["scope"] != fact["scope"] and fact.get("scopeConfirmed"):
            close(result, key, "source_rebound", now)
            item = None
        elif item and item["scope"] != fact["scope"]:
            item.update(status="awaiting", reasonCode="awaiting_evidence")
            continue
        if observed and previous and observed < previous:
            continue
        if not result["preferences"][family]:
            if observed and (previous is None or observed > previous):
                mark["seenAt"] = iso(observed)
            continue
        if item and family == "quota" and fact.get("expiresAt") != item.get("expiresAt"):
            fact = {**fact, "eligibility": "awaiting", "reasonCode": "window_ambiguous"}
        eligible = fact["eligibility"] == "eligible"
        usable_until = stamp(fact.get("usableUntil"))
        usable = eligible and observed is not None and observed <= now and usable_until is not None and now < usable_until
        if fact["eligibility"] == "disabled":
            if item:
                close(result, key, "source_disabled", now)
        elif fact["condition"] in {"clear", "removed"} and usable:
            if item and observed > (stamp(item["observedAt"]) or datetime.min.replace(tzinfo=UTC)):
                item['resolutionObservedAt'] = iso(observed)
                close(result, key, fact["reasonCode"], now)
        elif fact["condition"] == "breach" and usable:
            severity = fact["severity"]
            if family == "quota":
                pct = fact["evidence"]["usedPercent"]
                severity = 2 if pct >= result["preferences"]["higher"] else 1 if pct >= result["preferences"]["lower"] else 0
            if item is None and severity and (mark["serial"] == 0 or previous is None or observed > previous):
                retained = mark.get("lastQuota")
                if retained and retained["scope"] == fact["scope"] and retained.get("expiresAt") == fact.get("expiresAt"):
                    item = copy.deepcopy(retained)
                    result["history"] = [h for h in result["history"] if h["id"] != item["id"]]
                else:
                    mark["serial"] += 1
                    item = {"id": identity(key, fact["scope"], mark["serial"]), "key": key,
                            "family": family, "scope": fact["scope"], "firstDetectedAt": iso(now),
                            "acknowledgedAt": None, "acknowledgedTier": 0, "snoozedUntil": None,
                            "reachedTier": 0}
                current[key] = item
            if item:
                item.update({k: copy.deepcopy(fact.get(k)) for k in ("title", "observedAt", "usableUntil", "evidence", "actionType")})
                item.update(status="active", eligibility="eligible", severity=severity,
                            reachedTier=max(item["reachedTier"], severity), closedAt=None,
                            reasonCode="quota_below" if family == "quota" and not severity else fact["reasonCode"])
                # Keep original window identity if reset metadata becomes ambiguous.
                item.setdefault("expiresAt", fact.get("expiresAt"))
        elif item:
            item.update(status="awaiting", eligibility=fact["eligibility"], reasonCode=fact.get("reasonCode", "awaiting_evidence"))
        if observed and (previous is None or observed > previous):
            mark["seenAt"] = iso(observed)
    for key, item in list(current.items()):
        if key in seen:
            continue
        if item["family"] == "pricing" and pricing_complete and "pricing" in complete:
            close(result, key, "evidence_removed", now)
        else:
            item.update(status="awaiting", reasonCode="awaiting_evidence")
    prior_success = state["evaluation"].get("evaluatedAt")
    result["evaluation"] = {"status": "error" if failures else "ok", "lastAttemptAt": iso(now),
                            "evaluatedAt": prior_success if failures else iso(now),
                            "failedRules": list(failures), "pricingComplete": pricing_complete or not result["preferences"]["pricing"]}
    return result


def mutate(state, body, now):
    now = now.astimezone(UTC)
    result = copy.deepcopy(state)
    op = body.get("operation")
    if op == "preferences":
        result["preferences"] = preferences(body.get("preferences"))
        for key, item in list(result["current"].items()):
            if not result["preferences"][item["family"]]:
                close(result, key, "rule_disabled", now)
        return result
    item = next((x for x in result["current"].values() if x["id"] == body.get("id")), None)
    if not item:
        raise ValueError("Current incident not found")
    if op == "acknowledge":
        item.update(acknowledgedAt=iso(now), acknowledgedTier=item["reachedTier"])
    elif op == "snooze" and type(body.get("hours")) is int and body["hours"] in (1, 24):
        item["snoozedUntil"] = iso(now + timedelta(hours=body["hours"]))
    elif op == "unsnooze":
        item["snoozedUntil"] = None
    else:
        raise ValueError("Unsupported attention operation")
    return result


def presentation(item):
    e = item["evidence"]
    if item["family"] == "quota":
        summary = f"{e['usedPercent']}% used · {e['pool']} · {e['window']}"
    elif item["family"] == "pricing":
        summary = f"{e['model']} · {e['records']} records · {e['tokens']} measured tokens" + (" (partial scan)" if not e["countsComplete"] else "")
    else:
        summary = e.get("detail") or "Recorded source status"
    times = [("observedAt", "Evidence"), ("firstDetectedAt", "First detected"), ("expiresAt", "Window reset"), ("resolutionObservedAt", "Recovery evidence"), ("closedAt", "Closed"),
             ("acknowledgedAt", "Acknowledged" if item.get("acknowledged", True) else "Escalated"), ("snoozedUntil", "Snooze until")]
    dates = [{"label":label,"at":item[key]} for key,label in times if item.get(key)]
    dates += [{"label":label,"at":e[key]} for key,label in (("from","Event range start"),("to","Event range end")) if e.get(key)]
    return {"summary":summary, "timestamps":dates,
            "stateLabel":item["status"] + " · " + ("High" if item["severity"] == 2 else "Warning" if item["severity"] == 1 else "Below threshold")}


def snapshot(state, now, timezone, *, detail=True):
    evaluation = copy.deepcopy(state["evaluation"])
    evaluated = stamp(evaluation.get("evaluatedAt"))
    lag = None if evaluated is None else max(0, (now - evaluated).total_seconds())
    if evaluation["status"] == "ok" and (lag is None or lag > 90):
        evaluation["status"] = "stale"
    evaluation["lagSeconds"] = lag
    attempted = stamp(evaluation.get('lastAttemptAt'))
    failed_rules = evaluation.get('failedRules', [])
    evaluation_lost = attempted is None or (now-attempted).total_seconds()>90 or evaluation.get('reasonCode')=='persistence_failed' or evaluation['status']!='ok' and not failed_rules
    limited = any(row['enabled'] and (not row['observedSubjects'] or row['eligible']<row['observedSubjects']) for row in evaluation.get('eligibility',{}).values())
    evaluation['label'] = f"Evaluation {evaluation['status']}. " + ('Some evidence is unavailable or ineligible. ' if limited else '') + ('Pricing scan pending. ' if not evaluation.get('pricingComplete') else '') + 'Last success:'
    items = []
    for original in state["current"].values():
        item = copy.deepcopy(original)
        until, expiry = stamp(item.get("usableUntil")), stamp(item.get("expiresAt"))
        observed = stamp(item.get("observedAt"))
        if evaluation_lost or item['family'] in failed_rules or observed is None or observed > now or until is None or until <= now or expiry and expiry <= now:
            item.update(status="awaiting", reasonCode="awaiting_evidence")
        item["acknowledged"] = bool(item["acknowledgedAt"] and item["acknowledgedTier"] >= item["severity"])
        snoozed = stamp(item.get("snoozedUntil"))
        item["snoozed"] = bool(snoozed and now < snoozed)
        item["needsAttention"] = item["status"] == "active" and item["severity"] > 0 and not item["acknowledged"] and not item["snoozed"]
        item["reason"] = REASONS.get(item["reasonCode"], REASONS["awaiting_evidence"])
        item.update(presentation(item))
        item["controls"] = ([] if item["acknowledged"] else [{"operation":"acknowledge", "label":"Acknowledge"}]) + [
            {"operation":"snooze", "hours":1, "label":"Snooze 1 hour"},
            {"operation":"snooze", "hours":24, "label":"Snooze 24 hours"}]
        if item["snoozed"]:
            item["controls"].append({"operation":"unsnooze", "label":"Unsnooze"})
        item["actionLabel"] = {"capacity":"View capacity", "connections":"View connection status", "pricing":"View pricing diagnostics"}[item["actionType"]]
        items.append(item)
    out = {"schemaVersion": 1, "revision": state["revision"], "asOf": iso(now), "timezone": timezone,
           "badgeCount": sum(x["needsAttention"] for x in items), "evaluation": evaluation}
    out['badgeLabel'] = f"Attention {out['badgeCount']}" if evaluation['status']=='ok' else f"Attention {out['badgeCount']} !" if out['badgeCount'] else 'Attention !'
    if detail:
        out.update(preferences=copy.deepcopy(state["preferences"]), current=sorted(items, key=lambda x: (-x["severity"], x["firstDetectedAt"], x["id"])),
                   history=[{**h, **presentation(h), "controls":[], "actionLabel":"View diagnostics", "reason": REASONS.get(h["reasonCode"], h["reasonCode"])} for h in state["history"]])
    return out
