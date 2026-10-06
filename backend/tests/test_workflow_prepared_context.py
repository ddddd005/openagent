"""Prepared Context uses public ports and freezes detached preparation evidence."""

import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.bindings import ComponentRegistry, ComponentSelection, WorkflowComponents
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.prepared_context import (
    PREPARATION_CAPABILITY, PREPARED_CONTEXT_CAPABILITIES, PREPARED_CONTEXT_COMPONENT,
    PREPARED_CONTEXT_CONFIG_SCHEMA, PREPARED_CONTEXT_VERSION, PreparedPromptContext,
    PreparedRequestAdapter, check_prepared_request_capacity, register_prepared_context,
    validate_frozen_preparation,
)
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_preparation import make_prompt_context_config
from phase1_agent.prompt_values import collection_from_config
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


PROFILE = uid(1000)


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="prepared-context-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder)


def collection(text="Instructions", *, role="system", placement="before", depth=None):
    definition = {
        "schema_version": 1, "kind": "item", "item_id": uid(1001), "revision": 1,
        "name": "A title not to send", "text": text, "role": role, "enabled": True,
        "placement": placement, "depth": depth, "order": 0, "interpolation": "variables",
        "source": {"kind": "configuration"},
    }
    configuration = {
        "schema_version": 1, "kind": "config", "config_id": uid(1002), "revision": 1,
        "name": "Prepared", "inputs": [{
            "name": "instructions", "kind": "item", "item_instance_id": uid(1003),
            "item_id": uid(1001), "revision": 1, "overrides": {},
        }],
    }
    return collection_from_config(configuration, lambda *_: definition, lambda *_: None)


def macro():
    return {
        "schema_version": 1, "node_id": uid(1010), "kind": "macro",
        "enabled": True, "select": {"mode": "all"},
    }


def regex_config(*, purposes=("send",), sources=("human", "upstream_node"), pattern="city", replacement="town"):
    return {
        "schema_version": 1, "kind": "context_regex", "node_id": str(uuid4()),
        "enabled": True, "purposes": list(purposes), "sources": list(sources),
        "rule": {
            "pattern": pattern, "replacement": replacement, "flags": "", "mode": "all",
        },
    }


def components(config, *, context=None):
    registry = ComponentRegistry()
    if context is None:
        register_prepared_context(registry)
    else:
        registry.register(
            "context", PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
            context, capabilities=PREPARED_CONTEXT_CAPABILITIES,
            config_schema=copy.deepcopy(PREPARED_CONTEXT_CONFIG_SCHEMA),
        )
    selection = ComponentSelection(
        PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
        {"owner_component_id": PREPARED_CONTEXT_COMPONENT, "schema_version": 1, "payload": config},
        required_capabilities=(PREPARATION_CAPABILITY,),
    )
    return WorkflowComponents(
        registry, contexts={"A": selection, "B": selection}, workflow_definition_id=PROFILE,
    )


class FinalModel:
    def __init__(self, stage, calls, *, entered=None, release=None):
        self.stage, self.calls = stage, calls
        self.entered, self.release = entered, release

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(10)
        root = next(
            message for message in reversed(messages)
            if message["source"]["kind"] in ("human", "upstream_node")
        )
        text = loads_strict(root["blocks"][0]["text"])["text"]
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": text + self.stage}})),
        ))


def factory(calls, *, entered=None, release=None):
    return lambda stage: FinalModel(
        stage, calls, entered=entered if stage == "A" else None,
        release=release if stage == "A" else None,
    )


def bundle(path):
    with closing(SqliteStore(path)) as store:
        return store.read_bundle()


def snapshots(path):
    return bundle(path).get("input_snapshot", [])


def run_turn(service, sid, text="city", key="submit"):
    service.submit(sid, text, key)
    service.wait_for_idle(sid)
    assert service.get_session(sid)["error"] is None


def test_public_preparation_runs_through_service_and_freezes_complete_evidence(tmp_path):
    calls = []
    config = make_prompt_context_config(
        collection("after-root", role="assistant", placement="middle", depth=0),
    )
    path = tmp_path / "prepared.sqlite"
    with closing(WorkflowService(path, components=components(config), model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        for frozen in snapshots(path):
            evidence = validate_frozen_preparation(frozen)
            assert frozen["projection_version"] == 2
            assert evidence["s0"] == frozen["s0"]
            assert evidence["collection"]["items"][0]["text"] == "after-root"
            assert frozen["s0"][-1]["role"] == "assistant"
            assert frozen["s0"][-1]["source"]["kind"] == "prompt"
            assert all("A title not to send" not in canonical_bytes(message).decode() for message in frozen["s0"])
            stage = "A" if frozen["node_binding_id"] == A_BINDING else "B"
            assert next(row[1] for row in calls if row[0] == stage) == frozen["s0"]
            assert frozen["s0"][-1] not in next(
                turn["messages"] for turn in bundle(path)["turn"] if turn["snapshot_id"] == frozen["snapshot_id"]
            )
        assert service.get_session(sid)["can_submit"]


def test_scope_presets_rebind_while_declared_values_are_frozen(tmp_path):
    registry = create_variable_registry(
        workflow_id=PROFILE, revision=1,
        definitions=[{"name": "instruction", "type": "string", "default": "{{not_expanded}}"}],
    )
    variables = create_variable_snapshot(registry, workflow_session_id=uid(1999), node_binding_id=uid(1998))
    config = make_prompt_context_config(collection("{{instruction}}/{{node_binding_id}}"), steps=[macro()], variables=variables)
    path = tmp_path / "scope.sqlite"
    calls = []
    with closing(WorkflowService(path, components=components(config), model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        for frozen in snapshots(path):
            evidence = validate_frozen_preparation(frozen)
            assert evidence["variables"]["workflow_session_id"] == sid
            assert evidence["variables"]["node_binding_id"] == frozen["node_binding_id"]
            assert evidence["collection"]["items"][0]["text"] == "{{not_expanded}}/" + frozen["node_binding_id"]
        assert config["variables"] == variables


@pytest.mark.parametrize("wrong_owner", ["workflow_id", "revision"])
def test_variable_registry_must_belong_to_exact_workflow_definition(tmp_path, wrong_owner):
    registry = create_variable_registry(
        workflow_id=uid(2400) if wrong_owner == "workflow_id" else PROFILE,
        revision=77 if wrong_owner == "revision" else 1,
        definitions=[{"name": "instruction", "type": "string", "default": "foreign-workflow-value"}],
    )
    variables = create_variable_snapshot(
        registry, workflow_session_id=uid(2401), node_binding_id=uid(2402),
    )
    config = make_prompt_context_config(collection("{{instruction}}"), steps=[macro()], variables=variables)
    calls = []
    with closing(WorkflowService(
        tmp_path / "owner.sqlite", components=components(config), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        before = bundle(service.database)
        with pytest.raises(ContractValidationError, match="exact workflow definition"):
            service.submit(sid, "city", "foreign-registry")
        assert not calls and bundle(service.database) == before


def test_send_view_does_not_replace_canonical_history_or_display_view(tmp_path):
    config = make_prompt_context_config(collection(), context_regex=[regex_config()])
    calls, path = [], tmp_path / "views.sqlite"
    with closing(WorkflowService(path, components=components(config), model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        run_turn(service, sid, "next city", "second")
        a_snapshots = [row for row in snapshots(path) if row["node_binding_id"] == A_BINDING]
        latest = next(row for row in a_snapshots if row["parent_turn_id"] is not None)
        evidence = validate_frozen_preparation(latest)
        assert "city" in evidence["canonical_messages"][0]["blocks"][0]["text"]
        assert not evidence["display_view"]["overrides"]
        assert "town" in next(message for message in latest["s0"] if message["message_id"] == evidence["canonical_messages"][0]["message_id"])["blocks"][0]["text"]
        assert all(message["source"]["kind"] != "prompt" for message in evidence["canonical_messages"])
        assert len(evidence["logical_floors"]) == 3
        turns = bundle(path)["turn"]
        assert all(turn["final"]["value"]["text"].endswith(("A", "B")) for turn in turns)
        visible = bundle(path)["visible_message"]
        assert any(message["role"] == "user" and message["payload"]["text"] == "city" for message in visible)


def test_explicit_final_text_protection_is_preserved_during_preparation():
    from phase1_agent.prompt_preparation import prepare_prompt_context
    from phase1_agent.bindings import BasicContext

    node_input = {
        "schema_version": 1, "input_id": uid(2100), "port_id": "request",
        "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
        "source": {"kind": "visible_message", "visible_message_id": uid(2101)},
        "payload": {"text": "city old"},
    }
    root = BasicContext().prepare(node_input, [], prompt_id=uid(2102), prompt_revision=1, system_prompt="s")[-1]
    final = {
        "schema_version": 1, "message_id": str(uuid4()), "role": "assistant",
        "source": {"kind": "model", "request_id": str(uuid4())},
        "blocks": [{"kind": "text", "text": '{"text":"city final"}'}],
    }
    config = make_prompt_context_config(
        collection(), context_regex=[regex_config(sources=("model",), pattern="city", replacement="changed")],
    )
    result = prepare_prompt_context(
        node_input, [root, final], workflow_session_id=uid(2103), node_binding_id=A_BINDING,
        parent_turn_id=uid(2104), logical_floors=[[root["message_id"]], [final["message_id"]]],
        protected_blocks=[{"message_id": final["message_id"], "block_index": 0}],
        config=config, root_message_id=uid(2105),
    )
    assert result["send_view"]["overrides"] == []
    assert final in result["s0"]


@pytest.mark.parametrize("defect", ["s0", "config", "protection", "scope", "history"])
def test_corrupt_prepared_component_is_rejected_before_dispatch(tmp_path, defect):
    class Corrupt(PreparedPromptContext):
        def prepare_context(self, *args, **kwargs):
            value = super().prepare_context(*args, **kwargs)
            if defect == "s0":
                value["s0"][0]["blocks"][0]["text"] = "changed"
            elif defect == "config":
                value["config"]["steps"] = [macro()]
            elif defect == "protection":
                value["protected_blocks"] = [{"message_id": uid(3999), "block_index": 0}]
            elif defect == "scope":
                value["send_view"]["workflow_session_id"] = uid(3999)
            else:
                value["canonical_messages"][0]["source"] = {"kind": "human", "visible_message_id": uid(3999)}
            return value

    calls = []
    with closing(WorkflowService(
        tmp_path / "bad.sqlite", components=components(make_prompt_context_config(collection()), context=Corrupt()),
        model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        with pytest.raises(ContractValidationError):
            service.submit(sid, "city", "bad")
        assert calls == []
        assert not snapshots(service.database)


def test_tool_schema_capacity_failure_dispatches_nothing(tmp_path):
    config = make_prompt_context_config(collection("s"))
    config["limits"]["assembly"]["max_total_chars"] = 100
    calls = []
    path = tmp_path / "capacity.sqlite"
    with closing(WorkflowService(path, components=components(config), model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        with pytest.raises(PromptProcessingError, match="capacity"):
            service.submit(sid, "city", "capacity")
        assert calls == [] and not snapshots(path)


@pytest.mark.parametrize("limit", ["max_messages", "max_total_chars"])
def test_later_request_capacity_preserves_facts_without_second_dispatch(tmp_path, limit):
    class LargeToolService(WorkflowService):
        @staticmethod
        def _tools():
            tools = list(WorkflowService._tools())
            tools[0] = replace(tools[0], _implementation=lambda text: {"text": "x" * 10_000})
            return tools

    class ToolModel:
        def generate(self, messages, tools):
            calls.append(copy.deepcopy(messages))
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", '{"text":"city"}'),
            ))

    config = make_prompt_context_config(collection("s"))
    config["limits"]["assembly"][limit] = 2 if limit == "max_messages" else 2000
    calls, path = [], tmp_path / "later-capacity.sqlite"
    with closing(LargeToolService(
        path, components=components(config), model_factory=lambda stage: ToolModel(),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "city", "later-capacity")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"]["code"] == "prompt_capacity_exceeded"
        assert len(calls) == 1
        with closing(SqliteStore(path)) as store:
            run = next(row for row in store.list_records("run_record") if row["profile"] == "agent")
            facts = store.read_execution_facts(run["run_id"])
        assert sum(row["kind"] == "model_request" for row in facts) == 1
        assert sum(row["kind"] == "model_attempt_started" for row in facts) == 1
        assert facts[-1]["kind"] == "execution_failed"
        assert facts[-1]["payload"]["category"] == "contract"
        assert [row["payload"]["outcome"] for row in facts if row["kind"] == "tool_settled"] == ["success"]
        accepted = [row["payload"]["message"] for row in facts if row["kind"] == "message_accepted"]
        assert len(accepted) == 2 and "x" * 10_000 in accepted[-1]["blocks"][0]["model_visible_text"]
        assert not bundle(path).get("turn")


@pytest.mark.parametrize("defect", ["messages", "tool_description", "extra_tool"])
def test_replaced_kernel_cannot_bypass_actual_adapter_guards(tmp_path, defect):
    from phase1_agent.runtime import SnapshotKernel

    class BypassKernel(SnapshotKernel):
        def run(self, snapshot, tools, adapter, **kwargs):
            messages = copy.deepcopy(snapshot["s0"])
            wire_tools = [copy.deepcopy(tool.definition) for tool in tools]
            if defect == "messages":
                messages[-1]["blocks"][0]["text"] = "x" * 10_000
            elif defect == "tool_description":
                wire_tools[0]["function"]["description"] = "x" * 10_000
            else:
                wire_tools.append(copy.deepcopy(wire_tools[0]))
            adapter.generate(messages, wire_tools)
            raise AssertionError("Oversized request should not reach the model")

    calls, config = [], make_prompt_context_config(collection("s"))
    config["limits"]["assembly"]["max_total_chars"] = 2000
    selected = components(config)
    kernel_id = uid(2900)
    selected.registry.register(
        "kernel", kernel_id, "1.0", BypassKernel(),
        capabilities=frozenset({"json_final", "paired_tools", "bounded_retry"}),
        config_schema={"type": "object", "additionalProperties": False},
    )
    selected.kernels["A"] = ComponentSelection(
        kernel_id, "1.0", {"owner_component_id": kernel_id, "schema_version": 1, "payload": {}},
    )
    with closing(WorkflowService(
        tmp_path / "bypass.sqlite", components=selected, model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "city", "bypass")
        service.wait_for_idle(sid)
        assert calls == []
        expected_error = PromptProcessingError if defect == "messages" else ContractValidationError
        assert isinstance(service._execution_failures[sid], expected_error)


def test_prepared_dispatch_guard_preserves_adapter_cleanup(tmp_path):
    path = tmp_path / "guard.sqlite"
    config = make_prompt_context_config(collection())
    with closing(WorkflowService(path, components=components(config))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
    frozen = snapshots(path)[0]
    closed = []

    class Adapter:
        def generate(self, messages, tools):
            return "response"

        def close(self):
            closed.append(True)

    guard = PreparedRequestAdapter(Adapter(), frozen)
    tools = [copy.deepcopy(tool.definition) for tool in WorkflowService._tools()]
    assert guard.generate(frozen["s0"], tools) == "response"
    guard.close()
    assert closed == [True]


def test_prepared_projection_reaches_actual_http_transport_unchanged(tmp_path):
    from test_workflow_frozen_model import factory_with_wire

    captured = []
    config = make_prompt_context_config(
        collection("prepared after-root", placement="middle", depth=0),
        context_regex=[regex_config()],
    )
    path = tmp_path / "wire.sqlite"
    with closing(WorkflowService(
        path, components=components(config), model_factory=factory_with_wire(captured),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        assert [stage for stage, _ in captured] == ["A", "B"]
        for saved in snapshots(path):
            stage = "A" if saved["node_binding_id"] == A_BINDING else "B"
            wire = next(body for sent_stage, body in captured if sent_stage == stage)
            assert wire["messages"] == [
                {"role": message["role"], "content": message["blocks"][0]["text"]}
                for message in saved["s0"]
            ]
            assert wire["messages"][-1]["content"] == "prepared after-root"
            assert wire["model"] == saved["model_parameters"]["model"]
            assert len(wire["tools"]) == len(saved["tool_definitions"])
            for sent, definition in zip(wire["tools"], saved["tool_definitions"]):
                assert sent["function"]["name"] == definition["name"]
                assert sent["function"]["parameters"] == definition["parameters_schema"]
        assert "town" in captured[0][1]["messages"][0]["content"]


def test_pre_dispatch_transaction_failure_does_not_publish_prepared_state(tmp_path):
    armed = False

    def failure(point):
        if armed and point == "before_commit":
            raise RuntimeError("injected write failure")

    calls, path = [], tmp_path / "write.sqlite"
    with closing(WorkflowService(
        path, components=components(make_prompt_context_config(collection())),
        model_factory=factory(calls), fault_injector=failure,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        before = bundle(path)
        armed = True
        with pytest.raises(RuntimeError, match="injected"):
            service.submit(sid, "city", "write")
        assert calls == [] and bundle(path) == before


def test_activity_edit_does_not_change_saved_preparation(tmp_path):
    entered, release, calls = Event(), Event(), []
    path = tmp_path / "edit.sqlite"
    config = make_prompt_context_config(collection("original"))
    with closing(WorkflowService(
        path, components=components(config), model_factory=factory(calls, entered=entered, release=release),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "city", "first")
        assert entered.wait(10)
        saved = snapshots(path)[0]
        context, kernel, adapter, current = service._resolved["A"]
        changed = copy.deepcopy(current)
        changed["payload"]["context"]["collection"]["items"][0]["text"] = "changed live"
        service._resolved["A"] = context, kernel, adapter, changed
        release.set()
        service.wait_for_idle(sid)
        assert snapshots(path)[0] == saved
        assert calls[0][1] == saved["s0"]
        assert validate_frozen_preparation(saved)["collection"]["items"][0]["text"] == "original"


def test_prepared_frozen_evidence_survives_database_reopen(tmp_path):
    config, path = make_prompt_context_config(collection()), tmp_path / "reopen.sqlite"
    with closing(WorkflowService(path, components=components(config))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        before = snapshots(path)
    with closing(WorkflowService(path, components=components(config))) as reopened:
        assert snapshots(path) == before
        assert reopened.get_session(sid)["can_submit"]
        run_turn(reopened, sid, "second city", "second")
        assert len(snapshots(path)) == 4


def test_frozen_evidence_and_capacity_validator_do_not_execute_processing(tmp_path, monkeypatch):
    path, config = tmp_path / "replay.sqlite", make_prompt_context_config(collection(), context_regex=[regex_config()])
    with closing(WorkflowService(path, components=components(config))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        def forbidden(*_args, **_kwargs):
            raise AssertionError("macro/regex processing must not repeat")
        monkeypatch.setattr("phase1_agent.prompt_preparation.apply_context_regex", forbidden)
        monkeypatch.setattr("phase1_agent.prompt_preparation.process_prompt_collection", forbidden)
        for saved in snapshots(path):
            validate_frozen_preparation(saved)
            check_prepared_request_capacity(saved)
