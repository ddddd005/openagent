"""Public, secret-free current provider, binding and response contracts."""

from __future__ import annotations

from copy import deepcopy

from .contract_json import validate_json_value
from .frozen_model import FrozenModelParameters
from .graph_contracts import require, uuid4_string
from .host_sdk import DataTypeDefinition, ResourceIdentity
from .model_configuration import CHAT_MAX_TOKENS, DEFAULT_PROVIDER_ID, validate_provider


MODEL_PACKAGE_ID = "workflow.models"
CHAT_PROVIDER_TYPE = "workflow.chat-provider"
MODEL_BINDING_TYPE = "MODEL_BINDING"
MODEL_RESULT_TYPE = "MODEL_RESULT"
_CAPACITY_FIELDS = (
    "context_window_tokens", "output_reserve_tokens", "summary_max_tokens", "max_cold_input_tokens",
)
_CAPACITY_FIELD_SET = frozenset(_CAPACITY_FIELDS)


def object_schema(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": deepcopy(properties), "required": list(properties)}


def _uuid_schema() -> dict:
    return {"type": "string", "pattern":
            "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def validate_chat_provider(value: object) -> dict:
    require(type(value) is dict and set(value) == {
        "name", "protocol", "base_url", "credential_ref", "enabled",
    }, "model_provider_invalid", "Provider fields are invalid")
    validate_provider({"schema_version": 1, "kind": "chat_provider",
                       "provider_id": DEFAULT_PROVIDER_ID, "revision": 1, **value})
    return deepcopy(value)


def validate_model_parameters(value: object) -> dict:
    parameters = FrozenModelParameters.from_mapping(value)
    require(parameters.max_tokens is None or parameters.max_tokens <= CHAT_MAX_TOKENS,
            "model_parameters_unsupported", "Model output limit exceeds this transport contract")
    return dict(parameters.as_mapping())


def validate_model_reference(value: object) -> dict:
    reference = ResourceIdentity.from_dict(value).to_dict()
    require(reference["type_id"] == CHAT_PROVIDER_TYPE, "model_provider_type_mismatch",
            "Model references require the current Chat provider type")
    return reference


def validate_model_source_config(value: object) -> dict:
    require(type(value) is dict and set(value) == {"reference", "parameters"},
            "model_source_invalid", "Model source requires provider identity and explicit parameters")
    return {"reference": validate_model_reference(value["reference"]),
            "parameters": validate_model_parameters(value["parameters"])}


def validate_native_model_source_config(value: object) -> dict:
    require(type(value) is dict and set(value) == {"reference", "parameters", "capacity"},
            "model_source_invalid", "Native model source requires explicit capacity budgets")
    result = validate_model_source_config({key: value[key] for key in ("reference", "parameters")})
    return {**result, "capacity": validate_model_capacity(value["capacity"], result["parameters"])}


def validate_any_model_source_config(value: object) -> dict:
    return (validate_native_model_source_config(value) if type(value) is dict and "capacity" in value
            else validate_model_source_config(value))


def validate_model_capacity(value: object, parameters: dict) -> dict:
    require(type(value) is dict and set(value) == _CAPACITY_FIELD_SET
            and all(type(value[field]) is int and 0 < value[field] <= 2**53 - 1
                    for field in _CAPACITY_FIELDS),
            "model_capacity_invalid", "Native Agent requires explicit positive capacity budgets")
    maximum = parameters.get("max_tokens")
    require(type(maximum) is int and maximum > 0,
            "model_output_reserve_unknown", "Native Agent requires an explicit model output limit")
    require(maximum <= value["output_reserve_tokens"] < value["context_window_tokens"]
            and value["summary_max_tokens"] <= value["output_reserve_tokens"]
            and value["summary_max_tokens"] <= CHAT_MAX_TOKENS,
            "model_capacity_invalid", "Output and summary reserves exceed the explicit capacity")
    return deepcopy(value)


def validate_provider_record(record: object, reference: dict) -> dict:
    require(type(record) is dict and all(record.get(key) == reference[key] for key in reference)
            and record.get("data_schema_version") == 1,
            "model_provider_unavailable", "Current provider is missing or has an unsupported schema")
    value = validate_chat_provider(record["value"])
    require(value["enabled"], "model_provider_disabled", "Current provider is disabled")
    require(value["credential_ref"] is not None, "model_credential_unavailable",
            "Current provider needs a controlled credential reference")
    return value


def validate_public_model_binding(value: object) -> dict:
    validate_json_value(value)
    version = value.get("schema_version") if type(value) is dict else None
    expected = {
        "schema_version", "kind", "binding_id", "reference", "parameters", "capabilities",
    } | ({"capacity"} if version == 2 else set())
    require(type(value) is dict and set(value) == expected
            and type(version) is int and version in (1, 2)
            and value["kind"] == "workflow.model-binding",
            "model_binding_invalid", "Public model binding fields are invalid")
    uuid4_string(value["binding_id"])
    validate_model_reference(value["reference"])
    parameters = validate_model_parameters(value["parameters"])
    require(parameters == value["parameters"], "model_binding_invalid", "Binding parameters must be explicit")
    if version == 2:
        validate_model_capacity(value["capacity"], parameters)
    require(type(value["capabilities"]) is dict
            and type(value["capabilities"].get("tools")) is bool
            and type(value["capabilities"].get("stream")) is bool
            and value["capabilities"] == {"protocol": "chat", "tools": True, "stream": False,
                                         "thinking": "disabled"},
            "model_binding_invalid", "Binding capabilities do not match the supported transport")
    return deepcopy(value)


def validate_public_model_result(value: object) -> dict:
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "binding_id", "request_id", "finish_reason", "content",
        "tool_calls", "usage", "response_id", "model", "fact_refs",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.model-result",
            "model_result_invalid", "Public model result fields are invalid")
    for field in ("binding_id", "request_id"):
        uuid4_string(value[field])
    require(type(value["finish_reason"]) is str and bool(value["finish_reason"])
            and all(value[field] is None or type(value[field]) is str
                    for field in ("content", "response_id", "model"))
            and (value["usage"] is None or type(value["usage"]) is dict)
            and type(value["tool_calls"]) is list,
            "model_result_invalid", "Public model response fields are invalid")
    identities = []
    for call in value["tool_calls"]:
        require(type(call) is dict and set(call) == {"id", "name", "raw_arguments", "type"}
                and call["type"] == "function" and all(type(call[key]) is str for key in
                                                      ("id", "name", "raw_arguments"))
                and bool(call["id"]) and bool(call["name"]),
                "model_result_invalid", "Public model tool call fields are invalid")
        identities.append(call["id"])
    require(len(set(identities)) == len(identities), "model_result_invalid", "Tool call identities repeat")
    require(type(value["fact_refs"]) is list and len(value["fact_refs"]) == 3,
            "model_result_invalid", "Model result requires request, attempt and outcome facts")
    for ref in value["fact_refs"]:
        require(type(ref) is dict and set(ref) == {"fact_id"},
                "model_result_invalid", "Model fact references are invalid")
        uuid4_string(ref["fact_id"])
    require(len({ref["fact_id"] for ref in value["fact_refs"]}) == 3,
            "model_result_invalid", "Model facts must have distinct identities")
    return deepcopy(value)


def model_type_definitions() -> tuple[DataTypeDefinition, ...]:
    provider_schema = object_schema({
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "protocol": {"const": "chat"}, "base_url": {"type": "string"},
        "credential_ref": {"enum": [None, "env:DEEPSEEK_API_KEY"]}, "enabled": {"type": "boolean"},
    })
    binding_schema = object_schema({
        "schema_version": {"const": 1}, "kind": {"const": "workflow.model-binding"},
        "binding_id": _uuid_schema(), "reference": {"type": "object"},
        "parameters": {"type": "object"},
        "capabilities": object_schema({
            "protocol": {"const": "chat"}, "tools": {"type": "boolean", "const": True},
            "stream": {"type": "boolean", "const": False}, "thinking": {"const": "disabled"},
        }),
    })
    native_binding_schema = deepcopy(binding_schema)
    native_binding_schema["properties"]["schema_version"] = {"const": 2}
    native_binding_schema["properties"]["capacity"] = object_schema({
        field: {"type": "integer", "minimum": 1} for field in _CAPACITY_FIELDS
    })
    native_binding_schema["required"].append("capacity")
    result_schema = object_schema({
        "schema_version": {"const": 1}, "kind": {"const": "workflow.model-result"},
        "binding_id": _uuid_schema(), "request_id": _uuid_schema(),
        "finish_reason": {"type": "string", "minLength": 1},
        **{key: {"type": ["string", "null"]} for key in ("content", "response_id", "model")},
        "tool_calls": {"type": "array", "items": object_schema({
            "id": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1},
            "raw_arguments": {"type": "string"}, "type": {"const": "function"},
        })},
        "usage": {"type": ["object", "null"]},
        "fact_refs": {"type": "array", "minItems": 3, "maxItems": 3,
                      "items": object_schema({"fact_id": _uuid_schema()})},
    })
    return (
        DataTypeDefinition(CHAT_PROVIDER_TYPE, 1, provider_schema, scope="global",
                           validator=validate_chat_provider, max_bytes=8192),
        DataTypeDefinition(MODEL_BINDING_TYPE, 1, binding_schema, scope="content",
                           validator=validate_public_model_binding, max_bytes=8192,
                           references=lambda value: [deepcopy(value["reference"])],
                           reference_mapper=lambda value, mapping: deepcopy(value)),
        DataTypeDefinition(MODEL_BINDING_TYPE, 2, native_binding_schema, scope="content",
                           validator=validate_public_model_binding, max_bytes=8192,
                           references=lambda value: [deepcopy(value["reference"])],
                           reference_mapper=lambda value, mapping: deepcopy(value)),
        DataTypeDefinition(MODEL_RESULT_TYPE, 1, result_schema, scope="content",
                           validator=validate_public_model_result),
    )
