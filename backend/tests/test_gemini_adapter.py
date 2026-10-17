"""Offline generateContent wire tests; fixture signatures are not real signatures."""

from copy import deepcopy
import json

import httpx
import pytest

from phase1_agent.adapter import ProviderResponseError
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.frozen_model import FrozenModelParameters
from phase1_agent.gemini_adapter import GeminiAdapter
from phase1_agent.gemini_capabilities import validate_provider_parameters
from phase1_agent.model_request_contracts import validate_model_result
from phase1_agent.model_request_transport import resolve_model_request_transport
from phase1_agent.model_service import create_chat_transport
from test_model_request_service import ServiceHarness


MODEL = "gemini-3-flash-preview"
PARAMETERS = {"model": MODEL, "max_tokens": 1024, "temperature": 1, "stream": False,
              "thinking": {"mode": "level", "level": "low", "include_summary": True}}
MESSAGES = [{"kind": "text", "role": "user", "content": "Inspect both records"}]
TOOLS = [{"type": "function", "function": {
    "name": "inspect", "description": "Inspect one record",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"], "additionalProperties": False},
}}]


def response(parts, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "responseId": "fixture-response", "modelVersion": MODEL,
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2,
                              "thoughtsTokenCount": 3, "totalTokenCount": 9}}


def batch():
    return [
        {"text": "Compare both.", "thought": True, "thoughtSignature": "fixture-thought-only"},
        {"text": "Checking ", "thoughtSignature": "fixture-text-signed"},
        {"functionCall": {"name": "inspect", "args": {"id": 1}, "id": "native-1"},
         "thoughtSignature": "fixture-call-one"},
        {"text": "and "},
        {"functionCall": {"name": "inspect", "args": {"id": 2}, "id": "native-2"}},
    ]


def history(response_value):
    return [{"kind": "assistant_calls", "role": "assistant", "content": response_value.content,
             "thinking_summary": response_value.thinking_summary,
             "provider_metadata": deepcopy(response_value.provider_metadata),
             "calls": [{"id": call.id, "name": call.name, "raw_arguments": call.raw_arguments}
                       for call in response_value.tool_calls]},
            *[{"kind": "tool_result", "role": "tool", "tool_call_id": call.id,
               "status": "success", "content": f"Record {index}"}
              for index, call in enumerate(response_value.tool_calls, 1)]]


def fixture(handler):
    value = ServiceHarness()
    value.profile["value"]["model"]["id"] = MODEL
    value.profile["value"]["model"]["capacity"]["max_output_tokens"] = PARAMETERS["max_tokens"]
    value.profile["value"]["runtime"].update(
        thinking=PARAMETERS["thinking"], output_limit_tokens=PARAMETERS["max_tokens"])
    value.provider["value"].update(
        protocol="gemini", base_url="https://generativelanguage.googleapis.com/v1beta")
    credential_ref = value.provider["value"]["credential_ref"]
    value.broker.save(credential_ref, "private-gemini-fixture-secret")
    value.transport_calls = []

    def transport(request):
        value.transport_calls.append(request)
        return handler(request)

    value.service._resolve_transport = lambda protocol, model: resolve_model_request_transport(
        protocol, model, transport_factory=lambda **kwargs: create_chat_transport(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport))))
    return value


def request(value, *, key="normal:1", messages=None, tools=None):
    messages = [{"kind": "text", "role": "user", "content": "Hello"}] if messages is None else messages
    tools = [] if tools is None else tools
    prepared = value.prepare(key=key, messages=messages, tools=tools)
    return value.dispatch(prepared, key=key)


def test_three_channels_preserve_interleaved_parts_and_native_ids():
    original = batch()
    parsed = GeminiAdapter._parse_response(response(original), MODEL)
    assert parsed.content == "Checking and "
    assert parsed.thinking_summary == "Compare both."
    assert parsed.provider_metadata["content"] == {"role": "model", "parts": original}
    assert [binding["part_index"] for binding in parsed.provider_metadata["tool_call_bindings"]] == [2, 4]
    assert [call.name for call in parsed.tool_calls] == ["inspect", "inspect"]
    assert len({call.id for call in parsed.tool_calls}) == 2
    assert parsed.usage["completion_tokens"] == 5
    assert "fixture-call-one" not in parsed.content
    assert "fixture-call-one" not in parsed.thinking_summary
    prepared = GeminiAdapter.prepare_request(MESSAGES + history(parsed), TOOLS, PARAMETERS)
    assert prepared["wire_request"]["contents"][1] == {"role": "model", "parts": original}
    results = prepared["wire_request"]["contents"][2]["parts"]
    assert results == [
        {"functionResponse": {"name": "inspect", "id": "native-1", "response": {"result": "Record 1"}}},
        {"functionResponse": {"name": "inspect", "id": "native-2", "response": {"result": "Record 2"}}},
    ]


def test_http_wire_matches_all_accepted_facts_through_three_rounds():
    observed, index = [], [0]
    replies = [response(batch()), response([
        {"functionCall": {"name": "inspect", "args": {"id": 3}},
         "thoughtSignature": "fixture-next-round"}]), response([{"text": "Both inspected."}])]

    def handler(request):
        assert request.headers["x-goog-api-key"] == "private-gemini-fixture-secret"
        assert str(request.url).endswith("/models/" + MODEL + ":generateContent")
        observed.append(json.loads(request.content))
        reply = replies[index[0]]
        index[0] += 1
        return httpx.Response(200, json=reply)

    value = fixture(handler)
    messages = deepcopy(MESSAGES)
    for round_number in range(3):
        result = request(value, key=f"round-{round_number}", messages=messages, tools=TOOLS)
        assert validate_model_result(result) == result
        assert result["schema_version"] == 3
        if result["tool_calls"]:
            parsed = GeminiAdapter._parse_response(replies[round_number], MODEL)
            parsed = ModelResponse(
                finish_reason=result["finish_reason"], content=result["content"],
                tool_calls=tuple(ModelToolCall(
                    call["id"], call["name"], call["raw_arguments"], call["type"])
                    for call in result["tool_calls"]),
                usage=result["usage"], response_id=result["response_id"], model=result["model"],
                thinking_summary=result["thinking_summary"], provider_metadata=result["provider_metadata"])
            messages += history(parsed)
    facts = list(value.accepted.values())
    assert [fact["details"]["wire_request"] for fact in facts if fact["stage"] == "request"] == observed
    assert observed[1]["contents"][1]["parts"] == batch()
    assert observed[2]["contents"][3]["parts"][0]["thoughtSignature"] == "fixture-next-round"
    assert all(fact["details"]["projection"] == "gemini-generate-content@1"
               for fact in facts if fact["stage"] == "request")
    assert value.binding["schema_version"] == 5
    assert "private-gemini-fixture-secret" not in json.dumps(facts)
    assert observed[0]["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == TOOLS[0]["function"]["parameters"]
    value.close()


def test_acceptance_failure_reuses_retained_gemini_response_without_transport():
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(200, json=response([{"text": "Summary", "thought": True}, {"text": "Answer"}]))
    value = fixture(handler)
    value.fail_stage = "outcome"
    with pytest.raises(ContractValidationError) as error:
        request(value)
    assert error.value.reason_code == "model_fact_acceptance_failed"
    value.fail_stage = None
    result = value.service.retry_acceptance(value.context)
    assert result["thinking_summary"] == "Summary"
    assert result["provider_metadata"]["content"]["parts"][1] == {"text": "Answer"}
    assert request(value) == result
    assert len(observed) == 1
    value.close()


@pytest.mark.parametrize("status", [400, 429, 503])
def test_provider_http_failure_is_recorded_once_without_signature_bypass(status):
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(status, json={"error": {"message": "private-gemini-fixture-secret"}})
    value = fixture(handler)
    for _ in range(2):
        with pytest.raises(ContractValidationError) as error:
            request(value)
        assert error.value.reason_code == "model_provider_error"
    assert len(observed) == 1
    facts = list(value.accepted.values())
    assert facts[-1]["details"]["status_code"] == status
    assert facts[-1]["details"]["classification"] == "provider_error"
    assert "private-gemini-fixture-secret" not in json.dumps(facts)
    value.close()


def test_timeout_is_unknown_once_and_does_not_replay():
    observed = []
    def handler(request):
        observed.append(request)
        raise httpx.ReadTimeout("private-gemini-fixture-secret", request=request)
    value = fixture(handler)
    for _ in range(2):
        with pytest.raises(ContractValidationError) as error:
            request(value)
        assert error.value.reason_code == "model_dispatch_unknown"
    assert len(observed) == 1
    value.close()


@pytest.mark.parametrize("parts", [
    [{"functionCall": {"name": "inspect", "args": {"id": 1}}}],
    [{"functionCall": {"name": "inspect", "args": {"id": 1}},
      "thoughtSignature": "skip_thought_signature_validator"}],
    [{"text": "text", "functionCall": {"name": "inspect", "args": {}}}],
    [{"inlineData": {"mimeType": "image/png", "data": "fixture"}}],
])
def test_invalid_or_missing_signature_response_rejects_before_tool_execution(parts):
    with pytest.raises(ProviderResponseError):
        GeminiAdapter._parse_response(response(parts), MODEL)


def test_missing_signature_diagnostic_retains_source_and_part_without_leaking_parts():
    provider_response = response([
        {"text": "Private but visible thought", "thought": True},
        {"functionCall": {"name": "inspect", "args": {}}},
    ])
    with pytest.raises(ProviderResponseError) as rejected:
        GeminiAdapter._parse_response(provider_response, MODEL)
    assert rejected.value.diagnostic == {
        "code": "gemini_thought_signature_missing", "model": MODEL,
        "source": "provider_response", "part_index": 1,
    }
    assert "Private but visible thought" not in str(rejected.value)

    value = fixture(lambda request: httpx.Response(200, json=response([
        {"text": "Private but visible thought", "thought": True},
        {"functionCall": {"name": "inspect", "args": {}}},
    ])))
    with pytest.raises(ContractValidationError) as error:
        request(value)
    assert error.value.reason_code == "model_response_invalid"
    fact = list(value.accepted.values())[-1]
    assert fact["details"]["classification"] == "response_invalid"
    assert "Private but visible thought" not in json.dumps(fact)
    value.close()


@pytest.mark.parametrize("body", [
    b'{"candidates":[],"candidates":[]}',
    b'{"candidates":[],"bad":NaN}',
    b'{"candidates":[],"bad":"\xff"}',
])
def test_http_non_strict_or_non_utf8_json_is_confirmed_response_invalid(body):
    value = fixture(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ContractValidationError) as error:
        request(value)
    assert error.value.reason_code == "model_response_invalid"
    assert list(value.accepted.values())[-1]["details"]["classification"] == "response_invalid"
    value.close()


def test_signed_history_different_model_and_reordered_results_refuse_dispatch():
    parsed = GeminiAdapter._parse_response(response(batch()), MODEL)
    messages = MESSAGES + history(parsed)
    wrong = deepcopy(PARAMETERS)
    wrong["model"] = "gemini-3.1-pro-preview"
    with pytest.raises(ContractValidationError):
        GeminiAdapter.prepare_request(messages, TOOLS, wrong)
    messages[-1], messages[-2] = messages[-2], messages[-1]
    with pytest.raises(ContractValidationError) as error:
        GeminiAdapter.prepare_request(messages, TOOLS, PARAMETERS)
    assert error.value.reason_code == "gemini_tool_pairing_invalid"


def test_model_version_alias_does_not_change_frozen_signed_model_identity():
    value = response([{"text": "Answer"}])
    value["modelVersion"] = MODEL + "-provider-revision"
    parsed = GeminiAdapter._parse_response(value, MODEL)
    assert parsed.model == MODEL + "-provider-revision"
    assert parsed.provider_metadata["model"] == MODEL
    assert parsed.provider_metadata["response_model"] == MODEL + "-provider-revision"


def test_public_result_actual_model_must_match_original_response_model():
    value = fixture(lambda request: httpx.Response(200, json=response([{"text": "Answer"}])))
    prepared = value.prepare()
    result = value.dispatch(prepared)
    with pytest.raises(ContractValidationError):
        validate_model_result({**result, "model": "different-response-model"}, settings=prepared["settings"])
    with pytest.raises(ContractValidationError):
        validate_model_result({**result, "model": None}, settings=prepared["settings"])
    assert validate_model_result(result, settings=prepared["settings"]) == result
    value.close()


def test_optional_response_model_absence_is_not_filled_with_requested_identity():
    body = response([{"text": "Answer"}])
    del body["modelVersion"]
    value = fixture(lambda request: httpx.Response(200, json=body))
    prepared = value.prepare()
    result = value.dispatch(prepared)
    assert result["model"] is None
    assert result["provider_metadata"]["response_model"] is None
    assert result["provider_metadata"]["model"] == MODEL
    assert validate_model_result(result, settings=prepared["settings"]) == result
    value.close()


def test_missing_required_history_signature_has_local_part_diagnostic():
    parsed = GeminiAdapter._parse_response(response(batch()), MODEL)
    messages = MESSAGES + history(parsed)
    del messages[1]["provider_metadata"]["content"]["parts"][2]["thoughtSignature"]
    with pytest.raises(ContractValidationError) as error:
        GeminiAdapter.prepare_request(messages, TOOLS, PARAMETERS)
    assert error.value.reason_code == "gemini_thought_signature_missing"
    assert "part 2" in str(error.value)


def test_native_call_ids_are_optional_and_local_ids_never_sent_as_supplier_ids():
    parsed = GeminiAdapter._parse_response(response([
        {"functionCall": {"name": "inspect", "args": {}}, "thoughtSignature": "fixture"}]), MODEL)
    prepared = GeminiAdapter.prepare_request(MESSAGES + history(parsed), TOOLS, PARAMETERS)
    assert "id" not in prepared["wire_request"]["contents"][-1]["parts"][0]["functionResponse"]
    assert "id" not in prepared["wire_request"]["contents"][-2]["parts"][0]["functionCall"]


def test_duplicate_native_provider_call_ids_are_not_hidden_by_new_local_ids():
    parts = batch()
    parts[4]["functionCall"]["id"] = parts[2]["functionCall"]["id"]
    with pytest.raises(ProviderResponseError):
        GeminiAdapter._parse_response(response(parts), MODEL)


@pytest.mark.parametrize("schema", [
    {"type": "object", "oneOf": [{"type": "object"}]},
    {"type": "object", "properties": {"child": {"$ref": "#/definitions/child"}}},
    {"type": "object", "properties": {"value": {"type": ["null", "string"]}}},
    {"type": "object", "patternProperties": {".*": {"type": "string"}}},
])
def test_unsupported_schema_fails_before_http_without_silent_constraint_loss(schema):
    tools = deepcopy(TOOLS)
    tools[0]["function"]["parameters"] = schema
    with pytest.raises(ContractValidationError) as error:
        GeminiAdapter.prepare_request(MESSAGES, tools, PARAMETERS)
    assert error.value.reason_code == "model_tool_schema_unsupported"


@pytest.mark.parametrize("model,thinking,valid", [
    ("gemini-2.5-pro", "disabled", False),
    ("gemini-2.5-pro", {"mode": "budget", "budget": 127, "include_summary": True}, False),
    ("gemini-2.5-pro", {"mode": "budget", "budget": 32768, "include_summary": True}, True),
    ("gemini-2.5-flash", "disabled", True),
    ("gemini-2.5-flash", {"mode": "budget", "budget": -1, "include_summary": False}, True),
    ("gemini-2.5-flash-lite", {"mode": "budget", "budget": 100, "include_summary": True}, False),
    ("gemini-2.5-flash-lite", {"mode": "budget", "budget": 512, "include_summary": True}, True),
    ("gemini-3.1-pro-preview", {"mode": "level", "level": "minimal", "include_summary": True}, False),
    ("gemini-3-flash-preview", {"mode": "level", "level": "minimal", "include_summary": True}, True),
    ("gemini-guessed-future", {"mode": "level", "level": "low", "include_summary": True}, False),
])
def test_model_specific_thinking_capabilities_and_freeze(model, thinking, valid):
    parameters = {"model": model, "thinking": thinking, "stream": False}
    frozen = FrozenModelParameters.from_mapping(parameters)
    if not valid:
        with pytest.raises(ContractValidationError):
            validate_provider_parameters("gemini", dict(frozen.as_mapping()))
        return
    validate_provider_parameters("gemini", dict(frozen.as_mapping()))
    assert dict(frozen.as_mapping()) == parameters
    if isinstance(thinking, dict):
        thinking["include_summary"] = not thinking["include_summary"]
        assert dict(frozen.as_mapping())["thinking"] != thinking


def test_gemini_uses_only_the_saved_private_key():
    def handler(request):
        assert request.headers["x-goog-api-key"] == "private-gemini-fixture-secret"
        return httpx.Response(200, json=response([{"text": "Answer"}]))
    value = fixture(handler)
    request(value)
    assert value.provider["value"]["credential_ref"].startswith("local:")
    assert "private-gemini-fixture-secret" not in json.dumps(list(value.accepted.values()))
    value.close()


def test_25_unsigned_legacy_history_is_readable_but_3_tool_continuation_is_refused():
    messages = MESSAGES + [
        {"kind": "assistant_calls", "role": "assistant", "content": None,
         "calls": [{"id": "local", "name": "inspect", "raw_arguments": "{}"}]},
        {"kind": "tool_result", "role": "tool", "tool_call_id": "local",
         "status": "success", "content": "done"},
    ]
    with pytest.raises(ContractValidationError) as error:
        GeminiAdapter.prepare_request(messages, TOOLS, PARAMETERS)
    assert error.value.reason_code == "gemini_thought_signature_missing"
    legacy = {**PARAMETERS, "model": "gemini-2.5-flash", "thinking": "disabled"}
    assert GeminiAdapter.prepare_request(messages, TOOLS, legacy)["wire_request"]["contents"][1]["parts"] == [
        {"functionCall": {"name": "inspect", "args": {}}}]
