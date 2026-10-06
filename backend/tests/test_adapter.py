from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIResponseValidationError

from phase1_agent.adapter import DeepSeekAdapter, ProviderResponseError
from phase1_agent.contracts import ModelResponse, ModelToolCall


def completion_payload(
    *,
    content: str | None = "done",
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def make_http_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_generate_sends_complete_native_history_and_parses_response() -> None:
    captured: list[httpx.Request] = []
    response_calls = [
        {
            "id": "returned-1",
            "type": "function",
            "function": {"name": "read", "arguments": ' { "path" : "a" } '},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=completion_payload(
            content="working",
            finish_reason="tool_calls",
            tool_calls=response_calls,
        ))

    messages = [
        {
            "kind": "text",
            "role": "system",
            "source": "fixed_prompt",
            "id": "system",
            "content": "system prompt",
        },
        {
            "kind": "text",
            "role": "user",
            "source": "real_user",
            "id": "first:user",
            "content": "first request",
        },
        {
            "kind": "assistant_calls",
            "role": "assistant",
            "content": "I will inspect both files.",
            "calls": [
                {
                    "id": "call-z",
                    "name": "read",
                    "raw_arguments": ' { "path" : "z.txt" } ',
                    "arguments": {"path": "z.txt"},
                },
                {
                    "id": "call-a",
                    "name": "write",
                    "raw_arguments": '{"path":"a.txt","text":"draft"}',
                    "arguments": {"path": "a.txt", "text": "draft"},
                },
            ],
        },
        {
            "kind": "tool_result",
            "role": "tool",
            "tool_call_id": "call-z",
            "name": "read",
            "status": "success",
            "content": "source text",
        },
        {
            "kind": "tool_result",
            "role": "tool",
            "tool_call_id": "call-a",
            "name": "write",
            "status": "error",
            "content": "PRIVATE exception body",
        },
        {
            "kind": "assistant_calls",
            "role": "assistant",
            "calls": [
                {
                    "id": "call-final",
                    "name": "final_answer",
                    "raw_arguments": '{ "answer" : {"content":"draft"} }',
                    "arguments": {"answer": {"content": "draft"}},
                }
            ],
        },
        {
            "kind": "tool_result",
            "role": "tool",
            "tool_call_id": "call-final",
            "name": "final_answer",
            "status": "success",
            "content": '{\n  "content": "draft"\n}',
        },
        {
            "kind": "text",
            "role": "user",
            "source": "real_user",
            "id": "next:user",
            "content": "next real user",
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    client = make_http_client(handler)
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    try:
        result = adapter.generate(messages, tools)
    finally:
        client.close()

    assert isinstance(result, ModelResponse)
    assert result.finish_reason == "tool_calls"
    assert result.content == "working"
    assert result.tool_calls == (
        ModelToolCall(
            id="returned-1",
            name="read",
            raw_arguments=' { "path" : "a" } ',
            type="function",
        ),
    )
    assert result.usage is not None
    assert result.usage["prompt_tokens"] == 7
    assert len(captured) == 1
    request = captured[0]
    assert request.url == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer test-secret"
    assert b"test-secret" not in request.content
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["tool_choice"] == "auto"
    assert body["stream"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert body["tools"] == tools
    assert [message["role"] for message in body["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
        "user",
    ]
    assert body["messages"][2] == {
        "role": "assistant",
        "content": "I will inspect both files.",
        "tool_calls": [
            {
                "id": "call-z",
                "type": "function",
                "function": {"name": "read", "arguments": ' { "path" : "z.txt" } '},
            },
            {
                "id": "call-a",
                "type": "function",
                "function": {"name": "write", "arguments": '{"path":"a.txt","text":"draft"}'},
            },
        ],
    }
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call-z",
        "content": "source text",
    }
    assert body["messages"][4] == {
        "role": "tool",
        "tool_call_id": "call-a",
        "content": "Tool execution failed; details withheld.",
    }
    assert body["messages"][5]["tool_calls"][0]["function"]["arguments"] == (
        '{ "answer" : {"content":"draft"} }'
    )
    assert body["messages"][6] == {
        "role": "tool",
        "tool_call_id": "call-final",
        "content": '{\n  "content": "draft"\n}',
    }
    assert body["messages"][-1] == {"role": "user", "content": "next real user"}
    assert "PRIVATE exception body" not in request.content.decode()


def test_kindless_role_message_is_rejected_before_http_send() -> None:
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json=completion_payload())

    client = make_http_client(handler)
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    try:
        with pytest.raises(ValueError, match="unsupported message kind"):
            adapter.generate([{"role": "user", "content": "legacy shape"}], [])
    finally:
        client.close()

    assert not sent


def test_incomplete_response_tool_function_is_a_provider_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        SimpleNamespace(id="incomplete-call", type="function", function=None)
                    ],
                ),
            )
        ],
        usage=None,
    )
    client = make_http_client(lambda request: httpx.Response(500))
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    monkeypatch.setattr(adapter._client.chat.completions, "create", lambda **kwargs: response)
    try:
        with pytest.raises(ProviderResponseError, match="invalid required fields"):
            adapter.generate(
                [{"kind": "text", "role": "user", "content": "inspect"}],
                [],
            )
    finally:
        client.close()


@pytest.mark.parametrize("payload", [
    None,
    [],
    ["PRIVATE-RESPONSE"],
    "PRIVATE-RESPONSE",
    1,
    {},
    {"choices": []},
    {"choices": None},
    {"choices": "PRIVATE-RESPONSE"},
    {"choices": {}},
    {"choices": [None]},
    {"choices": ["PRIVATE-RESPONSE"]},
    {"choices": [{}]},
])
def test_http_200_invalid_response_envelope_is_not_an_adapter_program_error(payload: Any) -> None:
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, content=json.dumps(payload), headers={"content-type": "application/json"})

    with make_http_client(handler) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        with pytest.raises(ProviderResponseError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert len(captured) == 1
    assert "PRIVATE-RESPONSE" not in str(caught.value)


_MISSING = object()


@pytest.mark.parametrize(("path", "value"), [
    (("choices", 0, "finish_reason"), _MISSING),
    (("choices", 0, "finish_reason"), None),
    (("choices", 0, "finish_reason"), 7),
    (("choices", 0, "finish_reason"), ""),
    (("choices", 0, "message"), _MISSING),
    (("choices", 0, "message"), None),
    (("choices", 0, "message"), "PRIVATE-RESPONSE"),
    (("choices", 0, "message"), []),
    (("choices", 0, "message", "role"), _MISSING),
    (("choices", 0, "message", "role"), "user"),
    (("choices", 0, "message", "content"), 7),
    (("choices", 0, "message", "content"), {"secret": "PRIVATE-RESPONSE"}),
    (("choices", 0, "message", "tool_calls"), "PRIVATE-RESPONSE"),
    (("choices", 0, "message", "tool_calls"), {}),
    (("usage",), 7),
    (("usage",), []),
    (("id",), {}),
    (("model",), 7),
])
def test_http_200_invalid_required_response_fields_are_explicit_protocol_errors(path, value) -> None:
    payload = copy.deepcopy(completion_payload())
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is _MISSING:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=payload)

    with make_http_client(handler) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        with pytest.raises(ProviderResponseError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert len(captured) == 1
    assert "PRIVATE-RESPONSE" not in str(caught.value)


@pytest.mark.parametrize("calls", [
    None,
    [],
    [None],
    ["PRIVATE-RESPONSE"],
    [{}],
    [{"id": "call", "type": "function", "function": None}],
    [{"id": "call", "type": "function", "function": "PRIVATE-RESPONSE"}],
    [{"type": "function", "function": {"name": "read", "arguments": "{}"}}],
    [{"id": "", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    [{"id": "call", "function": {"name": "read", "arguments": "{}"}}],
    [{"id": "call", "type": "other", "function": {"name": "read", "arguments": "{}"}}],
    [{"id": "call", "type": "function", "function": {"arguments": "{}"}}],
    [{"id": "call", "type": "function", "function": {"name": "", "arguments": "{}"}}],
    [{"id": "call", "type": "function", "function": {"name": "read"}}],
    [{"id": "call", "type": "function", "function": {"name": "read", "arguments": {}}}],
])
def test_http_200_invalid_tool_call_fields_are_explicit_protocol_errors(calls) -> None:
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=completion_payload(
            content=None, finish_reason="tool_calls", tool_calls=calls,
        ))

    with make_http_client(handler) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        with pytest.raises(ProviderResponseError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert len(captured) == 1
    assert "PRIVATE-RESPONSE" not in str(caught.value)


@pytest.mark.parametrize("content", [b"{PRIVATE-RESPONSE", b"\xff"])
def test_http_200_response_json_decoding_error_retains_cause_without_payload(content) -> None:
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, content=content, headers={"content-type": "application/json"})

    with make_http_client(handler) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        with pytest.raises(ProviderResponseError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert isinstance(caught.value.__cause__, (json.JSONDecodeError, UnicodeDecodeError))
    assert len(captured) == 1
    assert "PRIVATE-RESPONSE" not in str(caught.value)


def test_sdk_response_validation_error_is_an_explicit_provider_error(monkeypatch) -> None:
    response = httpx.Response(200, request=httpx.Request("POST", "https://offline.invalid"))
    failure = APIResponseValidationError(response=response, body={"secret": "PRIVATE-RESPONSE"})

    def invalid_response(**kwargs):
        raise failure

    with make_http_client(lambda request: pytest.fail("Unexpected HTTP dispatch")) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        monkeypatch.setattr(adapter._client.chat.completions, "create", invalid_response)
        with pytest.raises(ProviderResponseError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert caught.value.__cause__ is failure
    assert "PRIVATE-RESPONSE" not in str(caught.value)


@pytest.mark.parametrize("failure", [
    ValueError("PRIVATE-PROGRAM"),
    TypeError("PRIVATE-PROGRAM"),
    AttributeError("PRIVATE-PROGRAM"),
    PermissionError("PRIVATE-PROGRAM"),
    RuntimeError("PRIVATE-PROGRAM"),
])
def test_local_client_program_errors_are_not_reclassified_as_provider_responses(monkeypatch, failure) -> None:
    def broken_client(**kwargs):
        raise failure

    with make_http_client(lambda request: pytest.fail("Unexpected HTTP dispatch")) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        monkeypatch.setattr(adapter._client.chat.completions, "create", broken_client)
        with pytest.raises(type(failure)) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert caught.value is failure


def test_response_accessor_program_error_is_not_hidden_by_shape_validation(monkeypatch) -> None:
    failure = ValueError("PRIVATE-PROGRAM")

    class BrokenChoice:
        finish_reason = "stop"

        @property
        def message(self):
            raise failure

    response = SimpleNamespace(choices=[BrokenChoice()])
    with make_http_client(lambda request: pytest.fail("Unexpected HTTP dispatch")) as client:
        adapter = DeepSeekAdapter(api_key="test-secret", http_client=client, max_retries=0)
        monkeypatch.setattr(adapter._client.chat.completions, "create", lambda **kwargs: response)
        with pytest.raises(ValueError) as caught:
            adapter.generate([{"kind": "text", "role": "user", "content": "inspect"}], [])
    assert caught.value is failure


def test_sdk_retry_reuses_identical_request_body() -> None:
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content)
        if len(request_bodies) == 1:
            return httpx.Response(
                503,
                headers={"retry-after-ms": "0"},
                json={"error": {"message": "temporary failure"}},
            )
        return httpx.Response(200, json=completion_payload())

    client = make_http_client(handler)
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    try:
        result = adapter.generate(
            [{"kind": "text", "role": "user", "content": "retry me"}],
            [],
        )
    finally:
        client.close()

    assert result.finish_reason == "stop"
    assert len(request_bodies) == 2
    assert request_bodies[0] == request_bodies[1]


def test_effective_parameters_are_immutable_and_sent_without_defaults() -> None:
    captured = []
    with make_http_client(
        lambda request: captured.append(json.loads(request.content))
        or httpx.Response(200, json=completion_payload())
    ) as client:
        adapter = DeepSeekAdapter(
            "test-secret", model="frozen-model", max_tokens=333,
            temperature=0.25, max_retries=0, http_client=client,
        )
        for attribute, replacement in (
            ("model", "changed"), ("max_tokens", 999), ("temperature", 1), ("max_retries", 3),
        ):
            with pytest.raises(AttributeError):
                setattr(adapter, attribute, replacement)
        with pytest.raises(TypeError):
            adapter.model_parameters["model"] = "changed"
        adapter.generate([{"kind": "text", "role": "user", "content": "frozen"}], [])
    assert captured[0]["model"] == "frozen-model"
    assert captured[0]["max_tokens"] == 333
    assert captured[0]["temperature"] == 0.25
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["stream"] is False


def test_mutated_sdk_retry_setting_fails_before_http_dispatch() -> None:
    captured = []
    with make_http_client(
        lambda request: captured.append(request) or httpx.Response(200, json=completion_payload())
    ) as client:
        adapter = DeepSeekAdapter("test-secret", max_retries=0, http_client=client)
        adapter._client.max_retries = 3
        with pytest.raises(ValueError, match="retry settings changed"):
            adapter.generate([{"kind": "text", "role": "user", "content": "frozen"}], [])
    assert captured == []


@pytest.mark.parametrize("parameters", [
    {"model": ""},
    {"model": " "},
    {"model": False},
    {"max_tokens": True},
    {"max_tokens": 0},
    {"temperature": True},
    {"temperature": float("nan")},
    {"temperature": float("inf")},
    {"temperature": -1},
    {"temperature": 2.1},
])
def test_invalid_model_settings_fail_before_creating_client(parameters) -> None:
    with pytest.raises(ValueError):
        DeepSeekAdapter("test-secret", **parameters)


@pytest.mark.parametrize(
    "messages",
    [
        [
            {
                "kind": "tool_result",
                "role": "tool",
                "tool_call_id": "orphan",
                "status": "success",
                "content": "result",
            }
        ],
        [
            {
                "kind": "assistant_calls",
                "role": "assistant",
                "calls": [{"id": "call-1", "name": "read", "raw_arguments": "{}"}],
            }
        ],
        [
            {
                "kind": "assistant_calls",
                "role": "assistant",
                "calls": [
                    {"id": "call-1", "name": "read", "raw_arguments": "{}"},
                    {"id": "call-2", "name": "write", "raw_arguments": "{}"},
                ],
            },
            {
                "kind": "tool_result",
                "role": "tool",
                "tool_call_id": "call-2",
                "status": "success",
                "content": "second first",
            },
            {
                "kind": "tool_result",
                "role": "tool",
                "tool_call_id": "call-1",
                "status": "success",
                "content": "first second",
            },
        ],
        [
            {
                "kind": "assistant_calls",
                "role": "assistant",
                "calls": [{"id": "call-1", "name": "read", "raw_arguments": "{}"}],
            },
            {"kind": "text", "role": "user", "content": "cannot interrupt pending calls"},
            {
                "kind": "tool_result",
                "role": "tool",
                "tool_call_id": "call-1",
                "status": "success",
                "content": "result",
            },
        ],
    ],
)
def test_invalid_call_result_history_is_rejected_before_http_send(
    messages: list[dict[str, Any]],
) -> None:
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json=completion_payload())

    client = make_http_client(handler)
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    try:
        with pytest.raises(ValueError):
            adapter.generate(messages, [])
    finally:
        client.close()

    assert not sent


@pytest.mark.parametrize(
    ("status", "expected_content"),
    [
        ("not_executed", "Tool was not executed."),
        ("uncertain", "Tool execution status is uncertain."),
    ],
)
def test_non_success_tool_status_uses_safe_content(
    status: str,
    expected_content: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=completion_payload())

    messages = [
        {
            "kind": "assistant_calls",
            "role": "assistant",
            "calls": [{"id": "call-1", "name": "write", "raw_arguments": "{}"}],
        },
        {
            "kind": "tool_result",
            "role": "tool",
            "tool_call_id": "call-1",
            "status": status,
            "content": "PRIVATE detail",
        },
    ]
    client = make_http_client(handler)
    adapter = DeepSeekAdapter(api_key="test-secret", http_client=client)
    try:
        adapter.generate(messages, [])
    finally:
        client.close()

    body = json.loads(captured[0].content)
    assert body["messages"][1]["content"] == expected_content
    assert "PRIVATE detail" not in captured[0].content.decode()
