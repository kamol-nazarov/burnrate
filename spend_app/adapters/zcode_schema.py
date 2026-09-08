"""Metadata-only ZCode database selection and exact token normalization."""
from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_millis, parse_iso_time
from spend_app.adapters.opencode_schema import count

REQUIRED = {"id", "session_id", "model_id", "input_tokens", "output_tokens"}
OPTIONAL = ("provider_id", "completed_at", "started_at", "reasoning_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "computed_total_tokens", "raw_usage_json", "status", "turn_id")


def select_rows(db, limit=None):
    columns = {row[1] for row in db.execute('PRAGMA table_info("model_usage")')}
    if not REQUIRED <= columns or not {"completed_at", "started_at"} & columns:
        raise ValueError("incompatible_zcode_schema")
    names = sorted(REQUIRED) + list(OPTIONAL)
    projection = ",".join('"' + name + '"' if name in columns else 'NULL' for name in names)
    predicate = " WHERE status='completed'" if "status" in columns else " WHERE completed_at IS NOT NULL" if "completed_at" in columns else ""
    sql = "SELECT " + projection + " FROM model_usage" + predicate
    if limit is not None:
        sql += " LIMIT " + str(int(limit))
    return [dict(zip(names, row)) for row in db.execute(sql)], "computed_total_tokens" in columns


def parse_row(record, modern=False):
    from spend_app.adapters.zcode_local import PLAN_PROVIDERS, canonical_model, _reasoning_tokens
    provider = record.get("provider_id")
    if provider and provider not in PLAN_PROVIDERS:
        return None, "non_plan"
    stamp = parse_millis(record.get("completed_at") or record.get("started_at"))
    if stamp is None or not record.get("id"):
        return None, "invalid_zcode_identity_time"
    inp, out = count(record.get("input_tokens")), count(record.get("output_tokens"))
    if inp is None or out is None:
        return None, "invalid_zcode_tokens"
    cached = count(record.get("cache_read_input_tokens"))
    writes = count(record.get("cache_creation_input_tokens"))
    complete = cached is not None and writes is not None
    cached, writes = cached or 0, writes or 0
    reason = _reasoning_tokens(record.get("reasoning_tokens"), record.get("raw_usage_json"))
    total = count(record.get("computed_total_tokens"))
    issue = None
    if not modern or total == inp + out:
        if cached + writes > inp or reason is not None and reason > out:
            return None, "incompatible_zcode_token_components"
        inp -= writes
    elif total == inp + out + cached + writes + (reason or 0):
        inp += cached
        out += reason or 0
    else:
        # A known total does not prove the component convention. Retain it as
        # unsplit evidence, with no inferred cache/price attribution.
        if total is None:
            return None, "unknown_zcode_token_convention"
        return UsageRow("zcode_local", "zcode", canonical_model(record.get("model_id")), stamp,
                        str(record.get("session_id") or "") or None, None, 0, 0, 0, 0, 0, None, None,
                        stable_id("zcode-local", record["id"]), total, False), "unknown_zcode_token_convention"
    if not provider:
        issue = "zcode_billing_scope_unavailable"
    return UsageRow("zcode_local", "zcode", canonical_model(record.get("model_id")), stamp,
                    str(record.get("session_id") or "") or None, None, inp, cached, writes, 0,
                    out, reason, None, stable_id("zcode-local", record["id"]), 0, complete), issue
