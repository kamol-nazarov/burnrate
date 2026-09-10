"""Pure reason normalization for persisted source evidence.

Inputs are supplied text only. No ingestion, registry, database, configuration,
clock or filesystem dependencies. Consumers retain their own status and reason
vocabulary; credential scrubbing alone is not a general privacy guarantee.
"""

import re

_CREDENTIAL_SHAPED = re.compile(
    r"(?i)(bearer\s+)\S+|(basic\s+)\S+|(sk-[a-z0-9_-]{8,})|(x-api-key\s*[:=]\s*)\S+|"
    r"(eyJ[A-Za-z0-9_-]{10,}\.)|(gh[pousr]_[A-Za-z0-9]{20,})|([A-Fa-f0-9]{32,})"
)
_MAX_REASON = 240


def sanitize_reason(value: object) -> str:
    """One-line, secret-free, class-free reason safe for DB/log/API."""
    text = str(value if value is not None else "").replace("\n", " ").replace("\r", " ")
    text = _CREDENTIAL_SHAPED.sub("[redacted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_REASON]


def source_reason_text(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = sanitize_reason(value)
    # ``sanitize_reason`` handles credential-shaped tokens and line breaks.
    # These additional replacements keep old/synthetic rows from exposing
    # personal locations or account identifiers through this API.
    text = re.sub(r"(?i)[A-Z]:\\[^\s;]+", "[path omitted]", text)
    text = re.sub(r"(?i)/(?:Users|home|var|etc|tmp)/[^\s;]+", "[path omitted]", text)
    text = re.sub(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[redacted]", text)
    if any(marker in text.lower() for marker in ("prompt", "raw response", "authorization", "bearer", "api key")):
        return "The latest source attempt reported a problem; other sources continue independently."
    return " ".join(text.split())[:240] or None
