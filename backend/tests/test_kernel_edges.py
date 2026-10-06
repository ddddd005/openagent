import json
from threading import Event

from jsonschema import Draft202012Validator

from phase1_agent.contracts import ModelResponse, ModelToolCall, RunInput, RunLimits
from phase1_agent.kernel import AgentKernel


SPEC = {"type": "object", "required": ["content"], "properties": {"content": {"type": "string", "minLength": 1}}}


class StubTool:
    def __init__(self, name, schema, *, fail=False, on_call=None):
        self.name = name
        self.schema = schema
        self.definition = {"type": "function", "function": {"name": name, "parameters": schema}}
        self.calls = []
        self.fail = fail
        self.on_call = on_call
        self.validator = Draft202012Validator(schema)

    def validate(self, arguments):
        self.validator.validate(arguments)

    def execute(self, arguments):
        self.calls.append(arguments)
        if self.on_call:
            self.on_call()
        if self.fail:
            raise RuntimeError("private exception body")
        return "done"


class ScriptedAdapter:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append((messages, tools))
        return next(self.responses)


def call(id, name, raw):
    return ModelToolCall(id=id, name=name, raw_arguments=raw)


def run(adapter, *tools, max_requests=3, cancellation=None):
    return AgentKernel().run(
        RunInput(
            run_id="r1",
            s0=(
                {"kind": "text", "id": "p", "role": "system", "source": "fixed_prompt", "content": "fixed"},
                {"kind": "text", "id": "u", "role": "user", "source": "real_user", "content": "work"},
            ),
            tools=tools,
            adapter=adapter,
            output_spec=SPEC,
            limits=RunLimits(max_model_requests=max_requests),
            correction_message="call final_answer with a valid answer",
            cancellation=cancellation,
        )
    )


def fixtures():
    scalar = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    final = {
        "type": "object",
        "properties": {"answer": {"type": "object"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    return StubTool("write", scalar), StubTool("final_answer", final)


def test_preflight_rejects_entire_batch_before_side_effects():
    write, final = fixtures()
    adapter = ScriptedAdapter(
        ModelResponse("tool_calls", tool_calls=(call("z", "write", '{"text":"ok"}'), call("a", "write", '{"wrong":1}')))
    )
    result = run(adapter, write, final)
    assert result.status == "protocol_error"
    assert result.history_delta == ()
    assert write.calls == []
    assert len(adapter.requests) == 1


def test_raw_arguments_preserved_and_partial_failure_continues():
    write, final = fixtures()
    write.fail = True
    raw = '{ "text" : "你好" }'
    adapter = ScriptedAdapter(
        ModelResponse("tool_calls", tool_calls=(call("z", "write", raw),)),
        ModelResponse("tool_calls", tool_calls=(call("f", "final_answer", '{"answer":{"content":"稿"}}'),)),
    )
    result = run(adapter, write, final)
    assert result.status == "success"
    assert json.loads(result.output) == {"content": "稿"}
    assert write.calls == [{"text": "你好"}]
    assert result.history_delta[0]["calls"][0]["raw_arguments"] == raw
    assert result.history_delta[1]["status"] == "error"
    assert "private exception" not in result.history_delta[1]["content"]
    assert adapter.requests[0][0] == adapter.requests[1][0][:2]


def test_text_and_invalid_business_final_share_one_correction_budget():
    write, final = fixtures()
    adapter = ScriptedAdapter(
        ModelResponse("stop", content="plain text"),
        ModelResponse("tool_calls", tool_calls=(call("f", "final_answer", '{"answer":{"content":""}}'),)),
    )
    result = run(adapter, write, final)
    assert result.status == "protocol_error"
    assert [message["source"] for message in result.history_delta] == [
        "assistant_turn", "protocol_feedback", "assistant_turn", "final_control"
    ]
    assert len(adapter.requests) == 2


def test_exhaustion_does_not_send_hidden_final_request():
    write, final = fixtures()
    adapter = ScriptedAdapter(ModelResponse("tool_calls", tool_calls=(call("z", "write", '{"text":"x"}'),)))
    result = run(adapter, write, final, max_requests=1)
    assert result.status == "steps_exhausted"
    assert len(adapter.requests) == 1
    assert write.calls == [{"text": "x"}]


def test_cancel_marks_unstarted_calls_without_suspending_pairing():
    signal = Event()
    write, final = fixtures()
    write.on_call = signal.set
    adapter = ScriptedAdapter(
        ModelResponse("tool_calls", tool_calls=(call("z", "write", '{"text":"1"}'), call("a", "write", '{"text":"2"}')))
    )
    result = run(adapter, write, final, cancellation=signal)
    assert result.status == "cancelled"
    assert write.calls == [{"text": "1"}]
    assert [message["status"] for message in result.history_delta[1:]] == ["success", "not_executed"]


def test_invalid_finish_and_mixed_final_do_not_execute_tools():
    write, final = fixtures()
    for response, status in (
        (ModelResponse("length", tool_calls=(call("z", "write", '{"text":"x"}'),)), "protocol_error"),
        (
            ModelResponse(
                "tool_calls",
                tool_calls=(call("z", "write", '{"text":"x"}'), call("f", "final_answer", '{"answer":{}}')),
            ),
            "protocol_error",
        ),
    ):
        result = run(ScriptedAdapter(response), write, final)
        assert result.status == status
        assert result.history_delta == ()
        assert write.calls == []
