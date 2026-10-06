"""Optional graph Agent adapter using the existing frozen snapshot kernel.

Only checkpoints and unreleased accepted packages live in this process. The
graph host persists snapshots, causal facts and accepted Turns through callbacks.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from .active_run import AcceptedProgress, ActiveRun
from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .execution_facts import ExecutionFactHistory
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters
from .model_configuration import chat_parameter_diagnostic, configuration_error
from .model_selection import _credential, _fingerprint, validate_model_binding, verify_model_binding
from .prompt_assembly import PromptAssemblyLimits, message_content_chars, validate_prompt_assembly
from .prompt_errors import PromptProcessingError
from .runtime import (
    CanonicalModelAdapter, KernelCheckpoint, KernelContractError, KernelPaused,
    KernelPauseRequested, RunFailed, SnapshotKernel,
)
from .tools import RegisteredTool
from .runtime_control import NodeExecutionSuspended


GRAPH_AGENT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a101"
GRAPH_AGENT_OUTPUT_SCHEMA = {
    "type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"], "additionalProperties": False,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ContractValidationError("Agent identity must be a canonical UUID4") from None
    _require(type(value) is str and parsed.version == 4 and str(parsed) == value,
             "Agent identity must be a canonical UUID4")


def graph_agent_tools() -> tuple[RegisteredTool, ...]:
    """Reuse the existing two trusted tools, without a tool installation surface."""
    from .workflow_tool_catalog import builtin_workflow_tools
    return builtin_workflow_tools()


def resolve_graph_model(config: dict, catalog: Any, *, node_id: str) -> dict:
    """Resolve the current global provider once; persist only secret-free evidence."""
    _uuid(node_id)
    _require(type(config) is dict and set(config) == {"provider_id", "parameters"},
             "Graph model configuration fields differ")
    provider_id = config["provider_id"]
    if provider_id is None:
        raise configuration_error("provider_missing", 409)
    _uuid(provider_id)
    if chat_parameter_diagnostic(config["parameters"]):
        raise configuration_error("model_parameters_unsupported", 409)
    provider = catalog.get_current("provider", provider_id)
    if provider is None:
        raise configuration_error("provider_missing", 409)
    if not provider["enabled"]:
        raise configuration_error("provider_unavailable", 409)
    binding = validate_model_binding({
        "node_id": node_id, "provider": provider,
        "credential_evidence": _fingerprint(_credential(provider)),
        "parameters": dict(FrozenModelParameters.from_mapping(config["parameters"]).as_mapping()),
    })
    return {"schema_version": 1, "kind": "workflow.model-resource", "binding": binding}


def validate_graph_model_resource(value: Any) -> dict:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {"schema_version", "kind", "binding"}
             and type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "workflow.model-resource", "Graph model resource fields differ")
    validate_model_binding(value["binding"])
    if chat_parameter_diagnostic(value["binding"]["parameters"]):
        raise configuration_error("model_parameters_unsupported", 409)
    return copy.deepcopy(value)


def create_graph_chat_adapter(binding: dict, catalog_scope: Callable) -> Any:
    """Construct the existing Chat adapter and fence credentials before dispatch."""
    from .adapter import DeepSeekAdapter

    binding = validate_model_binding(binding)

    def verify() -> str:
        with catalog_scope() as catalog:
            return verify_model_binding(binding, catalog)

    credential = verify()
    parameters = FrozenModelParameters.from_mapping(binding["parameters"])
    implementation = CanonicalModelAdapter(DeepSeekAdapter(
        api_key=credential, base_url=binding["provider"]["base_url"],
        model=parameters.model, max_tokens=parameters.max_tokens,
        temperature=parameters.temperature, max_retries=0,
    ))
    try:
        return FrozenConfiguredAdapter(
            implementation, parameters, dispatch_guard=verify,
            provider_address=binding["provider"]["base_url"],
        )
    except BaseException:
        implementation.close()
        raise


@dataclass(frozen=True)
class GraphAgentIdentity:
    workflow_session_id: str
    node_binding_id: str
    chain_run_id: str
    node_run_id: str
    base_commit_id: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            _uuid(value)


def make_graph_agent_snapshot(
    *, identity: GraphAgentIdentity, s0: list[dict], parent_turn_id: str | None,
    model_binding: dict, config: dict, input_id: str, evidence: dict,
    snapshot_id: str | None = None, tools: tuple[RegisteredTool, ...] | None = None,
) -> dict:
    """Freeze actual assembled messages using the existing InputSnapshot schema."""
    model_binding = validate_model_binding(model_binding)
    tools = graph_agent_tools() if tools is None else tools
    snapshot = validate_record("input_snapshot", {
        "schema_version": 1, "snapshot_id": snapshot_id or str(uuid4()),
        "workflow_session_id": identity.workflow_session_id,
        "node_binding_id": identity.node_binding_id, "input_id": input_id,
        "parent_turn_id": parent_turn_id, "component_id": GRAPH_AGENT_COMPONENT,
        "component_version": "graph-2",
        "config": {"owner_component_id": GRAPH_AGENT_COMPONENT, "schema_version": 1,
                   "payload": {"graph_agent": copy.deepcopy(config),
                               "graph_identity": asdict(identity),
                               "graph_preparation": copy.deepcopy(evidence),
                               "model_binding": model_binding}},
        "s0": copy.deepcopy(s0), "tool_definitions": [{
            "name": tool.name, "version": "1",
            "description": tool.definition["function"]["description"],
            "parameters_schema": copy.deepcopy(tool.schema),
        } for tool in tools],
        "model_parameters": model_binding["parameters"],
        "output_schema": copy.deepcopy(GRAPH_AGENT_OUTPUT_SCHEMA), "projection_version": 1,
    })
    validate_message_history(snapshot["s0"])
    if evidence.get("kind") == "workflow.prompt-assembly":
        assembly = validate_prompt_assembly(evidence["prompt"])
        _require(assembly["messages"] == snapshot["s0"], "Graph Agent S0 differs from assembled evidence")
    _check_request_capacity(snapshot, snapshot["s0"])
    return snapshot


@dataclass(frozen=True)
class GraphAgentCallbacks:
    prepare: Callable[[dict], None]
    fact: Callable[[dict], None]
    progress: Callable[[dict], None]
    archive: Callable[[dict], None]
    should_pause: Callable[[], bool] = lambda: False


class GraphAgentSuspended(NodeExecutionSuspended):
    """A controlled node boundary with no output release or new model replay."""
    def __init__(self, status: str, reason_code: str):
        super().__init__(status, reason_code, reason_code)


@dataclass(frozen=True)
class GraphAgentAccepted:
    package: dict

    def to_dict(self) -> dict:
        return copy.deepcopy(self.package)


def validate_graph_agent_accepted(value: Any) -> dict:
    """Validate archived evidence independently of any active kernel workspace."""
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "schema_version", "kind", "identity", "snapshot", "turn", "facts", "limits", "progress",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow.agent-accepted", "Graph Agent accepted package fields differ")
    _require(type(value["identity"]) is dict and set(value["identity"]) == {
        "workflow_session_id", "node_binding_id", "chain_run_id", "node_run_id", "base_commit_id",
    } and type(value["progress"]) is dict and set(value["progress"]) == {
        "model_requests", "attempts", "accepted_messages",
    }, "Graph Agent accepted identity or progress fields differ")
    identity = GraphAgentIdentity(**value["identity"])
    snapshot = validate_record("input_snapshot", value["snapshot"])
    turn = validate_record("turn", value["turn"])
    binding = validate_model_binding(snapshot["config"]["payload"].get("model_binding"))
    _require(snapshot["component_id"] == GRAPH_AGENT_COMPONENT
             and snapshot["component_version"] == "graph-2"
             and snapshot["config"]["owner_component_id"] == GRAPH_AGENT_COMPONENT
             and snapshot["config"]["payload"].get("graph_identity") == value["identity"]
             and snapshot["model_parameters"] == binding["parameters"]
             and snapshot["output_schema"] == GRAPH_AGENT_OUTPUT_SCHEMA,
             "Graph Agent accepted snapshot changed its registered contract")
    _require(snapshot["workflow_session_id"] == identity.workflow_session_id
             and snapshot["node_binding_id"] == identity.node_binding_id
             and turn["run_id"] == identity.node_run_id
             and turn["snapshot_id"] == snapshot["snapshot_id"]
             and turn["input_id"] == snapshot["input_id"]
             and turn["parent_turn_id"] == snapshot["parent_turn_id"]
             and turn["projection_version"] == snapshot["projection_version"],
             "Graph Agent accepted package ownership differs")
    _validate_limits(value["limits"])
    progress = AcceptedProgress(**value["progress"])
    facts = _validated_facts(value["facts"], snapshot, identity)
    accepted = [fact["payload"]["message"] for fact in facts if fact["kind"] == "message_accepted"]
    requests = sum(fact["kind"] == "model_request" for fact in facts)
    started = {fact["payload"]["attempt_id"] for fact in facts if fact["kind"] == "model_attempt_started"}
    finished = {fact["payload"]["attempt_id"] for fact in facts if fact["kind"] == "model_attempt_finished"}
    _require(canonical_bytes(accepted) == canonical_bytes(turn["messages"])
             and progress == AcceptedProgress(requests, len(started), len(accepted))
             and started == finished and 1 <= requests <= value["limits"]["max_model_requests"]
             and requests <= len(started) <= value["limits"]["max_model_attempts"],
             "Graph Agent result differs from accepted execution facts")
    validate_message_history(snapshot["s0"] + turn["messages"])
    validate_turn_final(turn, snapshot)
    return copy.deepcopy(value)


def _validated_facts(facts: list[dict], snapshot: dict, identity: GraphAgentIdentity) -> list[dict]:
    _require(type(facts) is list, "Graph Agent facts must be an array")
    history = ExecutionFactHistory(snapshot, owner={
        "run_id": identity.node_run_id, "chain_run_id": identity.chain_run_id,
        "workflow_session_id": identity.workflow_session_id, "node_binding_id": identity.node_binding_id,
        "snapshot_id": snapshot["snapshot_id"],
    })
    for fact in facts:
        history.append(fact)
        if fact["kind"] == "model_request":
            _check_request_capacity(snapshot, fact["payload"]["messages"])
    return history.facts


def _check_request_capacity(snapshot: dict, messages: list[dict]) -> None:
    evidence = snapshot["config"]["payload"].get("graph_preparation", {})
    prompt = evidence.get("prompt")
    limits = (PromptAssemblyLimits(**prompt["manifest"]["limits"])
              if type(prompt) is dict else PromptAssemblyLimits())
    tool_chars = len(canonical_bytes(snapshot["tool_definitions"]).decode("utf-8"))
    if len(messages) > limits.max_messages or message_content_chars(messages) + tool_chars > limits.max_total_chars:
        raise PromptProcessingError("prompt_capacity_exceeded", "Graph model request exceeds its total capacity limit")


def _validate_limits(limits: dict) -> dict:
    _require(type(limits) is dict and set(limits) == {"max_model_requests", "max_model_attempts"}
             and type(limits["max_model_requests"]) is int and 1 <= limits["max_model_requests"] <= 64
             and type(limits["max_model_attempts"]) is int and 1 <= limits["max_model_attempts"] <= 256,
             "Graph Agent execution budget is invalid")
    return copy.deepcopy(limits)


@dataclass
class _Workspace:
    identity: GraphAgentIdentity
    snapshot: dict
    binding: dict
    limits: dict
    active: ActiveRun
    status: str = "prepared"
    checkpoint: KernelCheckpoint | None = None
    facts: list[dict] = field(default_factory=list)
    accepted: dict | None = None
    finalizing: bool = False
    executing: bool = False


class GraphAgentRuntime:
    """Own native same-process resume state without owning graph persistence."""
    def __init__(self, adapter_factory: Callable[[dict], Any], *,
                 tools: tuple[RegisteredTool, ...] | None = None, kernel: Any = None):
        _require(callable(adapter_factory), "Graph Agent requires an adapter factory")
        self._adapter_factory = adapter_factory
        self.tools = graph_agent_tools() if tools is None else tools
        self._kernel = SnapshotKernel() if kernel is None else kernel
        self._workspaces: dict[str, _Workspace] = {}
        self._lock = RLock()

    def execute(self, *, identity: GraphAgentIdentity, snapshot: dict,
                model_binding: dict, limits: dict, callbacks: GraphAgentCallbacks) -> GraphAgentAccepted:
        snapshot, binding = validate_record("input_snapshot", snapshot), validate_model_binding(model_binding)
        limits = _validate_limits(limits)
        _require(snapshot["workflow_session_id"] == identity.workflow_session_id
                 and snapshot["node_binding_id"] == identity.node_binding_id
                 and snapshot["config"]["payload"].get("graph_identity") == asdict(identity)
                 and snapshot["model_parameters"] == binding["parameters"]
                 and snapshot["config"]["payload"].get("model_binding") == binding,
                 "Graph Agent execution differs from frozen ownership or model")
        with self._lock:
            workspace = self._workspaces.get(identity.node_run_id)
            if workspace is None:
                callbacks.prepare(copy.deepcopy(snapshot))
                workspace = _Workspace(identity, snapshot, binding, limits, ActiveRun(
                    identity.node_run_id, identity.chain_run_id,
                    identity.workflow_session_id, identity.base_commit_id,
                ))
                self._workspaces[identity.node_run_id] = workspace
            else:
                _require(workspace.identity == identity and workspace.snapshot == snapshot
                         and workspace.binding == binding and workspace.limits == limits,
                         "Graph Agent resume changed frozen execution")
                _require(workspace.status in {"paused", "budget_exhausted"}
                         and workspace.checkpoint is not None, "Graph Agent run cannot resume")
                self._kernel.validate_checkpoint(workspace.checkpoint, workspace.snapshot)
            _require(not workspace.executing, "Graph Agent run already has an executor")
            workspace.executing, workspace.status = True, "running"
            generation = workspace.active.snapshot().generation

        def progress(event: dict) -> None:
            with self._lock:
                view = workspace.active.snapshot()
                _require(view.generation == generation, "Stale Agent progress generation")
                counters = AcceptedProgress(**event)
                if counters != view.accepted_progress:
                    workspace.active.accept_progress(counters, expected_generation=generation,
                                                     expected_revision=view.revision)
            callbacks.progress(copy.deepcopy(event))

        def boundary(kind: str) -> bool:
            with self._lock:
                if workspace.active.snapshot().generation != generation or callbacks.should_pause():
                    return False
                if kind == "before_final":
                    workspace.finalizing = True
                return True

        def tool_event(kind: str, call_id: str, execution_id: str | None, outcome: str | None) -> None:
            with self._lock:
                view = workspace.active.snapshot()
                if view.generation != generation:
                    raise KernelPauseRequested()
                guard = {"expected_generation": generation, "expected_revision": view.revision}
                if kind == "queue":
                    workspace.active.queue_tool(call_id, **guard)
                elif kind == "start":
                    if not workspace.finalizing and callbacks.should_pause():
                        raise KernelPauseRequested()
                    workspace.active.start_tool(call_id, execution_id, **guard)
                elif kind == "settle":
                    current = next((tool for tool in view.tools if tool.tool_call_id == call_id), None)
                    _require(current is not None and current.tool_execution_id == execution_id,
                             "Agent tool settlement differs from dispatch")
                    workspace.active.settle_tool(call_id, outcome, **guard)
                elif kind == "skip":
                    _require(execution_id is None and outcome == "never_started", "Invalid skipped Agent tool")
                    workspace.active.skip_tool(call_id, **guard)
                else:
                    raise ContractValidationError("Unknown Agent tool callback")

        def fact(event: dict) -> None:
            with self._lock:
                _require(workspace.active.snapshot().generation == generation, "Stale Agent fact generation")
                _require(type(event) is dict and set(event) == {"kind", "payload"}, "Invalid Agent fact callback")
                record = {
                    "schema_version": 1, "fact_id": str(uuid4()), "run_id": identity.node_run_id,
                    "chain_run_id": identity.chain_run_id, "workflow_session_id": identity.workflow_session_id,
                    "node_binding_id": identity.node_binding_id, "snapshot_id": snapshot["snapshot_id"],
                    "generation": generation, "sequence": len(workspace.facts) + 1,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "kind": event["kind"], "payload": copy.deepcopy(event["payload"]),
                }
                _validated_facts([*workspace.facts, record], workspace.snapshot, identity)
                callbacks.fact(copy.deepcopy(record))
                workspace.facts.append(record)

        model = None
        try:
            model = self._adapter_factory(copy.deepcopy(binding))
            kernel_snapshot = copy.deepcopy(snapshot)
            result = self._kernel.run(
                kernel_snapshot, self.tools, model, **workspace.limits, checkpoint=workspace.checkpoint,
                on_progress=progress, on_boundary=boundary, on_tool_event=tool_event, on_fact=fact,
            )
            _require(canonical_bytes(kernel_snapshot) == canonical_bytes(snapshot),
                     "Agent kernel modified its frozen snapshot")
            view = workspace.active.snapshot()
            _require(workspace.finalizing and not view.pending_tools, "Agent result has no accepted final boundary")
            turn = validate_record("turn", {
                "schema_version": 1, "turn_id": str(uuid4()), "parent_turn_id": snapshot["parent_turn_id"],
                "run_id": identity.node_run_id, "snapshot_id": snapshot["snapshot_id"],
                "input_id": snapshot["input_id"], "messages": result.messages,
                "final": result.final, "projection_version": snapshot["projection_version"],
            })
            accepted = validate_graph_agent_accepted({
                "schema_version": 1, "kind": "workflow.agent-accepted", "identity": asdict(identity),
                "snapshot": snapshot, "turn": turn, "facts": workspace.facts,
                "limits": workspace.limits, "progress": asdict(view.accepted_progress),
            })
            _require(view.accepted_progress == AcceptedProgress(result.model_requests, result.attempts,
                                                               len(result.messages)),
                     "Agent result counters differ from accepted progress")
        except KernelPaused as exc:
            self._suspend(workspace, exc.checkpoint, "paused")
            raise GraphAgentSuspended("paused", "agent_paused") from None
        except RunFailed as exc:
            if isinstance(exc.__cause__, PromptProcessingError):
                workspace.status = "failed"
                raise KernelContractError(exc.__cause__.code, exc.messages, exc.model_requests,
                                          exc.attempts) from exc.__cause__
            if exc.code in {"model_request_budget_exhausted", "model_attempt_budget_exhausted"} and exc.checkpoint:
                self._suspend(workspace, exc.checkpoint, "budget_exhausted")
                raise GraphAgentSuspended("budget_exhausted", exc.code) from None
            workspace.status = "failed"
            raise
        except BaseException:
            workspace.status = "failed"
            raise
        finally:
            primary = sys.exc_info()[1]
            try:
                if model is not None and callable(getattr(model, "close", None)):
                    model.close()
            except BaseException as cleanup_error:
                workspace.status, workspace.checkpoint = "failed", None
                if primary is not None and not isinstance(primary, Exception):
                    note = getattr(primary, "add_note", None)
                    if callable(note):
                        note("Agent adapter cleanup also failed.")
                else:
                    raise KernelContractError("adapter_close_error", [], 0, 0) from cleanup_error
            finally:
                workspace.executing = False
        workspace.checkpoint, workspace.accepted = None, accepted
        return self.retry_archive(identity.node_run_id, callbacks=callbacks)

    def _suspend(self, workspace: _Workspace, checkpoint: KernelCheckpoint, status: str) -> None:
        with self._lock:
            self._kernel.validate_checkpoint(checkpoint, workspace.snapshot)
            view = workspace.active.snapshot()
            counters = AcceptedProgress(checkpoint.model_requests, checkpoint.attempts, len(checkpoint.messages))
            if counters != view.accepted_progress:
                view = workspace.active.accept_progress(counters, expected_generation=view.generation,
                                                       expected_revision=view.revision)
            workspace.active.fence(expected_generation=view.generation, expected_revision=view.revision)
            workspace.checkpoint, workspace.status = checkpoint, status
            workspace.finalizing = False

    def retry_archive(self, run_id: str, *, callbacks: GraphAgentCallbacks) -> GraphAgentAccepted:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            _require(workspace is not None and workspace.accepted is not None and not workspace.executing,
                     "Agent has no accepted archive package")
            package = validate_graph_agent_accepted(workspace.accepted)
            try:
                callbacks.archive(copy.deepcopy(package))
            except Exception as exc:
                workspace.status = "archive_failed"
                raise GraphAgentSuspended("archive_failed", "agent_archive_failed") from exc
            workspace.status = "succeeded"
            return GraphAgentAccepted(package)

    def extend_budget(self, run_id: str, *, requests: int, attempts: int) -> dict:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            limits = self.preview_budget(run_id, requests=requests, attempts=attempts)
            workspace.limits = limits
            return copy.deepcopy(limits)

    def preview_budget(self, run_id: str, *, requests: int, attempts: int) -> dict:
        """Let the host persist an increment before mutating the local checkpoint."""
        with self._lock:
            workspace = self._workspaces.get(run_id)
            _require(workspace is not None and not workspace.executing
                     and workspace.status in {"paused", "budget_exhausted"} and workspace.checkpoint is not None,
                     "Only a retained Agent checkpoint can receive budget")
            _require(type(requests) is int and type(attempts) is int and requests >= 0 and attempts >= 0
                     and requests + attempts > 0, "Agent budget increments must be nonnegative")
            return _validate_limits({
                "max_model_requests": workspace.limits["max_model_requests"] + requests,
                "max_model_attempts": workspace.limits["max_model_attempts"] + attempts,
            })

    def read_evidence(self, run_id: str) -> dict | None:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            if workspace is None:
                return None
            return {"identity": asdict(workspace.identity), "snapshot": copy.deepcopy(workspace.snapshot),
                    "facts": copy.deepcopy(workspace.facts), "status": workspace.status,
                    "limits": copy.deepcopy(workspace.limits),
                    "progress": asdict(workspace.active.snapshot().accepted_progress),
                    "accepted": copy.deepcopy(workspace.accepted)}

    def has_checkpoint(self, run_id: str) -> bool:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            return workspace is not None and workspace.checkpoint is not None and not workspace.executing

    def has_accepted(self, run_id: str) -> bool:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            return workspace is not None and workspace.accepted is not None and not workspace.executing

    def discard(self, run_id: str) -> None:
        with self._lock:
            workspace = self._workspaces.get(run_id)
            _require(workspace is None or not workspace.executing, "Cannot discard a live Agent executor")
            self._workspaces.pop(run_id, None)
