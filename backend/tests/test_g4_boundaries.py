import copy
import json
from threading import Event

import httpx
import pytest
from smolagents import Tool

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.context import PromptContext
from phase1_agent.contracts import ModelResponse, ModelToolCall, RunInput, RunLimits, RunResult
from phase1_agent.kernel import AgentKernel
from phase1_agent.tools import final_answer_tool, register_tool


SPEC = {
    "type": "object",
    "required": ["content"],
    "properties": {"content": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
CORRECTION = "Call final_answer with a nonempty content string."


class NodeTool(Tool):
    name = "record"
    description = "Record text in this node."
    inputs = {"text": {"type": "string", "description": "Text"}}
    output_type = "string"

    def __init__(self, store, on_call=None):
        super().__init__()
        self.store = store
        self.on_call = on_call

    def forward(self, text: str) -> str:
        self.store.append(text)
        if self.on_call:
            self.on_call()
        return text


class Responses:
    def __init__(self, *items):
        self.items = iter(items)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        item = next(self.items)
        if isinstance(item, Exception):
            raise item
        return item


def call(id, name="record", raw='{"text":"a"}'):
    return ModelToolCall(id=id, name=name, raw_arguments=raw)


def run(adapter, tool, *, context=None, run_id="r", budget=3, signal=None):
    context = context or PromptContext("fixed")
    s0 = context.build(run_id, "real input")
    result = AgentKernel().run(
        RunInput(
            run_id=run_id,
            s0=s0,
            tools=(register_tool(tool), final_answer_tool()),
            adapter=adapter,
            output_spec=SPEC,
            correction_message=CORRECTION,
            limits=RunLimits(max_model_requests=budget),
            cancellation=signal,
        )
    )
    return context, result


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse("tool_calls", tool_calls=(call("z"), call("z"))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call(None))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", "missing"))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw="{broken"))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw="[]"))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw='{"text":"a","text":"b"}'))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw='{"text":true}'))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", "final_answer", '{"answer":{}}'))),
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw=None))),
        ModelResponse("length", tool_calls=(call("z"),)),
    ],
)
def test_invalid_batch_never_executes_first_valid_call(response):
    store = []
    _, result = run(Responses(response), NodeTool(store))
    assert result.status == "protocol_error"
    assert result.dispatch_started
    assert result.history_delta == ()
    assert store == []


def test_plain_text_then_final_uses_only_explicit_correction():
    store = []
    adapter = Responses(
        ModelResponse("stop", content="ordinary text"),
        ModelResponse("tool_calls", tool_calls=(call("final", "final_answer", '{"answer":{"content":"改正"}}'),)),
    )
    _, result = run(adapter, NodeTool(store), budget=2)
    assert result.status == "success"
    assert result.output == '{\n  "content": "改正"\n}'
    assert [item["source"] for item in result.history_delta] == [
        "assistant_turn", "protocol_feedback", "assistant_turn", "final_control"
    ]
    assert result.history_delta[1]["content"] == CORRECTION
    assert store == []
    assert len(adapter.requests) == 2


def test_business_invalid_final_is_paired_then_corrected():
    adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("bad", "final_answer", '{"answer":{"content":""}}'),)),
        ModelResponse("tool_calls", tool_calls=(call("good", "final_answer", '{"answer":{"content":"可用"}}'),)),
    )
    _, result = run(adapter, NodeTool([]), budget=2)
    assert result.status == "success"
    assert result.history_delta[1]["status"] == "error"
    assert result.history_delta[1]["tool_call_id"] == "bad"
    assert result.history_delta[2]["source"] == "protocol_feedback"
    assert result.history_delta[-1]["tool_call_id"] == "good"


def test_transport_failure_does_not_leak_exception_or_duplicate_user():
    context, result = run(Responses(RuntimeError("private prompt or credential")), NodeTool([]))
    assert result.status == "model_error"
    assert result.dispatch_started
    assert result.error == {"category": "RuntimeError", "stage": "model_request"}
    assert result.history_delta == ()
    context.commit("r", result)
    context.commit("r", result)
    assert [message["source"] for message in context.history] == ["real_user"]
    assert "private" not in str(result)


def test_cancel_before_dispatch_drops_pending_user():
    signal = Event()
    signal.set()
    adapter = Responses()
    context, result = run(adapter, NodeTool([]), signal=signal)
    assert result.status == "cancelled"
    assert not result.dispatch_started
    assert adapter.requests == []
    context.commit("r", result)
    assert context.history == ()
    assert context.build("next", "new user")[-1]["source"] == "real_user"


def test_cancel_during_failing_tool_marks_uncertain_and_unstarted():
    signal = Event()
    store = []

    def interrupted_failure():
        signal.set()
        raise RuntimeError("operation may have written")

    tool = NodeTool(store, on_call=interrupted_failure)
    adapter = Responses(ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw='{"text":"b"}'))))
    context, result = run(adapter, tool, signal=signal)
    assert result.status == "cancelled"
    assert store == ["a"]
    assert [item["status"] for item in result.history_delta[1:]] == ["uncertain", "not_executed"]
    with pytest.raises(RuntimeError, match="pending"):
        context.build("next", "too early")
    context.commit("r", result)
    assert context.build("next", "now safe")[-1]["source"] == "real_user"


def test_unprintable_tool_result_still_gets_paired_error():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("conversion failed")

    store = []
    tool = NodeTool(store)
    tool.forward = lambda text: Unprintable()
    adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("z"), call("a", raw='{"text":"b"}'))),
        ModelResponse("tool_calls", tool_calls=(call("f", "final_answer", '{"answer":{"content":"done"}}'),)),
    )
    _, result = run(adapter, tool)
    assert result.status == "success"
    assert [message["tool_call_id"] for message in result.history_delta if message["kind"] == "tool_result"] == [
        "z", "a", "f"
    ]
    assert [message["status"] for message in result.history_delta if message["kind"] == "tool_result"] == [
        "error", "error", "success"
    ]


def test_same_batch_partial_success_keeps_order_and_effects():
    store = []

    def sometimes_fails(text):
        store.append(text)
        if text == "bad":
            raise RuntimeError("second operation failed after side effect")
        return "ok"

    tool = NodeTool(store)
    tool.forward = sometimes_fails
    adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("z", raw='{"text":"good"}'), call("a", raw='{"text":"bad"}'))),
        ModelResponse("tool_calls", tool_calls=(call("f", "final_answer", '{"answer":{"content":"done"}}'),)),
    )
    _, result = run(adapter, tool)
    assert result.status == "success"
    assert store == ["good", "bad"]
    results = [item for item in result.history_delta if item["kind"] == "tool_result"]
    assert [(item["tool_call_id"], item["status"]) for item in results] == [
        ("z", "success"), ("a", "error"), ("f", "success")
    ]
    assert [item["tool_call_id"] for item in adapter.requests[1] if item["kind"] == "tool_result"] == ["z", "a"]


def test_two_nodes_have_separate_prompts_stores_and_histories():
    left_store, right_store = [], []
    left_adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("left"),)),
        ModelResponse("tool_calls", tool_calls=(call("left-final", "final_answer", '{"answer":{"content":"left"}}'),)),
    )
    right_adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("right", raw='{"text":"right"}'),)),
        ModelResponse("tool_calls", tool_calls=(call("right-final", "final_answer", '{"answer":{"content":"right"}}'),)),
    )
    left = PromptContext("left system")
    right = PromptContext("right system")
    _, left_result = run(left_adapter, NodeTool(left_store), context=left, run_id="left")
    _, right_result = run(right_adapter, NodeTool(right_store), context=right, run_id="right")
    left.commit("left", left_result)
    right.commit("right", right_result)
    assert left_store == ["a"] and right_store == ["right"]
    assert left_result.output != right_result.output
    assert left.build("next-left", "again")[0]["content"] == "left system"
    assert right.build("next-right", "again")[0]["content"] == "right system"
    assert all("left" not in str(item) for item in right.history)


def canonical_history():
    store = []
    adapter = Responses(
        ModelResponse("tool_calls", tool_calls=(call("z"),)),
        ModelResponse("tool_calls", tool_calls=(call("f", "final_answer", '{"answer":{"content":"done"}}'),)),
    )
    context, result = run(adapter, NodeTool(store))
    context.commit("r", result)
    return list(copy.deepcopy(context.history))


def test_imported_canonical_history_is_isolated_and_replayed():
    records = canonical_history()
    imported = PromptContext("same fixed")
    imported.import_history(records)
    records[1]["calls"][0]["arguments"]["text"] = "tampered after import"
    next_s0 = imported.build("second", "next")
    assert next_s0[1]["source"] == "real_user"
    assert next_s0[2]["calls"][0]["arguments"] == {"text": "a"}
    assert next_s0[-1]["content"] == "next"
    with pytest.raises(RuntimeError, match="fresh"):
        imported.import_history([])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda records: records[1]["calls"][0].update(arguments={"text": "tampered"}),
        lambda records: records[1]["calls"][0].update(raw_arguments='{"text":"a","text":"b"}'),
        lambda records: records[1]["calls"][0].update(arguments={"text": True}),
        lambda records: records[1]["calls"][0].update(raw_arguments="[]"),
        lambda records: records[2].update(tool_call_id="wrong"),
        lambda records: records[2].update(name="wrong"),
        lambda records: records.pop(2),
        lambda records: records[2].update(id=records[1]["id"]),
        lambda records: records[0].update(source="fixed_prompt"),
        lambda records: records[0].update(source=["invalid"]),
        lambda records: records[2].update(status=["invalid"]),
    ],
)
def test_external_import_rejects_inconsistent_records(mutate):
    records = canonical_history()
    mutate(records)
    imported = PromptContext("fixed")
    with pytest.raises(ValueError):
        imported.import_history(records)
    assert imported.history == ()


def test_max_tokens_is_sent_only_when_configured():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "offline", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = DeepSeekAdapter(api_key="fake", http_client=client, max_tokens=64)
        response = adapter.generate([{"kind": "text", "role": "user", "content": "hi"}], [])
    assert bodies[0]["max_tokens"] == 64
    assert response.response_id == "offline"
    assert response.model == "deepseek-flash"
    with pytest.raises(ValueError):
        DeepSeekAdapter(api_key="fake", max_tokens=0)
