from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.model_request_contracts import (
    MODEL_REQUEST_SERVICE_REF, build_model_settings, compaction_policy_schema, default_model_runtime,
    model_binding_schema, model_profile_schema, model_request_operations, model_result_schema,
    model_settings_schema, validate_compaction_policy, validate_model_binding,
    validate_model_profile, validate_model_result, validate_model_settings,
    validate_operation_payload, validate_transport_capability,
)


IDS = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 12)]


def reference(type_id, identity):
    return {"envelope_version": 1, "scope": "workspace", "type_id": type_id, "resource_id": identity}


def synthetic_profile():
    return {"name": "Synthetic fixture", "provider": reference("workflow.chat-provider", IDS[0]),
            "model": {"id": "synthetic-chat", "capacity": {
                "context_window_tokens": 400_000, "input_limit_tokens": 400_000,
                "max_output_tokens": 64_000}},
            "runtime": default_model_runtime(), "enabled": True}


def synthetic_capability(protocol="chat", model="synthetic-chat"):
    return {"schema_version": 1, "transport_id": "synthetic.transport",
            "transport_version": "1.0.0", "protocol": protocol, "model_id": model,
            "output_limit_tokens": 64_000, "tools": True, "thinking": ["disabled", "budget"],
            "provenance": {"source": "synthetic:test-fixture", "version": "1"}}


def synthetic_settings(profile=None, purpose="normal", capability=None, protocol="chat"):
    profile = synthetic_profile() if profile is None else deepcopy(profile)
    return build_model_settings(
        profile, configuration={"reference": reference("workflow.model-profile", IDS[1]), "update_sequence": 3},
        provider={"reference": profile["provider"], "update_sequence": 5},
        protocol=protocol, transport_capability=synthetic_capability() if capability is None else capability,
        settings_id=IDS[2], request_id=IDS[3], binding_id=IDS[4], purpose=purpose,
    )


def result_fixture(settings=None):
    settings = synthetic_settings() if settings is None else settings
    return {"schema_version": 3, "kind": "workflow.model-result",
            **{key: settings[key] for key in ("binding_id", "request_id", "settings_id")},
            "finish_reason": "stop", "content": "answer", "tool_calls": [], "usage": None,
            "response_id": None, "model": settings["parameters"]["model"],
            "fact_refs": [{"fact_id": identity} for identity in IDS[5:8]],
            "thinking_summary": None, "provider_metadata": None}


def test_profile_defaults_and_explicit_nullable_context_roundtrip_are_detached():
    profile = synthetic_profile()
    assert profile["runtime"] == {
        "thinking": "disabled", "context_length_tokens": 300_000,
        "compaction_threshold_bps": 8500, "output_limit_tokens": 32_000,
    }
    validated = validate_model_profile(profile)
    validated["model"]["capacity"]["max_output_tokens"] = 1
    assert profile["model"]["capacity"]["max_output_tokens"] == 64_000
    profile["runtime"]["context_length_tokens"] = None
    assert validate_model_profile(profile)["runtime"]["context_length_tokens"] is None
    assert synthetic_settings(profile)["budget"]["context_length_tokens"] == 400_000


@pytest.mark.parametrize("field,value", [
    ("protocol", "chat"), ("parameters", {}), ("capacity", {}), ("temperature", 1),
    ("base_url", "https://example.invalid"), ("credential_ref", "private"),
])
def test_profile_rejects_old_and_private_top_level_fields(field, value):
    profile = synthetic_profile()
    profile[field] = value
    with pytest.raises(ContractValidationError):
        validate_model_profile(profile)


@pytest.mark.parametrize("field,value", [
    ("thinking", {"mode": "budget", "budget": 8, "include_summary": False}),
    ("output_limit_tokens", True), ("output_limit_tokens", 32_000.0),
    ("compaction_threshold_bps", 0), ("compaction_threshold_bps", 10_001),
    ("context_length_tokens", 400_001),
])
def test_profile_rejects_invalid_four_parameters(field, value):
    profile = synthetic_profile()
    profile["runtime"][field] = value
    with pytest.raises(ContractValidationError):
        validate_model_profile(profile)


def test_profile_does_not_guess_a_smaller_default_for_a_small_model():
    profile = synthetic_profile()
    profile["model"]["capacity"]["context_window_tokens"] = 16_384
    with pytest.raises(DiagnosticError) as error:
        validate_model_profile(profile)
    assert error.value.reason_code == "model_capacity_invalid"


def test_binding_only_authorizes_resource_and_purposes():
    binding = {"schema_version": 5, "kind": "workflow.model-binding", "binding_id": IDS[4],
               "reference": reference("workflow.model-profile", IDS[1]),
               "authorization": {"purposes": ["normal", "compaction"]}}
    assert validate_model_binding(binding) == binding
    for field in ("parameters", "capacity", "provider", "credential_ref", "lease"):
        invalid = {**binding, field: {}}
        with pytest.raises(ContractValidationError):
            validate_model_binding(invalid)
    binding["authorization"]["purposes"].append("normal")
    with pytest.raises(ContractValidationError):
        validate_model_binding(binding)


@pytest.mark.parametrize("path", [
    ("transport_version",), ("output_limit_tokens",), ("provenance",),
    ("provenance", "source"), ("provenance", "version"),
])
def test_preparation_settings_require_a_complete_trusted_declaration(path):
    capability = synthetic_capability()
    target = capability
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(DiagnosticError) as error:
        synthetic_settings(capability=capability)
    assert error.value.reason_code == "model_transport_capability_unavailable"


@pytest.mark.parametrize("field,value", [("protocol", "gemini"), ("model_id", "other-model")])
def test_preparation_rejects_declaration_protocol_or_exact_model_mismatch(field, value):
    capability = synthetic_capability()
    capability[field] = value
    with pytest.raises(DiagnosticError) as error:
        synthetic_settings(capability=capability)
    assert error.value.reason_code == "model_transport_capability_mismatch"


def test_installed_output_ceiling_and_tool_support_are_independent_of_manual_capacity():
    capability = synthetic_capability()
    capability["output_limit_tokens"] = 8192
    with pytest.raises(DiagnosticError) as error:
        synthetic_settings(capability=capability)
    assert error.value.reason_code == "model_capacity_invalid"
    capability["tools"] = False
    with pytest.raises(DiagnosticError) as error:
        validate_transport_capability(capability, protocol="chat", model="synthetic-chat",
                                      thinking="disabled", requires_tools=True)
    assert error.value.reason_code == "model_tools_unsupported"


def test_thinking_follows_current_protocol_and_installed_exact_model_capability():
    profile = synthetic_profile()
    profile["runtime"]["thinking"] = {"mode": "budget", "budget": 1024, "include_summary": True}
    with pytest.raises(DiagnosticError) as error:
        synthetic_settings(profile)
    assert error.value.reason_code == "model_thinking_unsupported"
    profile["model"]["id"] = "gemini-2.5-flash"
    capability = synthetic_capability("gemini", "gemini-2.5-flash")
    settings = synthetic_settings(profile, capability=capability, protocol="gemini")
    assert settings["capabilities"]["thinking"] == "budget"
    profile["runtime"]["thinking"]["budget"] = 24_577
    with pytest.raises(DiagnosticError) as error:
        synthetic_settings(profile, capability=capability, protocol="gemini")
    assert error.value.reason_code == "model_thinking_unsupported"


def test_settings_preserve_resource_revisions_and_share_global_output_limit():
    normal, compact = synthetic_settings(), synthetic_settings(purpose="compaction")
    assert normal["parameters"]["max_tokens"] == normal["budget"]["output_reserve_tokens"] == 32_000
    assert compact["parameters"]["max_tokens"] == compact["budget"]["output_reserve_tokens"] == 32_000
    assert compact["budget"]["normal_output_limit_tokens"] == 32_000
    assert compact["budget"]["summary_output_limit_tokens"] == 32_000
    assert normal["configuration"]["update_sequence"] == 3
    assert normal["provider"]["update_sequence"] == 5
    assert validate_model_settings(normal) == normal
    for field in ("base_url", "credential_ref", "generation", "lease"):
        with pytest.raises(ContractValidationError):
            validate_model_settings({**normal, field: "private"})


@pytest.mark.parametrize("path,value", [
    (("budget", "trigger_total_tokens"), 254_999),
    (("budget", "summary_output_limit_tokens"), 512),
    (("budget", "calculation_version"), 2),
    (("parameters", "max_tokens"), 1024),
    (("capabilities", "protocol"), "gemini"),
    (("capabilities", "thinking"), "budget"),
    (("configuration", "update_sequence"), True),
    (("configuration", "update_sequence"), 3.0),
])
def test_settings_reject_inconsistent_derived_values(path, value):
    settings = synthetic_settings()
    settings[path[0]][path[1]] = value
    with pytest.raises(ContractValidationError):
        validate_model_settings(settings)


def test_saved_profile_provider_cannot_be_substituted_by_another_revision():
    profile = synthetic_profile()
    with pytest.raises(DiagnosticError):
        build_model_settings(
            profile, configuration={"reference": reference("workflow.model-profile", IDS[1]), "update_sequence": 1},
            provider={"reference": reference("workflow.chat-provider", IDS[9]), "update_sequence": 1},
            protocol="chat", transport_capability=synthetic_capability(),
            settings_id=IDS[2], request_id=IDS[3], binding_id=IDS[4])


def test_result3_has_exact_settings_and_accepted_three_fact_references():
    settings = synthetic_settings()
    result = result_fixture(settings)
    assert validate_model_result(result, settings=settings) == result
    result["fact_refs"][2] = result["fact_refs"][0]
    with pytest.raises(ContractValidationError):
        validate_model_result(result)
    result = result_fixture(settings)
    result["settings_id"] = IDS[9]
    with pytest.raises(ContractValidationError):
        validate_model_result(result, settings=settings)


def test_chat_result_null_metadata_and_gemini_metadata_binding():
    settings = synthetic_settings()
    result = result_fixture(settings)
    result["thinking_summary"] = "unbacked thought"
    with pytest.raises(ContractValidationError):
        validate_model_result(result, settings=settings)
    profile = synthetic_profile()
    profile["model"]["id"] = "gemini-2.5-flash"
    settings = synthetic_settings(profile, protocol="gemini",
                                  capability=synthetic_capability("gemini", "gemini-2.5-flash"))
    result = result_fixture(settings)
    with pytest.raises(ContractValidationError):
        validate_model_result(result, settings=settings)
    result["provider_metadata"] = {
        "schema_version": 1, "provider": "gemini", "protocol": "gemini-generate-content@1",
        "model": "gemini-2.5-flash", "content": {"role": "model", "parts": [{"text": "answer"}]},
        "tool_call_bindings": [],
    }
    assert validate_model_result(result, settings=settings) == result
    result["provider_metadata"]["content"]["parts"][0]["text"] = "different"
    with pytest.raises(ContractValidationError):
        validate_model_result(result, settings=settings)


def test_policy2_removes_old_trigger_and_target_controls():
    policy = {"schema_version": 2, "kind": "workflow.context-compaction-policy",
              "enabled": True, "keep_depth": 2, "summary_prompt": ""}
    assert validate_compaction_policy(policy) == policy
    for key in ("trigger_tokens", "target_tokens"):
        with pytest.raises(ContractValidationError):
            validate_compaction_policy({**policy, key: None})


def test_uninstalled_operation_declarations_have_the_four_exact_operations():
    assert MODEL_REQUEST_SERVICE_REF.to_dict() == {
        "service_id": "workflow.models", "exact_version": "2.0.0"}
    operations = model_request_operations()
    assert [(item.capability, item.operation) for item in operations] == [
        ("models:call", "prepare-request"), ("models:call", "dispatch-request"),
        ("models:call", "discard-prepared-request"), ("model-projection:read", "measure-context"),
    ]
    for item in operations:
        item.to_dict()
        assert item.request_schema["additionalProperties"] is False
        assert item.response_schema["additionalProperties"] is False
    prepare = {"binding_id": IDS[4], "request_key": "agent:fixture:normal:1", "purpose": "normal",
               "messages": [{"role": "user", "blocks": [{"kind": "text", "text": "fixture"}]}], "tools": []}
    assert validate_operation_payload("prepare-request", prepare) == prepare
    dispatch = {"binding_id": IDS[4], "request_key": prepare["request_key"], "preparation_id": IDS[8]}
    assert validate_operation_payload("dispatch-request", dispatch) == dispatch
    with pytest.raises(ContractValidationError):
        validate_operation_payload("dispatch-request", {**dispatch, "parameters": {}})
    measure = {"binding_id": IDS[4], "preparation_id": IDS[8],
               "messages": prepare["messages"], "tools": []}
    assert validate_operation_payload("measure-context", measure) == measure
    with pytest.raises(ContractValidationError):
        validate_operation_payload("measure-context", {**measure, "request_key": "new-request"})
    discarded = {"request_id": IDS[3], "discarded": True}
    assert validate_operation_payload("discard-prepared-request", discarded, response=True) == discarded


def test_schemas_are_valid_and_do_not_install_or_replace_existing_versions():
    for schema in (model_profile_schema(), model_binding_schema(), model_settings_schema(),
                   model_result_schema(), compaction_policy_schema()):
        Draft202012Validator.check_schema(schema)
    from phase1_agent.model_contract import model_type_definitions
    definitions = model_type_definitions()
    assert (("global", "workflow.model-profile", 2) in {
        (item.scope, item.type_id, item.schema_version) for item in definitions})
    assert (("content", "MODEL_BINDING", 5) in {
        (item.scope, item.type_id, item.schema_version) for item in definitions})
    assert (("content", "MODEL_RESULT", 3) in {
        (item.scope, item.type_id, item.schema_version) for item in definitions})
    assert b"credential_ref" not in canonical_bytes(synthetic_settings())
