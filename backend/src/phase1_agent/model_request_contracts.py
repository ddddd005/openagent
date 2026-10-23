"""Uninstalled M1 model contracts. These definitions perform no runtime I/O."""

from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator, validators

from .contract_errors import require
from .contract_json import validate_json_value
from .host_sdk import ResourceIdentity, ServiceOperation, ServiceReference
from .provider_metadata import validate_provider_metadata, validate_thinking_summary


MAX_SAFE_INTEGER = 2**53 - 1
DEFAULT_CONTEXT_LENGTH_TOKENS = 300_000
DEFAULT_COMPACTION_THRESHOLD_BPS = 8500
DEFAULT_OUTPUT_LIMIT_TOKENS = 32_000
MODEL_PROFILE_SCHEMA_VERSION = 2
MODEL_BINDING_SCHEMA_VERSION = 5
MODEL_RESULT_SCHEMA_VERSION = 3
MODEL_REQUEST_SETTINGS_SCHEMA_VERSION = 1
COMPACTION_POLICY_SCHEMA_VERSION = 2
MODEL_REQUEST_SERVICE_REF = ServiceReference("workflow.models", "2.0.0")
_StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, instance: type(instance) is int),
)


def object_schema(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": deepcopy(properties), "required": list(properties)}


def _integer(minimum: int = 1, maximum: int = MAX_SAFE_INTEGER) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _const(value: object) -> dict:
    kind = {int: "integer", bool: "boolean", str: "string"}[type(value)]
    return {"type": kind, "const": value}


def _text(maximum: int = 128) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _uuid_schema() -> dict:
    return {"type": "string", "minLength": 36, "maxLength": 36, "pattern":
            "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def _reference_schema(type_id: str) -> dict:
    return object_schema({
        "envelope_version": _const(1), "scope": _text(), "type_id": _const(type_id),
        "resource_id": _uuid_schema(),
    })


def _thinking_schema() -> dict:
    return {"oneOf": [
        _const("disabled"),
        object_schema({"mode": _const("budget"), "budget": _integer(-1),
                       "include_summary": _const(True)}),
        object_schema({"mode": _const("level"),
                       "level": {"enum": ["minimal", "low", "medium", "high"]},
                       "include_summary": _const(True)}),
    ]}


def model_capacity_schema() -> dict:
    return object_schema({field: _integer() for field in (
        "context_window_tokens", "input_limit_tokens", "max_output_tokens")})


def model_runtime_schema() -> dict:
    return object_schema({
        "thinking": _thinking_schema(),
        "context_length_tokens": {"anyOf": [{"type": "null"}, _integer()]},
        "compaction_threshold_bps": _integer(1, 10_000),
        "output_limit_tokens": _integer(),
    })


def model_profile_schema() -> dict:
    return object_schema({
        "name": _text(), "provider": _reference_schema("workflow.chat-provider"),
        "model": object_schema({"id": _text(256), "capacity": model_capacity_schema()}),
        "runtime": model_runtime_schema(), "enabled": {"type": "boolean"},
    })


def model_binding_schema() -> dict:
    return object_schema({
        "schema_version": _const(MODEL_BINDING_SCHEMA_VERSION),
        "kind": _const("workflow.model-binding"), "binding_id": _uuid_schema(),
        "reference": _reference_schema("workflow.model-profile"),
        "authorization": object_schema({
            "purposes": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                         "items": {"enum": ["normal", "compaction"]}},
        }),
    })


def model_settings_schema() -> dict:
    revision = lambda type_id: object_schema({
        "reference": _reference_schema(type_id), "update_sequence": _integer(),
    })
    return object_schema({
        "schema_version": _const(MODEL_REQUEST_SETTINGS_SCHEMA_VERSION),
        "kind": _const("workflow.model-request-settings"),
        **{key: _uuid_schema() for key in ("settings_id", "request_id", "binding_id")},
        "purpose": {"enum": ["normal", "compaction"]},
        "configuration": revision("workflow.model-profile"),
        "provider": revision("workflow.chat-provider"),
        "protocol": {"enum": ["chat", "gemini"]},
        "parameters": object_schema({
            "model": _text(256), "thinking": _thinking_schema(),
            "stream": _const(False), "max_tokens": _integer(),
        }),
        "capabilities": object_schema({
            "protocol": {"enum": ["chat", "gemini"]}, "tools": {"type": "boolean"},
            "stream": _const(False), "thinking": {"enum": ["disabled", "budget", "level"]},
            "output_limit_tokens": {"anyOf": [{"type": "null"}, _integer()]},
        }),
        "capacity": model_capacity_schema(),
        "budget": object_schema({
            "context_length_tokens": _integer(),
            "compaction_threshold_bps": _integer(1, 10_000),
            **{key: _integer() for key in (
                "trigger_total_tokens", "output_reserve_tokens", "normal_output_limit_tokens",
                "summary_output_limit_tokens")},
            "calculation_version": _const(1),
        }),
    })


def compaction_policy_schema() -> dict:
    return object_schema({
        "schema_version": _const(COMPACTION_POLICY_SCHEMA_VERSION),
        "kind": _const("workflow.context-compaction-policy"),
        "enabled": {"type": "boolean"}, "keep_depth": _integer(0, 4096),
        "summary_prompt": {"type": "string", "maxLength": 100_000},
    })


def model_result_schema() -> dict:
    return object_schema({
        "schema_version": _const(MODEL_RESULT_SCHEMA_VERSION),
        "kind": _const("workflow.model-result"),
        **{key: _uuid_schema() for key in ("binding_id", "request_id", "settings_id")},
        "finish_reason": _text(),
        **{key: {"type": ["string", "null"]} for key in ("content", "response_id", "model")},
        "tool_calls": {"type": "array", "items": object_schema({
            "id": _text(256), "name": _text(256), "raw_arguments": {"type": "string"},
            "type": _const("function"),
        })},
        "usage": {"type": ["object", "null"]},
        "fact_refs": {"type": "array", "minItems": 3, "maxItems": 3, "uniqueItems": True,
                      "items": object_schema({"fact_id": _uuid_schema()})},
        "thinking_summary": {"type": ["string", "null"], "maxLength": 131_072},
        "provider_metadata": {"type": ["object", "null"]},
    })


def _validate(value: object, schema: dict, code: str) -> dict:
    validate_json_value(value)
    error = next(_StrictValidator(schema).iter_errors(value), None)
    require(error is None, code, "Model contract fields or values are invalid")
    return deepcopy(value)


def _reference(value: dict, code: str) -> None:
    ResourceIdentity.from_dict(value)
    require(value["scope"] == "workspace", code,
            "Model configurations and providers require workspace resource identities")


def _name(value: str, code: str) -> None:
    require(bool(value.strip()) and not any(ord(char) < 32 or ord(char) == 127 for char in value),
            code, "Model names and identities must contain nonempty text without controls")


def validate_model_capacity(value: object) -> dict:
    return _validate(value, model_capacity_schema(), "model_capacity_invalid")


def validate_model_runtime(value: object) -> dict:
    return _validate(value, model_runtime_schema(), "model_runtime_invalid")


def default_model_runtime() -> dict:
    return {"thinking": "disabled", "context_length_tokens": DEFAULT_CONTEXT_LENGTH_TOKENS,
            "compaction_threshold_bps": DEFAULT_COMPACTION_THRESHOLD_BPS,
            "output_limit_tokens": DEFAULT_OUTPUT_LIMIT_TOKENS}


def validate_model_profile(value: object) -> dict:
    result = _validate(value, model_profile_schema(), "model_profile_invalid")
    _name(result["name"], "model_profile_invalid")
    _name(result["model"]["id"], "model_profile_invalid")
    _reference(result["provider"], "model_profile_invalid")
    from .model_request_budget import calculate_budget
    calculate_budget(result["model"]["capacity"], result["runtime"],
                     transport_output_limit=result["model"]["capacity"]["max_output_tokens"])
    return result


def validate_model_binding(value: object) -> dict:
    result = _validate(value, model_binding_schema(), "model_binding_invalid")
    _reference(result["reference"], "model_binding_invalid")
    return result


def validate_model_settings(value: object) -> dict:
    result = _validate(value, model_settings_schema(), "model_request_settings_invalid")
    for key in ("configuration", "provider"):
        _reference(result[key]["reference"], "model_request_settings_invalid")
    parameters, capabilities, budget = result["parameters"], result["capabilities"], result["budget"]
    _name(parameters["model"], "model_request_settings_invalid")
    thinking_mode = ("disabled" if parameters["thinking"] == "disabled"
                     else parameters["thinking"]["mode"])
    require(capabilities["protocol"] == result["protocol"]
            and capabilities["thinking"] == thinking_mode
            and (result["protocol"] != "chat" or thinking_mode == "disabled"),
            "model_request_settings_invalid", "Settings capabilities differ from their parameters")
    if result["protocol"] == "gemini":
        from .gemini_capabilities import validate_provider_parameters
        validate_provider_parameters(result["protocol"], parameters)
    from .model_request_budget import calculate_budget
    expected = calculate_budget(result["capacity"], {
        "thinking": parameters["thinking"],
        "context_length_tokens": budget["context_length_tokens"],
        "compaction_threshold_bps": budget["compaction_threshold_bps"],
        "output_limit_tokens": budget["normal_output_limit_tokens"],
    }, transport_output_limit=capabilities["output_limit_tokens"], purpose=result["purpose"])
    require(budget == expected and parameters["max_tokens"] == expected["output_reserve_tokens"],
            "model_request_settings_invalid", "Settings budget or output limit is inconsistent")
    return result


def validate_compaction_policy(value: object) -> dict:
    return _validate(value, compaction_policy_schema(), "context_compaction_policy_invalid")


def validate_model_result(value: object, *, settings: object | None = None) -> dict:
    result = _validate(value, model_result_schema(), "model_result_invalid")
    require(len({call["id"] for call in result["tool_calls"]}) == len(result["tool_calls"]),
            "model_result_invalid", "Tool call identities repeat")
    validate_thinking_summary(result["thinking_summary"])
    if result["provider_metadata"] is not None:
        validate_provider_metadata(
            result["provider_metadata"], calls=result["tool_calls"], content=result["content"],
            thinking_summary=result["thinking_summary"], response_model=result["model"])
    else:
        require(result["thinking_summary"] is None, "model_result_invalid",
                "Thinking summaries require their provider metadata")
    if settings is not None:
        basis = validate_model_settings(settings)
        require(all(result[key] == basis[key] for key in ("binding_id", "request_id", "settings_id")),
                "model_result_invalid", "Result belongs to another request settings identity")
        require((basis["protocol"] == "chat" and result["provider_metadata"] is None)
                or (basis["protocol"] == "gemini" and result["provider_metadata"] is not None),
                "model_result_invalid", "Result metadata does not match its protocol")
        if result["provider_metadata"] is not None:
            require(result["provider_metadata"]["model"] == basis["parameters"]["model"],
                    "model_result_invalid", "Result protocol metadata belongs to another model")
    return result


def transport_capability_schema() -> dict:
    """A trusted registry declaration, never a field accepted from model profiles."""
    return object_schema({
        "schema_version": _const(1), "transport_id": _text(), "transport_version": _text(),
        "protocol": {"enum": ["chat", "gemini"]}, "model_id": _text(256),
        "output_limit_tokens": {"anyOf": [{"type": "null"}, _integer()]}, "tools": {"type": "boolean"},
        "thinking": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                     "items": {"enum": ["disabled", "budget", "level"]}},
        "provenance": object_schema({"source": _text(512), "version": _text()}),
    })


def validate_transport_capability(value: object, *, protocol: str, model: str,
                                  thinking: object, requires_tools: bool = False) -> dict:
    """The caller must obtain value from an exact installed trusted registration."""
    result = _validate(value, transport_capability_schema(), "model_transport_capability_unavailable")
    for text in (result["transport_id"], result["transport_version"], result["model_id"],
                 result["provenance"]["source"], result["provenance"]["version"]):
        _name(text, "model_transport_capability_unavailable")
    _validate(thinking, _thinking_schema(), "model_thinking_unsupported")
    require(result["protocol"] == protocol and result["model_id"] == model,
            "model_transport_capability_mismatch", "Transport declaration does not match protocol and model")
    mode = "disabled" if thinking == "disabled" else thinking["mode"]
    require(mode in result["thinking"] and (protocol != "chat" or mode == "disabled"),
            "model_thinking_unsupported", "Transport does not support the requested thinking mode")
    require(type(requires_tools) is bool and (not requires_tools or result["tools"]),
            "model_tools_unsupported", "Transport does not support required tools")
    # Validate explicit Gemini settings without inferring limits from the model ID.
    if protocol == "gemini":
        from .gemini_capabilities import validate_provider_parameters
        validate_provider_parameters(protocol, {"model": model, "thinking": thinking})
    return result


def build_model_settings(profile: object, *, configuration: dict, provider: dict, protocol: str,
                         transport_capability: object, settings_id: str, request_id: str,
                         binding_id: str, purpose: str = "normal", requires_tools: bool = False) -> dict:
    """Freeze public settings from an already authorized, coherent resource bundle."""
    profile = validate_model_profile(profile)
    require(profile["enabled"], "model_profile_disabled", "Model configuration is disabled")
    require(type(provider) is dict and provider.get("reference") == profile["provider"],
            "model_provider_type_mismatch", "Provider revision differs from the profile provider")
    capability = validate_transport_capability(
        transport_capability, protocol=protocol, model=profile["model"]["id"],
        thinking=profile["runtime"]["thinking"], requires_tools=requires_tools)
    from .model_request_budget import calculate_budget
    budget = calculate_budget(profile["model"]["capacity"], profile["runtime"],
                              transport_output_limit=capability["output_limit_tokens"], purpose=purpose)
    thinking = profile["runtime"]["thinking"]
    return validate_model_settings({
        "schema_version": 1, "kind": "workflow.model-request-settings",
        "settings_id": settings_id, "request_id": request_id, "binding_id": binding_id,
        "purpose": purpose, "configuration": configuration, "provider": provider,
        "protocol": protocol, "parameters": {
            "model": profile["model"]["id"], "thinking": thinking, "stream": False,
            "max_tokens": budget["output_reserve_tokens"],
        },
        "capabilities": {"protocol": protocol, "tools": capability["tools"], "stream": False,
                         "thinking": "disabled" if thinking == "disabled" else thinking["mode"],
                         "output_limit_tokens": capability["output_limit_tokens"]},
        "capacity": profile["model"]["capacity"], "budget": budget,
    })


def projection_schema() -> dict:
    return object_schema({
        "projection_version": {"type": "string", "maxLength": 128,
                               "pattern": "^[A-Za-z0-9_.:-]+@[A-Za-z0-9_.-]+(?![\\s\\S])"},
        "wire_digest": digest_schema(),
    })


def digest_schema() -> dict:
    return {"type": "string", "minLength": 79, "maxLength": 79,
            "pattern": "^json-v1:sha256:[0-9a-f]{64}$"}


def measurement_schema() -> dict:
    return object_schema({
        "input_tokens": _integer(0), "output_reserve_tokens": _integer(),
        "total_tokens": {"type": "integer", "minimum": 1},
        "context_window_tokens": _integer(), "input_limit_tokens": _integer(),
        "token_count_kind": {"enum": ["utf8_bytes_estimate", "provided"]},
        "estimator_id": _text(), "estimator_version": _text(),
    })


def capacity_diagnostic_schema() -> dict:
    return {"anyOf": [{"type": "null"}, object_schema({
        "code": _const("model_request_capacity_exceeded"),
        "input_limit_exceeded": {"type": "boolean"}, "context_window_exceeded": {"type": "boolean"},
    })]}


def model_request_operations() -> tuple[ServiceOperation, ...]:
    """Return declarations only; neither a registry nor a service factory is installed."""
    selection = {"binding_id": _uuid_schema(), "request_key": _text(512)}
    messages_tools = {"messages": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                      "tools": {"type": "array", "items": {"type": "object"}}}
    preparation = {**selection, "preparation_id": _uuid_schema()}
    evidence = {"projection": projection_schema(), "measurement": measurement_schema(),
                "hard_capacity_ok": {"type": "boolean"},
                "capacity_diagnostic": capacity_diagnostic_schema()}
    return (
        ServiceOperation("models:call", "prepare-request", object_schema({
            **selection, "purpose": {"enum": ["normal", "compaction"]}, **messages_tools,
        }), object_schema({
            "request_id": _uuid_schema(), "preparation_id": _uuid_schema(),
            "settings": model_settings_schema(), **evidence,
            "requires_compaction": {"type": "boolean"},
            "reserved_model_fact_ids": object_schema({
                key: _uuid_schema() for key in ("request", "attempt", "outcome")}),
        })),
        ServiceOperation("models:call", "dispatch-request", object_schema(preparation),
                         model_result_schema()),
        ServiceOperation("models:call", "discard-prepared-request", object_schema(preparation),
                         object_schema({"request_id": _uuid_schema(), "discarded": _const(True)})),
        ServiceOperation("model-projection:read", "measure-context", object_schema({
            "binding_id": _uuid_schema(), "preparation_id": _uuid_schema(), **messages_tools,
        }), object_schema({
            "settings_id": _uuid_schema(), "messages_digest": digest_schema(),
            "tools_digest": digest_schema(), **evidence,
        })),
    )


def validate_operation_payload(operation: str, value: object, *, response: bool = False) -> dict:
    """Validate declared JSON shapes; runtime ownership is an M2 host responsibility."""
    declaration = next((item for item in model_request_operations() if item.operation == operation), None)
    require(declaration is not None, "model_request_operation_invalid", "Unknown model request operation")
    schema = declaration.response_schema if response else declaration.request_schema
    result = _validate(value, schema, "model_request_invalid")
    if response and operation == "prepare-request":
        settings = validate_model_settings(result["settings"])
        require(result["request_id"] == settings["request_id"],
                "model_request_invalid", "Preparation request identity differs from its settings")
        from .model_request_budget import validate_measurement
        validate_measurement(result["measurement"], settings,
                             output_reserve_tokens=settings["budget"]["output_reserve_tokens"])
        _validate_capacity_evidence(result)
        expected_trigger = (settings["purpose"] == "normal"
                            and result["measurement"]["total_tokens"] >= settings["budget"]["trigger_total_tokens"])
        require(result["requires_compaction"] == expected_trigger
                and (settings["purpose"] != "compaction" or result["hard_capacity_ok"]),
                "model_request_invalid", "Preparation trigger or hard capacity evidence is inconsistent")
        require(len(set(result["reserved_model_fact_ids"].values())) == 3,
                "model_request_invalid", "Reserved model fact identities repeat")
    elif response and operation == "dispatch-request":
        validate_model_result(result)
    elif response and operation == "measure-context":
        from .model_request_budget import validate_measurement
        validate_measurement(result["measurement"])
        _validate_capacity_evidence(result)
    return result


def _validate_capacity_evidence(value: dict) -> None:
    from .model_request_budget import capacity_evidence
    expected = capacity_evidence(value["measurement"])
    require(value["hard_capacity_ok"] == expected["hard_capacity_ok"]
            and value["capacity_diagnostic"] == expected["capacity_diagnostic"],
            "model_request_invalid", "Hard capacity evidence differs from the measured input")
