"""DeepSeek Chat Completions adapter for the internal message contract."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from json import JSONDecodeError
from typing import Any

import httpx
from openai import APIResponseValidationError, OpenAI

from .contracts import Message, ModelResponse, ModelToolCall
from .frozen_model import FrozenModelParameters
from .gemini_capabilities import validate_provider_parameters


class ProviderResponseError(Exception):
    """The provider response violates the supported wire protocol."""


_TOOL_STATUSES = {"success", "error", "not_executed", "uncertain"}
_NON_SUCCESS_CONTENT = {
    "error": "Tool execution failed; details withheld.",
    "not_executed": "Tool was not executed.",
    "uncertain": "Tool execution status is uncertain.",
}


class DeepSeekAdapter:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        http_client: httpx.Client | None = None,
        max_tokens: int | None = None,
        max_retries: int = 1,
        temperature: float | int | None = None,
    ) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        parameters: dict[str, Any] = {"model": model}
        if max_tokens is not None:
            parameters["max_tokens"] = max_tokens
        if temperature is not None:
            parameters["temperature"] = temperature
        self._parameters = FrozenModelParameters.from_mapping(parameters)
        validate_provider_parameters("chat", dict(self._parameters.as_mapping()))
        self._max_retries = max_retries
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            max_retries=max_retries,
            timeout=60,
        )

    @property
    def model(self) -> str:
        return self._parameters.model

    @property
    def max_tokens(self) -> int | None:
        return self._parameters.max_tokens

    @property
    def temperature(self) -> float | int | None:
        return self._parameters.temperature

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def model_parameters(self) -> Mapping[str, Any]:
        return self._parameters.as_mapping()

    @property
    def provider_address(self) -> str:
        return str(self._client.base_url).rstrip("/")

    def close(self) -> None:
        self._client.close()

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        if self._client.max_retries != self.max_retries:
            raise ValueError("Model client retry settings changed after construction")
        return self.generate_prepared(self.prepare_request(messages, tools, self.model_parameters))

    @classmethod
    def prepare_request(cls, messages, tools, parameters):
        parameters = dict(FrozenModelParameters.from_mapping(parameters).as_mapping())
        validate_provider_parameters("chat", parameters)
        request_snapshot = copy.deepcopy(
            {
                "model": parameters["model"],
                "messages": cls._project_messages(copy.deepcopy(list(messages))),
                "tools": list(tools),
                "tool_choice": "auto",
                "stream": False,
                "extra_body": {"thinking": {"type": "disabled"}},
            }
        )
        for key in ("max_tokens", "temperature"):
            if key in parameters:
                request_snapshot[key] = parameters[key]
        wire = {key: copy.deepcopy(value) for key, value in request_snapshot.items()
                if key != "extra_body"}
        wire.update(copy.deepcopy(request_snapshot["extra_body"]))
        return {"projection": "deepseek-chat@1", "wire_request": wire,
                "transport_arguments": request_snapshot}

    def generate_prepared(self, prepared):
        if self._client.max_retries != self.max_retries:
            raise ValueError("Model client retry settings changed after construction")
        try:
            response = self._client.chat.completions.create(
                **copy.deepcopy(prepared["transport_arguments"]))
        except (JSONDecodeError, UnicodeDecodeError, APIResponseValidationError) as exc:
            raise ProviderResponseError("Provider response could not be decoded.") from exc
        return self._parse_response(response)

    @classmethod
    def _parse_response(cls, response: Any) -> ModelResponse:
        # The SDK constructs models permissively, so required wire fields must
        # be checked without treating unrelated adapter exceptions as provider errors.
        choices = cls._response_field(response, "choices")
        if not cls._response_sequence(choices) or not choices:
            raise ProviderResponseError("Provider response requires completion choices.")
        choice = choices[0]
        finish_reason = cls._response_field(choice, "finish_reason")
        message = cls._response_field(choice, "message")
        if not isinstance(finish_reason, str) or not finish_reason:
            raise ProviderResponseError("Provider response requires a finish reason.")
        if cls._response_field(message, "role") != "assistant":
            raise ProviderResponseError("Provider response requires an assistant message.")
        content = cls._response_field(message, "content")
        if content is not None and not isinstance(content, str):
            raise ProviderResponseError("Provider assistant content must be text or null.")
        response_calls = cls._response_field(message, "tool_calls")
        if response_calls is None:
            response_calls = ()
        elif not cls._response_sequence(response_calls):
            raise ProviderResponseError("Provider tool calls must be a sequence.")
        if finish_reason == "tool_calls" and not response_calls:
            raise ProviderResponseError("Provider tool-call response requires calls.")
        tool_calls = []
        for call in response_calls:
            call_id = cls._response_field(call, "id")
            call_type = cls._response_field(call, "type")
            function = cls._response_field(call, "function")
            name = cls._response_field(function, "name")
            arguments = cls._response_field(function, "arguments")
            if (
                not isinstance(call_id, str) or not call_id
                or call_type != "function"
                or not isinstance(name, str) or not name
                or not isinstance(arguments, str)
            ):
                raise ProviderResponseError("Provider tool call has invalid required fields.")
            tool_calls.append(ModelToolCall(
                id=call_id, name=name, raw_arguments=arguments, type=call_type,
            ))
        usage = cls._response_field(response, "usage")
        if usage is not None:
            if isinstance(usage, Mapping):
                usage = copy.deepcopy(dict(usage))
            else:
                model_dump = getattr(usage, "model_dump", None)
                if not callable(model_dump):
                    raise ProviderResponseError("Provider usage must be an object or null.")
                usage = model_dump(exclude_none=True)
                if not isinstance(usage, dict):
                    raise ProviderResponseError("Provider usage must be an object or null.")
        response_id = cls._response_field(response, "id")
        model = cls._response_field(response, "model")
        if any(value is not None and not isinstance(value, str) for value in (response_id, model)):
            raise ProviderResponseError("Provider response metadata must be text or null.")

        return ModelResponse(
            finish_reason=finish_reason,
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            response_id=response_id,
            model=model,
        )

    @staticmethod
    def _response_sequence(value: Any) -> bool:
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

    @staticmethod
    def _response_field(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    @staticmethod
    def _project_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        pending_call_ids: list[str] = []

        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("History messages must be mappings.")
            if message.get("provider_metadata") is not None or message.get("thinking_summary") is not None:
                raise ValueError("DeepSeek Chat cannot replay another provider's signed thinking history.")

            kind = message.get("kind")
            if pending_call_ids and kind != "tool_result":
                raise ValueError("Every assistant tool call must be followed by its ordered result.")

            if kind == "text":
                role = message.get("role")
                content = message.get("content")
                if (
                    not isinstance(role, str)
                    or role not in {"system", "user", "assistant"}
                    or not isinstance(content, str)
                ):
                    raise ValueError("Text history messages require a supported role and string content.")
                projected.append({"role": role, "content": content})
                continue

            if kind == "assistant_calls":
                if message.get("role") != "assistant":
                    raise ValueError("Assistant call history must have the assistant role.")
                calls = message.get("calls")
                if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)) or not calls:
                    raise ValueError("Assistant call history must contain at least one call.")

                wire_calls: list[dict[str, Any]] = []
                batch_ids: set[str] = set()
                for call in calls:
                    if not isinstance(call, Mapping):
                        raise ValueError("Assistant tool calls must be mappings.")
                    call_id = call.get("id")
                    name = call.get("name")
                    raw_arguments = call.get("raw_arguments")
                    if not all(isinstance(value, str) and value for value in (call_id, name)):
                        raise ValueError("Assistant tool calls require non-empty IDs and names.")
                    if not isinstance(raw_arguments, str):
                        raise ValueError("Assistant tool calls require raw argument strings.")
                    if call_id in batch_ids:
                        raise ValueError("Assistant tool call IDs must be unique within a batch.")
                    batch_ids.add(call_id)
                    wire_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": raw_arguments},
                        }
                    )

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": wire_calls,
                }
                if assistant_message["content"] is not None and not isinstance(
                    assistant_message["content"], str
                ):
                    raise ValueError("Assistant call content must be a string or null.")
                projected.append(assistant_message)
                pending_call_ids = [call["id"] for call in wire_calls]
                continue

            if kind == "tool_result":
                if message.get("role") != "tool":
                    raise ValueError("Tool result history must have the tool role.")
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("Tool results require a non-empty tool call ID.")
                if not pending_call_ids or call_id != pending_call_ids[0]:
                    raise ValueError("Tool results must pair with pending calls in their original order.")
                status = message.get("status")
                if not isinstance(status, str) or status not in _TOOL_STATUSES:
                    raise ValueError("Tool result status is not recognized.")
                content = message.get("content")
                if not isinstance(content, str):
                    raise ValueError("Tool result content must be a string.")
                projected.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content if status == "success" else _NON_SUCCESS_CONTENT[status],
                    }
                )
                pending_call_ids.pop(0)
                continue

            raise ValueError("History contains an unsupported message kind.")

        if pending_call_ids:
            raise ValueError("History ends with assistant tool calls that have no results.")
        return projected
