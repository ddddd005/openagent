"""Real service history remains canonical across prepared/legacy projections."""

from __future__ import annotations

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.bindings import WorkflowComponents
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle, validate_message_history
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.prepared_context import PreparedPromptContext, validate_frozen_preparation
from phase1_agent.prompt_preparation import make_prompt_context_config
from phase1_agent.prompt_values import context_view_messages
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService

from test_workflow_prepared_context import (
    PROFILE, bundle, collection, components, factory, run_turn, uid,
)


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="prepared-history-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder)


def context_regex(*, sources=("human", "upstream_node", "model")):
    return {
        "schema_version": 1, "kind": "context_regex", "node_id": uid(4000),
        "enabled": True, "purposes": ["send"], "sources": list(sources),
        "rule": {"pattern": "city", "replacement": "town", "flags": "", "mode": "all"},
    }


def declare_default_bootstrap(config):
    declared = components(config)
    return declared, WorkflowComponents(declared.registry, workflow_definition_id=PROFILE)


def select_prepared_for_next_call(service, declared):
    """Replace resolved public Context while preserving the fixed definition."""
    for stage, selection in declared.contexts.items():
        prepared = service._registry.resolve(
            "context", selection.component_id, selection.version, config=selection.config,
            required_capabilities=(
                "sources", "paired_history", "frozen_projection",
                *selection.required_capabilities,
            ),
        )
        prepared.require_api()
        _old_context, kernel, adapter, config = service._resolved[stage]
        config = copy.deepcopy(config)
        config["payload"]["context"] = copy.deepcopy(prepared.config["payload"])
        config["payload"]["resolved"]["context"] = copy.deepcopy(prepared.descriptor)
        service._resolved[stage] = prepared, kernel, adapter, config


def by_id(values, key):
    return {value[key]: value for value in values}


def binding_turns(saved, binding):
    runs = by_id(saved["run_record"], "run_id")
    return [turn for turn in saved["turn"] if runs[turn["run_id"]]["node_binding_id"] == binding]


def snapshot_for_turn(saved, turn):
    return by_id(saved["input_snapshot"], "snapshot_id")[turn["snapshot_id"]]


class ToolHistoryModel:
    """Create plain, feedback, mixed-tool and mixed-final facts via the real kernel."""

    def __init__(self, stage, calls):
        self.stage, self.calls = stage, calls
        self.request_index = 0

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
        self.request_index += 1
        if self.request_index == 1:
            return ModelResponse("stop", content="city ordinary assistant text")
        if self.request_index == 2:
            return ModelResponse("tool_calls", content="city mixed tool text", tool_calls=(
                ModelToolCall(
                    str(uuid4()), "inspect_text", dumps_pretty({"text": "city tool argument"}),
                ),
            ))
        root = next(
            message for message in reversed(messages)
            if message["source"]["kind"] in ("human", "upstream_node")
        )
        text = loads_strict(root["blocks"][0]["text"])["text"]
        return ModelResponse("tool_calls", content="city final commentary", tool_calls=(
            ModelToolCall(
                str(uuid4()), "final_answer",
                dumps_pretty({"answer": {"text": text + self.stage}}),
            ),
        ))


def history_factory(calls):
    return lambda stage: ToolHistoryModel(stage, calls)


def test_basic_prepared_basic_reopen_keeps_exact_canonical_roots_and_history(tmp_path):
    calls, path = [], tmp_path / "mixed.sqlite"
    config = make_prompt_context_config(
        collection("prepared-only", placement="middle", depth=1),
        context_regex=[context_regex()],
    )
    _declared, bootstrap = declare_default_bootstrap(config)
    with closing(WorkflowService(path, components=bootstrap, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "legacy city", "legacy")
        legacy = validate_bundle(bundle(path))
        assert all(row["projection_version"] == 1 for row in legacy["input_snapshot"])

    declared, bootstrap = declare_default_bootstrap(config)
    with closing(WorkflowService(path, components=bootstrap, model_factory=factory(calls))) as service:
        select_prepared_for_next_call(service, declared)
        run_turn(service, sid, "prepared city", "prepared")
        prepared = validate_bundle(bundle(path))
        for binding in (A_BINDING, B_BINDING):
            prior, current = binding_turns(prepared, binding)
            old_snapshot = snapshot_for_turn(prepared, prior)
            new_snapshot = snapshot_for_turn(prepared, current)
            evidence = validate_frozen_preparation(new_snapshot)
            assert new_snapshot["projection_version"] == 2
            assert evidence["canonical_messages"][1:-1] == prior["messages"]
            assert evidence["canonical_messages"][0] in old_snapshot["s0"]
            assert evidence["logical_floors"] == [
                [evidence["canonical_messages"][0]["message_id"]],
                [value["message_id"] for value in prior["messages"]],
                [evidence["root_locator"]["message_id"]],
            ]
            assert all(value["source"]["kind"] != "prompt" for value in evidence["canonical_messages"])
            if binding == A_BINDING:
                assert "legacy city" in evidence["canonical_messages"][0]["blocks"][0]["text"]
                assert "legacy town" in context_view_messages(evidence["send_view"])[0]["blocks"][0]["text"]

    _declared, bootstrap = declare_default_bootstrap(config)
    with closing(WorkflowService(path, components=bootstrap, model_factory=factory(calls))) as service:
        run_turn(service, sid, "back to legacy city", "legacy-again")
        final = validate_bundle(bundle(path))
        for binding in (A_BINDING, B_BINDING):
            first, second, latest = binding_turns(final, binding)
            second_snapshot = snapshot_for_turn(final, second)
            latest_snapshot = snapshot_for_turn(final, latest)
            second_evidence = validate_frozen_preparation(second_snapshot)
            latest_history = [
                message for message in latest_snapshot["s0"][:-1]
                if message["source"]["kind"] != "prompt"
            ]
            expected = [
                second_evidence["canonical_messages"][0], *first["messages"],
                second_evidence["canonical_messages"][-1], *second["messages"],
            ]
            assert latest_snapshot["projection_version"] == 1
            assert latest_history == expected
            assert validate_frozen_preparation(latest_snapshot) is None
            assert all(
                block.get("text") != "prepared-only"
                for message in latest_snapshot["s0"] for block in message["blocks"]
            )
            if binding == A_BINDING:
                assert "prepared city" in expected[-len(second["messages"]) - 1]["blocks"][0]["text"]
        assert service.get_session(sid)["can_submit"]
        assert len(service.get_session(sid)["messages"]) == 6

    assert all(by_id(final["turn"], "turn_id")[row["turn_id"]] == row for row in legacy["turn"])
    assert all(
        by_id(final["input_snapshot"], "snapshot_id")[row["snapshot_id"]] == row
        for row in prepared["input_snapshot"]
    )


def test_selected_reroll_history_keeps_complete_protocol_floor_without_unselected_sibling(tmp_path):
    calls, path = [], tmp_path / "selected.sqlite"
    config = make_prompt_context_config(
        collection("between floors", placement="middle", depth=2),
        context_regex=[context_regex()],
    )
    with closing(WorkflowService(
        path, components=components(config), model_factory=history_factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first city", "original")
        original = validate_bundle(bundle(path))
        source = original["chain_run"][0]
        session = original["workflow_session"][0]
        head = original["workflow_ref"][0]
        service.reroll(
            sid, source["chain_run_id"], idempotency_key="reroll",
            expected_session_revision=session["revision"], expected_ref_revision=head["revision"],
            expected_head_commit_id=head["head_commit_id"],
        )
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        rolled = validate_bundle(bundle(path))
        assert len(rolled["workflow_candidate"]) == 2
        run_turn(service, sid, "next city", "next")
        saved = validate_bundle(bundle(path))

    for binding in (A_BINDING, B_BINDING):
        previous, selected, latest = binding_turns(saved, binding)
        assert selected["parent_turn_id"] is None
        assert latest["parent_turn_id"] == selected["turn_id"]
        latest_snapshot = snapshot_for_turn(saved, latest)
        evidence = validate_frozen_preparation(latest_snapshot)
        assert evidence["canonical_messages"][1:-1] == selected["messages"]
        assert evidence["logical_floors"] == [
            [evidence["canonical_messages"][0]["message_id"]],
            [message["message_id"] for message in selected["messages"]],
            [evidence["root_locator"]["message_id"]],
        ]
        unselected_ids = {message["message_id"] for message in previous["messages"]}
        assert not unselected_ids.intersection(message["message_id"] for message in latest_snapshot["s0"])
        send_by_id = by_id(latest_snapshot["s0"], "message_id")
        for message in selected["messages"]:
            if any(block["kind"] != "text" for block in message["blocks"]):
                assert send_by_id[message["message_id"]] == message
        plain = selected["messages"][0]
        assert plain["blocks"][0]["text"] == "city ordinary assistant text"
        assert send_by_id[plain["message_id"]]["blocks"][0]["text"] == "town ordinary assistant text"
        prompt_entry = evidence["assembly"]["manifest"]["entries"][0]
        assert prompt_entry["floor_boundary"] == 1
        assert prompt_entry["message_index"] == 1
        validate_message_history(latest_snapshot["s0"])

    assert all(by_id(saved["turn"], "turn_id")[row["turn_id"]] == row for row in original["turn"])


def test_real_service_automatically_protects_saved_final_text_locators(tmp_path):
    class RecordingContext(PreparedPromptContext):
        def __init__(self):
            self.protected = []

        def prepare_context(self, *args, **kwargs):
            self.protected.append(copy.deepcopy(kwargs["protected_blocks"]))
            return super().prepare_context(*args, **kwargs)

    context, calls, path = RecordingContext(), [], tmp_path / "final.sqlite"
    config = make_prompt_context_config(collection(), context_regex=[context_regex(sources=("model",))])
    with closing(WorkflowService(
        path, components=components(config, context=context), model_factory=history_factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first city", "first")
        first = validate_bundle(bundle(path))
        run_turn(service, sid, "next city", "next")
        saved = validate_bundle(bundle(path))

    assert context.protected[:2] == [[], []]
    for binding, captured in zip((A_BINDING, B_BINDING), context.protected[2:]):
        prior, current = binding_turns(saved, binding)
        final_message = next(
            message for message in prior["messages"] if message["message_id"] == prior["final"]["message_id"]
        )
        assert final_message["blocks"][0] == {"kind": "text", "text": "city final commentary"}
        locator = {"message_id": final_message["message_id"], "block_index": 0}
        assert captured == [locator]
        evidence = validate_frozen_preparation(snapshot_for_turn(saved, current))
        assert evidence["protected_blocks"] == [locator]
        assert locator not in [
            {"message_id": override["message_id"], "block_index": override["block_index"]}
            for override in evidence["send_view"]["overrides"]
        ]
        send_by_id = by_id(evidence["s0"], "message_id")
        assert send_by_id[final_message["message_id"]] == final_message
        assert prior["final"] == binding_turns(first, binding)[0]["final"]
        assert context_view_messages(evidence["send_view"])[1]["blocks"][0]["text"] == (
            "town ordinary assistant text"
        )


def test_component_cannot_drop_automatic_final_protection_before_second_dispatch(tmp_path):
    class DropProtection(PreparedPromptContext):
        def prepare_context(self, *args, **kwargs):
            kwargs["protected_blocks"] = []
            return super().prepare_context(*args, **kwargs)

    calls, path = [], tmp_path / "drop.sqlite"
    config = make_prompt_context_config(collection(), context_regex=[context_regex(sources=("model",))])
    with closing(WorkflowService(
        path, components=components(config, context=DropProtection()),
        model_factory=history_factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first city", "first")
        before, call_count = bundle(path), len(calls)
        with pytest.raises(ContractValidationError, match="protected final locators"):
            service.submit(sid, "second city", "second")
        assert len(calls) == call_count
        assert bundle(path) == before
        assert service.get_session(sid)["can_submit"]
