"""Connection units: fake persistence, filesystem, vault, HTTP and collectors only."""
import asyncio
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from spend_app import connections as c
from spend_app.connection_api import payload, trusted
from spend_app.connection_paths import Inspector, LocationError, identity, normalize
from spend_app.connection_secrets import Vault, test_provider as probe_provider
from spend_app.providers import REGISTRY


class MemoryStore:
    def __init__(self):
        self.raw = json.dumps(c.empty_state())
        self.fail = False

    def read(self):
        return c.decode(self.raw)

    @contextmanager
    def transaction(self):
        state = self.read()
        yield state
        if self.fail:
            raise OSError("storage failed")
        self.raw = json.dumps(state)


class FakeInspector:
    def __init__(self):
        self.calls = []
        self.usable = False
        self.fail = False

    def normalize(self, raw, meta):
        if self.fail or not raw:
            raise LocationError("Missing location")
        return raw

    def inspect(self, source, path, meta):
        self.calls.append((source, path))
        return {"state": "readable", "usableSample": self.usable, "detail": "Readable sample"}


class FakeVault:
    def __init__(self):
        self.values = {}
        self.enabled = True
        self.fail_delete = False

    def available(self):
        return self.enabled

    def put(self, secret, ref=None):
        ref = ref or "burnrate-" + uuid4().hex
        self.values[ref] = secret
        return ref

    def get(self, ref):
        return self.values[ref]

    def delete(self, ref):
        if self.fail_delete:
            raise OSError()
        self.values.pop(ref, None)


@pytest.fixture
def service(monkeypatch):
    for name in ("OPENAI_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY", "CURSOR_API_KEY", "CURSOR_IMPORT_PATH", "OPENROUTER_MANAGEMENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = SimpleNamespace(database_path=Path("C:/fake/burnrate.db"), cursor_import_path=Path("C:/fake/imports"), openai_admin_key=None, anthropic_admin_key=None, cursor_api_key=None)
    return c.Service(settings, MemoryStore(), FakeInspector(), FakeVault(), Mock())


def body(source="codex_local", revision=0, operation="connect", **kwargs):
    return {"source": source, "revision": revision, "operation": operation, "location": "C:/fake/sessions", "requestId": str(uuid4()), **kwargs}


def test_save_reconstruct_disable_and_reconnect(service):
    first = service.mutate(body())
    assert first["connection"]["state"] == "awaiting_ingest"
    reconstructed = c.Service(service.settings, service.store, service.inspector, service.vault, service.probe)
    assert reconstructed.store.read()["bindings"]["codex_local"]["location"] == "C:/fake/sessions"
    reconstructed.mutate(body(revision=1, operation="disable"))
    assert not c.eligible(REGISTRY.get("codex_local"), service.store.read())
    service.discover()
    assert not c.eligible(REGISTRY.get("codex_local"), service.store.read())
    reconstructed.mutate(body(revision=2, operation="reconnect"))
    assert c.eligible(REGISTRY.get("codex_local"), service.store.read())


def test_retry_idempotency_and_conflicts(service):
    request = body()
    result = service.mutate(request)
    assert service.mutate(request) == result
    assert len(service.inspector.calls) == 1
    with pytest.raises(c.Conflict):
        service.mutate({**request, "location": "C:/other"})
    with pytest.raises(c.Conflict):
        service.mutate(body())


def test_failed_replacement_preserves_binding(service):
    service.mutate(body())
    before = service.store.raw
    service.inspector.fail = True
    with pytest.raises(LocationError):
        service.mutate(body(revision=1, location="C:/missing"))
    assert service.store.raw == before


def test_old_completion_and_empty_unpriced_states(service):
    service.mutate(body())
    service.completion("codex_local", 1, {"status": "partial", "eventsSeen": 2})
    assert service.store.read()["bindings"]["codex_local"]["state"] == "receiving_usage"
    service.mutate(body(revision=1, location="C:/fake/new"))
    service.completion("codex_local", 1, {"status": "success", "eventsSeen": 5})
    record = service.store.read()["bindings"]["codex_local"]
    assert record["lastImport"] is None
    assert record["state"] == "awaiting_ingest"
    service.completion("codex_local", 2, {"status": "success", "eventsSeen": 0})
    assert service.store.read()["bindings"]["codex_local"]["state"] == "waiting_activity"


def test_discovery_bounded_catalog_ambiguity_and_no_secret_reads(service):
    service.mutate(body(location="C:/different"))
    service.vault.get = Mock(side_effect=AssertionError("secret read"))
    result = service.discover()
    codex = next(row for row in result["connections"] if row["source"] == "codex_local")
    assert len(codex["candidates"]) == 2
    assert all(row["kind"] != "api" for row in result["connections"])
    assert len(result["connections"]) == 8
    service.probe.assert_not_called()
    service.inspector.fail = True
    assert all(candidate["state"] == "needs_attention" for row in service.discover()["connections"] for candidate in row["candidates"])


def test_external_credentials_are_not_returned_or_modified(service, monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "external-secret")
    status = service.list()
    assert "external-secret" not in json.dumps(status)
    assert next(row for row in status["connections"] if row["source"] == "openai_admin")["externallyManaged"]
    with pytest.raises(c.Conflict):
        service.mutate(body("openai_admin", secret="other"))
    assert service.vault.values == {}


def test_api_reference_and_cleanup_and_no_auto_borrowing(service):
    request = body("openai_admin", location="", mode="api", secret="test-key")
    result = service.mutate(request)
    assert "test-key" not in service.store.raw
    assert "credentialRef" not in json.dumps(result)
    service.probe.assert_called_once_with("openai_admin", "test-key")
    assert service.mutate(request) == result
    service.mutate(body("openai_admin", revision=1, operation="disable"))
    assert not service.vault.values
    for source in ("cursor_usage_service", "claude_oauth_usage", "zai_quota_endpoint"):
        with pytest.raises(LocationError):
            service.mutate(body(source))


def test_vault_unavailable_and_failed_commit_cleanup(service):
    service.vault.enabled = False
    with pytest.raises(LocationError):
        service.mutate(body("openai_admin", secret="key"))
    service.mutate(body())  # local setup remains usable
    service.vault.enabled = True
    service.store.fail = True
    with pytest.raises(OSError):
        service.mutate(body("openai_admin", secret="key"))
    assert not service.vault.values
    assert "openai_admin" not in service.store.read()["bindings"]


def test_dispatch_root_change_disable_and_unrelated_source(service, monkeypatch):
    monkeypatch.setattr(c, "Service", lambda settings: service)
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    codex = REGISTRY.get("codex_local")
    service.mutate(body(location="C:/fake/B"))
    _, binding, kwargs = c.managed_job(service.settings, object(), codex)
    assert str(kwargs["session_glob"]).replace("\\", "/") == "C:/fake/B/**/*.jsonl"
    service.mutate(body(revision=1, operation="disable"))
    assert c.managed_job(service.settings, object(), codex)[2] is None
    assert c.managed_job(service.settings, object(), REGISTRY.get("claude_local"))[2] is not None


def test_experimental_gate_legacy_and_managed(service):
    spec = REGISTRY.get("cursor_local")
    assert not c.eligible(spec, service.store.read())
    with service.store.transaction() as state:
        state["legacy"].append(spec.key)
    assert c.eligible(spec, service.store.read())
    service.mutate(body(spec.key, operation="disable"))
    assert not c.eligible(spec, service.store.read())


@pytest.mark.parametrize("path", ["https://host/data", r"\\host\data", r"\\?\C:\data", "C:/fakepath/file.jsonl", "C:/data/*.jsonl", "$(whoami)", "C:/file:stream", "relative", "C:/a\x00b"])
def test_invalid_paths_without_filesystem_access(path):
    with patch.object(Path, "resolve", side_effect=AssertionError("filesystem")):
        with pytest.raises(LocationError):
            normalize(path, REGISTRY.get("codex_local").connection)


def test_home_normalization_unicode_spaces_and_identity():
    with patch.object(Path, "resolve", lambda self, **kw: self), patch.object(Path, "is_dir", return_value=True), patch.object(Path, "is_file", return_value=False):
        path = normalize("C:/Users/名前 With Space/.codex", REGISTRY.get("codex_local").connection)
        assert path.replace("\\", "/").endswith("/.codex/sessions")
        assert identity("C:/Users/NAME") == identity("c:/users/name")


def test_permission_failure_is_friendly():
    with patch.object(Path, "resolve", side_effect=PermissionError("private path")):
        with pytest.raises(LocationError, match="Permission denied"):
            normalize("C:/fake/sessions", REGISTRY.get("codex_local").connection)


def request(origin="http://127.0.0.1:3060", host="127.0.0.1:3060", data=b"{}", **headers):
    async def stream():
        yield data
    return SimpleNamespace(headers={"host": host, "origin": origin, "content-type": "application/json", "x-burnrate-request": "1", **headers}, url=SimpleNamespace(scheme="http"), stream=stream)


def test_cross_origin_and_payload_guards():
    assert trusted(request())
    assert not trusted(request(origin="http://evil.test"))
    assert not trusted(request(host="evil.test", origin="http://evil.test"))
    assert not trusted(request(origin="http://127.0.0.1:3060/path"))
    with pytest.raises(PermissionError):
        asyncio.run(payload(request(**{"x-burnrate-request": "0"})))
    with pytest.raises(LocationError):
        asyncio.run(payload(request(data=b" " * 16385)))
    assert asyncio.run(payload(request())) == {}


@pytest.mark.parametrize("status,reason", [(401, "Key rejected"), (403, "Key rejected"), (429, "rate limited"), (500, "unavailable")])
def test_provider_test_single_request_redacted(status, reason, monkeypatch):
    response = Mock(status_code=status)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    client = Mock()
    client.stream.return_value = response
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("spend_app.connection_secrets.httpx.Client", Mock(return_value=client))
    with pytest.raises(LocationError, match=reason):
        probe_provider("openai_admin", "test-secret")
    assert client.stream.call_count == 1
    assert client.stream.call_args.args[1] == "https://api.openai.com/v1/organization/usage/completions"


def test_production_store_transaction_uses_versioned_json_without_database():
    db = Mock()
    db.execute.return_value.fetchone.return_value = None
    @contextmanager
    def fake_connect(path):
        yield db
    with patch.object(c, "connect", fake_connect):
        store = c.Store("fake")
        with store.transaction() as state:
            state["bindings"]["codex_local"] = {"revision": 1}
        calls = db.execute.call_args_list
        assert calls[0].args == ("BEGIN IMMEDIATE",)
        serialized = calls[-1].args[1][1]
        assert c.decode(serialized)["bindings"]["codex_local"]["revision"] == 1


@pytest.mark.parametrize("source,data,usable", [
    ("codex_local", b'{"type":"event_msg","payload":{"type":"token_count"}}\n', True),
    ("codex_local", b'{"type":"session_meta"}\n', False),
    ("claude_local", b'{"type":"assistant","message":[]}\n', False),
    ("claude_local", b'{"type":"assistant","message":{"usage":{"input_tokens":2}}}\n', True),
    ("grok_local", b'{"msg":"started","sid":"x"}\n', False),
])
def test_metadata_sample_never_returns_content(source, data, usable, monkeypatch):
    import io
    from spend_app import connection_paths as paths
    monkeypatch.setattr(paths, "source_files", lambda *args, **kwargs: iter([Path("C:/fake/sample.jsonl")]))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(data))
    result = Inspector().inspect(source, "C:/fake", REGISTRY.get(source).connection)
    assert result["usableSample"] is usable
    assert set(result) == {"state", "usableSample", "detail"}


def test_malformed_metadata_is_not_verified(monkeypatch):
    import io
    from spend_app import connection_paths as paths
    monkeypatch.setattr(paths, "source_files", lambda *args, **kwargs: iter([Path("C:/fake/sample.jsonl")]))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(b'{"secret":"not a transcript"}\n'))
    with pytest.raises(LocationError, match="not a supported"):
        Inspector().inspect("codex_local", "C:/fake", REGISTRY.get("codex_local").connection)


def test_schema_validation_uses_readonly_metadata_query(monkeypatch):
    from spend_app import connection_paths as paths
    meta = REGISTRY.get("opencode_local").connection
    monkeypatch.setattr(paths, "source_files", lambda *args, **kwargs: iter([Path("C:/fake/source.db")]))
    db = Mock()
    db.__enter__ = Mock(return_value=db)
    db.__exit__ = Mock(return_value=False)
    db.execute.side_effect = [Mock(), [(i, name) for i, name in enumerate(meta.columns)], Mock(fetchone=lambda: None)]
    connector = Mock(return_value=db)
    monkeypatch.setattr(paths.sqlite3, "connect", connector)
    assert Inspector().inspect("opencode_local", "C:/fake", meta)["usableSample"] is False
    assert "mode=ro" in connector.call_args.args[0]
    assert "query_only" in db.execute.call_args_list[0].args[0]
    db.execute.side_effect = [Mock(), [(0, "unrelated")]]
    with pytest.raises(LocationError, match="incompatible"):
        Inspector().inspect("opencode_local", "C:/fake", meta)


def test_discovery_walk_is_bounded_and_skips_links(monkeypatch):
    from spend_app import connection_paths as paths
    monkeypatch.setattr(paths, "confined", lambda path, root=None: Path(path))
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    link = SimpleNamespace(is_symlink=lambda: True)
    class Entries:
        def __enter__(self):
            return iter([link] * 257)
        def __exit__(self, *args):
            pass
    scanner = Mock(return_value=Entries())
    monkeypatch.setattr(paths.os, "scandir", scanner)
    with pytest.raises(LocationError, match="limit reached"):
        list(paths.source_files("C:/fake", "**/*.jsonl", budget=256))
    assert scanner.call_count == 1


def test_cache_scope_and_reconnect_keep_event_identity(monkeypatch):
    from spend_app.connection_paths import approved, cache_identity
    from spend_app.adapters.opencode_local import progress_for_location
    from spend_app.adapters import opencode_local
    monkeypatch.setattr(Path, "resolve", lambda self, **kwargs: self)
    with approved("C:/B", 1):
        first = cache_identity("C:/one.db", "C:/B/log.jsonl")
        other = cache_identity("C:/two.db", "C:/B/log.jsonl")
    with approved("C:/B", 2):
        assert first != cache_identity("C:/one.db", "C:/B/log.jsonl")
    assert first != other
    reader = Mock(return_value={"input_tokens": 5})
    monkeypatch.setattr(opencode_local, "read_opencode_progress", reader)
    assert progress_for_location(object(), Path("C:/B/db"), "scopeB", "session", "provider") == {"input_tokens": 5}
    assert reader.call_args_list[0].kwargs == {"session_id": "scopeB:session", "provider_id": "provider"}
    assert reader.call_args_list[1].kwargs == {"session_id": "session", "provider_id": "provider"}


def test_managed_activity_never_calls_default_profile(service, monkeypatch):
    from spend_app import quotas
    service.mutate(body())
    monkeypatch.setattr(c, "Store", lambda path: service.store)
    for name in ("_traycer_activity", "_grok_active_sessions", "_cursor_active_sessions", "_zcode_active_sessions", "_antigravity_active_sessions", "_claude_active_sessions"):
        monkeypatch.setattr(quotas, name, lambda: {})
    reader = Mock(side_effect=AssertionError("wrong default profile"))
    monkeypatch.setattr(quotas, "_codex_active_sessions", reader)
    assert quotas.default_activity_collector(service.settings.database_path)["codexSessions"] == []
    reader.assert_not_called()


def test_actual_execute_supplies_binding_path_and_records_revision(service, monkeypatch):
    from spend_app.connection_paths import APPROVED_ROOT
    monkeypatch.setattr(c, "Service", lambda settings: service)
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    service.mutate(body(location="C:/fake/B"))
    captured = []
    def ingest(**kwargs):
        captured.append((kwargs, APPROVED_ROOT.get()))
        return {"status": "partial", "eventsSeen": 3}
    result = c.execute(service.settings, "prices", REGISTRY.get("codex_local"), ingest)
    assert result["eventsSeen"] == 3
    assert captured[0][1] == "C:/fake/B"
    assert captured[0][0]["database_path"] == service.settings.database_path
    assert service.store.read()["bindings"]["codex_local"]["importRevision"] == 1
    assert APPROVED_ROOT.get() is None


def test_late_validation_cannot_replace_newer_revision(service):
    original = service.inspector.inspect
    def intervening_change(*args):
        with service.store.transaction() as state:
            state["bindings"]["codex_local"] = {"revision": 1, "location": "C:/newer"}
        return original(*args)
    service.inspector.inspect = intervening_change
    with pytest.raises(c.Conflict):
        service.mutate(body())
    assert service.store.read()["bindings"]["codex_local"]["location"] == "C:/newer"


def test_recheck_failure_exposes_attention_without_replacing_location(service):
    service.mutate(body())
    service.inspector.fail = True
    with pytest.raises(LocationError):
        service.mutate(body(revision=1, operation="recheck"))
    record = service.store.read()["bindings"]["codex_local"]
    assert record["state"] == "needs_attention"
    assert record["location"] == "C:/fake/sessions"


def test_vault_cleanup_journal_retries_only_owned_orphans(service):
    service.mutate(body("openai_admin", mode="api", secret="first"))
    first = next(iter(service.vault.values))
    service.vault.fail_delete = True
    service.mutate(body("openai_admin", revision=1, mode="api", secret="second"))
    assert first in service.store.read()["cleanup"]
    current = service.store.read()["bindings"]["openai_admin"]["credentialRef"]
    service.vault.fail_delete = False
    service.cleanup()
    assert first not in service.vault.values
    assert service.vault.values[current] == "second"


def test_authentication_is_required_for_discovery_and_status():
    from spend_app.connection_api import access_allowed
    for path in ("/api/connections", "/api/connections/discover", "/api/connections/verify"):
        req = SimpleNamespace(url=SimpleNamespace(path=path), headers={})
        assert not access_allowed(req, "configured-token")
        req.headers["authorization"] = "Bearer wrong"
        assert not access_allowed(req, "configured-token")
        req.headers["authorization"] = "Bearer configured-token"
        assert access_allowed(req, "configured-token")


def test_opencode_replay_event_ids_do_not_include_binding_revision():
    from datetime import UTC, datetime
    from spend_app.adapters.opencode_local import SessionSnapshot, plan_rows
    from spend_app.connection_paths import approved
    snapshot = SessionSnapshot("session", "provider", "model", None, 10, 2, 0, 3, None, None, datetime(2026, 9, 7, tzinfo=UTC))
    with approved("C:/A", 1):
        rows_a, progress, _ = plan_rows([snapshot], {})
    with approved("C:/B", 9):
        rows_b, _, _ = plan_rows([snapshot], {})
        replay, _, _ = plan_rows([snapshot], progress)
    assert rows_a[0].raw_id == rows_b[0].raw_id
    assert replay == []


def test_moved_opencode_session_uses_accounted_delta_not_second_baseline(monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime
    from spend_app.adapters import opencode_local as adapter
    first = adapter.SessionSnapshot("session", "provider", "model", None, 10, 2, 0, 3, None, None, datetime(2026, 9, 7, tzinfo=UTC))
    _, progress, _ = adapter.plan_rows([first], {})
    accounted = progress["session\x1fprovider"]
    reader = Mock(side_effect=[None, accounted])
    monkeypatch.setattr(adapter, "read_opencode_progress", reader)
    previous = adapter.progress_for_location(object(), Path("C:/new/store.db"), "new-location", "session", "provider")
    rows, _, _ = adapter.plan_rows([replace(first, input_tokens=15)], {"session\x1fprovider": previous})
    assert len(rows) == 1
    assert rows[0].input_tokens == 5


def test_secure_storage_uses_only_explicit_windows_backend(monkeypatch):
    import sys
    fake = Mock()
    backend_type = Mock(return_value=fake)
    monkeypatch.setitem(sys.modules, "keyring.backends.Windows", SimpleNamespace(WinVaultKeyring=backend_type))
    vault = Vault("C:/fake/app.db")
    assert vault.backend() is fake
    backend_type.assert_called_once_with()
    fake.get_password.return_value = "test-key"
    ref = vault.put("test-key", "burnrate-unit")
    assert ref == "burnrate-unit"
    assert "test-key" not in vault.service
    fake.set_password.assert_called_once_with(vault.service, ref, "test-key")


def test_link_escape_is_rejected_without_reading_target(monkeypatch):
    from spend_app.connection_paths import confined
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: Path("C:/outside") if path.name == "escape" else path)
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    with pytest.raises(LocationError, match="outside"):
        confined("C:/approved/escape", "C:/approved")


def test_managed_import_errors_do_not_publish_paths_or_keys():
    from spend_app.adapters.common import public_error
    from spend_app.connection_paths import approved
    with approved("C:/private", 2):
        result = public_error(OSError("C:/private/path contains plain-secret"))
    assert "private" not in result
    assert "plain-secret" not in result


@pytest.mark.parametrize("source,expected", [("openai_admin", 2), ("anthropic_admin", 2), ("cursor_admin", 1), ("openrouter", 1)])
def test_explicit_provider_tests_use_only_bounded_fixed_reads(source, expected, monkeypatch):
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    client = Mock()
    client.stream.return_value = response
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    constructor = Mock(return_value=client)
    monkeypatch.setattr("spend_app.connection_secrets.httpx.Client", constructor)
    probe_provider(source, "key")
    assert client.stream.call_count == expected
    assert constructor.call_args.kwargs == {"timeout": 10, "trust_env": False, "follow_redirects": False}
    assert all("key" not in call.args[1] for call in client.stream.call_args_list)
    response.read.assert_not_called()


def test_recheck_does_not_reenable_disabled_binding(service):
    service.mutate(body())
    service.mutate(body(revision=1, operation="disable"))
    service.mutate(body(revision=2, operation="recheck"))
    record = service.store.read()["bindings"]["codex_local"]
    assert record["enabled"] is False
    assert record["state"] == "disabled"


def test_existing_external_native_consent_is_preserved_not_created(service, monkeypatch):
    monkeypatch.delenv("BURNRATE_ENABLE_CURSOR_USAGE_SERVICE", raising=False)
    spec = REGISTRY.get("cursor_usage_service")
    assert not c.eligible(spec, service.store.read())
    monkeypatch.setenv("BURNRATE_ENABLE_CURSOR_USAGE_SERVICE", "1")
    assert c.eligible(spec, service.store.read())
    with pytest.raises(LocationError):
        service.mutate(body("cursor_usage_service"))
