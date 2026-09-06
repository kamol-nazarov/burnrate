"""Configured-cost terms. Calendar proration; end_date is last active day."""

import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

TOOLS = (
    "codex",
    "claude-code",
    "cursor",
    "grok",
    "opencode",
    "zcode",
    "openrouter",
    "xai",
    "antigravity",
    "custom",
)
CADENCES = ("monthly", "quarterly", "annual")
FIELDS = {"name", "tool_key", "amount_usd", "cadence", "start_date", "end_date"}


class PlanError(ValueError):
    pass


class PlanConflict(PlanError):
    pass


def iso_date(value, optional=False):
    if optional and value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or not 1900 <= parsed.year <= 2200:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise PlanError(
            "Use a real ISO date (YYYY-MM-DD), between 1900 and 2200"
        ) from None


def validate_term(values):
    if set(values) != FIELDS:
        raise PlanError("Unknown or missing plan fields")
    name = values["name"]
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 80
        or any(ord(c) < 32 for c in name)
    ):
        raise PlanError("Plan name must contain 1–80 printable characters")
    if values["cadence"] not in CADENCES:
        raise PlanError("unsupported subscription cadence")
    if values["tool_key"] not in TOOLS:
        raise PlanError("Unsupported tool association")
    raw = values["amount_usd"]
    try:
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            raise InvalidOperation()
        amount = Decimal(str(raw))
        if (
            not amount.is_finite()
            or not 0 <= amount <= 1000000
            or amount != amount.quantize(Decimal(".01"))
        ):
            raise InvalidOperation()
    except (InvalidOperation, ValueError):
        raise PlanError(
            "USD amount must be finite, between 0 and 1000000, with at most two decimal places"
        ) from None
    start, end = iso_date(values["start_date"]), iso_date(values["end_date"], True)
    if end and end < start:
        raise PlanError("Last active date cannot precede the start date")
    return {
        **values,
        "name": name.strip(),
        "tool_key": "opencode" if values["tool_key"] == "zcode" else values["tool_key"],
        "amount_usd": float(amount),
    }


def create_plan(connection, values):
    term = validate_term(values)
    plan_id = connection.execute(
        "INSERT INTO subscription_plans(end_date) VALUES(?)", (term["end_date"],)
    ).lastrowid
    connection.execute(
        "INSERT INTO subscriptions(plan_id,name,tool_key,amount_usd,cadence,start_date,end_date) VALUES(?,?,?,?,?,?,?)",
        (
            plan_id,
            *(
                term[key]
                for key in (
                    "name",
                    "tool_key",
                    "amount_usd",
                    "cadence",
                    "start_date",
                    "end_date",
                )
            ),
        ),
    )
    return plan_id


def list_plans(connection, today):
    plans = []
    for plan in connection.execute(
        "SELECT id,version,end_date FROM subscription_plans ORDER BY id"
    ):
        terms = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM subscriptions WHERE plan_id=? ORDER BY start_date",
                (plan["id"],),
            )
        ]
        if not terms:
            continue
        for term in terms:
            term["amount_usd"] = str(Decimal(str(term["amount_usd"])))
            effective_end = (
                min(v for v in (term["end_date"], plan["end_date"]) if v)
                if term["end_date"] or plan["end_date"]
                else None
            )
            term["status"] = (
                "ended"
                if effective_end
                and (
                    effective_end < today.isoformat()
                    or effective_end < term["start_date"]
                )
                else "scheduled"
                if term["start_date"] > today.isoformat()
                else "active"
            )
            term["monthly_equivalent"] = str(
                Decimal(term["amount_usd"])
                / {"monthly": 1, "quarterly": 3, "annual": 12}[term["cadence"]]
            )
        plans.append(
            {
                "id": plan["id"],
                "version": plan["version"],
                "end_date": plan["end_date"],
                "terms": terms,
            }
        )
    return {
        "plans": plans,
        "tools": TOOLS,
        "cadences": CADENCES,
        "today": today.isoformat(),
        "semantics": "Calendar-based configured costs, not verified payments. Last active dates are inclusive. Ending a plan here does not cancel its provider subscription.",
    }


def _validate_overlap(terms):
    terms.sort(key=lambda t: t["start_date"])
    for term in terms:
        if term["end_date"] and term["end_date"] < term["start_date"]:
            raise PlanError("A term cannot end before it starts")
    for first, second in pairwise(terms):
        if first["end_date"] is None or first["end_date"] >= second["start_date"]:
            raise PlanError("Effective terms cannot overlap")


def effective_terms(connection):
    """Keep the existing term shape for aggregation/materialization callers."""
    rows = []
    for row in connection.execute(
        "SELECT s.*, p.end_date AS plan_end FROM subscriptions s LEFT JOIN subscription_plans p ON p.id=s.plan_id"
    ):
        item = dict(row)
        ends = [v for v in (item["end_date"], item.pop("plan_end")) if v]
        item["end_date"] = min(ends) if ends else None
        if not item["end_date"] or item["start_date"] <= item["end_date"]:
            rows.append(item)
    return rows


def _sum(terms, start, end):
    from spend_app.subscriptions import daily_cost

    total = Decimal(0)
    for term in terms:
        day = max(start, iso_date(term["start_date"]))
        stop = min(end, iso_date(term["end_date"], True) or end)
        while day <= stop:
            total += daily_cost(term["amount_usd"], term["cadence"], day)
            day += timedelta(days=1)
    return total


def apply_mutation(connection, body, today):
    allowed = {
        "operation",
        "request_id",
        "plan_id",
        "expected_version",
        "term_id",
        "values",
        "effective_date",
        "end_date",
        "preview",
        "confirmed",
        "preview_token",
    }
    if not isinstance(body, dict) or set(body) - allowed:
        raise PlanError("Unknown request fields")
    op = body.get("operation")
    if op not in ("add", "schedule", "end", "correct"):
        raise PlanError("Unsupported plan operation")
    common = {"operation", "request_id"}
    operation_fields = {
        "add": {"values"},
        "schedule": {"plan_id", "expected_version", "values", "effective_date"},
        "end": {"plan_id", "expected_version", "end_date"},
        "correct": {
            "plan_id",
            "expected_version",
            "term_id",
            "values",
            "preview",
            "confirmed",
            "preview_token",
        },
    }
    if set(body) - common - operation_fields[op]:
        raise PlanError("Fields do not match the selected operation")
    request_id = body.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,100}", request_id
    ):
        raise PlanError("A valid request_id is required")
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    previous = connection.execute(
        "SELECT digest,response FROM subscription_mutations WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if previous:
        if previous["digest"] != digest:
            raise PlanConflict(
                "This request_id was already used for a different change"
            )
        return json.loads(previous["response"])
    preview = body.get("preview", False)
    if type(preview) is not bool or type(body.get("confirmed", False)) is not bool:
        raise PlanError("Preview and confirmation must be boolean")
    if op == "add":
        term = validate_term(body.get("values") or {})
        plan_id = create_plan(connection, term)
        result = {"id": plan_id, "version": 1}
    else:
        plan_id = body.get("plan_id")
        if type(plan_id) is not int or type(body.get("expected_version")) is not int:
            raise PlanError("Plan identity and expected version are required")
        plan = connection.execute(
            "SELECT version,end_date FROM subscription_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise PlanError("Plan does not exist")
        if plan[0] != body["expected_version"]:
            raise PlanConflict(
                "This plan changed since you opened it. Reload its history before saving."
            )
        old = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM subscriptions WHERE plan_id=? ORDER BY start_date",
                (plan_id,),
            )
        ]
        terms = [dict(row) for row in old]
        if op == "schedule":
            start = iso_date(body.get("effective_date"))
            if start < today:
                raise PlanError("Use historical correction for a date before today")
            if plan["end_date"] and start.isoformat() > plan["end_date"]:
                raise PlanError("Scheduled change is after the plan's last active date")
            current = next(
                (
                    t
                    for t in terms
                    if t["start_date"] <= start.isoformat()
                    and (not t["end_date"] or t["end_date"] >= start.isoformat())
                ),
                None,
            )
            if current is None or current["start_date"] == start.isoformat():
                raise PlanError(
                    "A term already starts on this date, or the plan is inactive; use correction"
                )
            values = body.get("values") or {}
            if set(values) != {"amount_usd", "cadence"}:
                raise PlanError("Schedule accepts only amount and cadence")
            new = validate_term(
                {
                    **{k: current[k] for k in FIELDS},
                    **values,
                    "start_date": start.isoformat(),
                }
            )
            current["end_date"] = (start - timedelta(days=1)).isoformat()
            terms.append({**new, "id": None, "plan_id": plan_id})
        elif op == "end":
            ending = iso_date(body.get("end_date"), True)
            if ending is not None and ending < iso_date(terms[0]["start_date"]):
                raise PlanError("Last active date cannot precede the first term")
            connection.execute(
                "UPDATE subscription_plans SET end_date=? WHERE id=?",
                (ending.isoformat() if ending else None, plan_id),
            )
            terms[-1]["end_date"] = None
        else:
            term = next((t for t in terms if t["id"] == body.get("term_id")), None)
            if term is None:
                raise PlanError("Term does not exist")
            term.update(validate_term(body.get("values") or {}))
        _validate_overlap(terms)
        if op == "correct":
            start = min(iso_date(t["start_date"]) for t in old + terms)
            end = max(
                [today]
                + [iso_date(t["end_date"]) for t in old + terms if t["end_date"]]
            )
            if plan["end_date"]:
                end = min(end, iso_date(plan["end_date"]))
            token = hashlib.sha256(
                json.dumps([plan_id, plan[0], terms], sort_keys=True).encode()
            ).hexdigest()
            impact = {
                "from": start.isoformat(),
                "through": end.isoformat(),
                "before_usd": str(_sum(old, start, end)),
                "after_usd": str(_sum(terms, start, end)),
                "note": "Preview covers these calendar dates only; open-ended future days are not included.",
                "preview_token": token,
            }
            if preview:
                return impact
            if not body.get("confirmed") or body.get("preview_token") != token:
                raise PlanError(
                    "Preview and explicitly confirm the historical correction first"
                )
        elif preview:
            raise PlanError("Preview is supported for historical correction only")
        for term in terms:
            if term["id"] is None:
                connection.execute(
                    "INSERT INTO subscriptions(plan_id,name,tool_key,amount_usd,cadence,start_date,end_date) VALUES(?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        *(
                            term[k]
                            for k in (
                                "name",
                                "tool_key",
                                "amount_usd",
                                "cadence",
                                "start_date",
                                "end_date",
                            )
                        ),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE subscriptions SET name=?,tool_key=?,amount_usd=?,cadence=?,start_date=?,end_date=? WHERE id=?",
                    (
                        *(
                            term[k]
                            for k in (
                                "name",
                                "tool_key",
                                "amount_usd",
                                "cadence",
                                "start_date",
                                "end_date",
                            )
                        ),
                        term["id"],
                    ),
                )
        connection.execute(
            "UPDATE subscription_plans SET version=version+1 WHERE id=?", (plan_id,)
        )
        result = {"id": plan_id, "version": plan[0] + 1}
        # Reconcile every previously materialized date for this logical plan,
        # including deleting obsolete daily rows after an end-date correction.
        bounds = connection.execute(
            "SELECT MIN(date),MAX(date) FROM subscription_daily_costs WHERE subscription_id IN (SELECT id FROM subscriptions WHERE plan_id=?)",
            (plan_id,),
        ).fetchone()
        if bounds[0]:
            from spend_app.subscriptions import materialize_subscription_days

            materialize_subscription_days(
                connection, start=iso_date(bounds[0]), end=iso_date(bounds[1])
            )
    connection.execute(
        "INSERT INTO subscription_mutations VALUES(?,?,?)",
        (request_id, digest, json.dumps(result)),
    )
    return result
