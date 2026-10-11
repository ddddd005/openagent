from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.contract_json import canonical_bytes, content_digest
from phase1_agent.model_request_budget import (
    calculate_budget, measure_context, measure_prepared_request,
    validate_context_reduction, validate_measurement,
)
from phase1_agent.model_request_contracts import MAX_SAFE_INTEGER, validate_operation_payload
from test_model_request_contracts import IDS, synthetic_capability, synthetic_profile, synthetic_settings


def mock_prepared(settings, target_bytes=None, text="fixture"):
    """A synthetic projector with real wire framing and independent byte padding."""
    wire = {"model": settings["parameters"]["model"], "messages": [{"role": "user", "content": text}],
            "tools": [], "max_tokens": settings["parameters"]["max_tokens"], "stream": False}
    if target_bytes is not None:
        wire["padding"] = ""
        padding_bytes = target_bytes - len(canonical_bytes(wire))
        assert padding_bytes >= 0
        wire["padding"] = "x" * padding_bytes
    return {"projection": "synthetic-chat@1", "wire_request": wire,
            "transport_arguments": {"secret_fixture": "not-in-public-projection"}}


def test_exact_trigger_includes_output_reserve_on_real_wire():
    settings = synthetic_settings()
    before = measure_prepared_request(mock_prepared(settings, 222_999), settings)
    boundary = measure_prepared_request(mock_prepared(settings, 223_000), settings)
    assert before["measurement"]["total_tokens"] == 254_999
    assert before["requires_compaction"] is False
    assert boundary["measurement"]["total_tokens"] == 255_000
    assert boundary["requires_compaction"] is True
    assert boundary["hard_capacity_ok"] is True
    assert boundary["capacity_diagnostic"] is None


def test_utf8_estimate_and_digest_use_actual_wire_including_protocol_framing():
    settings = synthetic_settings()
    prepared = mock_prepared(settings, text="\u4e2d\u6587\u5b57")
    result = measure_prepared_request(prepared, settings)
    assert result["measurement"]["input_tokens"] == len(canonical_bytes(prepared["wire_request"]))
    assert result["measurement"]["input_tokens"] > len(canonical_bytes(prepared["wire_request"]["messages"]))
    assert result["measurement"]["token_count_kind"] == "utf8_bytes_estimate"
    assert result["measurement"]["estimator_id"] == "canonical-json-utf8-bytes"
    assert result["measurement"]["estimator_version"] == "1"
    assert result["projection"] == {"projection_version": "synthetic-chat@1",
                                    "wire_digest": content_digest(prepared["wire_request"])}
    assert b"secret_fixture" not in canonical_bytes(result)
    prepared["wire_request"]["tools"] = [{"function": {"name": "final_answer"}}]
    changed = measure_prepared_request(prepared, settings)
    assert changed["measurement"]["input_tokens"] > result["measurement"]["input_tokens"]
    assert changed["projection"]["wire_digest"] != result["projection"]["wire_digest"]


def test_ceil_rounding_is_exact_at_small_and_large_integer_boundaries():
    profile = synthetic_profile()
    runtime = {**profile["runtime"], "context_length_tokens": 10_001,
               "compaction_threshold_bps": 1, "output_limit_tokens": 1}
    assert calculate_budget(profile["model"]["capacity"], runtime, transport_output_limit=1)["trigger_total_tokens"] == 2
    capacity = {"context_window_tokens": MAX_SAFE_INTEGER, "input_limit_tokens": MAX_SAFE_INTEGER,
                "max_output_tokens": 1}
    runtime["context_length_tokens"] = MAX_SAFE_INTEGER
    runtime["compaction_threshold_bps"] = 9999
    # Exact external expected value; floating multiplication rounds this boundary incorrectly.
    assert calculate_budget(capacity, runtime, transport_output_limit=1)["trigger_total_tokens"] == 9_006_298_534_815_517


def test_context_null_and_lowered_context_only_change_trigger_not_actual_capacity():
    profile = synthetic_profile()
    profile["runtime"]["context_length_tokens"] = None
    settings = synthetic_settings(profile)
    assert settings["budget"]["trigger_total_tokens"] == 340_000
    profile["runtime"]["context_length_tokens"] = 100_000
    settings = synthetic_settings(profile)
    assert settings["budget"]["trigger_total_tokens"] == 85_000
    result = measure_prepared_request(mock_prepared(settings, 200_000), settings)
    assert result["requires_compaction"] is True
    assert result["hard_capacity_ok"] is True
    assert result["measurement"]["context_window_tokens"] == 400_000


@pytest.mark.parametrize("cold_limit,input_bytes,cold_exceeded,window_exceeded", [
    (223_000, 223_000, False, False), (223_000, 223_001, True, False),
    (400_000, 368_000, False, False), (400_000, 368_001, False, True),
    (200_000, 368_001, True, True),
])
def test_hard_cold_input_and_window_limits_are_checked_independently(
        cold_limit, input_bytes, cold_exceeded, window_exceeded):
    profile = synthetic_profile()
    profile["model"]["capacity"]["input_limit_tokens"] = cold_limit
    settings = synthetic_settings(profile)
    result = measure_prepared_request(mock_prepared(settings, input_bytes), settings)
    if not (cold_exceeded or window_exceeded):
        assert result["hard_capacity_ok"] is True and result["capacity_diagnostic"] is None
    else:
        assert result["hard_capacity_ok"] is False
        assert result["capacity_diagnostic"] == {
            "code": "model_request_capacity_exceeded", "input_limit_exceeded": cold_exceeded,
            "context_window_exceeded": window_exceeded,
        }


def test_summary_reserve_is_min_output_limit_1024_and_never_recursively_triggers():
    settings = synthetic_settings(purpose="compaction")
    result = measure_prepared_request(mock_prepared(settings, 300_000), settings)
    assert result["measurement"]["total_tokens"] == 301_024
    assert result["requires_compaction"] is False
    profile = synthetic_profile()
    profile["runtime"]["output_limit_tokens"] = 512
    settings = synthetic_settings(profile, purpose="compaction")
    assert settings["budget"]["summary_output_limit_tokens"] == 512
    assert settings["parameters"]["max_tokens"] == 512


def test_compaction_instruction_overhead_can_exceed_cold_capacity_and_must_reject():
    profile = synthetic_profile()
    profile["model"]["capacity"]["input_limit_tokens"] = 224_000
    settings = synthetic_settings(profile, purpose="compaction")
    assert measure_prepared_request(mock_prepared(settings, 224_000), settings)["hard_capacity_ok"]
    with pytest.raises(DiagnosticError) as error:
        measure_prepared_request(mock_prepared(settings, 224_001), settings)
    assert error.value.reason_code == "model_request_capacity_exceeded"
    assert error.value.diagnostics[0]["capacity_diagnostic"]["input_limit_exceeded"] is True


def test_readonly_context_measurement_uses_the_same_compaction_settings_with_normal_reserve():
    profile = synthetic_profile()
    profile["runtime"]["context_length_tokens"] = 100_000
    settings = synthetic_settings(profile, purpose="compaction")
    messages, tools = [{"role": "user", "blocks": [{"kind": "text", "text": "before"}]}], []
    before = measure_context(mock_prepared(settings, 224_000), settings, messages=messages, tools=tools)
    after_messages = [{"role": "user", "blocks": [{"kind": "text", "text": "after"}]}]
    after = measure_context(mock_prepared(settings, 150_000), settings, messages=after_messages, tools=tools)
    assert before["measurement"]["total_tokens"] == 256_000
    assert after["measurement"]["total_tokens"] == 182_000
    assert before["measurement"]["output_reserve_tokens"] == after["measurement"]["output_reserve_tokens"] == 32_000
    assert after["measurement"]["total_tokens"] > settings["budget"]["trigger_total_tokens"]
    assert validate_context_reduction(before, after, settings) == after
    assert before["settings_id"] == after["settings_id"] == settings["settings_id"]
    assert before["messages_digest"] == content_digest(messages)
    assert after["messages_digest"] == content_digest(after_messages)
    assert "reserved_model_fact_ids" not in before and "request_id" not in before
    with pytest.raises(ContractValidationError):
        measure_context(mock_prepared(synthetic_settings()), synthetic_settings(), messages=messages, tools=[])


@pytest.mark.parametrize("changed", ["settings", "projector", "estimator", "tools", "growth", "hard_capacity"])
def test_context_reduction_rejects_unrelated_basis_growth_or_actual_capacity_failure(changed):
    settings = synthetic_settings(purpose="compaction")
    messages = [{"role": "user", "blocks": []}]
    before = measure_context(mock_prepared(settings, 390_000), settings, messages=messages, tools=[])
    after = measure_context(mock_prepared(settings, 150_000), settings, messages=messages, tools=[])
    if changed == "settings":
        after["settings_id"] = IDS[9]
    elif changed == "projector":
        after["projection"]["projection_version"] = "synthetic-chat@2"
    elif changed == "estimator":
        after["measurement"].update(token_count_kind="provided", estimator_id="synthetic-counter")
    elif changed == "tools":
        after["tools_digest"] = content_digest([{"name": "other"}])
    elif changed == "growth":
        after = deepcopy(before)
    else:
        after = measure_context(mock_prepared(settings, 380_000), settings, messages=messages, tools=[])
    with pytest.raises(ContractValidationError):
        validate_context_reduction(before, after, settings)


def test_provided_counter_receives_exact_wire_bytes_and_requires_identity_before_invocation():
    settings = synthetic_settings()
    prepared = mock_prepared(settings)
    calls = []

    def counter(wire):
        calls.append(wire)
        return 223_000

    with pytest.raises(ContractValidationError):
        measure_prepared_request(prepared, settings, counter=counter)
    assert calls == []
    result = measure_prepared_request(prepared, settings, counter=counter,
                                      estimator_id="synthetic-token-counter", estimator_version="3")
    assert calls == [canonical_bytes(prepared["wire_request"])]
    assert result["measurement"]["token_count_kind"] == "provided"
    assert result["requires_compaction"] is True
    assert result["measurement"]["estimator_version"] == "3"
    with pytest.raises(ContractValidationError):
        measure_prepared_request(prepared, settings, estimator_id="renamed-byte-estimator")


@pytest.mark.parametrize("count", [True, -1, 1.0, "1", None, MAX_SAFE_INTEGER + 1])
def test_provided_counter_rejects_invalid_estimates(count):
    with pytest.raises(ContractValidationError):
        measure_prepared_request(mock_prepared(synthetic_settings()), synthetic_settings(),
                                 counter=lambda wire: count, estimator_id="fixture", estimator_version="1")


@pytest.mark.parametrize("projection", ["synthetic-chat", "", "synthetic-chat@1\n"])
def test_projection_requires_exact_projector_identity_version(projection):
    prepared = mock_prepared(synthetic_settings())
    prepared["projection"] = projection
    with pytest.raises(ContractValidationError):
        measure_prepared_request(prepared, synthetic_settings())


def test_prepare_response_validates_same_settings_trigger_capacity_and_reserved_fact_ids():
    settings = synthetic_settings()
    measured = measure_prepared_request(mock_prepared(settings, 223_000), settings)
    response = {"request_id": settings["request_id"], "preparation_id": IDS[8], "settings": settings,
                **measured, "reserved_model_fact_ids": dict(zip(("request", "attempt", "outcome"), IDS[5:8]))}
    assert validate_operation_payload("prepare-request", response, response=True) == response
    for field, value in (("request_id", IDS[9]), ("requires_compaction", False),
                         ("hard_capacity_ok", False)):
        with pytest.raises(ContractValidationError):
            validate_operation_payload("prepare-request", {**response, field: value}, response=True)
    response["reserved_model_fact_ids"]["outcome"] = IDS[5]
    with pytest.raises(ContractValidationError):
        validate_operation_payload("prepare-request", response, response=True)


def test_measurement_cannot_change_sum_capacity_or_estimator_version():
    settings = synthetic_settings()
    measurement = measure_prepared_request(mock_prepared(settings), settings)["measurement"]
    for field, value in (("total_tokens", 1), ("input_limit_tokens", 1), ("estimator_version", "2")):
        with pytest.raises(ContractValidationError):
            validate_measurement({**measurement, field: value}, settings)


def test_no_cache_discount_or_mutation_is_accepted_as_budget_basis():
    settings = synthetic_settings()
    prepared = mock_prepared(settings, 223_000)
    snapshot = deepcopy((prepared, settings))
    first = measure_prepared_request(prepared, settings)
    prepared["cache_hit"] = True
    second = measure_prepared_request(prepared, settings)
    assert first == second
    del prepared["cache_hit"]
    assert (prepared, settings) == snapshot
    capability = synthetic_capability()
    capability["output_limit_tokens"] = 0
    with pytest.raises(ContractValidationError):
        synthetic_settings(capability=capability)
