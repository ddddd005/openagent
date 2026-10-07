"""Native compaction keeps raw facts and effective context in accepted order."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.context_compaction import (
    measure_context_capacity, validate_compaction_operation,
)
from phase1_agent.context_compaction_policy import default_compaction_policy
from phase1_agent.context_compaction_policy import frame_checkpoint
from phase1_agent.contract_errors import ContractValidationError, ModelRequestError
from phase1_agent.contract_graph import validate_message_history
from phase1_agent.contracts import ModelResponse
from phase1_agent.execution_facts import ExecutionFactHistory
from phase1_agent.runtime import (
    CanonicalModelAdapter, KernelContractError, KernelPaused, RunFailed, SnapshotKernel,
)
from test_runtime_pause import ScriptedAdapter, batch, call, final, setup


def text(role, value, source):
    return {
        "schema_version": 1, "message_id": str(uuid4()), "role": role,
        "source": source, "blocks": [{"kind": "text", "text": value}],
    }


def native_setup(*, old_size=900, trigger=500, depth=0, enabled=True,
                 window=10_000, implementation=None):
    snapshot, tools = setup(implementation)
    snapshot["model_parameters"]["max_tokens"] = 16
    snapshot["s0"] = [
        text("system", "Fixed rule", {"kind": "prompt", "prompt_id": str(uuid4()), "revision": 1}),
        text("user", "a" * old_size, {"kind": "human", "visible_message_id": str(uuid4())}),
        text("assistant", "b" * old_size, {"kind": "model", "request_id": str(uuid4())}),
        text("user", "Current task", {"kind": "upstream_node", "output_id": str(uuid4())}),
    ]
    policy = {**default_compaction_policy(), "enabled": enabled,
              "trigger_tokens": trigger, "keep_depth": depth}
    snapshot["config"]["payload"]["context_compaction"] = {
        "policy": policy, "context_window_tokens": window, "output_reserve_tokens": 16,
        "layout": [
            {"kind": "fixed", "round_id": None},
            {"kind": "history", "round_id": "round-1"},
            {"kind": "history", "round_id": "round-1"},
            {"kind": "current", "round_id": None},
        ],
    }
    return snapshot, tools


def count_tokens(messages, tools):
    return sum(len(block.get("text", block.get("model_visible_text", "")))
               for message in messages for block in message["blocks"]) + len(tools) * 7


def public_fact_events(snapshot, events):
    owner = {
        "run_id": str(uuid4()), "chain_run_id": str(uuid4()),
        "workflow_session_id": snapshot["workflow_session_id"],
        "node_binding_id": snapshot["node_binding_id"], "snapshot_id": snapshot["snapshot_id"],
        "generation": str(uuid4()),
    }
    return [{
        "schema_version": 1, "fact_id": str(uuid4()), **owner, "sequence": index,
        "created_at": "2026-10-07T00:00:00.000Z", **deepcopy(event),
    } for index, event in enumerate(events, start=1)]


def test_two_compactions_distinguish_prior_and_later_append_without_rewriting_raw_facts():
    snapshot, tools = native_setup()
    original = deepcopy(snapshot)
    facts = []
    adapter = ScriptedAdapter(
        ModelResponse("stop", content="First checkpoint."),
        ModelResponse("stop", content="new information " * 100),
        ModelResponse("stop", content="Second checkpoint."),
        final("done"),
    )
    result = SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    assert (result.model_requests, result.attempts) == (2, 2)
    assert len(adapter.requests) == 4
    assert [operation["kind"] for operation in result.context_operations] == [
        "compact", "append", "append", "compact", "append", "append",
    ]
    accepted = [event["payload"]["message"] for event in facts if event["kind"] == "message_accepted"]
    assert accepted == result.messages
    assert snapshot == original
    compact = [operation for operation in result.context_operations if operation["kind"] == "compact"]
    assert compact[0]["checkpoint"]["message_id"] in compact[1]["covered_message_ids"]
    assert result.messages[0]["message_id"] in compact[1]["covered_message_ids"]
    assert result.messages[1]["message_id"] not in compact[1]["covered_message_ids"]
    assert result.messages[0] not in result.effective_messages
    assert result.effective_messages[-2:] == result.messages[-2:]
    assert adapter.requests[0][:-1] == snapshot["s0"]
    assert adapter.requests[2][:-1] == adapter.requests[1] + result.messages[:2]
    assert all(operation["result"]["tool_calls"] == [] for operation in compact)
    history = ExecutionFactHistory(snapshot, token_counter=count_tokens)
    for event in public_fact_events(snapshot, facts):
        history.append(event)
    assert history.effective_messages == result.effective_messages


def test_wire_prefix_and_tool_definitions_are_kept_for_the_maintenance_request():
    snapshot, tools = native_setup(old_size=20, trigger=300)

    class Capture:
        def __init__(self):
            self.messages, self.tools = [], []
            self.responses = [
                ModelResponse("stop", content="accepted text " * 60),
                ModelResponse("stop", content="checkpoint"),
                final("done"),
            ]

        def generate(self, messages, definitions):
            self.messages.append(deepcopy(messages))
            self.tools.append(deepcopy(definitions))
            return self.responses.pop(0)

    transport = Capture()
    result = SnapshotKernel().run(snapshot, tools, CanonicalModelAdapter(transport), token_counter=count_tokens)
    assert len(transport.messages) == 3
    assert transport.messages[1][:-3] == transport.messages[0]
    assert transport.tools[0] == transport.tools[1] == transport.tools[2]
    assert result.context_operations[2]["kind"] == "compact"
    assert transport.messages[1][-1]["role"] == "user"


def test_pending_batch_finishes_before_summary_even_across_pause():
    effects, facts = [], []
    snapshot, tools = native_setup(
        old_size=100, trigger=500,
        implementation=lambda text: effects.append(text) or {"text": text * 100},
    )
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"one"}', "p1"),
              call("inspect_text", '{"text":"two"}', "p2")),
        ModelResponse("stop", content="short checkpoint"),
        final("done"),
    )
    checks = 0

    def boundary(kind):
        nonlocal checks
        if kind == "before_tool":
            checks += 1
            return checks == 1
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens,
                             on_boundary=boundary, on_fact=facts.append)
    checkpoint = caught.value.checkpoint
    assert effects == ["one"]
    assert len(checkpoint.pending_tools) == 1
    assert not any(event["kind"].startswith("context_compaction") for event in facts)
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint,
                                  token_counter=count_tokens, on_fact=facts.append)
    assert effects == ["one", "two"]
    assert [message["role"] for message in adapter.requests[1][-3:-1]] == ["tool", "tool"]
    compact = next(operation for operation in result.context_operations if operation["kind"] == "compact")
    assert all(message["role"] != "tool" for message in compact["request"]["messages"]
               if message["message_id"] in compact["covered_message_ids"])
    validate_message_history(result.effective_messages)


@pytest.mark.parametrize("policy", [None, {**default_compaction_policy(), "enabled": False}])
def test_without_enabled_policy_stops_after_batch_closes_not_during_tool_dispatch(policy):
    effects = []
    snapshot, tools = native_setup(
        old_size=10, trigger=0, window=300,
        implementation=lambda text: effects.append(text) or {"text": text * 100},
    )
    snapshot["config"]["payload"]["context_compaction"]["policy"] = policy
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"one"}', "p1"),
        call("inspect_text", '{"text":"two"}', "p2"),
    ))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens)
    assert caught.value.code == "context_capacity_exceeded"
    assert effects == ["one", "two"]
    assert len(adapter.requests) == 1
    validate_message_history(snapshot["s0"] + caught.value.messages)


def test_safety_watermark_triggers_even_with_later_configured_threshold():
    snapshot, tools = native_setup(old_size=2230, trigger=100_000, window=5000)
    snapshot["config"]["payload"]["context_compaction"]["policy"]["summary_prompt"] = "Summarize."
    facts = []
    result = SnapshotKernel().run(
        snapshot, tools, ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("done")),
        token_counter=count_tokens, on_fact=facts.append,
    )
    assert result.context_operations[0]["kind"] == "compact"
    started = next(event["payload"] for event in facts if event["kind"] == "context_compaction_started")
    assert started["capacity"]["input_tokens"] < 100_000
    assert started["capacity"]["total_tokens"] >= 4500


def test_pause_after_summary_reuses_accepted_view_without_another_auxiliary_call():
    snapshot, tools = native_setup()
    adapter = ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("done"))
    facts = []
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens,
                             on_fact=facts.append, on_boundary=lambda kind: kind != "after_compaction")
    checkpoint = caught.value.checkpoint
    assert checkpoint.messages == ()
    assert len(checkpoint.context_operations) == 1
    assert checkpoint.effective_messages[1]["source"]["kind"] == "context_checkpoint"
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint,
                                  token_counter=count_tokens, on_fact=facts.append)
    assert len(adapter.requests) == 2
    assert result.context_operations[0] == checkpoint.context_operations[0]
    assert sum(event["kind"] == "context_compaction_started" for event in facts) == 1


@pytest.mark.parametrize("field", ["effective_messages", "context_operations"])
def test_checkpoint_hmac_covers_new_work_view_and_operation_history(field):
    snapshot, tools = native_setup()
    adapter = ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("must not dispatch"))
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens,
                             on_boundary=lambda kind: kind != "after_compaction")
    checkpoint = caught.value.checkpoint
    changed = replace(checkpoint, **{field: ()})
    with pytest.raises(KernelContractError) as failure:
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=changed, token_counter=count_tokens)
    assert failure.value.code == "configuration_error"
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("response", [
    ModelResponse("stop", content=" "),
    ModelResponse("tool_calls", tool_calls=(call("inspect_text", '{"text":"forbidden"}', "summary-tool"),)),
    TimeoutError("private transport failure"),
    ModelRequestError("model_dispatch_unknown"),
])
def test_invalid_summary_or_transport_failure_does_not_replace_or_dispatch_tools(response):
    effects, facts = [], []
    snapshot, tools = native_setup(implementation=lambda text: effects.append(text))
    adapter = ScriptedAdapter(response, final("must not dispatch"))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    assert len(adapter.requests) == 1
    assert effects == []
    assert not any(event["kind"] in ("context_compaction_applied", "message_accepted") for event in facts)
    assert "private transport failure" not in repr(facts)
    if isinstance(response, ModelRequestError):
        assert caught.value.code == "model_dispatch_unknown"


def test_summary_must_shrink_and_protected_recent_rounds_are_not_silently_dropped():
    snapshot, tools = native_setup(depth=1)
    adapter = ScriptedAdapter(ModelResponse("stop", content="must not dispatch"))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens)
    assert caught.value.code == "context_compaction_no_eligible_text"
    assert adapter.requests == []
    snapshot, tools = native_setup()
    facts = []
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, ScriptedAdapter(ModelResponse("stop", content="x" * 5000)),
                             token_counter=count_tokens, on_fact=facts.append)
    assert caught.value.code == "context_compaction_insufficient"
    assert not any(event["kind"] == "context_compaction_applied" for event in facts)


def test_maintenance_headroom_is_checked_before_auxiliary_dispatch():
    snapshot, tools = native_setup(old_size=450, trigger=0, window=1000)
    adapter = ScriptedAdapter(ModelResponse("stop", content="must not dispatch"))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens)
    assert caught.value.code == "context_compaction_request_capacity_exceeded"
    assert adapter.requests == []


def test_default_byte_measurement_is_labeled_and_includes_tools_and_reserve():
    snapshot, tools = native_setup()
    descriptor = snapshot["config"]["payload"]["context_compaction"]
    plain = measure_context_capacity(snapshot["s0"], [], descriptor)
    with_tools = measure_context_capacity(snapshot["s0"], [deepcopy(tool.definition) for tool in tools], descriptor)
    assert with_tools["token_count_kind"] == "utf8_bytes_estimate"
    assert with_tools["input_tokens"] > plain["input_tokens"]
    assert with_tools["total_tokens"] == with_tools["input_tokens"] + 16


def test_declared_output_limit_cannot_exceed_reserved_capacity():
    snapshot, tools = native_setup()
    snapshot["model_parameters"]["max_tokens"] = 100
    adapter = ScriptedAdapter(final("must not dispatch"))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens)
    assert caught.value.code == "configuration_error"
    assert adapter.requests == []


def test_final_answer_closure_cannot_bypass_disabled_policy_watermark():
    snapshot, tools = native_setup(old_size=10, window=300, enabled=False)
    facts = []
    adapter = ScriptedAdapter(final("answer " * 100))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    assert caught.value.code == "context_capacity_exceeded"
    assert caught.value.messages[-1]["role"] == "tool"
    assert caught.value.messages[-1]["blocks"][0]["status"] == "success"
    assert len(adapter.requests) == 1
    validate_message_history(snapshot["s0"] + caught.value.messages)


@pytest.mark.parametrize("kind", [
    "context_compaction_started", "context_compaction_finished", "context_compaction_applied",
])
def test_maintenance_fact_failure_does_not_drive_later_work(kind):
    snapshot, tools = native_setup()
    adapter = ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("must not dispatch"))
    facts = []

    def persist(event):
        facts.append(deepcopy(event))
        if event["kind"] == kind:
            raise OSError("private store failure")

    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=persist)
    assert caught.value.code == "fact_persistence_error"
    assert len(adapter.requests) == (0 if kind == "context_compaction_started" else 1)
    assert facts[-1]["kind"] == kind
    assert caught.value.messages == []


def test_applied_fact_requires_started_result_and_rejects_fixed_or_current_coverage():
    snapshot, tools = native_setup()
    facts = []
    SnapshotKernel().run(snapshot, tools,
                         ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("done")),
                         token_counter=count_tokens, on_fact=facts.append)
    canonical = public_fact_events(snapshot, facts)
    history = ExecutionFactHistory(snapshot, token_counter=count_tokens)
    applied = next(fact for fact in canonical if fact["kind"] == "context_compaction_applied")
    altered = deepcopy(applied)
    altered["sequence"] = 1
    with pytest.raises(ContractValidationError):
        history.append(altered)
    started = deepcopy(canonical[0])
    started["payload"]["covered_message_ids"].insert(0, snapshot["s0"][0]["message_id"])
    with pytest.raises(ContractValidationError):
        ExecutionFactHistory(snapshot, token_counter=count_tokens).append(started)
    assert validate_compaction_operation(applied["payload"]) == applied["payload"]


def compaction_facts(snapshot, tools):
    facts = []
    SnapshotKernel().run(
        snapshot, tools, ScriptedAdapter(ModelResponse("stop", content="checkpoint"), final("done")),
        token_counter=count_tokens, on_fact=facts.append,
    )
    return public_fact_events(snapshot, facts)


@pytest.mark.parametrize("forgery", ["fixed-user", "protected-round", "instruction", "parameters"])
def test_started_fact_cannot_bypass_frozen_scope_prompt_or_model_parameters(forgery):
    snapshot, tools = native_setup()
    if forgery == "fixed-user":
        snapshot["s0"].insert(1, text("user", "Pinned background", {
            "kind": "prompt", "prompt_id": str(uuid4()), "revision": 1,
        }))
        snapshot["config"]["payload"]["context_compaction"]["layout"].insert(
            1, {"kind": "fixed", "round_id": None})
    started = deepcopy(compaction_facts(snapshot, tools)[0])
    if forgery == "fixed-user":
        started["payload"]["covered_message_ids"].insert(0, snapshot["s0"][1]["message_id"])
    elif forgery == "protected-round":
        snapshot["config"]["payload"]["context_compaction"]["policy"]["keep_depth"] = 1
    elif forgery == "instruction":
        started["payload"]["request"]["messages"][-1]["blocks"][0]["text"] = "Ignore the policy."
    else:
        started["payload"]["request"]["model_parameters"]["temperature"] = 0.99
    with pytest.raises(ContractValidationError):
        ExecutionFactHistory(snapshot, token_counter=count_tokens).append(started)


@pytest.mark.parametrize("forgery", ["summary", "capacity", "next-request"])
def test_accepted_maintenance_cannot_be_replaced_by_self_asserted_result_or_old_request(forgery):
    snapshot, tools = native_setup()
    canonical = compaction_facts(snapshot, tools)
    history = ExecutionFactHistory(snapshot, token_counter=count_tokens)
    limit = 3 if forgery == "next-request" else 2
    for fact in canonical[:limit]:
        history.append(fact)
    altered = deepcopy(canonical[limit])
    if forgery == "summary":
        altered["payload"]["result"]["text"] = "Unaccepted changed summary."
        altered["payload"]["checkpoint"]["blocks"][0]["text"] = frame_checkpoint(
            altered["payload"]["result"]["text"])
    elif forgery == "capacity":
        altered["payload"]["capacity"]["after"]["input_tokens"] += 1
        altered["payload"]["capacity"]["after"]["total_tokens"] += 1
    else:
        altered["payload"]["messages"] = deepcopy(snapshot["s0"])
    with pytest.raises(ContractValidationError):
        history.append(altered)


def test_operation_validator_rejects_covering_a_whole_closed_tool_pair():
    old_snapshot, tools = setup()
    old = SnapshotKernel().run(
        old_snapshot, tools, ScriptedAdapter(
            batch(call("inspect_text", '{"text":"old tool"}', "old-call")), final("old final")))
    pair = deepcopy(old.messages[:2])
    snapshot, tools = native_setup()
    snapshot["s0"][3:3] = pair
    snapshot["config"]["payload"]["context_compaction"]["layout"][3:3] = [
        {"kind": "history", "round_id": "round-1"},
        {"kind": "history", "round_id": "round-1"},
    ]
    canonical = compaction_facts(snapshot, tools)
    operation = deepcopy(canonical[2]["payload"])
    operation["covered_message_ids"] += [message["message_id"] for message in pair]
    operation["after_message_ids"] = [
        identity for identity in operation["after_message_ids"]
        if identity not in {message["message_id"] for message in pair}
    ]
    with pytest.raises(ContractValidationError, match="protocol feedback or tool messages"):
        validate_compaction_operation(operation)


@pytest.mark.parametrize("response", [
    ModelResponse("stop", content="checkpoint", tool_calls=None),
    ModelResponse("stop", content="checkpoint", tool_calls=(object(),)),
    ModelResponse("stop", content=object()),
    ModelResponse("stop", content="checkpoint", usage={"invalid": object()}),
    ModelResponse("stop", content="checkpoint", usage={"invalid": float("nan")}),
    ModelResponse(None, content="checkpoint"),
    ModelResponse("stop", content="checkpoint", response_id=object()),
])
def test_unprojectable_auxiliary_response_records_legal_failure_without_response_evidence(response):
    snapshot, tools = native_setup()
    facts = []
    adapter = ScriptedAdapter(response, final("must not dispatch"))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    assert caught.value.code == "context_compaction_summary_invalid"
    assert len(adapter.requests) == 1
    assert caught.value.messages == []
    finished = next(event["payload"] for event in facts if event["kind"] == "context_compaction_finished")
    assert finished["outcome"] == "protocol_error" and finished["result"] is None
    assert not any(event["kind"] == "context_compaction_applied" for event in facts)
    history = ExecutionFactHistory(snapshot, token_counter=count_tokens)
    for fact in public_fact_events(snapshot, facts):
        history.append(fact)


def test_pause_above_early_trigger_reuses_compact_view_before_business_dispatch():
    snapshot, tools = native_setup()
    adapter = ScriptedAdapter(ModelResponse("stop", content="shorter checkpoint " * 40), final("done"))
    facts = []
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append,
                             on_boundary=lambda kind: kind != "after_compaction")
    checkpoint = caught.value.checkpoint
    accepted = checkpoint.context_operations[-1]
    assert accepted["kind"] == "compact"
    assert accepted["capacity"]["after"]["input_tokens"] >= 500
    assert accepted["capacity"]["after"]["total_tokens"] < 9000
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint,
                                  token_counter=count_tokens, on_fact=facts.append)
    assert result.final["value"] == {"text": "done"}
    assert len(adapter.requests) == 2
    assert adapter.requests[1] == list(checkpoint.effective_messages)
    assert sum(event["kind"] == "context_compaction_started" for event in facts) == 1
    assert [operation["kind"] for operation in result.context_operations] == ["compact", "append", "append"]


def test_auxiliary_call_cannot_bypass_retired_prepared_snapshot_guard():
    snapshot, tools = native_setup()
    snapshot["config"]["payload"]["context_preparation"] = {"retired": True}
    facts = []
    adapter = ScriptedAdapter(ModelResponse("stop", content="must not dispatch"))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    assert caught.value.code == "message_contract_error"
    assert adapter.requests == []
    assert not any(event["kind"] == "context_compaction_started" for event in facts)


@pytest.mark.parametrize("budget", ["context_window_tokens", "max_cold_input_tokens"])
def test_maintenance_fact_rechecks_auxiliary_budgets_instead_of_trusting_dispatch(budget):
    snapshot, tools = native_setup()
    started = deepcopy(compaction_facts(snapshot, tools)[0])
    settings = snapshot["config"]["payload"]["context_compaction"]
    if budget == "context_window_tokens":
        settings[budget] = started["payload"]["capacity"]["total_tokens"] + 1
        started["payload"]["capacity"][budget] = settings[budget]
    else:
        settings[budget] = started["payload"]["capacity"]["input_tokens"]
    with pytest.raises(ContractValidationError, match="Compaction request exceeds"):
        ExecutionFactHistory(snapshot, token_counter=count_tokens).append(started)
