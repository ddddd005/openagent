"""Public, secret-free current provider, binding and response contracts."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit

from .contract_json import validate_json_value
from .contract_errors import require, uuid4_string
from .credential_contract import credential_id
from .frozen_model import FrozenModelParameters
from .host_sdk import DataTypeDefinition, ResourceIdentity
from .gemini_capabilities import binding_capabilities
from .provider_metadata import validate_provider_metadata, validate_thinking_summary


MODEL_PACKAGE_ID = "workflow.models"
CHAT_PROVIDER_TYPE = "workflow.chat-provider"
MODEL_PROFILE_TYPE = "workflow.model-profile"
_LEGACY_CHAT_MAX_TOKENS = 8192
DEFAULT_PROVIDER_ID = "7be319b8-30bd-4674-b7bf-d1cf54a1a110"
DEFAULT_PROFILE_ID = "7be319b8-30bd-4674-b7bf-d1cf54a1a111"
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
    require(type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128
            and not any(ord(char) < 32 or ord(char) == 127 for char in value["name"])
            and value["protocol"] in ("chat", "gemini") and type(value["enabled"]) is bool,
            "model_provider_invalid", "Provider name, protocol or enabled state is invalid")
    address = value["base_url"]
    require(type(address) is str and 0 < len(address) <= 2048
            and not any(char in address for char in "\\?#")
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in address),
            "model_provider_invalid", "Provider URL is invalid")
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError:
        require(False, "model_provider_invalid", "Provider URL is invalid")
    require(parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and (port is None or 1 <= port <= 65535)
            and (parsed.scheme == "https" or parsed.hostname in ("localhost", "127.0.0.1", "::1")),
            "model_provider_invalid", "Provider requires HTTPS or a local HTTP endpoint")
    reference = value["credential_ref"]
    if reference is not None:
        credential_id(reference)
    return deepcopy(value)


def validate_local_provider(value: object) -> dict:
    return validate_chat_provider(value)


def validate_model_parameters(value: object) -> dict:
    parameters = FrozenModelParameters.from_mapping(value)
    require(len(parameters.model) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in parameters.model),
            "model_parameters_unsupported", "Model name must be bounded and contain no control characters")
    require(parameters.max_tokens is None or parameters.max_tokens <= _LEGACY_CHAT_MAX_TOKENS,
            "model_parameters_unsupported", "Model output limit exceeds this transport contract")
    return dict(parameters.as_mapping())


def validate_provider_reference(value: object) -> dict:
    reference = ResourceIdentity.from_dict(value).to_dict()
    require(reference["type_id"] == CHAT_PROVIDER_TYPE, "model_provider_type_mismatch",
            "Model references require the current Chat provider type")
    return reference


def validate_model_reference(value: object) -> dict:
    reference = ResourceIdentity.from_dict(value).to_dict()
    require(reference["scope"] == "workspace" and reference["type_id"] == MODEL_PROFILE_TYPE,
            "model_profile_type_mismatch", "Model sources reference a workspace model configuration")
    return reference


def validate_model_source_config(value: object) -> dict:
    require(type(value) is dict and set(value) == {"reference"},
            "model_source_invalid", "Model source accepts only a global configuration reference")
    return {"reference": validate_model_reference(value["reference"])}


def validate_any_model_source_config(value: object) -> dict:
    return validate_model_source_config(value)


def validate_model_profile(value: object) -> dict:
    require(type(value) is dict and set(value) == {
        "name", "protocol", "provider", "parameters", "capacity", "enabled",
    }, "model_profile_invalid", "Named model configuration fields are invalid")
    require(type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128
            and not any(ord(char) < 32 or ord(char) == 127 for char in value["name"])
            and value["protocol"] in ("chat", "gemini") and type(value["enabled"]) is bool,
            "model_profile_invalid", "Model configuration name, protocol or enabled state is invalid")
    provider = validate_provider_reference(value["provider"])
    parameters = validate_model_parameters(value["parameters"])
    require(parameters == value["parameters"], "model_profile_invalid",
            "Saved model configuration parameters must be explicit")
    from .gemini_capabilities import validate_provider_parameters
    validate_provider_parameters(value["protocol"], parameters)
    capacity = validate_model_capacity(value["capacity"], parameters)
    return {**deepcopy(value), "provider": provider, "parameters": parameters, "capacity": capacity}


def validate_model_profile_record(record: object, reference: dict) -> dict:
    require(type(record) is dict and all(record.get(key) == reference[key] for key in reference)
            and type(record.get("data_schema_version")) is int and record["data_schema_version"] == 1,
            "model_profile_unavailable", "Referenced model configuration is missing or unavailable")
    value = validate_model_profile(record["value"])
    require(value["enabled"], "model_profile_disabled", "Referenced model configuration is disabled")
    return value


def resolve_model_source(config: object, records: list[dict]) -> dict:
    config = validate_model_source_config(config)
    reference = config["reference"]
    record = next((row for row in records if all(row.get(key) == reference[key] for key in reference)), None)
    profile = validate_model_profile_record(record, reference)
    provider_ref = profile["provider"]
    record = next((row for row in records if all(row.get(key) == provider_ref[key] for key in provider_ref)), None)
    provider = validate_provider_record(record, provider_ref)
    require(profile["protocol"] == provider["protocol"], "model_source_protocol_mismatch",
            "Model configuration and provider protocols differ")
    return {"reference": reference, "provider_reference": provider_ref,
            "parameters": profile["parameters"], "capacity": profile["capacity"], "provider": provider}


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
            and value["summary_max_tokens"] <= _LEGACY_CHAT_MAX_TOKENS,
            "model_capacity_invalid", "Output and summary reserves exceed the explicit capacity")
    return deepcopy(value)


def validate_provider_record(record: object, reference: dict) -> dict:
    require(type(record) is dict and all(record.get(key) == reference[key] for key in reference)
            and type(record.get("data_schema_version")) is int
            and record["data_schema_version"] == 3,
            "model_provider_unavailable", "Current provider is missing or has an unsupported schema")
    value = validate_local_provider(record["value"])
    require(value["enabled"], "model_provider_disabled", "Current provider is disabled")
    require(value["credential_ref"] is not None, "model_credential_unavailable",
            "Current provider needs a controlled credential reference")
    return value


def validate_public_model_binding(value: object) -> dict:
    validate_json_value(value)
    version = value.get("schema_version") if type(value) is dict else None
    expected = {
        "schema_version", "kind", "binding_id", "reference", "parameters", "capabilities",
        "capacity",
    }
    require(type(value) is dict and set(value) == expected
            and type(version) is int and version in (2, 4)
            and value["kind"] == "workflow.model-binding",
            "model_binding_invalid", "Public model binding fields are invalid")
    uuid4_string(value["binding_id"])
    validate_model_reference(value["reference"])
    parameters = validate_model_parameters(value["parameters"])
    require(parameters == value["parameters"], "model_binding_invalid", "Binding parameters must be explicit")
    validate_model_capacity(value["capacity"], parameters)
    protocol = "gemini" if version == 4 else "chat"
    require(type(value["capabilities"]) is dict
            and type(value["capabilities"].get("tools")) is bool
            and type(value["capabilities"].get("stream")) is bool
            and value["capabilities"] == binding_capabilities(protocol, parameters),
            "model_binding_invalid", "Binding capabilities do not match the supported transport")
    return deepcopy(value)


def validate_public_model_result(value: object) -> dict:
    validate_json_value(value)
    version = value.get("schema_version") if type(value) is dict else None
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "binding_id", "request_id", "finish_reason", "content",
        "tool_calls", "usage", "response_id", "model", "fact_refs",
    } | ({"thinking_summary", "provider_metadata"} if version == 2 else set())
            and type(version) is int and version in (1, 2)
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
    if version == 2:
        validate_thinking_summary(value["thinking_summary"])
        validate_provider_metadata(value["provider_metadata"], calls=value["tool_calls"],
                                   content=value["content"],
                                   thinking_summary=value["thinking_summary"],
                                   response_model=value["model"])
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
    from .model_request_contracts import (
        model_binding_schema, model_profile_schema, model_result_schema,
        validate_model_binding, validate_model_profile, validate_model_result,
    )

    local_provider_schema = object_schema({
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "protocol": {"enum": ["chat", "gemini"]}, "base_url": {"type": "string"},
        "credential_ref": {"anyOf": [
            {"type": "null"},
            {"type": "string", "pattern": "^local:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
        ]},
        "enabled": {"type": "boolean"},
    })
    return (
        DataTypeDefinition(CHAT_PROVIDER_TYPE, 3, local_provider_schema, scope="global",
                           validator=validate_local_provider, max_bytes=8192),
        DataTypeDefinition(MODEL_PROFILE_TYPE, 2, model_profile_schema(), scope="global",
                           validator=validate_model_profile, max_bytes=8192,
                           references=lambda value: [deepcopy(value["provider"])],
                           reference_mapper=lambda value, mapping: deepcopy(value)),
        DataTypeDefinition(MODEL_BINDING_TYPE, 5, model_binding_schema(), scope="content",
                           validator=validate_model_binding, max_bytes=8192,
                           references=lambda value: [deepcopy(value["reference"])],
                           reference_mapper=lambda value, mapping: deepcopy(value)),
        DataTypeDefinition(MODEL_RESULT_TYPE, 3, model_result_schema(), scope="content",
                           validator=validate_model_result),
    )
