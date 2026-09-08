"""BURNRATE-owned Windows vault references and explicit, bounded API tests."""
import base64
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from spend_app.connection_paths import LocationError
from spend_app.integration_policy import assert_allowed_host, identity_headers


class Vault:
    def __init__(self, database):
        self.service = "BURNRATE/connections/" + hashlib.sha256(os.path.normcase(str(database)).encode()).hexdigest()[:24]

    def backend(self):
        try:
            if os.name != "nt":
                raise ImportError()
            # Explicit Windows Credential Manager backend; never use global
            # keyring backend selection, plaintext plugins or fallback stores.
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
        except Exception:
            raise LocationError("Windows Credential Manager is unavailable. Local setup still works.") from None

    def available(self):
        try:
            self.backend()
            return True
        except LocationError:
            return False

    def put(self, secret, ref=None):
        ref = ref or "burnrate-" + uuid.uuid4().hex
        try:
            self.backend().set_password(self.service, ref, secret)
            if self.get(ref) != secret:
                raise RuntimeError()
            return ref
        except Exception:
            try:
                self.delete(ref)
            except Exception:
                pass
            raise LocationError("Secure credential storage failed; the existing connection was preserved.") from None

    def get(self, ref):
        if not isinstance(ref, str) or not ref.startswith("burnrate-"):
            raise LocationError("Invalid credential reference.")
        try:
            secret = self.backend().get_password(self.service, ref)
            if not secret:
                raise ValueError()
            return secret
        except Exception:
            raise LocationError("Saved credential unavailable. Replace it in Connect harness.") from None

    def delete(self, ref):
        if not ref.startswith("burnrate-"):
            raise LocationError("Not a BURNRATE-owned credential.")
        backend = self.backend()
        if backend.get_password(self.service, ref) is not None:
            backend.delete_password(self.service, ref)


def test_provider(source, key):
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    headers = identity_headers()
    method = "GET"
    params = {}
    body = None
    if source == "openai_admin":
        url = "https://api.openai.com/v1/organization/usage/completions"
        params = {"start_time": int(start.timestamp()), "end_time": int(end.timestamp()), "limit": 1}
        headers["Authorization"] = "Bearer " + key
    elif source == "anthropic_admin":
        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"
        params = {"starting_at": start.isoformat(), "ending_at": end.isoformat(), "limit": 1}
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif source == "cursor_admin":
        url = "https://api.cursor.com/teams/filtered-usage-events"
        method = "POST"
        body = {"startDate": int(start.timestamp() * 1000), "endDate": int(end.timestamp() * 1000), "page": 1, "pageSize": 1}
        headers["Authorization"] = "Basic " + base64.b64encode((key + ":").encode()).decode()
    elif source == "openrouter":
        url = "https://openrouter.ai/api/v1/credits"
        headers["Authorization"] = "Bearer " + key
    else:
        raise LocationError("Unsupported documented provider.")
    from urllib.parse import urlsplit
    assert_allowed_host("openrouter_credits" if source == "openrouter" else source, urlsplit(url).hostname)
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            # At most two fixed read endpoints (usage and cost), no pagination,
            # inference, redirects or retries. Stop at the first failure.
            urls = [url]
            if source == "openai_admin":
                urls.append("https://api.openai.com/v1/organization/costs")
            elif source == "anthropic_admin":
                urls.append("https://api.anthropic.com/v1/organizations/cost_report")
            for endpoint in urls:
                assert_allowed_host("openrouter_credits" if source == "openrouter" else source, urlsplit(endpoint).hostname)
                with client.stream(method, endpoint, headers=headers, params=params, json=body) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise LocationError("Key rejected or missing required admin/read scope. Check the key type and permissions.")
                    if status == 429:
                        raise LocationError("Provider rate limited the test. Wait before trying again.")
                    if status != 200:
                        raise LocationError("Provider test unavailable. Try again later; the previous connection is unchanged.")
                    # Never consume or expose a potentially large response body.
    except httpx.HTTPError:
        raise LocationError("Provider could not be reached. Check connectivity and retry later.") from None
