"""Hermetic hooks for the spend suite.

Collection is limited by pytest.ini `testpaths = tests_spend`. This module
does not rewrite Path.home() globally — CLI tests assert default glob strings
that include the real home path without ingesting it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

FROZEN_NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_FAKE_TRANSPORTS = frozenset(
    {
        "MockTransport",
        "ASGITransport",
        "WSGITransport",
        "ASGI2Transport",
        "_TestClientTransport",
    }
)


class FrozenDateTime(datetime):
    """`datetime.now(UTC)` always returns the frozen acceptance clock."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "isolated_home: set HOME/USERPROFILE to a tmp_path (does not patch Path.home)",
    )


def pytest_ignore_collect(collection_path, config) -> bool | None:
    """Refuse files that are not under tests_spend (belt-and-suspenders with testpaths)."""
    tests_root = Path(__file__).resolve().parent
    path = Path(collection_path).resolve()
    try:
        path.relative_to(tests_root)
    except ValueError:
        return True
    return None


@pytest.fixture
def frozen_clock(monkeypatch):
    """Opt-in freeze of spend_app datetime.now to FROZEN_NOW."""
    monkeypatch.setattr("spend_app.aggregate.datetime", FrozenDateTime)
    monkeypatch.setattr("spend_app.api.datetime", FrozenDateTime, raising=False)
    return FrozenDateTime


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Opt-in HOME/USERPROFILE rewrite. Does not patch Path.home()."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture(autouse=True)
def _opt_in_isolated_home(request):
    if request.node.get_closest_marker("isolated_home"):
        request.getfixturevalue("isolated_home")


def _transport_name(client) -> str:
    transport = getattr(client, "_transport", None)
    return type(transport).__name__ if transport is not None else ""


def _request_host(client, url) -> str:
    import httpx

    merged = url if isinstance(url, httpx.URL) else httpx.URL(str(url))
    if not merged.is_absolute_url:
        merged = client.base_url.join(merged)
    return (merged.host or "").lower()


@pytest.fixture(autouse=True)
def _block_live_httpx(monkeypatch):
    """Fail httpx calls whose host is not loopback, unless the client is mocked.

    MockTransport and Starlette TestClient (`_TestClientTransport` / host
    `testserver`) never hit the network. Default HTTPTransport is allowed
    only for 127.0.0.1 / localhost.
    """
    import httpx

    original = httpx.Client.request

    def request(self, method, url, *args, **kwargs):
        if _transport_name(self) in _FAKE_TRANSPORTS:
            return original(self, method, url, *args, **kwargs)
        host = _request_host(self, url)
        if host not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"blocked live network request to host {host!r}")
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", request)
