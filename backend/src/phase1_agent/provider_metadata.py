"""Opaque, versioned Gemini protocol state kept apart from visible text."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict, validate_json_value


GEMINI_PROTOCOL = "gemini-generate-content@1"
MAX_PROVIDER_METADATA_BYTES = 2_000_000
MAX_THINKING_SUMMARY_CHARS = 131_072
_UNSET = object()
_FIELDS = {
    "schema_version", "provider", "protocol", "model", "content", "tool_call_bindings",
}


def _require(condition: bool, diagnostic: str) -> None:
    if not condition:
        raise ContractValidationError(diagnostic)


def validate_thinking_summary(value: Any) -> str | None:
    _require(value is None or type(value) is str
             and len(value) <= MAX_THINKING_SUMMARY_CHARS,
             "Thinking summary must be bounded visible text")
    validate_json_value(value)
    return value


def validate_provider_metadata(
    value: Any, *, calls: Sequence[Mapping[str, Any]] | None = None,
    content: Any = _UNSET, thinking_summary: Any = _UNSET, model: str | None = None,
    response_model: Any = _UNSET,
) -> dict[str, Any]:
    """Check shape and binding, never claim cryptographic signature validity.

    ``calls`` contains id/name/raw_arguments records. Original provider IDs stay
    inside the unmodified Content; bindings name this layer's canonical IDs.
    ``model`` is the frozen request identity; ``response_model`` is the actual
    response version, which may differ when the request used a provider alias.
    """
    validate_json_value(value)
    _require(type(value) is dict and _FIELDS <= set(value) <= _FIELDS | {"response_model"},
             "Provider metadata envelope fields are invalid")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["provider"] == "gemini" and value["protocol"] == GEMINI_PROTOCOL,
             "Provider metadata version or protocol is unsupported")
    _require(type(value["model"]) is str and 0 < len(value["model"]) <= 256,
             "Provider metadata requires its original model identity")
    _require(model is None or value["model"] == model,
             "Provider metadata belongs to a different model")
    actual_model = value.get("response_model")
    _require(actual_model is None or type(actual_model) is str and 0 < len(actual_model) <= 256,
             "Provider response model identity is invalid")
    if response_model is not _UNSET:
        if "response_model" in value:
            _require(response_model == actual_model,
                     "Provider response identity differs from its recorded model")
        elif response_model is not None:
            _require(response_model == value["model"],
                     "Provider response identity differs from its recorded model")
    _require(len(canonical_bytes(value)) <= MAX_PROVIDER_METADATA_BYTES,
             "Provider metadata exceeds its byte limit")
    original = value["content"]
    _require(type(original) is dict and original.get("role") == "model"
             and type(original.get("parts")) is list and 0 < len(original["parts"]) <= 256,
             "Provider Content requires an ordered bounded model part list")
    function_parts, visible, thoughts, native_ids = [], [], [], []
    for index, part in enumerate(original["parts"]):
        _require(type(part) is dict and bool(part),
                 f"Provider part {index} is invalid")
        bodies = set(part) & {"text", "functionCall"}
        _require(len(bodies) == 1
                 and not (set(part) & {"functionResponse", "inlineData", "fileData", "executableCode",
                                      "codeExecutionResult"}),
                 f"Provider part {index} is outside the text and tools protocol")
        if "thought" in part:
            _require(type(part["thought"]) is bool, f"Provider part {index} thought flag is invalid")
        if "thoughtSignature" in part:
            signature = part["thoughtSignature"]
            _require(type(signature) is str and bool(signature)
                     and signature != "skip_thought_signature_validator",
                     f"Provider part {index} has invalid signature state")
        if "text" in part:
            _require(type(part["text"]) is str, f"Provider part {index} text is invalid")
            (thoughts if part.get("thought") is True else visible).append(part["text"])
        else:
            call = part["functionCall"]
            _require(type(call) is dict and type(call.get("name")) is str and bool(call["name"])
                     and type(call.get("args")) is dict and part.get("thought") is not True,
                     f"Provider function part {index} is invalid")
            if "id" in call:
                _require(type(call["id"]) is str and bool(call["id"]),
                         f"Provider function part {index} identity is invalid")
                _require(call["id"] not in native_ids,
                         f"Provider function part {index} repeats a native identity")
                native_ids.append(call["id"])
            function_parts.append((index, call))
    bindings = value["tool_call_bindings"]
    _require(type(bindings) is list and len(bindings) == len(function_parts),
             "Provider call bindings must cover every function part")
    identities = []
    for binding, (index, _) in zip(bindings, function_parts):
        _require(type(binding) is dict and set(binding) == {"part_index", "tool_call_id"}
                 and type(binding["part_index"]) is int and binding["part_index"] == index
                 and type(binding["tool_call_id"]) is str
                 and 0 < len(binding["tool_call_id"]) <= 256,
                 f"Provider call binding at part {index} is invalid")
        identities.append(binding["tool_call_id"])
    _require(len(identities) == len(set(identities)), "Provider call bindings repeat an identity")
    if calls is not None:
        _require(len(calls) == len(function_parts), "Provider call count differs from canonical calls")
        for binding, (_, original_call), canonical_call in zip(bindings, function_parts, calls):
            _require(type(canonical_call) is dict
                     and canonical_call.get("id") == binding["tool_call_id"]
                     and canonical_call.get("name") == original_call["name"],
                     "Provider call binding differs from its canonical call")
            try:
                parsed = loads_strict(canonical_call.get("raw_arguments"))
            except (ContractValidationError, TypeError):
                raise ContractValidationError("Provider canonical arguments are not strict JSON") from None
            _require(type(parsed) is dict
                     and canonical_bytes(parsed) == canonical_bytes(original_call["args"]),
                     "Provider call arguments differ from original function part")
    if content is not _UNSET:
        expected = "".join(visible)
        _require(content == expected or content is None and not expected,
                 "Provider visible text differs from canonical answer content")
    if thinking_summary is not _UNSET:
        validate_thinking_summary(thinking_summary)
        expected = "".join(thoughts)
        _require(thinking_summary == expected or thinking_summary is None and not expected,
                 "Provider visible thought summary differs from original parts")
    return deepcopy(value)


def remap_provider_metadata(
    value: Any, id_mapping: Mapping[str, str], *,
    calls: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Change only local association IDs, leaving all signed parts untouched."""
    result = validate_provider_metadata(value)
    for binding in result["tool_call_bindings"]:
        identity = binding["tool_call_id"]
        _require(identity in id_mapping, "Provider call binding has no explicit identity mapping")
        binding["tool_call_id"] = id_mapping[identity]
    return validate_provider_metadata(result, calls=calls)


def canonical_message_metadata(message: Mapping[str, Any]) -> dict[str, Any]:
    """Detach optional channels after checking against canonical blocks."""
    fields = {}
    if "thinking_summary" in message:
        fields["thinking_summary"] = validate_thinking_summary(message["thinking_summary"])
    if message.get("provider_metadata") is not None:
        blocks = message["blocks"]
        fields["provider_metadata"] = validate_provider_metadata(
            message["provider_metadata"],
            calls=[{"id": block["tool_call_id"], "name": block["tool_name"],
                    "raw_arguments": block["raw_arguments"]}
                   for block in blocks if block["kind"] == "tool_call"],
            content="\n".join(block["text"] for block in blocks if block["kind"] == "text"),
            thinking_summary=message.get("thinking_summary"),
        )
    elif "provider_metadata" in message:
        fields["provider_metadata"] = None
    return fields
