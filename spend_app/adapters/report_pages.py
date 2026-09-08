"""Bounded reporting pagination. No retries, provider probes or writes here."""
import time
from datetime import timedelta


def reporting_window(now):
    """Revisit completed UTC hours for late reports, without shifting buckets."""
    from datetime import UTC
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return end.replace(hour=0) - timedelta(days=7), end


class IncompleteReport(ValueError):
    pass


def fetch(client, *, url, params, max_pages=100, seconds=20, clock=time.monotonic):
    pages, seen, page = [], set(), None
    deadline = clock() + seconds
    for _ in range(max_pages):
        remaining = deadline - clock()
        if remaining <= 0:
            raise IncompleteReport("Reporting deadline reached; historical coverage is incomplete.")
        response = client.get(url, params={**params, **({"page": page} if page else {})}, timeout=min(10, remaining))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or type(payload.get("has_more")) is not bool:
            raise IncompleteReport("Invalid reporting page; previous accepted history was preserved.")
        pages.append(payload)
        if not payload["has_more"]:
            return pages
        page = payload.get("next_page")
        if not isinstance(page, str) or not page or page in seen:
            raise IncompleteReport("Missing or repeated reporting cursor; partial pages are not complete coverage.")
        seen.add(page)
    raise IncompleteReport("Reporting page limit reached; historical coverage is incomplete.")
