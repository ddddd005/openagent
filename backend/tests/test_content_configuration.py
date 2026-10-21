"""Offline management authority checks without an HTTP or graph runtime."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.content_configuration import ContentConfigurationService, LOREBOOK_RESOURCE_TYPE
from phase1_agent.content_contracts import default_presentation
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.host_sdk import DataTypeDefinition, TypeRegistry
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE
from phase1_agent.prompt_lifecycle_nodes import PROMPT_RESOURCE_TYPE
from phase1_agent.workflow_host import WorkflowHost
from credential_test_support import TestProtector


def reference(record):
    return {field: record[field] for field in ("envelope_version", "scope", "type_id", "resource_id")}


def prompt_record():
    return {"envelope_version": 1, "scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE,
            "resource_id": str(uuid4()), "data_schema_version": 2, "update_sequence": 1,
            "value": {"enabled": True, "members": [{
                "id": str(uuid4()), "text": "Original prompt", "presentation": default_presentation(),
                "metadata": {}, "lifecycle": "per_request", "compaction": "never"}]}}


def lorebook_record():
    return {"envelope_version": 1, "scope": "project:content", "type_id": LOREBOOK_RESOURCE_TYPE,
            "resource_id": str(uuid4()), "data_schema_version": 1, "update_sequence": 1,
            "value": {"name": "Current lorebook", "enabled": True, "entries": []}}


def provider_record():
    return {"envelope_version": 1, "scope": "workspace", "type_id": CHAT_PROVIDER_TYPE,
            "resource_id": str(uuid4()), "data_schema_version": 3, "update_sequence": 1,
            "value": {"name": "Current provider", "enabled": True, "protocol": "chat",
                      "base_url": "https://offline.invalid/v1", "credential_ref": None}}


@pytest.mark.parametrize("record_factory", [prompt_record, lorebook_record])
def test_content_save_reload_current_values_conflict_and_delete(tmp_path, record_factory):
    path = tmp_path / "content.sqlite"
    manager = ContentConfigurationService(path)
    original = record_factory()
    request = {"record": original, "expected_sequence": 0, "idempotency_key": str(uuid4())}
    accepted = manager.save_global_resource(**request)
    assert accepted == {"reference": reference(original), "update_sequence": 1, "deleted": False}
    changed = deepcopy(original)
    changed["update_sequence"] = 2
    changed["value"]["enabled"] = False
    manager.save_global_resource(changed, expected_sequence=1, idempotency_key=str(uuid4()))
    assert manager.save_global_resource(**request) == accepted
    assert manager.get_global_resource(reference(original)) == changed
    assert ContentConfigurationService(path).get_global_resource(reference(original)) == changed
    with pytest.raises(Exception) as conflict:
        manager.save_global_resource(changed, expected_sequence=1, idempotency_key=str(uuid4()))
    assert conflict.value.reason_code == "stale_revision"
    assert manager.list_global_resources() == [changed]
    removed = manager.delete_global_resource(reference(original), expected_sequence=2,
                                              idempotency_key=str(uuid4()))
    assert removed["deleted"] is True and removed["update_sequence"] == 3
    assert manager.get_global_resource(reference(original)) is None
    assert manager.list_global_resources() == []
    assert not hasattr(manager, "credentials")
    assert not (tmp_path / "content.sqlite.credentials.sqlite").exists()


def test_provider_and_content_management_do_not_read_or_delete_each_others_records(tmp_path):
    path = tmp_path / "shared.sqlite"
    content = ContentConfigurationService(path)
    providers = ProviderConfigurationService(path, credential_protector=TestProtector())
    prompt, provider = prompt_record(), provider_record()
    content.save_global_resource(prompt, expected_sequence=0, idempotency_key=str(uuid4()))
    providers.save_global_resource(provider, expected_sequence=0, idempotency_key=str(uuid4()))
    assert content.list_global_resources() == [prompt]
    assert providers.list_global_resources() == [provider]
    for manager, foreign in ((providers, prompt), (content, provider)):
        for action in (
            lambda: manager.get_global_resource(reference(foreign)),
            lambda: manager.save_global_resource(foreign, expected_sequence=0, idempotency_key=str(uuid4())),
            lambda: manager.delete_global_resource(reference(foreign), expected_sequence=1,
                                                    idempotency_key=str(uuid4())),
        ):
            with pytest.raises(Exception) as denied:
                action()
            assert denied.value.reason_code.endswith("_resource_type_denied")
    assert content.get_global_resource(reference(prompt)) == prompt
    assert providers.get_global_resource(reference(provider)) == provider


def test_content_management_uses_current_trusted_global_types_but_not_provider_types(tmp_path):
    types = TypeRegistry()
    current_types = [types]
    manager = ContentConfigurationService(
        tmp_path / "extensions.sqlite", type_registry_factory=lambda: current_types[0])
    record = {"envelope_version": 1, "scope": "workspace", "type_id": "extension.content",
              "resource_id": str(uuid4()), "data_schema_version": 1, "update_sequence": 1,
              "value": "Trusted extension"}
    with pytest.raises(Exception) as missing:
        manager.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    assert missing.value.reason_code == "content_resource_type_denied"
    replacement = TypeRegistry()
    replacement.register(DataTypeDefinition("extension.content", 1, {"type": "string"}, scope="global"))
    replacement.register(DataTypeDefinition(CHAT_PROVIDER_TYPE, 3, {"type": "object"}, scope="global"))
    replacement.register(DataTypeDefinition("extension.session", 1, {"type": "string"}, scope="session"))
    current_types[0] = replacement
    manager.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    assert manager.get_global_resource(reference(record)) == record
    assert manager.list_global_resources() == [record]
    invalid = deepcopy(record)
    invalid["value"] = 3
    invalid["update_sequence"] = 2
    with pytest.raises(Exception) as invalid_schema:
        manager.save_global_resource(invalid, expected_sequence=1, idempotency_key=str(uuid4()))
    assert invalid_schema.value.reason_code == "host_schema_mismatch"
    for foreign_type in (CHAT_PROVIDER_TYPE, "extension.session", "unregistered.content"):
        with pytest.raises(Exception) as denied:
            manager.list_global_resources(type_id=foreign_type)
        assert denied.value.reason_code == "content_resource_type_denied"
    manager.delete_global_resource(reference(record), expected_sequence=1, idempotency_key=str(uuid4()))
    assert manager.list_global_resources() == []


def test_named_management_uses_content_authority_not_provider_or_graph(tmp_path, monkeypatch):
    host = WorkflowHost(tmp_path / "management.sqlite")
    app = GraphApplication(host)
    manager = host.provider_configuration
    def wrong_authority(*args, **kwargs):
        pytest.fail("Content management reached provider or graph execution")
    monkeypatch.setattr(manager, "save_global_resource", wrong_authority)
    monkeypatch.setattr(manager, "get_global_resource", wrong_authority)
    monkeypatch.setattr(manager, "delete_global_resource", wrong_authority)
    with closing(manager._store_factory()) as store:
        store._connection.execute("INSERT INTO records VALUES(?,?,?,1,'test')", (
            "workflow_session", str(uuid4()), '{"damaged":"unrelated"}'))
    record = prompt_record()
    command = {"record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())}
    accepted = app.command("resource.save", command)
    assert app.query("resource.read", {"identity": reference(record)}) == record
    assert app.query("resource.list") == [record]
    assert app.query("receipt.read", {"operation": "resource.save", "parameters": command})["outcome"] == "matched"
    assert accepted["receipt"]["accepted"] == accepted["result"]
    assert host._graph is None
    app.command("resource.delete", {"identity": reference(record), "expected_sequence": 1,
                                    "idempotency_key": str(uuid4())})
    assert host._graph is None
    host.close()


def test_resource_listing_merges_current_business_records_without_graph(tmp_path):
    host = WorkflowHost(tmp_path / "management.sqlite")
    app = GraphApplication(host)
    records = [prompt_record(), lorebook_record(), provider_record()]
    for record in records:
        app.command("resource.save", {"record": record, "expected_sequence": 0,
                                      "idempotency_key": str(uuid4())})
    assert app.query("resource.list") == sorted(
        records, key=lambda row: (row["scope"], row["type_id"], row["resource_id"]))
    assert host._graph is None
    assert host.content_configuration.list_global_resources(type_id=PROMPT_RESOURCE_TYPE) == [records[0]]
    assert host.provider_configuration.list_global_resources(type_id=CHAT_PROVIDER_TYPE) == [records[2]]


def test_content_operations_do_not_construct_provider_configuration(tmp_path):
    host = WorkflowHost(tmp_path / "content-only.sqlite")
    app, record = GraphApplication(host), prompt_record()
    app.command("resource.save", {"record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())})
    assert app.query("resource.read", {"identity": reference(record)}) == record
    assert app.query("resource.list", {"type_id": PROMPT_RESOURCE_TYPE}) == [record]
    app.command("resource.delete", {"identity": reference(record), "expected_sequence": 1,
                                    "idempotency_key": str(uuid4())})
    assert host._providers is None and host._graph is None
    assert not (tmp_path / "content-only.sqlite.credentials.sqlite").exists()


def test_http_handler_dispatches_management_without_binding_a_socket_or_graph(tmp_path, monkeypatch):
    import phase1_agent.server as transport

    class MockHTTPServer:
        def __init__(self, address, handler):
            self.server_address = address
            self.RequestHandlerClass = handler
        def server_close(self):
            pass

    monkeypatch.setattr(transport, "LocalHTTPServer", MockHTTPServer)
    host = WorkflowHost(tmp_path / "management.sqlite")
    host.provider_configuration.credentials.protector = TestProtector()
    server = transport.create_server(host, port=0)
    responses = []

    def dispatch(path, data):
        handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.command, handler.path, handler.server = "POST", path, server
        handler._validate_peer = lambda **kwargs: None
        handler._read_json = lambda maximum: data
        handler._json = lambda status, result: responses.append((status, result))
        handler._error = lambda status, code, message, *args, **kwargs: responses.append(
            (status, {"error": code, "message": message}))
        handler._dispatch("POST")
        return responses[-1]

    prompt = prompt_record()
    assert dispatch("/api/graph/commands", {
        "operation": "resource.save",
        "parameters": {"record": prompt, "expected_sequence": 0, "idempotency_key": str(uuid4())},
    })[0] == 200
    assert dispatch("/api/graph/queries", {
        "operation": "resource.read", "parameters": {"identity": reference(prompt)}}) == (200, prompt)
    for retired in ("read", "list", "save", "delete"):
        status, error = dispatch("/api/graph/resources/" + retired, {})
        assert status == 404 and error["error"] == "not_found"
    provider = provider_record()
    body = {"identity": reference(provider), "expected_sequence": 0,
            "credential_ref": "local:" + str(uuid4()), "expected_generation": 0,
            "idempotency_key": str(uuid4()),
            "provider_seed": {key: provider["value"][key] for key in (
                "name", "enabled", "protocol", "base_url")}}
    saved = dispatch("/api/graph/commands", {
        "operation": "provider.credential.save", "parameters": {**body, "secret": "synthetic-private-key"}})
    assert saved[0] == 200 and saved[1]["result"]["outcome"] == "saved"
    assert "synthetic-private-key" not in str(responses)
    assert dispatch("/api/graph/queries", {
        "operation": "provider.credential.receipt", "parameters": {"request": body}})[1]["outcome"] == "saved"
    assert dispatch("/api/graph/commands", {
        "operation": "provider.credential.bind", "parameters": body})[0] == 404
    assert dispatch("/api/graph/consumer/queries", {
        "operation": "credential.status", "parameters": {"credential_ref": body["credential_ref"]}})[0] == 403
    assert host._graph is None
    server.server_close()
