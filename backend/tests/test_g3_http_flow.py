import json

import httpx
from smolagents import Tool

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.context import PromptContext
from phase1_agent.contracts import RunInput, RunLimits
from phase1_agent.kernel import AgentKernel
from phase1_agent.tools import final_answer_tool, register_tool


class MemoryRead(Tool):
    name = "read_note"
    description = "Read a note from node-local storage."
    inputs = {"path": {"type": "string", "description": "Note path"}}
    output_type = "string"

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.calls = []

    def forward(self, path: str) -> str:
        self.calls.append(path)
        return self.store[path]


class MemoryWrite(Tool):
    name = "write_note"
    description = "Write a note to node-local storage."
    inputs = {
        "path": {"type": "string", "description": "Note path"},
        "text": {"type": "string", "description": "Note text"},
    }
    output_type = "string"

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.calls = []

    def forward(self, path: str, text: str) -> str:
        self.calls.append((path, text))
        self.store[path] = text
        return "已写入"


def response(*, calls=None):
    return {
        "id": "cmpl-offline",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": None if calls else "plain", "tool_calls": calls},
            }
        ],
    }


def model_call(id, name, raw):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": raw}}


def test_formal_context_kernel_sdk_http_and_next_real_user():
    payloads = []
    attempts = []
    raw = '{ "path" : "notes.txt" }'
    write_raw = '{"path":"draft.txt","text":"草稿正文"}'
    answer = '{"answer":{"content":"草稿正文"}}'
    responses = [
        response(calls=[model_call("z", "read_note", raw), model_call("a", "write_note", write_raw)]),
        response(calls=[model_call("f1", "final_answer", answer)]),
        response(calls=[model_call("f2", "final_answer", '{"answer":{"content":"第二稿"}}')]),
    ]

    def handler(request):
        attempts.append(request.content)
        if len(attempts) == 1:
            return httpx.Response(503, headers={"retry-after-ms": "0"}, json={"error": {"message": "retry"}})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(api_key="offline-only", http_client=client)
    store = {"notes.txt": "参考资料"}
    read = MemoryRead(store)
    write = MemoryWrite(store)
    context = PromptContext("固定 system 提示词")
    tools = (register_tool(read), register_tool(write), final_answer_tool())
    output_spec = {
        "type": "object",
        "required": ["content"],
        "properties": {"content": {"type": "string", "minLength": 1}},
        "additionalProperties": True,
    }
    try:
        first_s0 = context.build("run-1", "读取资料并写草稿")
        first = AgentKernel().run(
            RunInput(
                run_id="run-1",
                s0=first_s0,
                tools=tools,
                adapter=adapter,
                output_spec=output_spec,
                correction_message="请调用 final_answer，提供符合约束的 JSON 对象。",
                limits=RunLimits(max_model_requests=2),
            )
        )
        assert first.status == "success"
        assert first.output == '{\n  "content": "草稿正文"\n}'
        context.commit("run-1", first)
        second_s0 = context.build("run-2", "写第二稿")
        second = AgentKernel().run(
            RunInput(
                run_id="run-2",
                s0=second_s0,
                tools=tools,
                adapter=adapter,
                output_spec=output_spec,
                correction_message="请调用 final_answer，提供符合约束的 JSON 对象。",
                limits=RunLimits(max_model_requests=1),
            )
        )
        assert second.status == "success"
        context.commit("run-2", second)
    finally:
        client.close()

    assert attempts[0] == attempts[1]
    assert len(payloads) == 3
    assert read.calls == ["notes.txt"]
    assert write.calls == [("draft.txt", "草稿正文")]
    assert store["draft.txt"] == "草稿正文"
    assert all(body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"} for body in payloads)
    assert all(body["tool_choice"] == "auto" and body["stream"] is False for body in payloads)
    assert all(body["tools"] == [tool.definition for tool in tools] for body in payloads)
    assert payloads[0]["messages"] == [
        {"role": "system", "content": "固定 system 提示词"},
        {"role": "user", "content": "读取资料并写草稿"},
    ]
    assert payloads[1]["messages"][:2] == payloads[0]["messages"]
    assert [call["id"] for call in payloads[1]["messages"][2]["tool_calls"]] == ["z", "a"]
    assert [call["function"]["arguments"] for call in payloads[1]["messages"][2]["tool_calls"]] == [raw, write_raw]
    assert payloads[1]["messages"][3] == {"role": "tool", "tool_call_id": "z", "content": "参考资料"}
    assert payloads[1]["messages"][4] == {"role": "tool", "tool_call_id": "a", "content": "已写入"}
    assert [message["role"] for message in payloads[2]["messages"]] == [
        "system", "user", "assistant", "tool", "tool", "assistant", "tool", "user"
    ]
    assert payloads[2]["messages"][5]["tool_calls"][0]["id"] == "f1"
    assert payloads[2]["messages"][6] == {"role": "tool", "tool_call_id": "f1", "content": first.output}
    assert payloads[2]["messages"][-1] == {"role": "user", "content": "写第二稿"}
    assert all(b"offline-only" not in attempt for attempt in attempts)
