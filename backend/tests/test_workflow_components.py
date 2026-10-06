"""Public fixed-workflow replacements need no default component private state."""

import copy
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.bindings import ComponentRegistry, ComponentSelection, WorkflowComponents
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import canonical_bytes, dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import KernelPaused, KernelResult, RunFailed
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, OUTPUT_BINDING, WorkflowService


CONTEXT = "c5ee55a7-7c51-4873-86d8-4d0abaf72701"
KERNEL = "c5ee55a7-7c51-4873-86d8-4d0abaf72702"
OUTPUT = "c5ee55a7-7c51-4873-86d8-4d0abaf72703"
PROFILE = "c5ee55a7-7c51-4873-86d8-4d0abaf72704"
PROMPT = "c5ee55a7-7c51-4873-86d8-4d0abaf72705"
VERSION = "2.4"
CONTEXT_CAPS = frozenset({"sources", "paired_history", "frozen_projection"})
KERNEL_CAPS = frozenset({"json_final", "paired_tools", "bounded_retry", "reroll"})
EMPTY_SCHEMA = {"type": "object", "additionalProperties": False}


def uid():
    return str(uuid4())


def envelope(owner, payload):
    return {"owner_component_id": owner, "schema_version": 1, "payload": payload}


def message(role, source, blocks):
    return {"schema_version": 1, "message_id": uid(), "role": role, "source": source, "blocks": blocks}


def input_source(node_input):
    source = node_input["source"]
    if source["kind"] == "visible_message":
        return {"kind": "human", "visible_message_id": source["visible_message_id"]}
    return {"kind": "upstream_node", "output_id": source["output_id"]}


class IndependentContext:
    def __init__(self, *, corrupt_history=False, reorder_history=False):
        self.calls = []
        self.corrupt_history = corrupt_history
        self.reorder_history = reorder_history

    def prepare(self, node_input, history, *, config):
        self.calls.append((copy.deepcopy(node_input), copy.deepcopy(history), copy.deepcopy(config)))
        if self.reorder_history and len(history) == 6:
            history = history[3:] + history[:3]
        return [
            message("system", {"kind": "prompt", "prompt_id": PROMPT, "revision": 1},
                    [{"kind": "text", "text": config["label"]}]),
            *history,
            message("user", input_source(node_input),
                    [{"kind": "text", "text": dumps_pretty(node_input["payload"])}]),
        ]

    def project_turn(self, turn, node_input, snapshot):
        frozen_input = next(
            value for value in reversed(snapshot["s0"])
            if value["role"] == "user" and value["source"] == input_source(node_input)
        )
        values = [frozen_input, *turn["messages"]]
        if self.corrupt_history:
            values[0]["blocks"][0]["text"] = "changed private copy"
        return values


class FinalModel:
    def __init__(self, stage, calls, entered=None, release=None):
        self.stage, self.calls = stage, calls
        self.entered, self.release = entered, release

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        text = loads_strict(messages[-1]["blocks"][0]["text"])["text"]
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(uid(), "final_answer", dumps_pretty({"answer": {"text": text + self.stage}})),
        ))


class IndependentKernel:
    """One-attempt conformance fixture, not a second production agent engine."""

    def __init__(self, *, defect=None, entered=None, release=None):
        self.snapshots = []
        self.defect = defect
        self.entered, self.release = entered, release

    def run(
        self, snapshot, tools, adapter, *, max_model_requests, max_model_attempts,
        checkpoint, on_progress, on_boundary, on_tool_event, on_fact,
    ):
        assert checkpoint is None
        self.snapshots.append(copy.deepcopy(snapshot))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        definitions = [copy.deepcopy(tool.definition) for tool in tools]
        request_id, attempt_id, call_id, execution_id = uid(), uid(), uid(), uid()
        on_fact({"kind": "model_request", "payload": {
            "request_id": request_id, "request_index": 1, "messages": snapshot["s0"],
            "tools": definitions, "model_parameters": snapshot["model_parameters"],
        }})
        on_fact({"kind": "model_attempt_started", "payload": {
            "request_id": request_id, "attempt_id": attempt_id, "request_index": 1,
            "attempt_index": 1, "retry_index": 0,
        }})
        response = adapter.generate(copy.deepcopy(snapshot["s0"]), definitions)
        on_fact({"kind": "model_attempt_finished", "payload": {
            "request_id": request_id, "attempt_id": attempt_id, "outcome": "responded",
            "usage": None, "response_id": None, "model": None,
        }})
        call = response.tool_calls[0]
        answer = loads_strict(call.raw_arguments)["answer"]
        assistant = message("assistant", {"kind": "model", "request_id": request_id}, [{
            "kind": "tool_call", "tool_call_id": call_id, "tool_name": "final_answer",
            "tool_definition_version": "1", "raw_arguments": call.raw_arguments,
            "parsed_arguments": {"answer": answer},
        }])
        settled = message("tool", {"kind": "tool", "tool_execution_id": execution_id}, [{
            "kind": "tool_result", "tool_call_id": call_id, "tool_execution_id": execution_id,
            "status": "success", "is_error": False, "content": answer,
            "model_visible_text": canonical_bytes(answer).decode("utf-8"),
        }])
        messages = [assistant, settled]
        on_fact({"kind": "message_accepted", "payload": {"message": assistant}})
        on_progress({"model_requests": 1, "attempts": 1, "accepted_messages": 1})
        on_tool_event("queue", call_id, None, None)
        if self.defect == "failure":
            on_fact({"kind": "execution_failed", "payload": {
                "code": "protocol_error", "category": "protocol", "model_requests": 1, "attempts": 1,
            }})
            raise RunFailed("protocol_error", [assistant], 1, 1)
        if self.defect != "no_final_boundary":
            assert on_boundary("before_final")
        on_tool_event("start", call_id, execution_id, None)
        on_fact({"kind": "tool_dispatch", "payload": {
            "tool_call_id": call_id, "tool_execution_id": execution_id,
        }})
        on_tool_event("settle", call_id, execution_id, "success")
        on_fact({"kind": "tool_settled", "payload": {
            "tool_call_id": call_id, "tool_execution_id": execution_id,
            "outcome": "success", "message": settled,
        }})
        if self.defect != "missing_accepted_fact":
            on_fact({"kind": "message_accepted", "payload": {"message": settled}})
            on_progress({"model_requests": 1, "attempts": 1, "accepted_messages": 2})
        if self.defect == "mutated_snapshot":
            snapshot["s0"][-1]["blocks"][0]["text"] = "changed after dispatch"
        if self.defect == "bad_final":
            answer = {"text": "a final never accepted"}
        return KernelResult(messages, {"message_id": assistant["message_id"], "value": answer}, 1, 1)


class IndependentOutput:
    def __init__(self):
        self.calls = []
        self.invalid = False

    def emit(self, node_input, *, config):
        self.calls.append(copy.deepcopy(node_input))
        if self.invalid:
            return {"text": 123}
        return {"text": config["prefix"] + node_input["payload"]["text"]}


@dataclass(frozen=True)
class IndependentCheckpoint:
    snapshot_id: str
    messages: tuple = ()
    pending_tools: tuple = ()
    model_requests: int = 0
    attempts: int = 0


class PausableIndependentKernel(IndependentKernel):
    def __init__(self, entered, release):
        super().__init__()
        self.entered, self.release = entered, release
        self.first = True
        self.incompatible = False
        self.checkpoint = None

    def validate_checkpoint(self, checkpoint, snapshot):
        if (self.incompatible or checkpoint is not self.checkpoint
                or checkpoint.snapshot_id != snapshot["snapshot_id"]):
            raise ContractValidationError("Incompatible independent kernel checkpoint")

    def run(self, snapshot, tools, adapter, **kwargs):
        checkpoint = kwargs["checkpoint"]
        if self.first:
            self.first = False
            self.entered.set()
            assert self.release.wait(5)
            if not kwargs["on_boundary"]("before_request"):
                self.checkpoint = IndependentCheckpoint(snapshot["snapshot_id"])
                raise KernelPaused(self.checkpoint)
        elif checkpoint is not None:
            self.validate_checkpoint(checkpoint, snapshot)
        kwargs["checkpoint"] = None
        entered, release = self.entered, self.release
        self.entered = self.release = None
        try:
            return super().run(snapshot, tools, adapter, **kwargs)
        finally:
            self.entered, self.release = entered, release


def selected(*, context=None, kernel=None, output=None, kernel_caps=KERNEL_CAPS):
    registry = ComponentRegistry()
    contexts, kernels = {}, {}
    if context is not None:
        registry.register("context", CONTEXT, VERSION, context, capabilities=CONTEXT_CAPS,
                          config_schema={"type": "object", "properties": {"label": {"type": "string"}},
                                         "required": ["label"], "additionalProperties": False})
        contexts = {
            stage: ComponentSelection(CONTEXT, VERSION, envelope(CONTEXT, {"label": stage}))
            for stage in ("A", "B")
        }
    if kernel is not None:
        registry.register("kernel", KERNEL, VERSION, kernel, capabilities=kernel_caps, config_schema=EMPTY_SCHEMA)
        kernels = {stage: ComponentSelection(KERNEL, VERSION, envelope(KERNEL, {})) for stage in ("A", "B")}
    output_selection = None
    if output is not None:
        registry.register("io", OUTPUT, VERSION, output, capabilities=frozenset({"structured_io"}),
                          config_schema={"type": "object", "properties": {"prefix": {"type": "string"}},
                                         "required": ["prefix"], "additionalProperties": False})
        output_selection = ComponentSelection(OUTPUT, VERSION, envelope(OUTPUT, {"prefix": "published:"}))
    return WorkflowComponents(registry, contexts, kernels, output_selection, PROFILE)


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="components-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


@pytest.mark.parametrize("replace_context,replace_kernel", [(True, False), (False, True), (True, True)])
def test_public_components_run_archive_isolate_histories_and_reopen(
    database, replace_context, replace_kernel,
):
    calls = []
    context, kernel, output = IndependentContext(), IndependentKernel(), IndependentOutput()
    components = selected(context=context if replace_context else None,
                          kernel=kernel if replace_kernel else None, output=output)
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        for index in range(2):
            service.submit(sid, f"input-{index}", f"request-{index}")
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
        view = service.get_session(sid)
        assert view["messages"][-1]["payload"] == {"text": "published:input-1AB"}
        saved = durable(database)
        assert len(saved["turn"]) == 4 and len(saved["node_run"]) == 2
        assert len(saved["output_delivery"]) == 2
        runs = {row["run_id"]: row for row in saved["run_record"]}
        for binding in (A_BINDING, B_BINDING):
            turns = [row for row in saved["turn"] if runs[row["run_id"]]["node_binding_id"] == binding]
            assert turns[1]["parent_turn_id"] == turns[0]["turn_id"]
        a_turn = next(row for row in saved["turn"] if runs[row["run_id"]]["node_binding_id"] == A_BINDING)
        b_snapshot = next(row for row in saved["input_snapshot"] if row["node_binding_id"] == B_BINDING)
        assert all(row["message_id"] not in {
            value["message_id"] for value in a_turn["messages"]
        } for row in b_snapshot["s0"])
        assert b_snapshot["s0"][-1]["source"]["kind"] == "upstream_node"
        output_binding = next(row for row in saved["node_binding"] if row["node_binding_id"] == OUTPUT_BINDING)
        assert output_binding["component_id"] == OUTPUT
        assert len(output.calls) == 2
        if replace_context:
            assert context.calls[0][2] == {"label": "A"}
            assert context.calls[1][2] == {"label": "B"}
        if replace_kernel:
            assert all(row["config"]["payload"]["resolved"]["kernel"]["component_id"] == KERNEL
                       for row in kernel.snapshots)
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda _: pytest.fail("reopen dispatched a model"))) as reopened:
        assert reopened.get_session(sid)["messages"] == view["messages"]
        assert durable(database)["input_snapshot"] == saved["input_snapshot"]


@pytest.mark.parametrize("defect", [
    "bad_final", "missing_accepted_fact", "no_final_boundary", "mutated_snapshot", "failure",
])
def test_replacement_contract_fault_cannot_publish_or_enter_success_history(database, defect):
    calls = []
    components = selected(context=IndependentContext(), kernel=IndependentKernel(defect=defect))
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "not a successful turn", "one")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is not None
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert "resume" not in view["available_actions"]
        saved = durable(database)
        assert saved.get("turn", []) == [] and saved.get("workflow_candidate", []) == []
        assert len(calls) == 1


def test_context_gets_detached_read_only_history_and_cannot_rewrite_source(database):
    context, calls = IndependentContext(corrupt_history=True), []
    components = selected(context=context, kernel=IndependentKernel())
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "original", "first")
        service.wait_for_idle(sid)
        original = durable(database)
        with pytest.raises(ContractValidationError, match="historical input source"):
            service.submit(sid, "second", "second")
        assert len(calls) == 2
        after = durable(database)
        assert after["turn"] == original["turn"]
        assert after["input_snapshot"] == original["input_snapshot"]


def test_context_cannot_reverse_complete_valid_historical_turns(database):
    calls = []
    components = selected(context=IndependentContext(reorder_history=True), kernel=IndependentKernel())
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        for index in range(2):
            service.submit(sid, f"original-{index}", f"first-{index}")
            service.wait_for_idle(sid)
        original = durable(database)
        with pytest.raises(ContractValidationError, match="accepted history sources"):
            service.submit(sid, "reordered history", "third")
        assert len(calls) == 4
        assert durable(database)["input_snapshot"] == original["input_snapshot"]


def test_custom_checkpoint_validation_stays_with_original_kernel_not_default_private_hmac(database):
    entered, release, calls = Event(), Event(), []
    kernel = PausableIndependentKernel(entered, release)
    components = selected(context=IndependentContext(), kernel=kernel,
                          kernel_caps=KERNEL_CAPS | {"pause_resume"})
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "same private checkpoint", "first")
        assert entered.wait(5)
        active = service.get_session(sid)
        node = active["nodes"][0]
        try:
            service.interrupt(
                sid, node["run_id"], idempotency_key="stop",
                expected_session_revision=active["revision"], expected_run_revision=node["revision"],
            )
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused" and calls == []
        original = durable(database)["input_snapshot"]
        kernel.incompatible = True
        with pytest.raises(ContractValidationError, match="Incompatible independent"):
            service.resume(
                sid, node["run_id"], idempotency_key="incompatible",
                expected_session_revision=paused["revision"],
                expected_run_revision=paused["nodes"][0]["revision"],
            )
        assert calls == [] and durable(database)["input_snapshot"] == original
        kernel.incompatible = False
        context, _, adapter, config = service._resolved["A"]
        service._resolved["A"] = context, object(), adapter, config
        service.resume(
            sid, node["run_id"], idempotency_key="continue",
            expected_session_revision=paused["revision"], expected_run_revision=paused["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        finished = service.get_session(sid)
        assert finished["error"] is None
        assert finished["nodes"][0]["run_id"] == node["run_id"]
        assert [stage for stage, *_ in calls] == ["A", "B"]


def test_custom_kernel_without_pause_resume_has_honest_control_capabilities(database):
    entered, release, calls = Event(), Event(), []
    kernel = IndependentKernel(entered=entered, release=release)
    components = selected(context=IndependentContext(), kernel=kernel, output=IndependentOutput())
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "custom run", "first")
        assert entered.wait(5)
        view = service.get_session(sid)
        node = view["nodes"][0]
        try:
            assert "interrupt" not in view["available_actions"]
            with pytest.raises(ContractValidationError) as unsupported:
                service.interrupt(
                    sid, node["run_id"], idempotency_key="unsupported",
                    expected_session_revision=view["revision"], expected_run_revision=node["revision"],
                )
            assert unsupported.value.reason_code == "unsupported"
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None


def test_running_component_choice_is_frozen_even_if_live_stage_selection_changes(database):
    entered, release, calls = Event(), Event(), []
    original = IndependentKernel(entered=entered, release=release)
    components = selected(context=IndependentContext(), kernel=original)
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "frozen", "one")
        assert entered.wait(5)
        a_context, _, adapter, config = service._resolved["A"]
        service._resolved["A"] = a_context, object(), adapter, config
        release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert len(original.snapshots) == 2
        assert original.snapshots[0]["config"]["payload"]["resolved"]["kernel"]["component_id"] == KERNEL


def test_custom_kernel_without_reroll_capability_cannot_authorize_chain_replacement(database):
    calls = []
    components = selected(context=IndependentContext(), kernel=IndependentKernel(),
                          kernel_caps=KERNEL_CAPS - {"reroll"})
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "no reroll capability", "one")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert not view["can_reroll"]
        before = durable(database)
        with pytest.raises(ContractValidationError) as unsupported:
            service.reroll(
                sid, receipt["chain_run_id"], idempotency_key="unsupported",
                expected_session_revision=view["revision"],
                expected_ref_revision=view["ref_revision"],
                expected_head_commit_id=view["head_commit_id"],
            )
        assert unsupported.value.reason_code == "unsupported"
        assert durable(database) == before and len(calls) == 2


def test_accepted_custom_kernel_result_retries_archive_without_redispatch(database, monkeypatch):
    calls = []
    kernel = IndependentKernel()
    components = selected(context=IndependentContext(), kernel=kernel)
    original_archive = SqliteStore.archive_success
    failed = False

    def archive(store, records, key):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("archive fault")
        return original_archive(store, records, key)

    monkeypatch.setattr(SqliteStore, "archive_success", archive)
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "durable success", "first")
        service.wait_for_idle(sid)
        failed_view = service.get_session(sid)
        node = failed_view["nodes"][0]
        assert node["status"] == "final_ready"
        assert failed_view["available_actions"] == ["retry_archive"]
        service.retry_archive(sid, node["run_id"], idempotency_key="archive",
                              expected_session_revision=failed_view["revision"])
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert [stage for stage, *_ in calls] == ["A", "B"]
        assert len(kernel.snapshots) == 2 and len(durable(database)["turn"]) == 2


def test_invalid_generic_output_preserves_agent_success_and_manual_publish_retry(database):
    calls = []
    output = IndependentOutput()
    output.invalid = True
    components = selected(context=IndependentContext(), kernel=IndependentKernel(), output=output)
    with closing(WorkflowService(database, components=components,
                                 model_factory=lambda stage: FinalModel(stage, calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "output contract", "one")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["error"] is not None
        assert failed["available_actions"] == ["retry_publish"]
        original = durable(database)
        assert len(original["turn"]) == 2
        assert all(row["status"] == "succeeded" for row in original["run_record"])
        assert original.get("node_run", []) == [] and original.get("workflow_candidate", []) == []
        output.invalid = False
        run = next(row for row in original["run_record"] if row["node_binding_id"] == B_BINDING)
        service.retry_publish(sid, run["run_id"], idempotency_key="publish",
                              expected_session_revision=failed["revision"])
        final = service.get_session(sid)
        assert final["error"] is None
        assert final["messages"][-1]["payload"] == {"text": "published:output contractAB"}
        assert durable(database)["turn"] == original["turn"] and len(calls) == 2


@pytest.mark.parametrize("case", ["capabilities", "version", "config", "api", "checkpoint_api"])
def test_invalid_public_component_selection_fails_before_model_or_tool_dispatch(database, case):
    calls = []
    kernel = IndependentKernel()
    caps = KERNEL_CAPS
    if case == "capabilities":
        caps -= {"paired_tools"}
    if case == "api":
        kernel.run = lambda snapshot: None
    if case == "checkpoint_api":
        caps |= {"pause_resume"}
    components = selected(kernel=kernel, kernel_caps=caps)
    if case in ("version", "config"):
        selected_kernel = components.kernels["A"]
        replacement = ComponentSelection(
            KERNEL, "missing" if case == "version" else VERSION,
            envelope(KERNEL, {"unsupported": True}) if case == "config" else selected_kernel.config,
        )
        components = WorkflowComponents(components.registry, kernels={"A": replacement},
                                        workflow_definition_id=PROFILE)
    with pytest.raises(ContractValidationError):
        WorkflowService(database, components=components, model_factory=lambda stage: calls.append(stage))
    assert calls == []
    with closing(SqliteStore(database)) as store:
        assert store.list_records("run_record") == []


def test_legacy_frozen_budget_reopens_and_rerolls_without_rewriting_old_evidence(database):
    class LegacyWorkflow(WorkflowService):
        def _register_components(self):
            super()._register_components()
            for _, _, _, config in self._resolved.values():
                config["payload"]["kernel"].pop("max_model_attempts")

    with closing(LegacyWorkflow(database)) as old:
        sid = old.create_session()["workflow_session_id"]
        receipt = old.submit(sid, "legacy snapshot", "first")
        old.wait_for_idle(sid)
        assert old.get_session(sid)["error"] is None
        original = durable(database)
        with closing(SqliteStore(database)) as store:
            bootstrap = store.read_receipt("workflow.bootstrap", "fixed-workflow:offline")
        assert all("max_model_attempts" not in row["config"]["payload"]["kernel"]
                   for row in original["input_snapshot"])
    with closing(WorkflowService(database)) as current:
        view = current.get_session(sid)
        assert durable(database) == original
        current.reroll(
            sid, receipt["chain_run_id"], idempotency_key="reroll",
            expected_session_revision=view["revision"],
            expected_ref_revision=view["ref_revision"],
            expected_head_commit_id=view["head_commit_id"],
        )
        current.wait_for_idle(sid)
        assert current.get_session(sid)["error"] is None
        after = durable(database)
        assert len(after["workflow_candidate"]) == 2
        for kind in ("input_snapshot", "run_record", "turn", "node_binding"):
            assert all(row in after[kind] for row in original[kind])
        with closing(SqliteStore(database)) as store:
            assert store.read_receipt("workflow.bootstrap", "fixed-workflow:offline") == bootstrap
