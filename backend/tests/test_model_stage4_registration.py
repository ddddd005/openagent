from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.model_contract import (
    CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE, model_type_definitions,
)
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.model_package import create_model_package
from phase1_agent.model_request_contracts import default_model_runtime
from phase1_agent.storage import SqliteStore

from credential_test_support import TestProtector


def _identity(type_id):
    return {"envelope_version": 1, "scope": "workspace",
            "type_id": type_id, "resource_id": str(uuid4())}


def _profile(provider):
    return {
        "name": "Four parameter profile", "provider": deepcopy(provider),
        "model": {"id": "deepseek-chat", "capacity": {
            "context_window_tokens": 400_000, "input_limit_tokens": 400_000,
            "max_output_tokens": 64_000,
        }},
        "runtime": default_model_runtime(), "enabled": True,
    }


def test_registered_model_contracts_are_the_single_current_identity_set():
    loaded = CapabilityPackageLoader((create_content_package(), create_model_package())).load(
        {"workflow.models": "1.0.0"})
    registry = loaded.registry
    definitions = model_type_definitions()
    assert {(item.scope, item.type_id, item.schema_version) for item in definitions} == {
        ("global", CHAT_PROVIDER_TYPE, 3),
        ("global", MODEL_PROFILE_TYPE, 2),
        ("content", "MODEL_BINDING", 5),
        ("content", "MODEL_RESULT", 3),
    }
    source = registry.get("models.source", "7").definition
    chat = registry.get("models.chat", "5").definition
    assert source.outputs[0].data_schema_version == 5
    assert source.service_requirements[0]["exact_version"] == "2.0.0"
    assert source.service_requirements[0]["operations"] == ["bind-native-model"]
    assert chat.inputs[0].data_schema_version == 5
    assert chat.outputs[0].data_schema_version == 3
    assert chat.service_requirements[0]["operations"] == ["prepare-request", "dispatch-request"]
    assert registry.get("models.source", "5") is None
    assert registry.get("models.source", "6") is None
    assert registry.get("models.chat", "3") is None
    assert registry.get("models.chat", "4") is None
    service = registry.services.get(MODEL_SERVICE_REF)
    assert service is not None
    assert { (item.capability, item.operation) for item in service.definition.operations } == {
        ("models:resolve", "bind-native-model"),
        ("models:call", "prepare-request"),
        ("models:call", "dispatch-request"),
        ("models:call", "discard-prepared-request"),
        ("model-projection:read", "measure-context"),
        ("model-facts:read", "read-model-request-facts"),
    }


def test_provider_management_saves_reads_lists_and_replays_schema2_receipt(tmp_path):
    manager = ProviderConfigurationService(
        tmp_path / "model-settings.sqlite", credential_protector=TestProtector())
    provider = _identity(CHAT_PROVIDER_TYPE)
    provider_value = {
        "name": "Offline provider", "protocol": "chat",
        "base_url": "https://provider.invalid/v1", "credential_ref": None, "enabled": True,
    }
    provider_record = {**provider, "data_schema_version": 3, "update_sequence": 1,
                       "value": provider_value}
    provider_key = str(uuid4())
    provider_receipt = manager.save_global_resource(
        provider_record, expected_sequence=0, idempotency_key=provider_key)
    assert manager.save_global_resource(
        provider_record, expected_sequence=0, idempotency_key=provider_key) == provider_receipt

    profile = _identity(MODEL_PROFILE_TYPE)
    profile_record = {**profile, "data_schema_version": 2, "update_sequence": 1,
                      "value": _profile(provider)}
    receipt_key = str(uuid4())
    accepted = manager.save_global_resource(
        profile_record, expected_sequence=0, idempotency_key=receipt_key)
    assert manager.save_global_resource(
        profile_record, expected_sequence=0, idempotency_key=receipt_key) == accepted
    assert manager.get_global_resource(profile) == profile_record
    assert manager.list_global_resources(scope="workspace", type_id=MODEL_PROFILE_TYPE) == [profile_record]


def test_legacy_profile_schema_is_visible_as_error_and_never_rewritten(tmp_path):
    manager = ProviderConfigurationService(
        tmp_path / "legacy-model-settings.sqlite", credential_protector=TestProtector())
    profile = _identity(MODEL_PROFILE_TYPE)
    legacy_record = {
        **profile, "data_schema_version": 1, "update_sequence": 4,
        "value": {
            "name": "Legacy profile", "protocol": "chat", "provider": _identity(CHAT_PROVIDER_TYPE),
            "parameters": {"model": "deepseek-chat", "max_tokens": 8192,
                           "temperature": 0.25, "thinking": "disabled", "stream": False},
            "capacity": {"context_window_tokens": 32768, "output_reserve_tokens": 8192,
                         "summary_max_tokens": 1024, "max_cold_input_tokens": 32768},
            "enabled": True,
        },
    }
    original_payload = canonical_bytes(legacy_record).decode("utf-8")
    with closing(manager._store_factory()) as store:
        store._connection.execute(
            "INSERT INTO global_resource_current VALUES(?,?,?,?,0,?)",
            (profile["scope"], profile["type_id"], profile["resource_id"], 4, original_payload))

    with pytest.raises(ContractValidationError) as get_error:
        manager.get_global_resource(profile)
    assert get_error.value.reason_code == "host_unknown_type"
    with pytest.raises(ContractValidationError) as list_error:
        manager.list_global_resources(scope="workspace", type_id=MODEL_PROFILE_TYPE)
    assert list_error.value.reason_code == "host_unknown_type"
    with closing(manager._store_factory()) as store:
        stored = store._connection.execute(
            "SELECT update_sequence,payload FROM global_resource_current WHERE scope=? AND type_id=? AND resource_id=?",
            (profile["scope"], profile["type_id"], profile["resource_id"]),
        ).fetchone()
    assert stored["update_sequence"] == 4
    assert stored["payload"] == original_payload
    assert loads_strict(stored["payload"]) == legacy_record
