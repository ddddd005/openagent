"""Offline policy validation and pure configuration-node registration."""

from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.context_compaction_policy import (
    CHECKPOINT_PREAMBLE, COMPACTION_POLICY_COMPONENT, COMPACTION_POLICY_TYPE, DEFAULT_COMPACTION_PROMPT,
    compaction_policy_config, compaction_policy_exports, compaction_policy_schema,
    default_compaction_policy, effective_summary_prompt, frame_checkpoint, register_compaction_policy,
    validate_compaction_policy, validate_compaction_policy_config,
)
from phase1_agent.contract_errors import ContractValidationError


def loaded():
    package = CapabilityPackage(PackageManifest(
        "test.compaction-policy", "1.0.0", exports=compaction_policy_exports(),
    ), register_compaction_policy)
    return CapabilityPackageLoader((package,)).load({"test.compaction-policy": "1.0.0"})


def test_default_policy_has_explicit_disabled_target_and_only_default_pressure_threshold():
    expected = {
        "schema_version": 1, "kind": "workflow.context-compaction-policy",
        "enabled": True, "trigger_tokens": 0, "keep_depth": 2,
        "summary_prompt": "", "target_tokens": None,
    }
    assert default_compaction_policy() == expected
    assert validate_compaction_policy(expected) == expected
    Draft202012Validator(compaction_policy_schema()).validate(expected)
    expected["keep_depth"] = 0
    assert default_compaction_policy()["keep_depth"] == 2


@pytest.mark.parametrize("prompt", ["", " ", "\r\n\t", "\u3000"])
def test_empty_summary_prompt_uses_fixed_default(prompt):
    policy = {**default_compaction_policy(), "summary_prompt": prompt}
    assert effective_summary_prompt(policy) == DEFAULT_COMPACTION_PROMPT
    assert policy["summary_prompt"] == prompt


def test_custom_summary_prompt_preserves_exact_text():
    policy = {**default_compaction_policy(), "summary_prompt": "\n保留人物和数值。\n"}
    assert effective_summary_prompt(policy) == "\n保留人物和数值。\n"
    assert validate_compaction_policy(policy)["summary_prompt"] == policy["summary_prompt"]


def test_checkpoint_framing_is_fixed_and_preserves_summary_text():
    summary = "\n必须保留 15 项。\n"
    assert frame_checkpoint(summary) == (
        CHECKPOINT_PREAMBLE + "\n\n<context-checkpoint>\n" + summary + "\n</context-checkpoint>"
    )


@pytest.mark.parametrize("summary", [None, "", " \r\n\t", [], 1])
def test_checkpoint_framing_rejects_empty_or_nontext_summary(summary):
    with pytest.raises(ContractValidationError) as caught:
        frame_checkpoint(summary)
    assert caught.value.reason_code == "context_compaction_summary_invalid"


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", True), ("schema_version", 2), ("kind", "workflow.other"),
    ("enabled", 1), ("enabled", None), ("trigger_tokens", True), ("trigger_tokens", -1),
    ("trigger_tokens", 1.5), ("trigger_tokens", None), ("trigger_tokens", 2**53),
    ("keep_depth", True), ("keep_depth", -1), ("keep_depth", 4097), ("keep_depth", 0.5),
    ("summary_prompt", None), ("summary_prompt", []),
    pytest.param("summary_prompt", "a" * 100_001, id="oversized-summary-prompt"),
])
def test_invalid_policy_rejects_boolean_numeric_lookalikes_and_invalid_fields(field, value):
    with pytest.raises(ContractValidationError) as caught:
        validate_compaction_policy({**default_compaction_policy(), field: value})
    assert caught.value.reason_code == "context_compaction_policy_invalid"


@pytest.mark.parametrize("value", [0, 128, "", False, {}, []])
def test_non_null_compaction_target_is_not_silently_executed_or_ignored(value):
    with pytest.raises(ContractValidationError) as caught:
        validate_compaction_policy({**default_compaction_policy(), "target_tokens": value})
    assert caught.value.reason_code == "context_compaction_target_reserved"


@pytest.mark.parametrize("value", [None, [], {}, {"enabled": True}])
def test_policy_requires_exact_business_envelope(value):
    with pytest.raises(ContractValidationError) as caught:
        validate_compaction_policy(value)
    assert caught.value.reason_code == "context_compaction_policy_invalid"


def test_unrecognized_fields_and_missing_target_are_rejected():
    policy = default_compaction_policy()
    with pytest.raises(ContractValidationError):
        validate_compaction_policy({**policy, "context_window_tokens": 1000})
    del policy["target_tokens"]
    with pytest.raises(ContractValidationError):
        validate_compaction_policy(policy)


def test_zero_retention_depth_and_positive_threshold_are_valid():
    policy = {**default_compaction_policy(), "trigger_tokens": 2048, "keep_depth": 0}
    assert validate_compaction_policy(policy) == policy
    assert validate_compaction_policy({**policy, "enabled": False})["enabled"] is False


def test_defaults_and_validated_values_are_detached():
    config = compaction_policy_config()
    accepted = validate_compaction_policy_config(config)
    accepted["enabled"] = False
    assert config["enabled"] is True
    assert compaction_policy_config()["enabled"] is True
    policy = default_compaction_policy()
    accepted_policy = validate_compaction_policy(policy)
    accepted_policy["keep_depth"] = 0
    assert policy["keep_depth"] == 2


def test_config_validator_requires_only_configuration_fields():
    assert validate_compaction_policy_config(compaction_policy_config()) == compaction_policy_config()
    with pytest.raises(ContractValidationError):
        validate_compaction_policy_config(default_compaction_policy())
    with pytest.raises(ContractValidationError):
        validate_compaction_policy_config({**compaction_policy_config(), "max_tokens": 128})


def test_registered_node_outputs_only_configuration_without_host_or_runtime_state():
    capabilities = loaded()
    registry = capabilities.registry
    entry = registry.get(COMPACTION_POLICY_COMPONENT, "1")
    config = deepcopy(entry.definition.default_config)
    entry.config_validator(config)
    output = entry.executor(config, {}, object())["output"]
    assert output == default_compaction_policy()
    assert registry.validate_content(output, COMPACTION_POLICY_TYPE, 1) == output
    assert entry.definition.inputs == ()
    assert entry.definition.capabilities == ()
    assert entry.definition.outputs[0].data_type == COMPACTION_POLICY_TYPE
    output["keep_depth"] = 0
    assert entry.executor(config, {}, object())["output"]["keep_depth"] == 2
    assert registry.executors.catalog() == []


def test_reserved_target_is_null_and_read_only_in_node_configuration_schema():
    schema = compaction_policy_schema(envelope=False)
    assert schema["properties"]["target_tokens"] == {
        "type": "null", "const": None, "readOnly": True,
        "title": "精简目标 tokens（保留占位）",
    }
    Draft202012Validator(schema).validate(compaction_policy_config())
