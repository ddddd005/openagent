from contextlib import closing
import json
import os
import sqlite3
from uuid import uuid4

import httpx
import pytest

from phase1_agent.credential_store import LocalCredentialBroker, PrivateCredentialStore, WindowsCredentialProtector
from phase1_agent.credential_contract import CredentialLease
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.model_contract import validate_provider_record
from phase1_agent.provider_discovery import discover_provider_models
from test_models_service_integration import ModelDatabaseFixture
from threading import Event
from copy import deepcopy
from credential_test_support import TestProtector, mutate_test_credential


def request(reference=None, generation=0):
    return {"credential_ref": reference or "local:" + str(uuid4()),
            "expected_generation": generation, "idempotency_key": str(uuid4())}


def test_private_store_is_encrypted_and_receipts_are_secret_free(tmp_path):
    path = tmp_path / "private.sqlite"
    store = PrivateCredentialStore(path, TestProtector())
    body = request()
    result = store.mutate("credential.save", body, "dummy-unit-test-key")
    assert result["generation"] == 1 and result["state"] == "saved"
    assert store.read(body["credential_ref"]) == ("dummy-unit-test-key", 1)
    assert store.receipt("credential.save", body) == result
    assert store.mutate("credential.save", body, "dummy-unit-test-key") == result
    assert b"dummy-unit-test-key" not in path.read_bytes()
    with closing(sqlite3.connect(path)) as connection:
        saved = connection.execute("SELECT request,result FROM receipts").fetchone()
        assert "secret" not in saved[0] and "dummy-unit-test-key" not in str(saved)


def test_same_identity_different_key_is_rejected(tmp_path):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    body = request()
    store.mutate("credential.save", body, "first-dummy-key")
    with pytest.raises(Exception) as failure:
        store.mutate("credential.save", body, "changed-dummy-key")
    assert failure.value.reason_code == "idempotency_conflict"
    assert store.read(body["credential_ref"])[0] == "first-dummy-key"


def test_receipt_requires_original_exact_generation_type(tmp_path):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    body = request()
    store.mutate("credential.save", body, "dummy-key")
    with pytest.raises(Exception) as failure:
        store.receipt("credential.save", {**body, "expected_generation": False})
    assert failure.value.reason_code == "invalid_request"


def test_receipt_rejects_corrupted_boolean_original_generation(tmp_path):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    body = request()
    store.mutate("credential.save", body, "dummy-key")
    with closing(sqlite3.connect(store.path)) as connection:
        original = {"operation": "credential.save", **body, "expected_generation": False}
        connection.execute("UPDATE receipts SET request=? WHERE key=?",
                           (json.dumps(original), body["idempotency_key"]))
        connection.commit()
    with pytest.raises(Exception) as failure:
        store.receipt("credential.save", body)
    assert failure.value.reason_code == "idempotency_conflict"


def test_cas_preserves_other_writers_key(tmp_path):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    body = request()
    store.mutate("credential.save", body, "first-dummy-key")
    with pytest.raises(Exception) as failure:
        store.mutate("credential.save", request(body["credential_ref"]), "later-dummy-key")
    assert failure.value.reason_code == "stale_credential_generation"
    assert store.read(body["credential_ref"])[0] == "first-dummy-key"


def test_unreadable_protected_key_is_not_reported_ready_and_does_not_leak_failure(tmp_path):
    protector = TestProtector()
    store = PrivateCredentialStore(tmp_path / "private.sqlite", protector)
    body = request()
    store.mutate("credential.save", body, "dummy-private-key")
    def fail(value):
        raise ValueError("dummy-private-key")
    protector.unprotect = fail
    with pytest.raises(Exception) as failure:
        store.status(body["credential_ref"])
    assert failure.value.reason_code == "credential_protection_failed"
    assert "dummy-private-key" not in str(failure.value)


@pytest.mark.parametrize("operation", ["credential.save", "credential.delete"])
def test_frozen_run_rejects_rotation_even_with_identical_key_or_deletion(tmp_path, operation):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    body = request()
    store.mutate("credential.save", body, "dummy-unit-key")
    broker = LocalCredentialBroker(store)
    lease = broker.freeze(body["credential_ref"])
    assert broker.authorize(lease) == "dummy-unit-key"
    store.mutate(operation, request(body["credential_ref"], 1), "dummy-unit-key" if operation.endswith("save") else None)
    with pytest.raises(Exception):
        broker.authorize(lease)
    assert store.status(body["credential_ref"])["generation"] == 2


@pytest.mark.parametrize("reference", ["env:DEEPSEEK_API_KEY", "env:GEMINI_API_KEY", "env:ANY_KEY"])
def test_retired_environment_route_is_rejected_even_when_present(tmp_path, monkeypatch, reference):
    store = PrivateCredentialStore(tmp_path / "private.sqlite", TestProtector())
    monkeypatch.setenv(reference[4:], "retired-environment-key")
    broker = LocalCredentialBroker(store)
    with pytest.raises(Exception) as rejected:
        broker.freeze(reference)
    assert rejected.value.reason_code == "credential_reference_invalid"
    with pytest.raises(Exception) as rejected:
        broker.authorize(CredentialLease(reference, b""))
    assert rejected.value.reason_code == "credential_reference_invalid"
    assert not store.path.exists()


def test_environment_status_entry_is_rejected_by_management(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retired-environment-key")
    with closing(GraphWorkflowService(tmp_path / "public.sqlite")) as service:
        with pytest.raises(Exception) as rejected:
            GraphApplication(service).query("credential.status", {"credential_ref": "env:DEEPSEEK_API_KEY"})
    assert rejected.value.reason_code == "credential_reference_invalid"


def test_unsupported_system_protection_does_not_fall_back_to_plaintext(tmp_path):
    protector = TestProtector()
    protector.available = False
    store = PrivateCredentialStore(tmp_path / "private.sqlite", protector)
    with pytest.raises(Exception) as failure:
        store.mutate("credential.save", request(), "dummy-key")
    assert failure.value.reason_code == "credential_protection_unavailable"
    assert not store.path.exists()


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows current-user facility")
def test_real_windows_protection_roundtrip_in_isolated_vault(tmp_path):
    store = PrivateCredentialStore(tmp_path / "isolated.sqlite")
    body = request()
    store.mutate("credential.save", body, "fake-key-for-dpapi-test-only")
    reopened = PrivateCredentialStore(store.path)
    assert reopened.read(body["credential_ref"])[0] == "fake-key-for-dpapi-test-only"
    assert b"fake-key-for-dpapi-test-only" not in store.path.read_bytes()
    with pytest.raises(Exception):
        WindowsCredentialProtector(tmp_path / "other.sqlite").unprotect(
            store.protector.protect(b"fake-key-for-path-isolation"))


def test_management_only_save_and_original_receipt_never_write_key_to_product_store(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "public.sqlite", credential_protector=TestProtector())) as service:
        app, body = GraphApplication(service), request()
        combined = {**body, "identity": {
            "envelope_version": 1, "scope": "workspace", "type_id": "workflow.chat-provider",
            "resource_id": str(uuid4())}, "expected_sequence": 0, "provider_seed": {
                "name": "Test provider", "protocol": "chat", "base_url": "https://offline.invalid",
                "enabled": True}}
        result = app.command("provider.credential.save", {**combined, "secret": "dummy-private-api-key"})
        assert "dummy-private-api-key" not in str(result)
        assert result["receipt"]["accepted"] == result["result"]
        assert app.query("provider.credential.receipt", {"request": combined}) == result["result"]
        assert b"dummy-private-api-key" not in service.database.read_bytes()
        for operation, parameters in [
            ("credential.status", {"credential_ref": body["credential_ref"]}),
            ("credential.protection", {}),
            ("provider.credential.receipt", {"request": combined}),
        ]:
            with pytest.raises(Exception) as failure:
                app.for_consumer().query(operation, parameters)
            assert failure.value.reason_code == "application_scope_denied"
        with pytest.raises(Exception):
            app.for_consumer().command("provider.credential.save", {**combined, "secret": "dummy-private-api-key"})


def provider(protocol="gemini"):
    return {"name": "test", "protocol": protocol,
            "base_url": "https://unit.invalid/v1beta" if protocol == "gemini" else "https://unit.invalid/v1",
            "credential_ref": "local:" + str(uuid4()), "enabled": True}


def client(handler):
    def factory(**options):
        assert options["follow_redirects"] is False and options["trust_env"] is False
        return httpx.Client(transport=httpx.MockTransport(handler), **options)
    return factory


def test_gemini_pagination_filters_methods_and_keeps_capacity_semantics():
    calls = []
    def handler(req):
        calls.append(req)
        assert req.headers["x-goog-api-key"] == "dummy-discovery-key"
        assert "key=" not in str(req.url)
        if len(calls) == 1:
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"],
                 "inputTokenLimit": 1000, "outputTokenLimit": 100},
                {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "next"})
        assert req.url.params["pageToken"] == "next"
        return httpx.Response(200, json={"models": [
            {"name": "models/future-unknown", "supportedGenerationMethods": ["generateContent"]}]})
    rows = discover_provider_models(provider(), "dummy-discovery-key", client(handler))
    assert len(calls) == 2
    assert rows[0]["input_token_limit"] == 1000
    assert rows[0]["output_token_limit"] == 100
    assert rows[0]["context_window_tokens"] is None
    assert rows[1]["adapted"] is False


def test_chat_discovery_tolerates_missing_capacity_without_inventing_numbers():
    def handler(req):
        assert req.headers["Authorization"] == "Bearer dummy-discovery-key"
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "chat", "context_window": 128000, "max_output_tokens": 8192},
                                               {"id": "proxy-model"}]})
    rows = discover_provider_models(provider("chat"), "dummy-discovery-key", client(handler))
    assert rows[0]["context_window_tokens"] == 128000
    assert rows[1]["context_window_tokens"] is None and rows[1]["output_token_limit"] is None


@pytest.mark.parametrize("mode", ["redirect", "provider-error", "body-capacity", "duplicate", "pagination"])
def test_discovery_errors_are_bounded_redacted_and_never_retried(mode):
    calls = []
    def handler(req):
        calls.append(req)
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://different.invalid/models"})
        if mode == "provider-error":
            return httpx.Response(403, text="dummy-sensitive-error-key")
        if mode == "body-capacity":
            return httpx.Response(200, content=b" " * 1_000_001)
        row = {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}
        return httpx.Response(200, json={"models": [row, row]} if mode == "duplicate"
                              else {"models": [], "nextPageToken": "repeated"})
    with pytest.raises(Exception) as failure:
        discover_provider_models(provider(), "dummy-sensitive-error-key", client(handler))
    assert "dummy-sensitive-error-key" not in str(failure.value)
    assert len(calls) == (2 if mode == "pagination" else 1)


def test_provider_schema_three_is_explicit_and_old_schemas_stay_unchanged():
    identity = {"envelope_version": 1, "scope": "workspace", "type_id": "workflow.chat-provider", "resource_id": str(uuid4())}
    value = {**provider(), "credential_ref": "local:" + str(uuid4())}
    record = {**identity, "data_schema_version": 3, "update_sequence": 1, "value": value}
    assert validate_provider_record(record, identity) == value
    with pytest.raises(Exception):
        validate_provider_record({**record, "data_schema_version": 2}, identity)


@pytest.mark.parametrize("mutation", [None, "credential.save", "credential.delete"])
def test_local_key_executes_through_existing_model_host_and_blocks_frozen_changes(tmp_path, mutation):
    fixture, entered, released = ModelDatabaseFixture(), Event(), Event()
    path = tmp_path / "local-model.sqlite"
    body = request()
    with closing(GraphWorkflowService(path, credential_protector=TestProtector(),
                                      public_model_factory=fixture.factory)) as service:
        mutate_test_credential(service.provider_configuration.credentials,
                               body["credential_ref"], "dummy-local-model-key")
        record = fixture.write(service, 1)
        record = {**record, "update_sequence": 2, "data_schema_version": 3,
                  "value": {**record["value"], "credential_ref": body["credential_ref"]}}
        service.save_global_resource(record, expected_sequence=1, idempotency_key=str(uuid4()))
        initial = fixture.create(service, fixture.document(service, gate=(entered, released)))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            if mutation:
                change = request(body["credential_ref"], 1)
                if mutation.endswith("save"):
                    mutate_test_credential(service.provider_configuration.credentials,
                                           change["credential_ref"], "dummy-local-model-key")
                else:
                    GraphApplication(service).command(mutation, change)
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        final = service.get_session(initial["workflow_session_id"])
        history = service.get_run(final["workflow_session_id"], started["active_chain_run_id"])
        assert final["status"] == ("failed" if mutation else "succeeded"), final
        assert len(fixture.calls) == (0 if mutation else 1)
        if mutation:
            assert history["runtime_facts"][-1]["details"]["classification"] == "not_dispatched"
        assert "dummy-local-model-key" not in str(history)
        assert b"dummy-local-model-key" not in path.read_bytes()


@pytest.mark.parametrize("change", ["provider", "credential"])
def test_discovery_rechecks_saved_provider_and_key_after_async_network_result(tmp_path, change):
    with closing(GraphWorkflowService(tmp_path / "discovery.sqlite", credential_protector=TestProtector())) as service:
        body = request()
        mutate_test_credential(service.provider_configuration.credentials,
                               body["credential_ref"], "dummy-list-key")
        record = ModelDatabaseFixture.write(service, 1)
        record = {**record, "update_sequence": 2, "data_schema_version": 3,
                  "value": {**record["value"], "credential_ref": body["credential_ref"]}}
        service.save_global_resource(record, expected_sequence=1, idempotency_key=str(uuid4()))
        identity = {key: record[key] for key in ("envelope_version", "scope", "type_id", "resource_id")}
        def handler(req):
            if change == "provider":
                updated = deepcopy(record)
                updated["update_sequence"] = 3
                updated["value"]["base_url"] = "https://other.unit.invalid"
                service.save_global_resource(updated, expected_sequence=2, idempotency_key=str(uuid4()))
            else:
                service.provider_configuration.delete_credential(body["credential_ref"], 1, str(uuid4()))
            return httpx.Response(200, json={"data": [{"id": "mock-chat"}]})
        service.provider_configuration.discovery_client_factory = client(handler)
        with pytest.raises(Exception) as failure:
            service.provider_configuration.list_provider_models(identity, 2)
        assert failure.value.reason_code in ("stale_provider_sequence", "model_credential_unavailable")
