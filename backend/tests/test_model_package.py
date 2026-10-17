"""Current model package declarations and model-request boundary checks."""

import json
from uuid import UUID

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE, validate_chat_provider
from phase1_agent.model_package import MODEL_FRONTEND_EXTENSIONS, create_model_package
from phase1_agent.model_request_service import ModelRequestService
from test_model_request_service import ServiceHarness


def uid(number):
    return str(UUID(int=number, version=4))


def source_config():
    return {"reference": {"envelope_version": 1, "scope": "workspace",
                          "type_id": MODEL_PROFILE_TYPE, "resource_id": uid(1)}}


def profile_config():
    return {
        "name": "Unit model configuration",
        "provider": {"envelope_version": 1, "scope": "workspace",
                     "type_id": CHAT_PROVIDER_TYPE, "resource_id": uid(5)},
        "model": {"id": "deepseek-test", "capacity": {
            "context_window_tokens": 400000, "input_limit_tokens": 400000,
            "max_output_tokens": 64000,
        }},
        "runtime": {"thinking": "disabled", "context_length_tokens": 300000,
                    "compaction_threshold_bps": 8500, "output_limit_tokens": 32000},
        "enabled": True,
    }


class BoundaryFixture:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("The retired PublicModelService fixture is unavailable.")


def test_model_package_declares_exact_current_resource_management_and_source_editor():
    package = create_model_package()
    loaded = CapabilityPackageLoader((create_content_package(), package)).load(
        {"workflow.models": "1.0.0"})
    expected = sorted([
        {**row, "schema_version": 3, "host_protocol_version": 2,
         "package_id": "workflow.models", "package_version": "1.0.0"}
        if row["binding"]["slot"] == "node-fields"
        else {**row, "schema_version": 2, "host_protocol_version": 1,
              "package_id": "workflow.models", "package_version": "1.0.0"}
        for row in MODEL_FRONTEND_EXTENSIONS
    ], key=lambda row: row["extension_id"])
    assert list(loaded.frontend_extensions) == expected
    assert package.manifest.to_dict()["exports"]["frontend_extensions"] == [
        {"extension_id": row["extension_id"]} for row in MODEL_FRONTEND_EXTENSIONS]
    fields = next(row for row in loaded.frontend_extensions if row["kind"] == "field-editor")
    assert fields["binding"]["target"] == {"component_id": "models.source", "component_version": "7"}


def test_model_package_exports_without_agent_dependency():
    loaded = CapabilityPackageLoader((create_content_package(), create_model_package())).load(
        {"workflow.models": "1.0.0"})
    assert loaded.registry.get("workflow.agent", "1") is None
    assert loaded.registry.get("models.source", "7").resource_dependencies_declaration is not None
    assert loaded.registry.get("models.chat", "5").definition.input_storage == "references"
    assert loaded.registry.data_types.get(CHAT_PROVIDER_TYPE, 3, scope="global") is not None
    assert loaded.package_lock == ({"package_id": "workflow.content", "version": "1.0.0"},
                                   {"package_id": "workflow.models", "version": "1.0.0"})


@pytest.mark.parametrize("changes", [
    {"api_key": "fixture-only-secret"},
    {"credential_ref": "env:ARBITRARY_KEY"},
    {"credential_ref": "env:DEEPSEEK_API_KEY"},
    {"credential_ref": "env:GEMINI_API_KEY"},
    {"base_url": "https://user:fixture-only-secret@example.test"},
    {"base_url": "https://example.test?token=fixture-only-secret"},
    {"base_url": "http://remote.test"},
], ids=["api-key-field", "arbitrary-environment", "deepseek-environment", "gemini-environment",
        "url-userinfo", "url-token-query", "remote-http"])
def test_provider_rejects_secret_fields_and_uncontrolled_connections(changes):
    provider = {"name": "Test", "protocol": "chat", "base_url": "https://example.test",
                "credential_ref": "local:" + uid(99), "enabled": True}
    with pytest.raises(ContractValidationError):
        validate_chat_provider({**provider, **changes})


def test_model_service_requires_explicit_private_credential_broker():
    callbacks = {
        "authorize_binding": lambda *args: None,
        "read_resource_bundle": lambda *args: None,
        "check_resource_bundle": lambda *args: None,
        "accept_fact": lambda *args: None,
        "resolve_transport": lambda *args: None,
    }
    with pytest.raises(ContractValidationError) as rejected:
        ModelRequestService(**callbacks, credential_broker=object())
    assert rejected.value.reason_code == "model_credential_broker_unavailable"


def test_saved_private_key_is_used_instead_of_any_environment_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retired-environment-key")
    harness = ServiceHarness()
    prepared = harness.prepare()
    result = harness.dispatch(prepared)
    assert harness.communications[0]["api_key"] == "synthetic-private-key"
    assert "retired-environment-key" not in canonical_bytes(
        [prepared, result, list(harness.accepted.values())]).decode()
    harness.close()


def test_binding_and_request_facts_are_secret_free_and_reentry_never_redispatches():
    harness = ServiceHarness()
    prepared = harness.prepare()
    result = harness.dispatch(prepared)
    public = canonical_bytes([
        harness.binding, prepared, result, list(harness.accepted.values()),
    ])
    for forbidden in (b"synthetic-private-key", b"credential_ref", b"base_url"):
        assert forbidden not in public
    assert harness.prepare() == prepared
    assert harness.dispatch(prepared) == result
    assert len(harness.communications) == 1
    harness.close()


def test_more_than_old_run_request_limit_succeeds_and_idempotent_reentry_reuses_each_response():
    harness = ServiceHarness()
    messages = [{"kind": "text", "role": "user", "content": "fixture"}]
    results = []
    for index in range(129):
        key = f"normal:{index}"
        prepared = harness.prepare(key=key, messages=messages)
        result = harness.dispatch(prepared, key=key)
        assert result["content"] == "answer"
        assert harness.dispatch(prepared, key=key) == result
        assert len(harness.communications) == index + 1
        results.append(result)
    assert len({result["request_id"] for result in results}) == 129
    assert len(harness.communications) == 129
    assert len(harness.accepted) == 129 * 3
    harness.close()


def test_public_model_request_can_return_tool_calls_without_executing_them():
    harness = ServiceHarness()
    harness.response = ModelResponse(
        "tool_calls", tool_calls=(ModelToolCall("call-1", "inspect", "{}"),))
    tool = {"type": "function", "function": {
        "name": "inspect", "description": "Inspect",
        "parameters": {"type": "object"},
    }}
    prepared = harness.prepare(tools=[tool])
    result = harness.dispatch(prepared)
    assert result["tool_calls"] == [{"id": "call-1", "name": "inspect",
                                    "raw_arguments": "{}", "type": "function"}]
    assert len(harness.communications) == 1
    harness.close()


def test_distinct_invocations_keep_separate_requests_and_response_caches():
    harness = ServiceHarness()
    first_prepared = harness.prepare()
    first = harness.dispatch(first_prepared)
    first_owner = tuple(vars(harness.context).values())
    harness.context.node_binding_id = uid(23)
    harness.context.node_run_id = uid(22)
    harness.owner = tuple(vars(harness.context).values())
    second_prepared = harness.prepare()
    second = harness.dispatch(second_prepared)
    second_owner = tuple(vars(harness.context).values())
    assert second_owner != first_owner
    assert first["request_id"] != second["request_id"]
    assert len(harness.communications) == 2
    harness.context.node_binding_id, harness.context.node_run_id = first_owner[2:]
    harness.owner = first_owner
    assert harness.dispatch(first_prepared) == first
    assert len(harness.communications) == 2
    harness.context.node_binding_id, harness.context.node_run_id = second_owner[2:]
    harness.owner = second_owner
    assert harness.dispatch(second_prepared) == second
    assert len(harness.communications) == 2
    harness.close()
