"""Non-streaming Gemini generateContent transport with no retries or tool execution."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator

from .adapter import ProviderResponseError, _NON_SUCCESS_CONTENT, _TOOL_STATUSES
from .contract_json import loads_strict, validate_json_value
from .contract_errors import ContractValidationError
from .contracts import ModelResponse, ModelToolCall
from .frozen_model import FrozenModelParameters
from .gemini_capabilities import (
    GEMINI_BASE_URL, GEMINI_PROTOCOL, model_capability, thinking_wire,
    validate_provider_parameters,
)
from .graph_contracts import require
from .provider_metadata import validate_provider_metadata


_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "description", "minimum", "maximum", "minItems", "maxItems", "minLength",
    "maxLength", "pattern", "format",
}


class GeminiProtocolError(ProviderResponseError):
    def __init__(self, code, *, model, part_index=None):
        super().__init__("Gemini response failed local protocol validation")
        self.diagnostic = {"code": code, "model": model, "source": "provider_response"}
        if part_index is not None:
            self.diagnostic["part_index"] = part_index


def _validate_function_schema(value):
    require(type(value) is dict, "model_tool_schema_unsupported",
            "Gemini function schema must be an object")
    require(set(value) <= _SCHEMA_KEYS, "model_tool_schema_unsupported",
            "Gemini function schema contains unsupported JSON Schema keywords")
    kind = value.get("type")
    require(kind in ("object", "array", "string", "integer", "number", "boolean", "null"),
            "model_tool_schema_unsupported", "Gemini function schema requires an explicit type")
    if "properties" in value:
        require(kind == "object" and type(value["properties"]) is dict,
                "model_tool_schema_unsupported", "Function properties require an object schema")
        for nested in value["properties"].values():
            _validate_function_schema(nested)
    if "items" in value:
        require(kind == "array", "model_tool_schema_unsupported", "Function items require an array")
        _validate_function_schema(value["items"])
    if isinstance(value.get("additionalProperties"), dict):
        _validate_function_schema(value["additionalProperties"])
    Draft202012Validator.check_schema(value)


def _tool_declarations(tools):
    result = []
    for tool in tools:
        require(type(tool) is dict and tool.get("type") == "function"
                and type(tool.get("function")) is dict,
                "model_request_invalid", "Gemini tools require registered functions")
        function = tool["function"]
        require(set(function) == {"name", "description", "parameters"}
                and type(function["name"]) is str and bool(function["name"])
                and type(function["description"]) is str,
                "model_request_invalid", "Gemini function declaration is invalid")
        _validate_function_schema(function["parameters"])
        require(function["parameters"]["type"] == "object", "model_tool_schema_unsupported",
                "Gemini function arguments must have an object schema")
        result.append({"name": function["name"], "description": function["description"],
                       "parametersJsonSchema": deepcopy(function["parameters"])})
    require(len({value["name"] for value in result}) == len(result), "model_request_invalid",
            "Gemini function names repeat")
    return result


def _signature_check(content, model, *, source="provider_response"):
    if not model_capability(model)["required_call_signature"]:
        return
    for index, part in enumerate(content["parts"]):
        if "functionCall" in part:
            if type(part.get("thoughtSignature")) is not str or not part["thoughtSignature"]:
                if source == "history":
                    require(False, "gemini_thought_signature_missing",
                            f"Gemini history for model {model} is missing its required signature at part {index}")
                raise GeminiProtocolError("gemini_thought_signature_missing", model=model, part_index=index)
            return


class GeminiAdapter:
    max_retries = 0

    def __init__(self, api_key, model, *, base_url=GEMINI_BASE_URL, http_client=None,
                 max_tokens=None, temperature=None, thinking="disabled"):
        parameters = {"model": model, "thinking": thinking, "stream": False}
        if max_tokens is not None:
            parameters["max_tokens"] = max_tokens
        if temperature is not None:
            parameters["temperature"] = temperature
        self._parameters = FrozenModelParameters.from_mapping(parameters)
        validate_provider_parameters("gemini", dict(self._parameters.as_mapping()))
        self._base_url = str(httpx.URL(base_url)).rstrip("/")
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=60)
        self._owns_client = http_client is None

    @property
    def model_parameters(self):
        return self._parameters.as_mapping()

    @property
    def provider_address(self):
        return self._base_url

    def close(self):
        if self._owns_client:
            self._client.close()

    @classmethod
    def prepare_request(cls, messages, tools, parameters):
        parameters = dict(FrozenModelParameters.from_mapping(parameters).as_mapping())
        validate_provider_parameters("gemini", parameters)
        contents, system = cls._project_messages(messages, parameters["model"])
        config = {"thinkingConfig": thinking_wire(parameters)}
        if "max_tokens" in parameters:
            config["maxOutputTokens"] = parameters["max_tokens"]
        if "temperature" in parameters:
            config["temperature"] = parameters["temperature"]
        wire = {"contents": contents, "generationConfig": config}
        if system:
            wire["systemInstruction"] = {"parts": system}
        declarations = _tool_declarations(tools)
        if declarations:
            wire["tools"] = [{"functionDeclarations": declarations}]
            wire["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return {"projection": GEMINI_PROTOCOL, "wire_request": deepcopy(wire),
                "transport_arguments": deepcopy(wire),
                "model": parameters["model"]}

    def generate(self, messages, tools):
        return self.generate_prepared(self.prepare_request(messages, tools, self.model_parameters))

    def generate_prepared(self, prepared):
        require(prepared["projection"] == GEMINI_PROTOCOL and prepared["model"] == self._parameters.model,
                "model_request_conflict", "Prepared Gemini request differs from the frozen model")
        response = self._client.post(
            self._base_url + "/models/" + self._parameters.model + ":generateContent",
            headers={"x-goog-api-key": self._api_key},
            json=deepcopy(prepared["wire_request"]),
        )
        response.raise_for_status()
        try:
            value = loads_strict(response.content.decode("utf-8"))
        except (ContractValidationError, UnicodeDecodeError) as error:
            raise ProviderResponseError("Gemini response could not be decoded") from error
        return self._parse_response(value, self._parameters.model)

    @staticmethod
    def _project_messages(messages, model):
        contents, system, pending, result_parts = [], [], [], []
        for message in messages:
            require(isinstance(message, Mapping), "model_request_invalid",
                    "Gemini history messages must be mappings")
            kind = message.get("kind")
            require(not pending or kind == "tool_result", "gemini_tool_pairing_invalid",
                    "Gemini calls must be followed by their original ordered tool results")
            if kind == "tool_result":
                require(message.get("role") == "tool" and pending
                        and message.get("tool_call_id") == pending[0][0],
                        "gemini_tool_pairing_invalid", "Gemini tool result is mismatched or out of order")
                _, function = pending.pop(0)
                status = message.get("status")
                require(type(status) is str and status in _TOOL_STATUSES
                        and type(message.get("content")) is str,
                        "model_request_invalid", "Gemini tool result fields are invalid")
                result = {"result": message["content"]} if status == "success" else {
                    "error": _NON_SUCCESS_CONTENT[status]}
                part = {"functionResponse": {"name": function["name"], "response": result}}
                if "id" in function:
                    part["functionResponse"]["id"] = deepcopy(function["id"])
                result_parts.append(part)
                if not pending:
                    contents.append({"role": "user", "parts": result_parts})
                    result_parts = []
                continue
            require(kind in ("text", "assistant_calls"), "model_request_invalid",
                    "Gemini history contains an unsupported message kind")
            role, body = message.get("role"), message.get("content")
            require(role in ("system", "user", "assistant")
                    and (type(body) is str or (kind == "assistant_calls" and body is None)),
                    "model_request_invalid", "Gemini history text fields are invalid")
            metadata = message.get("provider_metadata")
            if role != "assistant":
                require(metadata is None and message.get("thinking_summary") is None,
                        "gemini_history_invalid", "Only model replies may carry Gemini protocol state")
                if role == "system":
                    system.append({"text": body})
                else:
                    contents.append({"role": "user", "parts": [{"text": body}]})
                continue
            calls = message.get("calls", []) if kind == "assistant_calls" else []
            if kind == "assistant_calls":
                require(isinstance(calls, Sequence) and not isinstance(calls, (str, bytes))
                        and bool(calls), "model_request_invalid", "Assistant call batch must not be empty")
                calls = list(calls)
            if metadata is not None:
                envelope = validate_provider_metadata(
                    metadata, calls=calls, content=body,
                    thinking_summary=message.get("thinking_summary"), model=model)
                original = envelope["content"]
                _signature_check(original, model, source="history")
                contents.append(deepcopy(original))
                pending = [(binding["tool_call_id"],
                            deepcopy(original["parts"][binding["part_index"]]["functionCall"]))
                           for binding in envelope["tool_call_bindings"]]
            else:
                require(not calls or not model_capability(model)["required_call_signature"],
                        "gemini_thought_signature_missing",
                        "Gemini tool history has no original provider metadata or required signature")
                require(message.get("thinking_summary") is None, "gemini_history_invalid",
                        "A Gemini thinking summary without its original protocol envelope cannot be replayed")
                parts = [{"text": body}] if body else []
                for call in calls:
                    require(type(call) is dict and type(call.get("id")) is str
                            and bool(call["id"]) and type(call.get("name")) is str
                            and bool(call["name"]) and type(call.get("raw_arguments")) is str,
                            "model_request_invalid", "Gemini call history fields are invalid")
                    try:
                        arguments = loads_strict(call["raw_arguments"])
                    except (ContractValidationError, TypeError):
                        require(False, "model_request_invalid", "Gemini call arguments are invalid JSON")
                    require(type(arguments) is dict, "model_request_invalid",
                            "Gemini call arguments must be an object")
                    function = {"name": call["name"], "args": arguments}
                    parts.append({"functionCall": function})
                    pending.append((call["id"], function))
                if parts:
                    contents.append({"role": "model", "parts": parts})
        require(not pending, "gemini_tool_pairing_invalid", "Gemini history ends with missing tool results")
        require(bool(contents), "model_request_invalid", "Gemini request needs conversation contents")
        return contents, system

    @staticmethod
    def _parse_response(value, model):
        try:
            validate_json_value(value)
            require(type(value) is dict and type(value.get("candidates")) is list
                    and bool(value["candidates"]), "model_response_invalid",
                    "Gemini response requires a completion candidate")
            candidate = value["candidates"][0]
            require(type(candidate) is dict and candidate.get("finishReason") in ("STOP", "MAX_TOKENS"),
                    "model_response_invalid", "Gemini candidate did not complete with usable content")
            original = candidate.get("content")
            require(type(original) is dict and original.get("role") == "model"
                    and type(original.get("parts")) is list and bool(original["parts"]),
                    "model_response_invalid", "Gemini response requires original model parts")
            text, thoughts, calls, bindings, native_ids = [], [], [], [], set()
            for index, part in enumerate(original["parts"]):
                require(type(part) is dict, "model_response_invalid", "Gemini response part is invalid")
                if "text" in part:
                    require(type(part["text"]) is str, "model_response_invalid", "Gemini text must be a string")
                    (thoughts if part.get("thought") is True else text).append(part["text"])
                elif "functionCall" in part:
                    function = part["functionCall"]
                    require(type(function) is dict and type(function.get("name")) is str
                            and bool(function["name"]) and type(function.get("args")) is dict,
                            "model_response_invalid", "Gemini functionCall requires a name and object arguments")
                    if "id" in function:
                        require(type(function["id"]) is str and bool(function["id"])
                                and function["id"] not in native_ids, "model_response_invalid",
                                "Gemini functionCall native identities must be nonempty and unique")
                        native_ids.add(function["id"])
                    call_id = str(uuid4())
                    calls.append(ModelToolCall(
                        id=call_id, name=function["name"],
                        raw_arguments=json.dumps(function["args"], ensure_ascii=False, separators=(",", ":"))))
                    bindings.append({"part_index": index, "tool_call_id": call_id})
                else:
                    require(False, "model_response_invalid",
                            "Gemini response contains unsupported non-text, non-function content")
            _signature_check(original, model)
            content, summary = "".join(text), "".join(thoughts) or None
            metadata = validate_provider_metadata({
                "schema_version": 1, "provider": "gemini", "protocol": GEMINI_PROTOCOL, "model": model,
                "content": original, "tool_call_bindings": bindings,
                "response_model": value.get("modelVersion"),
            }, calls=[{"id": call.id, "name": call.name, "raw_arguments": call.raw_arguments}
                      for call in calls], content=content, thinking_summary=summary, model=model)
            usage = value.get("usageMetadata")
            require(usage is None or type(usage) is dict, "model_response_invalid",
                    "Gemini usage metadata must be an object")
            if usage is not None:
                usage = deepcopy(usage)
                for source, target in (("promptTokenCount", "prompt_tokens"),
                                       ("totalTokenCount", "total_tokens")):
                    if source in usage:
                        usage[target] = usage[source]
                usage["completion_tokens"] = (
                    usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0))
            require(all(value.get(key) is None or type(value[key]) is str
                        for key in ("responseId", "modelVersion")), "model_response_invalid",
                    "Gemini response identifiers must be text or null")
            return ModelResponse(
                finish_reason="tool_calls" if calls else (
                    "length" if candidate["finishReason"] == "MAX_TOKENS" else "stop"),
                content=content, tool_calls=tuple(calls), thinking_summary=summary,
                provider_metadata=metadata, usage=usage,
                response_id=value.get("responseId"), model=value.get("modelVersion"))
        except ProviderResponseError:
            raise
        except Exception as error:
            raise ProviderResponseError("Gemini response violates the supported protocol") from error
