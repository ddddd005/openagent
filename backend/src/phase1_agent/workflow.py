"""A fixed, persisted A -> B -> output workflow for the first local UI.

The coordinator owns dispatch and archive boundaries. Conversation views expose
public messages; explicit workbench reads expose selected closed archives, never
live model snapshots or pending tool outcomes.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from .active_run import AcceptedProgress, ActiveRun
from .bindings import (
    BasicContext, ComponentRegistry, ComponentSelection, WorkflowComponents,
    build_snapshot,
)
from .contract_graph import validate_fork_request, validate_message_history, validate_turn_final
from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, dumps_pretty, loads_strict
from .contracts import ModelResponse, ModelToolCall
from .contracts_v2 import validate_record
from .event_contracts import CORE_EVENT_TYPES, core_payload_schema_ref
from .event_registry import EventBehaviorRegistry, FrozenEventDeclarations, ReportScope
from .execution_recovery import fact_reference, interruption_evidence
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters, create_configured_adapter
from .prompt_store import PromptConfigStore
from .model_configuration import DEFAULT_PROVIDER_ID, default_provider, configuration_error
from .model_configuration_store import ModelConfigurationStore
from .model_selection import (
    resolve_model_selection, validate_model_binding, validate_model_plan,
    validate_model_selection, validate_saved_model_plan, verify_model_binding,
)
from .prepared_context import (
    PREPARATION_CAPABILITY, PREPARED_PROJECTION_VERSION, bind_prompt_config_scope,
    PreparedRequestAdapter, check_prepared_request_capacity, validate_frozen_preparation,
    validate_prompt_registry_owner,
    PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION, register_prepared_context,
)
from .prompt_preparation import validate_context_preparation
from .prompt_selection import (
    compose_selected_prompt_context, make_prompt_selection_plan, resolve_prompt_selection,
    validate_prompt_selection, validate_prompt_selection_plan,
)
from .prompt_variables import create_variable_snapshot
from .variable_preparation import (
    prepare_variable_assignments, rederive_root_variable_assignments,
    validate_frozen_variable_preparation, validate_root_variable_rederivation,
)
from .variable_store import VariableStore
from .preparation_program import (
    merge_program_definitions, program_definitions, validate_typed_value,
    validate_variable_name,
)
from .program_variable_store import ProgramVariableStore, program_variable_error
from .workflow_result_ports import (
    ResultPortStore, make_result_package, make_result_port_routes,
    validate_result_port_routes, make_archive_submission, make_archive_receipt,
)
from .reroll_preparation import (
    inspect_completed_reroll_origin, inspect_inherited_reroll_origin,
    inspect_interrupted_reroll_origin, inspect_closed_reroll_origin,
)
from .runtime import (
    CanonicalModelAdapter, KernelContractError, KernelPaused, KernelPauseRequested,
    RunFailed, SnapshotKernel,
)
from .run_events import EventProjection, RunEventSource
from .storage import ACTIVE_SESSION_SELECTION_ID, SqliteStore
from .tools import final_answer_tool, register_callable
from .workflow_control import (
    BudgetControlRequest, LegacyControlRequest, RunControlRequest, WorkflowOperation,
    control_error,
)
from .workflow_reply_candidates import list_workflow_reply_candidates
from .workflow_operations import (
    accept_retry, dispatch_workflow_operation, operation_store,
)
from .workflow_context_view import (
    SelectedArchiveReader, context_error, materialize_prompt_draft, preview_context,
    validate_context_identity, validate_context_request,
)
from .prompt_errors import PromptProcessingError
from .prompt_assembly import PromptAssemblyLimits
from .workbench_interfaces import WorkbenchInterfaces
from .workbench_resources import WorkbenchResourceStore


AGENT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a101"
OUTPUT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a102"
CONTEXT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a103"
KERNEL_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a104"
ADAPTER_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a105"
WORKFLOW_IDS = {
    "offline": "7be319b8-30bd-4674-b7bf-d1cf54a1a106",
    "deepseek": "7be319b8-30bd-4674-b7bf-d1cf54a1a107",
}
A_BINDING = "7be319b8-30bd-4674-b7bf-d1cf54a1a108"
B_BINDING = "7be319b8-30bd-4674-b7bf-d1cf54a1a109"
OUTPUT_BINDING = "7be319b8-30bd-4674-b7bf-d1cf54a1a10a"
PROMPT_IDS = {
    "A": "7be319b8-30bd-4674-b7bf-d1cf54a1a10b",
    "B": "7be319b8-30bd-4674-b7bf-d1cf54a1a10c",
}
VERSION = "1.0.0"
TEXT_SCHEMA_REF = {"schema_id": "writing_text", "version": 1}
OUTPUT_SCHEMA = {
    "type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"], "additionalProperties": False,
}
PROMPTS = {
    "A": "Create a draft from the user's request. Use inspect_text, then call final_answer with {text: string}.",
    "B": "Revise the complete draft JSON supplied by the upstream node. Use inspect_text, then call final_answer with {text: string}.",
}
_CURRENT_PARENT = object()
_HOST_INTERRUPTION_SIGNALS = (KeyboardInterrupt, SystemExit, asyncio.CancelledError, GeneratorExit)


def _uid() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _config(owner: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"owner_component_id": owner, "schema_version": 1, "payload": payload}


def _record(**values: Any) -> dict[str, Any]:
    return {"schema_version": 1, "created_at": _now(), **values}


def _request_digest(value: Any) -> str:
    return "workflow-op-v1:" + content_digest(value).rsplit(":", 1)[1]


def _variable_ref(state: dict[str, Any]) -> dict[str, Any]:
    return {field: state[field] for field in (
        "workflow_session_id", "workflow_id", "registry_revision", "revision",
    )}


class _DatabaseLease:
    """One live coordinator owns a database, including startup reconciliation."""

    def __init__(self, database: Path):
        self._file = open(str(database) + ".lock", "a+b")
        self._file.seek(0, 2)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise ContractValidationError("Database already has a live workflow owner") from None

    def close(self) -> None:
        self._file.close()


class OfflineAdapter:
    """A deterministic integration fixture, deliberately not a language model."""

    def __init__(self, stage: str):
        self.stage = stage
        self.requests = 0

    def generate(self, messages, tools) -> ModelResponse:
        self.requests += 1
        input_index = next(
            index for index in range(len(messages) - 1, -1, -1)
            if messages[index]["role"] == "user"
            and messages[index]["source"]["kind"] in ("human", "upstream_node")
        )
        payload = loads_strict(messages[input_index]["blocks"][0]["text"])
        text = payload["text"]
        if not any(message["role"] == "tool" for message in messages[input_index + 1:]):
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(_uid(), "inspect_text", dumps_pretty({"text": text})),
            ))
        text = text.strip()
        if self.stage == "A":
            text = "[Offline draft]\n\n" + text
        else:
            text = "[Offline revision]\n\n" + text.removeprefix("[Offline draft]\n\n")
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(_uid(), "final_answer", dumps_pretty({"answer": {"text": text}})),
        ))


class ConversationOutput:
    """Pure I/O component; its local delivery is the durable UI projection."""

    def emit(self, node_input: dict[str, Any], *, config: dict | None = None) -> dict[str, Any]:
        return copy.deepcopy(node_input["payload"])


class _ExecutionPaused(Exception):
    """A worker stopped at an in-process checkpoint, not a failed run."""


class _BehaviorReporter:
    """A producer reports payloads; execution ownership stays with the coordinator."""

    def __init__(self, report, preview):
        self._report, self._preview = report, preview

    def __call__(self, event_type, payload_schema_ref, payload):
        return self._report(event_type, payload_schema_ref, payload)

    def report(self, event_type, payload_schema_ref, payload):
        return self._report(event_type, payload_schema_ref, payload)

    def preview(self, text):
        return self._preview(text)


class WorkflowService(WorkbenchInterfaces):
    def __init__(
        self, database_path: str | Path, mode: str = "offline",
        model_factory: Callable[..., Any] | None = None,
        *, fault_injector: Callable[[str], None] | None = None,
        components: WorkflowComponents | None = None,
        event_source_limit: int = 128,
    ):
        if mode not in WORKFLOW_IDS:
            raise ContractValidationError("Unsupported workflow mode")
        if mode == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
            raise ContractValidationError("DEEPSEEK_API_KEY is required for deepseek mode")
        self.database = Path(database_path).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        if components is not None and not isinstance(components, WorkflowComponents):
            raise ContractValidationError("components must be WorkflowComponents")
        if components is not None:
            if not isinstance(components.registry, ComponentRegistry):
                raise ContractValidationError("components require a ComponentRegistry")
            if ((components.contexts or components.kernels or components.output is not None)
                    and components.workflow_definition_id is None):
                raise ContractValidationError("Custom components require a stable workflow_definition_id")
        self._components = components
        self._definition_id = (
            components.workflow_definition_id
            if components is not None and components.workflow_definition_id is not None
            else WORKFLOW_IDS[mode]
        )
        self._factory = model_factory
        if type(event_source_limit) is not int or not 1 <= event_source_limit <= 512:
            raise ContractValidationError("Event source retention must be between 1 and 512")
        self._event_source_limit = event_source_limit
        self._fault_injector = fault_injector
        self._lock = threading.RLock()
        self._errors: dict[str, dict[str, str]] = {}
        self._execution_failures: dict[str, BaseException] = {}
        self._uncertain_tools: dict[str, list[dict[str, str]]] = {}
        self._progress: dict[str, dict[str, Any]] = {}
        self._active_results: dict[str, Any] = {}
        self._active_runs: dict[str, ActiveRun] = {}
        self._run_checkpoints: dict[str, Any] = {}
        self._fact_sequences: dict[str, int] = {}
        self._event_sources: dict[str, RunEventSource] = {}
        self._event_declarations: dict[str, FrozenEventDeclarations] = {}
        self._event_states: dict[str, dict[str, Any]] = {}
        self._event_generations: dict[str, str] = {}
        self._event_once: dict[tuple[str, str], dict[str, Any]] = {}
        self._event_ephemeral_order: dict[str, list[str]] = {}
        self._event_dedup_limit = 256
        self._event_previews: dict[str, tuple[str, int]] = {}
        self._event_terminal_order: list[str] = []
        self._event_execution_fixed: set[str] = set()
        self._event_registry = (
            components.event_registry if components is not None
            and components.event_registry is not None else EventBehaviorRegistry()
        )
        if not isinstance(self._event_registry, EventBehaviorRegistry):
            raise ContractValidationError("Workflow event registry must be EventBehaviorRegistry")
        self._finalizing_runs: set[str] = set()
        self._interrupting: set[str] = set()
        self._archive_retry_receipts: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._publish_retry_receipts: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._futures: dict[str, Future] = {}
        self._closed = False
        self._callbacks_revoked = False
        self._lease = _DatabaseLease(self.database)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fixed-workflow")
        try:
            self._register_components()
            self._bootstrap()
            self._bootstrap_model_configuration()
            with closing(self._store()) as resources_store:
                WorkbenchResourceStore(resources_store).bootstrap()
            self._recover_unfinished()
        except Exception:
            self._executor.shutdown(wait=True)
            self._lease.close()
            raise

    def _store(self) -> SqliteStore:
        return operation_store(self.database, fault_injector=self._fault_injector)

    def _callback_store(self) -> SqliteStore:
        if self._callbacks_revoked:
            raise ContractValidationError("Workflow callback owner is closed")
        return self._store()

    @staticmethod
    def _find_event_run(store, run_id):
        for kind in ("run_record", "node_run"):
            for run in store.list_records(kind):
                if run["run_id"] == run_id:
                    return run
        raise ContractValidationError("Event target run was not found")

    def _frozen_event_declarations(self, snapshot, producer):
        frozen = snapshot["config"]["payload"].get("run_events")
        if frozen is not None:
            declarations = FrozenEventDeclarations.from_json(frozen)
            if canonical_bytes(declarations.to_json()["producer"]) != canonical_bytes(producer):
                raise ContractValidationError("Frozen event producer is incompatible")
            return declarations
        # Old v1 snapshots explicitly expose coordinator-owned core events only.
        return EventBehaviorRegistry().freeze(producer=producer)

    def _new_event_declarations(self, producer):
        return self._event_registry.freeze(
            producer=producer,
            event_refs=self._registry.event_refs(
                producer["kind"], producer["component_id"], producer["component_version"],
            ),
        )

    def _new_event_plan(self):
        return {
            "schema_version": 1, "kind": "fixed_workflow_event_plan",
            "nodes": {
                stage: self._new_event_declarations(self._resolved[stage][1].descriptor).to_json()
                for stage in ("A", "B")
            },
        }

    @staticmethod
    def _validate_event_plan(plan):
        if (type(plan) is not dict or set(plan) != {"schema_version", "kind", "nodes"}
                or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
                or plan["kind"] != "fixed_workflow_event_plan"
                or type(plan["nodes"]) is not dict or set(plan["nodes"]) != {"A", "B"}):
            raise ContractValidationError("Unsupported frozen workflow event plan")
        for frozen in plan["nodes"].values():
            FrozenEventDeclarations.from_json(frozen)
        return copy.deepcopy(plan)

    def _ensure_event_source(self, store, run):
        if self._callbacks_revoked:
            raise ContractValidationError("Workflow event owner is closed")
        run_id = run["run_id"]
        if run_id in self._event_sources:
            return self._event_sources[run_id]
        session = self._session(store, run["workflow_session_id"])
        binding = store.get_record("node_binding", {
            "workflow_definition_id": session["workflow_definition_id"],
            "workflow_definition_revision": session["definition_revision"],
            "node_binding_id": run["node_binding_id"],
        })
        chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
        scope = ReportScope.from_records(session, binding, run, chain=chain)
        if run["profile"] == "agent":
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
            producer = snapshot["config"]["payload"]["resolved"]["kernel"]
            declarations = self._frozen_event_declarations(snapshot, producer)
        else:
            declarations = EventBehaviorRegistry().freeze(producer=self._output_resolution.descriptor)
        active = self._active_runs.get(run_id)
        generation = active.snapshot().generation if active is not None else _uid()
        self._event_generations[run_id] = generation
        self._event_declarations[run_id] = declarations
        self._event_states[run_id] = scope.to_json()
        source = RunEventSource(
            run["workflow_session_id"], run["node_binding_id"], run_id,
            generation=generation, declarations=declarations, lock=self._lock,
            owner_check=lambda candidate: self._event_generations.get(run_id) == candidate,
            snapshot_provider=lambda: self._event_snapshot(run_id),
        )
        self._event_sources[run_id] = source
        if run_id not in self._active_runs:
            self._release_event_source(run_id)
        return source

    def _seal_observed_terminal_source(self, store, run, source):
        if run["profile"] == "agent":
            completed = (
                run["status"] == "succeeded" and (
                    ResultPortStore(
                        store, allowed_bindings=(A_BINDING, B_BINDING),
                    ).get_release(run["run_id"]) is not None
                    or run["run_id"] not in self._active_results
                )
            )
            if completed or run["status"] in {"closed", "superseded", "recovery_unavailable"}:
                source.seal()
        elif any(
            delivery["output_id"] == run["result_output_id"]
            and delivery["status"] == "succeeded"
            for delivery in store.list_records("output_delivery")
        ):
            source.seal()

    def _event_snapshot(self, run_id):
        """Read coordinator state, never reconstruct history from an event buffer."""
        with self._lock, closing(self._store()) as store:
            run = self._find_event_run(store, run_id)
            view = self._view(store, run["workflow_session_id"])
            latest = bool(view["chains"] and view["chains"][-1]["chain_run_id"] == run["chain_run_id"])
            actions = []
            if latest:
                actions = [
                    "pause" if action == "interrupt" else action
                    for action in view["available_actions"]
                    if action not in {"retry_publish", "continue_pending_input"}
                    and (action not in {"interrupt", "resume", "extend_budget", "retry_archive"}
                         or run["profile"] == "agent" and (
                             action == "interrupt" and run["status"] in {"prepared", "running"}
                             or action in {"resume", "extend_budget"} and run["status"] in {"paused", "failed"}
                             or action == "retry_archive" and run_id in self._active_results
                         ))
                ]
            state = {
                **self._event_states[run_id], "status": run["status"],
                "revision": run["revision"], "available_actions": actions,
                "action_rejections": {},
            }
            if run["profile"] == "agent":
                producer = self._event_declarations[run_id].producer
                builtin = producer["component_id"] == KERNEL_COMPONENT and producer["component_version"] == VERSION
                pause_resume = builtin or "pause_resume" in producer["capabilities"]
                reroll = builtin or "reroll" in producer["capabilities"]
                for action in (
                    "pause", "resume", "extend_budget", "retry_archive",
                    "reroll", "close_execution", "continue_workflow",
                ):
                    if action not in actions:
                        state["action_rejections"][action] = (
                            "unsupported" if action in {"pause", "resume", "extend_budget"}
                            and not pause_resume else "unsupported" if action == "reroll"
                            and not reroll else "invalid_state"
                        )
                facts = store.read_execution_facts(run_id)
                state["budget"] = {
                    **self._budget_limits(run, store.list_records("workflow_operation")),
                    "model_requests": sum(row["kind"] == "model_request" for row in facts),
                    "model_attempts": sum(row["kind"] == "model_attempt_started" for row in facts),
                }
            return state

    def subscribe_run_events(
        self, session_id, run_id, *, projection_ref="workflow.public", cursor=None,
        queue_limit=64, callback=None,
    ):
        """Observe a fixed authorized projection without issuing execution commands."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            run = self._find_event_run(store, run_id)
            if run["workflow_session_id"] != session_id:
                raise ContractValidationError("Event target belongs to another workflow session")
            source = self._ensure_event_source(store, run)
            self._seal_observed_terminal_source(store, run, source)
            public_types = {
                "run_started", "run_state", "command_result", "budget_changed",
                "workflow_operation_result",
                "final_ready", "archive_result", "ports_released", "workflow_published",
                "preview_delta", "preview_cleared",
            }
            if projection_ref not in {"workflow.public", "workflow.monitor"}:
                raise control_error("Event projection is not authorized", "unsupported")
            event_types = CORE_EVENT_TYPES if projection_ref == "workflow.monitor" else public_types
            from .event_contracts import CORE_PAYLOAD_SCHEMAS
            allowlist = {
                event_type: tuple(CORE_PAYLOAD_SCHEMAS[event_type]["properties"])
                for event_type in event_types
            }
            if projection_ref == "workflow.monitor":
                allowlist = {event_type: None for event_type in event_types}
                frozen = self._event_declarations[run_id].to_json()
                for behavior in frozen["behaviors"]:
                    allowlist[behavior["event_type"]] = None
            projection = EventProjection(
                projection_ref, "1", allowlist=allowlist,
                snapshot_fields=(
                    "workflow_session_id", "node_binding_id", "run_id", "chain_run_id",
                    "workflow_definition_id", "workflow_definition_revision", "status", "revision",
                    "available_actions", "action_rejections", "budget",
                ),
                include_private=projection_ref == "workflow.monitor",
            )
            return source.subscribe(
                projection, cursor=cursor, queue_limit=queue_limit, callback=callback,
            )

    def _publish_event(self, run_id, event_type, payload, *, once=None, visibility="private"):
        if self._callbacks_revoked:
            raise ContractValidationError("Workflow event owner is closed")
        source = self._event_sources.get(run_id)
        if source is None:
            with closing(self._store()) as store:
                source = self._ensure_event_source(store, self._find_event_run(store, run_id))
        key = (run_id, once) if once is not None else None
        if key is not None and key in self._event_once:
            event = self._event_once[key]
            if canonical_bytes(event["payload"]) != canonical_bytes(payload):
                raise ContractValidationError("Event milestone identity was reused with different content")
            return copy.deepcopy(event)
        event = source.publish(
            event_type, payload, payload_schema_ref=core_payload_schema_ref(event_type),
            generation=self._event_generations[run_id], visibility=visibility,
        )
        if key is not None and event is not None:
            self._event_once[key] = copy.deepcopy(event)
            if once.startswith(("state:", "command:", "budget:")):
                order = self._event_ephemeral_order.setdefault(run_id, [])
                order.append(once)
                while len(order) > self._event_dedup_limit:
                    self._event_once.pop((run_id, order.pop(0)), None)
        return event

    def _publish_run_state(self, run, previous_status, *, phase=None, reason_code=None):
        once = "state:" + str(run["revision"]) + ":" + (phase or "")
        previous = self._event_once.get((run["run_id"], once))
        if previous is not None:
            return copy.deepcopy(previous)
        state = self._event_snapshot(run["run_id"])
        return self._publish_event(run["run_id"], "run_state", {
            "previous_status": previous_status, "status": run["status"], "phase": phase,
            "available_actions": state["available_actions"],
            "action_rejections": state["action_rejections"], "reason_code": reason_code,
        }, once=once, visibility="business_candidate")

    def _publish_control_event(self, run, operation):
        self._publish_event(run["run_id"], "workflow_operation_result", {
            "operation_id": operation["operation_id"], "operation_kind": operation["kind"],
            "target": {field: run[field] for field in ("workflow_session_id", "node_binding_id", "run_id")},
            "status": "accepted", "reason_code": None,
        }, once="command:" + operation["operation_id"], visibility="business_candidate")

    def _sync_event_generation(self, run_id):
        active = self._active_runs.get(run_id)
        source = self._event_sources.get(run_id)
        if active is not None and source is not None:
            generation = active.snapshot().generation
            self._event_generations[run_id] = generation
            source.advance_generation(generation)

    def _revoke_failed_callbacks(self, run_id, code, category):
        active = self._active_runs.get(run_id)
        if active is not None:
            view = active.snapshot()
            active.fence(expected_generation=view.generation, expected_revision=view.revision)
            self._sync_event_generation(run_id)
            self._clear_event_preview(run_id, "execution_failed")
        source = self._event_sources.get(run_id)
        if source is not None and not source.sealed:
            self._publish_event(run_id, "diagnostic", {"code": code, "category": category})

    def _clear_event_preview(self, run_id, reason):
        preview = self._event_previews.pop(run_id, None)
        if preview is not None:
            self._publish_event(run_id, "preview_cleared", {
                "preview_id": preview[0], "reason_code": reason,
            }, visibility="business_candidate")

    def _report_behavior(self, run_id, generation, event_type, payload_schema_ref, payload):
        with self._lock:
            if self._callbacks_revoked:
                return None
            active = self._active_runs.get(run_id)
            if active is None or active.snapshot().generation != generation:
                return None
            with closing(self._store()) as store:
                run = self._find_event_run(store, run_id)
                chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
                if run["status"] not in {"running", "pausing"} or chain["status"] not in {"running", "paused"}:
                    return None
                return self._event_sources[run_id].report(
                    event_type, payload, payload_schema_ref=payload_schema_ref,
                    generation=generation,
                )

    def _report_preview(self, run_id, generation, text):
        with self._lock:
            if self._callbacks_revoked:
                return None
            active = self._active_runs.get(run_id)
            if active is None or active.snapshot().generation != generation:
                return None
            with closing(self._store()) as store:
                run = self._find_event_run(store, run_id)
            if run["status"] != "running" or run_id in self._interrupting:
                return None
            capabilities = self._event_declarations[run_id].producer["capabilities"]
            if "stream_preview" not in capabilities:
                raise control_error("Kernel does not support preview reporting", "unsupported")
            preview_id, index = self._event_previews.get(run_id, (_uid(), 0))
            event = self._publish_event(run_id, "preview_delta", {
                "preview_id": preview_id, "fragment_index": index + 1, "text": text,
            }, visibility="business_candidate")
            if event is not None:
                self._event_previews[run_id] = (preview_id, index + 1)
            return event

    def _release_event_source(self, run_id):
        if run_id not in self._event_terminal_order:
            self._event_terminal_order.append(run_id)
        while len(self._event_terminal_order) > self._event_source_limit:
            expired = self._event_terminal_order.pop(0)
            source = self._event_sources.pop(expired, None)
            if source is not None:
                source.close()
            self._event_states.pop(expired, None)
            self._event_declarations.pop(expired, None)
            self._event_generations.pop(expired, None)
            self._event_execution_fixed.discard(expired)
            self._event_ephemeral_order.pop(expired, None)
            self._event_once = {key: value for key, value in self._event_once.items() if key[0] != expired}

    def dispatch_operation(self, operation: dict[str, Any] | WorkflowOperation) -> dict[str, Any]:
        """Validate and dispatch a public v1 command through this workflow owner."""
        return dispatch_workflow_operation(self, operation)

    def _bootstrap_model_configuration(self) -> None:
        with closing(self._store()) as store:
            catalog = ModelConfigurationStore(store)
            if catalog.get_current("provider", DEFAULT_PROVIDER_ID) is None:
                catalog.write(
                    "provider", default_provider(), expected_revision=0,
                    idempotency_key="bootstrap-chat-provider-v1",
                )

    def list_model_providers(self) -> list[dict[str, Any]]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return ModelConfigurationStore(store).list_providers()

    def get_model_configuration(
        self, kind: str, identity: str, version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            catalog = ModelConfigurationStore(store)
            record = (catalog.get_current(kind, identity) if version is None
                      else catalog.get_revision(kind, identity, version))
            if record is None:
                raise configuration_error("not_found", 404)
            return record

    def save_model_configuration(self, kind: str, **request) -> dict[str, Any]:
        """Persist configuration only; do not dispatch or change active runs."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return ModelConfigurationStore(store).write(kind, **request)

    def diagnose_model_configuration(self, identity: str, version: int) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            catalog = ModelConfigurationStore(store)
            record = catalog.get_revision("model", identity, version)
            if record is None:
                raise configuration_error("not_found", 404)
            return {
                "config_id": identity, "revision": version,
                "diagnostics": catalog.diagnostics(record),
                "execution_supported": not record["nodes"] or self._supports_selected_model(),
            }

    def _supports_selected_model(self):
        if self._factory is None:
            return self.mode == "deepseek"
        try:
            inspect.signature(self._factory).bind("A", {}, {})
        except (TypeError, ValueError):
            return False
        return True

    def save_prompt_config(
        self, kind: str, record: dict[str, Any], *,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        """Save a catalog revision without selecting or dispatching a workflow."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return PromptConfigStore(store).write_revision(
                kind, record, expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    def list_prompt_configs(self, kind: str) -> list[dict[str, Any]]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return PromptConfigStore(store).list_current(kind)

    @staticmethod
    def _require_prompt_value(value):
        if value is None:
            error = ContractValidationError("Prompt catalog reference was not found")
            error.status_code = 404
            error.reason_code = "not_found"
            raise error
        return value

    def get_prompt_config_head(self, kind: str, identifier: str) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._require_prompt_value(PromptConfigStore(store).get_head(kind, identifier))

    def get_prompt_config_revision(
        self, kind: str, identifier: str, revision: int,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._require_prompt_value(
                PromptConfigStore(store).get_revision(kind, identifier, revision),
            )

    def delete_prompt_config(
        self, kind: str, identifier: str, *,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return PromptConfigStore(store).delete(
                kind, identifier, expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    def resolve_prompt_config(self, config_id: str, revision: int) -> dict[str, Any]:
        """Resolve exact configuration references, not model messages or live values."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            catalog = PromptConfigStore(store)
            config = self._require_prompt_value(catalog.get_revision("config", config_id, revision))
            items = catalog.resolve_config(config_id, revision)
            return {"config": config, "items": items}

    def _register_components(self) -> None:
        self._registry = (
            self._components.registry.detached()
            if self._components is not None else ComponentRegistry()
        )
        self._registry.register("context", CONTEXT_COMPONENT, VERSION, BasicContext(),
            capabilities=frozenset({"sources", "paired_history", "frozen_projection"}),
            config_schema=_object_schema({
                "prompt_id": {"type": "string", "format": "uuid"},
                "prompt_revision": {"type": "integer", "minimum": 1},
                "system_prompt": {"type": "string", "minLength": 1},
            }))
        self._registry.register("kernel", KERNEL_COMPONENT, VERSION, SnapshotKernel(),
            capabilities=frozenset({"json_final", "paired_tools", "bounded_retry"}),
            config_schema=_object_schema({
                "max_model_requests": {"type": "integer", "minimum": 1, "maximum": 8},
                "max_model_attempts": {"type": "integer", "minimum": 1, "maximum": 32},
                "max_automatic_retries": {"const": 3},
            }))
        self._registry.register("adapter", ADAPTER_COMPONENT, VERSION, self._new_adapter,
            capabilities=frozenset({"canonical_messages", "non_streaming", "native_tools"}),
            config_schema={**_object_schema({
                "model": {"type": "string", "minLength": 1},
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
                "thinking": {"const": "disabled"}, "stream": {"const": False},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            }), "required": ["model", "thinking", "stream"]})
        self._registry.register("io", OUTPUT_COMPONENT, VERSION, ConversationOutput(),
            capabilities=frozenset({"structured_io"}), config_schema=_object_schema({}))
        if not self._registry.is_registered("context", PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION):
            register_prepared_context(self._registry)
        self._resolved = {}
        for stage in ("A", "B"):
            context_config = {"prompt_id": PROMPT_IDS[stage], "prompt_revision": 1, "system_prompt": PROMPTS[stage]}
            kernel_config = {
                "max_model_requests": 8, "max_model_attempts": 32,
                "max_automatic_retries": 3,
            }
            adapter_config = {
                "model": "offline_fixture" if self.mode == "offline" else "deepseek-flash",
                "max_tokens": 2048, "thinking": "disabled", "stream": False,
            }
            context = self._registry.resolve("context", CONTEXT_COMPONENT, VERSION,
                required_capabilities=("sources", "paired_history", "frozen_projection"), config=_config(CONTEXT_COMPONENT, context_config))
            kernel = self._registry.resolve("kernel", KERNEL_COMPONENT, VERSION,
                required_capabilities=("json_final", "paired_tools", "bounded_retry"), config=_config(KERNEL_COMPONENT, kernel_config))
            adapter = self._registry.resolve("adapter", ADAPTER_COMPONENT, VERSION,
                required_capabilities=("canonical_messages", "native_tools", "non_streaming"), config=_config(ADAPTER_COMPONENT, adapter_config))
            if self._components is not None:
                for selections in (self._components.contexts, self._components.kernels):
                    if not set(selections) <= {"A", "B"}:
                        raise ContractValidationError("Component selections require A or B binding keys")
                if stage in self._components.contexts:
                    context = self._select_component(
                        "context", self._components.contexts[stage],
                        ("sources", "paired_history", "frozen_projection"),
                    )
                    context_config = copy.deepcopy(context.config["payload"])
                if stage in self._components.kernels:
                    kernel = self._select_component(
                        "kernel", self._components.kernels[stage],
                        ("json_final", "paired_tools", "bounded_retry"),
                    )
                    kernel_config = copy.deepcopy(kernel.config["payload"])
            context.require_api()
            kernel.require_api()
            payload = {
                "context": context_config, "kernel": kernel_config, "adapter": adapter_config,
                "resolved": {"context": context.descriptor, "kernel": kernel.descriptor,
                             "adapter": adapter.descriptor},
            }
            if self._components is not None and stage in self._components.kernels:
                payload["budget"] = {
                    "max_model_requests": 8, "max_model_attempts": 32, "max_automatic_retries": 3,
                }
            self._resolved[stage] = (context, kernel, adapter, {
                "owner_component_id": AGENT_COMPONENT, "schema_version": 1,
                "payload": payload,
            })
        self._output_resolution = (
            self._select_component("io", self._components.output, ("structured_io",))
            if self._components is not None and self._components.output is not None
            else self._registry.resolve("io", OUTPUT_COMPONENT, VERSION,
                required_capabilities=("structured_io",), config=_config(OUTPUT_COMPONENT, {}))
        )
        self._output_resolution.require_api()
        if "events" in self._output_resolution.descriptor["capabilities"]:
            raise control_error(
                "The fixed Output port does not support live behavior reporting", "unsupported",
            )
        self._output = self._output_resolution.implementation

    def _select_component(self, kind, selection, required):
        if not isinstance(selection, ComponentSelection):
            raise ContractValidationError("Component selection must be ComponentSelection")
        return self._registry.resolve(
            kind, selection.component_id, selection.version, config=selection.config,
            required_capabilities=(*required, *selection.required_capabilities),
        )

    def _snapshot_components(self, snapshot, *, store=None):
        """Resolve the saved exact combination, never a mutable stage selection."""
        frozen = validate_record("input_snapshot", snapshot)
        check_prepared_request_capacity(frozen)
        payload = frozen["config"]["payload"]
        if "prompt_selection_plan" in payload:
            validate_prompt_selection_plan(payload["prompt_selection_plan"])
        if "model_selection_plan" in payload:
            if store is not None:
                validate_saved_model_plan(payload["model_selection_plan"], ModelConfigurationStore(store))
            else:
                with closing(self._store()) as model_store:
                    validate_saved_model_plan(payload["model_selection_plan"], ModelConfigurationStore(model_store))
        if "model_binding" in payload:
            binding = validate_model_binding(payload["model_binding"])
            if binding["parameters"] != payload["adapter"]:
                raise ContractValidationError("Frozen provider parameters disagree with adapter config")
            plan = payload.get("model_selection_plan")
            stage = "A" if frozen["node_binding_id"] == A_BINDING else "B"
            if plan is None or plan["nodes"].get(stage) != binding:
                raise ContractValidationError("Frozen provider differs from the complete model plan")
        if "result_port_routes" in payload:
            routes = validate_result_port_routes(
                payload["result_port_routes"], allowed_bindings=(A_BINDING, B_BINDING),
            )
            if routes["node_binding_id"] != frozen["node_binding_id"]:
                raise ContractValidationError("Frozen result routes belong to another node")
        if PREPARATION_CAPABILITY in payload["resolved"]["context"]["capabilities"]:
            validate_prompt_registry_owner(
                payload["context"], workflow_definition_id=self._definition_id, definition_revision=1,
            )
            if payload["context"]["schema_version"] == 2:
                if store is None:
                    with closing(self._store()) as variable_store:
                        self._validate_saved_variable_evidence(variable_store, frozen)
                else:
                    self._validate_saved_variable_evidence(store, frozen)
            elif payload["context"]["schema_version"] == 3:
                if store is None:
                    with closing(self._store()) as variable_store:
                        self._validate_saved_program_evidence(variable_store, frozen)
                else:
                    self._validate_saved_program_evidence(store, frozen)
        if (FrozenModelParameters.from_mapping(frozen["model_parameters"])
                != FrozenModelParameters.from_mapping(payload["adapter"])):
            raise ContractValidationError("Frozen model settings disagree with component config")
        components = []
        for kind, required in (
            ("context", ("sources", "paired_history", "frozen_projection")),
            ("kernel", ("json_final", "paired_tools", "bounded_retry")),
            ("adapter", ("canonical_messages", "native_tools", "non_streaming")),
        ):
            descriptor = payload["resolved"][kind]
            private_config = copy.deepcopy(payload[kind])
            if (kind == "kernel" and descriptor["component_id"] == KERNEL_COMPONENT
                    and "max_model_attempts" not in private_config):
                private_config["max_model_attempts"] = private_config["max_model_requests"] * 4
            resolved = self._registry.resolve(
                kind, descriptor["component_id"], descriptor["component_version"],
                config=_config(descriptor["component_id"], private_config),
                required_capabilities=required,
            )
            if canonical_bytes(resolved.descriptor) != canonical_bytes(descriptor):
                raise ContractValidationError("Frozen component descriptor is incompatible")
            resolved.require_api()
            components.append(resolved)
        if "run_events" in payload:
            declarations = FrozenEventDeclarations.from_json(payload["run_events"])
            if canonical_bytes(declarations.to_json()["producer"]) != canonical_bytes(components[1].descriptor):
                raise ContractValidationError("Frozen event producer differs from the selected kernel")
        if "run_event_plan" in payload:
            self._validate_event_plan(payload["run_event_plan"])
        return (*components, copy.deepcopy(frozen["config"]))

    @staticmethod
    def _validate_saved_program_evidence(store, frozen):
        evidence = frozen["config"]["payload"]["program_variable_preparation"]
        variables = ProgramVariableStore(store)
        saved = variables.read(evidence["session_id"], evidence["revision"])
        prepared = frozen["config"]["payload"]["context_preparation"]["program"]
        expected = {**prepared["state"], "revision": evidence["revision"]}
        if canonical_bytes(saved) != canonical_bytes(expected):
            raise ContractValidationError("Frozen program values differ from their saved state")
        row = store._connection.execute(
            "SELECT digest,payload FROM program_variable_receipts WHERE idempotency_key=?",
            (evidence["receipt_key"],),
        ).fetchone()
        if row is None:
            raise ContractValidationError("Frozen program variable receipt is missing")
        receipt = loads_strict(row["payload"])
        if receipt != {"session_id": evidence["session_id"], "state": saved}:
            raise ContractValidationError("Frozen program receipt differs from its saved state")
        preparation = frozen["config"]["payload"]["context_preparation"]
        expected_request = {
            "kind": "run_preparation", "session_id": evidence["session_id"],
            "input_id": preparation["node_input"]["input_id"],
            "program_digest": prepared["evidence_digest"],
        }
        if row["digest"] != content_digest(expected_request):
            raise ContractValidationError("Frozen program receipt has another processing proof")

    @staticmethod
    def _validate_saved_variable_evidence(store, frozen):
        evidence = frozen["config"]["payload"]["variable_preparation"]
        variables = VariableStore(store)
        receipt = variables.read_preparation(evidence["receipt_key"])
        if receipt is None or _variable_ref(receipt["after"]) != evidence["state_ref"]:
            raise ContractValidationError("Frozen variables have no matching durable preparation receipt")
        if receipt["request"].get("preparation_digest") != content_digest({
            "frozen": evidence["frozen"], "rederivation": evidence["rederivation"],
        }):
            raise ContractValidationError("Frozen variable rules differ from their durable preparation proof")
        if receipt["request"]["operation"] == "prepare":
            if canonical_bytes(receipt["request"]["assignments"]) != canonical_bytes(
                evidence["frozen"]["resolved_assignments"],
            ):
                raise ContractValidationError("Frozen assignments differ from the saved preparation request")
            basis = variables.snapshot_from_state(
                receipt["before"], node_binding_id=frozen["node_binding_id"],
            )
            if canonical_bytes(basis) != canonical_bytes(evidence["frozen"]["basis_snapshot"]):
                raise ContractValidationError("Frozen variable basis differs from the saved predecessor")
        elif receipt["request"]["operation"] == "prepare_snapshot":
            if canonical_bytes(receipt["request"]["snapshot"]) != canonical_bytes(
                frozen["config"]["payload"]["context"]["variables"],
            ):
                raise ContractValidationError("Frozen variable materialization differs from its preparation receipt")
        else:
            raise ContractValidationError("Frozen variable Context has no valid preparation operation")
        saved = variables.read_snapshot(evidence["state_ref"], node_binding_id=frozen["node_binding_id"])
        expected = frozen["config"]["payload"]["context"]["variables"]
        if canonical_bytes(saved) != canonical_bytes(expected):
            raise ContractValidationError("Frozen variables differ from the saved immutable state")
        seed = evidence["seed"]
        if seed is not None and variables.get_bound_state_ref("workflow_commit", seed["commit_id"]) != seed["state_ref"]:
            raise ContractValidationError("Frozen variable seed differs from its stable commit")
    def _kernel_supports(self, run, capability):
        with closing(self._store()) as store:
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
        _, kernel, _, _ = self._snapshot_components(snapshot)
        descriptor = kernel.descriptor
        if descriptor["component_id"] == KERNEL_COMPONENT and descriptor["component_version"] == VERSION:
            return capability in ("pause_resume", "reroll")
        return capability in descriptor["capabilities"]

    def _emit_output(self, node_input):
        value = self._output_resolution.implementation.emit(
            copy.deepcopy(node_input), config=copy.deepcopy(self._output_resolution.config["payload"]),
        )
        try:
            Draft202012Validator(OUTPUT_SCHEMA).validate(value)
        except ValidationError:
            raise ContractValidationError("Output component violated its structured I/O schema") from None
        return copy.deepcopy(value)

    def _verify_model_binding(self, binding):
        with closing(self._store()) as store:
            return verify_model_binding(binding, ModelConfigurationStore(store))

    def _verify_model_plan(self, plan, *, store=None):
        if plan is not None:
            if store is not None:
                catalog = ModelConfigurationStore(store)
                for binding in validate_saved_model_plan(plan, catalog)["nodes"].values():
                    verify_model_binding(binding, catalog)
                return
            with closing(self._store()) as model_store:
                catalog = ModelConfigurationStore(model_store)
                for binding in validate_saved_model_plan(plan, catalog)["nodes"].values():
                    verify_model_binding(binding, catalog)

    def _new_adapter(self, stage: str, model_parameters, *, model_binding=None):
        parameters = FrozenModelParameters.from_mapping(model_parameters)
        provider = None
        credential = None
        guard = None
        if model_binding is not None:
            binding = validate_model_binding(model_binding)
            if dict(parameters.as_mapping()) != binding["parameters"]:
                raise ContractValidationError("Model provider disagrees with frozen settings")
            credential = self._verify_model_binding(binding)
            provider = binding["provider"]
            guard = lambda: self._verify_model_binding(binding)
            if self.mode == "offline" and self._factory is None:
                raise configuration_error("offline_model_unsupported", 409)
        if self._factory is not None:
            model = create_configured_adapter(
                self._factory, stage, parameters,
                legacy_defaults=FrozenModelParameters(
                    "offline_fixture" if self.mode == "offline" else "deepseek-flash", 2048,
                ),
                provider=provider,
            )
            try:
                return FrozenConfiguredAdapter(
                    model, parameters, dispatch_guard=guard,
                    provider_address=provider["base_url"] if provider is not None else None,
                )
            except ContractValidationError as error:
                close = getattr(model, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as cleanup_error:
                        raise error from cleanup_error
                raise error
        if self.mode == "offline":
            if parameters != FrozenModelParameters("offline_fixture", 2048):
                raise ContractValidationError("Offline fixture supports only fixed default model settings")
            return FrozenConfiguredAdapter(OfflineAdapter(stage), parameters)
        from .adapter import DeepSeekAdapter
        return FrozenConfiguredAdapter(CanonicalModelAdapter(DeepSeekAdapter(
            api_key=credential if provider is not None else os.environ["DEEPSEEK_API_KEY"],
            model=parameters.model,
            max_tokens=parameters.max_tokens, temperature=parameters.temperature, max_retries=0,
            **({"base_url": provider["base_url"]} if provider is not None else {}),
        )), parameters, dispatch_guard=guard,
            provider_address=provider["base_url"] if provider is not None else None)

    @staticmethod
    def _tools():
        def inspect_text(text: str) -> dict[str, Any]:
            return {"characters": len(text), "lines": len(text.splitlines())}
        tool = register_callable("inspect_text", "Count characters and lines in a text.", {
            "type": "object", "properties": {"text": {"type": "string", "description": "Text to inspect"}},
            "required": ["text"], "additionalProperties": False,
        }, inspect_text)
        return (tool, final_answer_tool())

    def _bootstrap(self) -> None:
        ports = [
            {"port_id": "request", "direction": "input", "schema_ref": TEXT_SCHEMA_REF, "required": True},
            {"port_id": "reply", "direction": "output", "schema_ref": TEXT_SCHEMA_REF},
        ]
        self._agent_definition = {"schema_version": 1, "component_id": AGENT_COMPONENT,
            "component_version": VERSION, "kind": "agent", "capabilities": ["json_final"], "ports": ports}
        output_descriptor = self._output_resolution.descriptor
        io_definition = {**self._agent_definition,
                         "component_id": output_descriptor["component_id"],
                         "component_version": output_descriptor["component_version"],
                         "kind": "io", "capabilities": copy.deepcopy(output_descriptor["capabilities"])}
        bindings = []
        for stage, identity in (("A", A_BINDING), ("B", B_BINDING), ("output", OUTPUT_BINDING)):
            bindings.append({"schema_version": 1, "node_binding_id": identity,
                "workflow_definition_id": self._definition_id, "workflow_definition_revision": 1,
                "component_id": output_descriptor["component_id"] if stage == "output" else AGENT_COMPONENT,
                "component_version": output_descriptor["component_version"] if stage == "output" else VERSION,
                "config": self._resolved[stage][3] if stage != "output" else self._output_resolution.config})
        definition = {"schema_version": 1, "workflow_definition_id": self._definition_id,
            "revision": 1, "bindings": [A_BINDING, B_BINDING, OUTPUT_BINDING], "edges": [
                {"from_node_binding_id": first, "from_port_id": "reply", "to_node_binding_id": second,
                 "to_port_id": "request"} for first, second in ((A_BINDING, B_BINDING), (B_BINDING, OUTPUT_BINDING))
            ]}
        with closing(self._store()) as store:
            sessions = store.list_records("workflow_session")
            if any(row["workflow_definition_id"] != self._definition_id for row in sessions):
                raise ContractValidationError("Use a separate database for another workflow mode")
            existing_bindings = {
                row["node_binding_id"]: row for row in store.list_records("node_binding")
            }
            for index, binding in enumerate(bindings):
                previous = existing_bindings.get(binding["node_binding_id"])
                if previous is None or binding["node_binding_id"] == OUTPUT_BINDING:
                    continue
                legacy = copy.deepcopy(binding)
                kernel_budget = legacy["config"]["payload"].get(
                    "budget", legacy["config"]["payload"]["kernel"],
                )
                if kernel_budget["max_model_attempts"] == kernel_budget["max_model_requests"] * 4:
                    kernel_budget.pop("max_model_attempts")
                    if canonical_bytes(previous) == canonical_bytes(legacy):
                        # Keep the original definition and bootstrap receipt immutable.
                        bindings[index] = previous
            store.save_bundle([("node_definition", self._agent_definition), ("node_definition", io_definition),
                ("workflow_definition_revision", definition), *[("node_binding", row) for row in bindings]],
                "fixed-workflow:" + self.mode, operation="workflow.bootstrap")
            if sessions and self._session_selection(store) is None:
                store.save_bundle([("session_selection", _record(
                    session_selection_id=ACTIVE_SESSION_SELECTION_ID,
                    active_workflow_session_id=sessions[-1]["workflow_session_id"],
                    revision=1,
                ))], "active-session:legacy-init", operation="workflow.migrate_selection",
                    expected_session_selection_revisions={ACTIVE_SESSION_SELECTION_ID: 0})

    def _session(self, store, session_id):
        session = store.get_record("workflow_session", {"workflow_session_id": session_id})
        if session["workflow_definition_id"] != self._definition_id:
            raise ContractValidationError("Workflow session has another definition")
        return session

    def _save(
        self, store, session_id, records, key, operation="workflow.update",
        request_digest: str | None = None,
        expected_ref_heads: dict[str, tuple[int, str]] | None = None,
        after_save=None,
    ):
        session = self._session(store, session_id)
        revision = session["revision"]
        session["revision"] += 1
        values = [*records, ("workflow_session", session)]
        if after_save is not None or any(kind in (
            "input_snapshot", "state_snapshot", "workflow_commit",
        ) for kind, _ in records):
            def bind(saved_store, saved):
                self._bind_saved_variables(saved_store, saved)
                if after_save is not None:
                    after_save(saved_store, saved)
            return store.save_bundle_prepared(
                lambda _: values, key, operation=operation,
                expected_session_revisions={session_id: revision},
                request_digest=request_digest or _request_digest([[kind, row] for kind, row in values]),
                expected_ref_heads=expected_ref_heads, after_save=bind,
            )
        return store.save_bundle(values, key, operation=operation,
                                 expected_session_revisions={session_id: revision},
                                 request_digest=request_digest,
                                 expected_ref_heads=expected_ref_heads)

    def _save_prepared(self, store, sid, builder, key, *, operation, request_digest):
        revision = self._session(store, sid)["revision"]

        def build(saved_store):
            records = builder(saved_store)
            session = self._session(saved_store, sid)
            session["revision"] += 1
            return [*records, ("workflow_session", session)]

        return store.save_bundle_prepared(
            build, key, operation=operation, request_digest=request_digest,
            expected_session_revisions={sid: revision},
            after_save=self._bind_saved_variables,
        )

    def _bind_saved_variables(self, store, records):
        self._bind_program_variables(store, records)
        variables = VariableStore(store)
        for snapshot in [row for row in records if "snapshot_id" in row and "s0" in row]:
            evidence = snapshot["config"]["payload"].get("variable_preparation")
            if evidence is None:
                continue
            reference = evidence["state_ref"]
            if reference["workflow_session_id"] == snapshot["workflow_session_id"]:
                variables.bind_state_in_transaction("input_snapshot", snapshot["snapshot_id"], reference)
            seed = evidence.get("seed")
            if seed is not None:
                commit = store.get_record("workflow_commit", {"commit_id": seed["commit_id"]})
                for kind, identity in (
                    ("state_snapshot", commit["state_snapshot_id"]), ("workflow_commit", commit["commit_id"]),
                ):
                    if variables.get_bound_state_ref(kind, identity) is None:
                        variables.bind_state_in_transaction(kind, identity, seed["state_ref"])
        for snapshot in [row for row in records if "state_snapshot_id" in row and "node_states" in row]:
            reference = None
            for binding in (B_BINDING, A_BINDING):
                selected = next(
                    (node["selected_result"] for node in snapshot["node_states"]
                     if node["node_binding_id"] == binding), None,
                )
                if selected is not None and selected["kind"] == "agent_turn":
                    turn = store.get_record("turn", {"turn_id": selected["turn_id"]})
                    frozen = store.get_record("input_snapshot", {"snapshot_id": turn["snapshot_id"]})
                    preparation = frozen["config"]["payload"].get("variable_preparation")
                    if preparation is not None:
                        reference = preparation["state_ref"]
                        break
            if reference is not None and reference["workflow_session_id"] != snapshot["workflow_session_id"]:
                inherited = variables.read_snapshot(reference, node_binding_id=binding)
                sid = snapshot["workflow_session_id"]
                inherited["workflow_session_id"] = sid
                inherited["values"]["workflow_session_id"]["value"] = sid
                current = variables.get_current(
                    workflow_session_id=sid, workflow_id=reference["workflow_id"],
                    registry_revision=reference["registry_revision"],
                )
                copied = variables.prepare_snapshot_in_transaction(
                    inherited, expected_revision=current["revision"] if current else 0,
                    idempotency_key="variables:state:" + snapshot["state_snapshot_id"],
                    source_ref=reference,
                )
                reference = _variable_ref(copied["after"])
            if reference is not None:
                variables.bind_state_in_transaction("state_snapshot", snapshot["state_snapshot_id"], reference)
        for commit in [row for row in records if "commit_id" in row and "state_snapshot_id" in row]:
            reference = variables.get_bound_state_ref("state_snapshot", commit["state_snapshot_id"])
            if reference is not None:
                variables.bind_state_in_transaction("workflow_commit", commit["commit_id"], reference)

    def _bind_program_variables(self, store, records):
        variables = ProgramVariableStore(store)
        for snapshot in [row for row in records if "snapshot_id" in row and "s0" in row]:
            evidence = snapshot["config"]["payload"].get("program_variable_preparation")
            if evidence is not None:
                variables.bind("input_snapshot", snapshot["snapshot_id"],
                               evidence["session_id"], evidence["revision"])
        for snapshot in [row for row in records if "state_snapshot_id" in row and "node_states" in row]:
            sid = snapshot["workflow_session_id"]
            current = variables.current(sid)
            if current["revision"] == 0:
                continue
            reference = None
            for binding in (B_BINDING, A_BINDING):
                selected = next((node["selected_result"] for node in snapshot["node_states"]
                                 if node["node_binding_id"] == binding), None)
                if selected is not None and selected["kind"] == "agent_turn":
                    turn = store.get_record("turn", {"turn_id": selected["turn_id"]})
                    reference = variables.reference("input_snapshot", turn["snapshot_id"])
                    if reference is not None:
                        break
            if reference is None:
                reference = {"session_id": sid, "revision": current["revision"]}
            variables.bind("state_snapshot", snapshot["state_snapshot_id"],
                           reference["session_id"], reference["revision"])
        for commit in [row for row in records if "commit_id" in row and "state_snapshot_id" in row]:
            reference = variables.reference("state_snapshot", commit["state_snapshot_id"])
            if reference is not None:
                variables.bind("workflow_commit", commit["commit_id"],
                               reference["session_id"], reference["revision"])
        self._restore_program_selected_values(store, records)

    @staticmethod
    def _restore_program_selected_values(store, records):
        operations = [row for row in records if row.get("kind") == "select_candidate"
                      and "operation_id" in row]
        if not operations:
            return
        variables = ProgramVariableStore(store)
        for head in [row for row in records if "workflow_ref_id" in row]:
            reference = variables.reference("workflow_commit", head["head_commit_id"])
            if reference is None:
                continue
            sid = head["workflow_session_id"]
            inherited = variables.read(reference["session_id"], reference["revision"])
            current = variables.current(sid)
            if (canonical_bytes(current["values"]) != canonical_bytes(inherited["values"])
                    or canonical_bytes(current.get("data", {})) != canonical_bytes(inherited.get("data", {}))):
                variables.write_in_transaction(
                    sid, inherited, expected_revision=current["revision"],
                    key="program:select:" + operations[0]["operation_id"],
                    request={"operation": "select_candidate", "session_id": sid, "reference": reference},
                )

    @staticmethod
    def _workflow_ref(store: SqliteStore, session_id: str) -> dict[str, Any]:
        refs = [row for row in store.list_records("workflow_ref")
                if row["workflow_session_id"] == session_id]
        if len(refs) != 1:
            raise ContractValidationError("Workflow session has no unique version head")
        return refs[0]

    @staticmethod
    def _session_selection(store: SqliteStore) -> dict[str, Any] | None:
        rows = store.list_records("session_selection")
        if len(rows) > 1:
            raise ContractValidationError("Multiple active session selections")
        return rows[0] if rows else None

    @staticmethod
    def _operation(
        kind: str, session_id: str, target_kind: str, target_id: str,
        key: str, revisions: list[dict[str, Any]], payload: dict[str, Any],
        *, expected_head_commit_id: str | None = None,
    ) -> dict[str, Any]:
        value = {
            "schema_version": 1, "operation_id": _uid(), "kind": kind,
            "scope": {"kind": "workflow_session", "id": session_id},
            "target": {"kind": target_kind, "id": target_id},
            "idempotency_key": key, "expected_revisions": revisions,
            "payload": payload,
        }
        if expected_head_commit_id is not None:
            value["expected_head_commit_id"] = expected_head_commit_id
        return WorkflowOperation(value).to_mapping()

    def _state_snapshot(
        self, session_id: str, nodes: list[dict[str, Any]],
        selections: list[dict[str, Any]], visible_refs: list[dict[str, Any]],
        *, pending_input_id: str | None = None,
    ) -> dict[str, Any]:
        states = []
        for node in nodes:
            private = node["private_data"]
            turn_id = node.get("selected_turn_id", private["payload"].get("head_turn_id"))
            output_id = private["payload"].get("last_output_id")
            selected = ({"kind": "agent_turn", "turn_id": turn_id} if turn_id is not None
                        else {"kind": "node_output", "output_id": output_id} if output_id is not None
                        else None)
            states.append({
                "node_binding_id": node["node_binding_id"],
                "data_version": node["data_version"],
                "private_data": copy.deepcopy(private),
                "selected_result": selected,
            })
        return _record(
            state_snapshot_id=_uid(), workflow_session_id=session_id,
            workflow_definition_id=self._definition_id, definition_revision=1,
            node_states=states, selection_refs=copy.deepcopy(selections),
            visible_message_refs=[
                {field: copy.deepcopy(ref[field])
                 for field in ("visible_message_id", "sequence", "role", "boundary")}
                for ref in sorted(visible_refs, key=lambda item: item["sequence"])
            ], pending_input_id=pending_input_id,
        )

    def _recover_unfinished(self) -> None:
        with closing(self._store()) as store:
            bundle = store.read_bundle()
            for session in bundle.get("workflow_session", []):
                sid = session["workflow_session_id"]
                records = []
                open_chains = set()
                for chain in bundle.get("chain_run", []):
                    if chain["workflow_session_id"] == sid and chain["status"] not in ("succeeded", "superseded", "closed", "recovery_unavailable"):
                        changed = copy.deepcopy(chain)
                        changed["status"] = "recovery_unavailable"
                        records.append(("chain_run", changed))
                        open_chains.add(chain["chain_run_id"])
                for run in bundle.get("run_record", []):
                    if run["workflow_session_id"] == sid and run["status"] not in ("succeeded", "superseded", "closed", "recovery_unavailable"):
                        changed = copy.deepcopy(run)
                        changed.update(status="recovery_unavailable", revision=run["revision"] + 1)
                        records.append(("run_record", changed))
                for ref in bundle.get("visible_message_ref", []):
                    if ref["workflow_session_id"] == sid and ref["role"] == "user" and ref["boundary"]["chain_run_id"] in open_chains:
                        changed = copy.deepcopy(ref)
                        changed["boundary"]["input_status"] = "failed"
                        records.append(("visible_message_ref", changed))
                if records:
                    self._save(store, sid, records, "reopen:" + _uid())

    def create_session(self, *, copy_from=None, expected_source_revision=None,
                       expected_data_revision=None, idempotency_key=None,
                       workflow_id="frontend:main-test") -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            from .workbench_resources import workflow_identity
            workflow_identity(workflow_id)
            copy_key = None
            copied_state = None
            copy_digest = None
            if copy_from is not None:
                from .workbench_resources import require, resource_id, resource_revision
                resource_id(copy_from)
                resource_id(idempotency_key)
                resource_revision(expected_source_revision)
                resource_revision(expected_data_revision, zero=True)
                copy_key = copy_from + ":" + idempotency_key
                copy_digest = "workflow-op-v1:" + content_digest({
                    "kind": "copy_empty_session", "source": copy_from,
                    "revision": expected_source_revision, "data_revision": expected_data_revision,
                    "workflow_id": workflow_id,
                }).rsplit(":", 1)[1]
                receipt = store.read_receipt_with_digest("workflow.copy_empty", copy_key)
                if receipt is not None:
                    require(receipt[0] == copy_digest, "Copy idempotency request differs")
                    child = next(row for row in receipt[1] if "workflow_session_id" in row
                                 and "workflow_definition_id" in row and row["workflow_session_id"] != copy_from)
                    return self._view(store, child["workflow_session_id"])
                source = self._session(store, copy_from)
                require(source["revision"] == expected_source_revision, "Source session changed")
                self._assert_runtime_forkable(copy_from, store)
                require(not self._formal_refs(store, copy_from), "Copy requires an empty session")
                copied_state = self._program_current_state(store, copy_from)
                require(copied_state["revision"] == expected_data_revision, "Source data changed")
            sid = _uid()
            session = _record(workflow_session_id=sid, workflow_definition_id=self._definition_id,
                              definition_revision=1, revision=1, source={"kind": "new"})
            nodes = []
            for binding in (A_BINDING, B_BINDING, OUTPUT_BINDING):
                nodes.append(_record(workflow_session_id=sid, node_binding_id=binding, data_version=1,
                    private_data={"owner_component_id": (
                        self._output_resolution.descriptor["component_id"]
                        if binding == OUTPUT_BINDING else AGENT_COMPONENT
                    ),
                                  "schema_version": 1, "payload": {} if binding == OUTPUT_BINDING else {"head_turn_id": None}}))
            checkpoint = self._checkpoint(sid, nodes, [], kind="initial", revision=1)
            operation = self._operation(
                "initialize_session", sid, "workflow_session", sid,
                "seed:" + sid, [], {},
            )
            snapshot = self._state_snapshot(sid, checkpoint["nodes"], [], [])
            commit = _record(
                commit_id=_uid(), workflow_session_id=sid,
                state_snapshot_id=snapshot["state_snapshot_id"], parent_commit_id=None,
                operation_id=operation["operation_id"],
                source={"kind": "session_seed", "workflow_session_id": sid},
            )
            ref = _record(
                workflow_ref_id=_uid(), workflow_session_id=sid,
                head_commit_id=commit["commit_id"], revision=1,
            )
            records = [
                ("workflow_session", session), *[("node_session", row) for row in nodes],
                ("workflow_checkpoint", checkpoint), ("workflow_operation", operation),
                ("state_snapshot", snapshot), ("workflow_commit", commit), ("workflow_ref", ref),
            ]
            if copy_from is not None:
                updated_source = copy.deepcopy(source)
                updated_source["revision"] += 1
                records.append(("workflow_session", updated_source))
            selection = self._session_selection(store)
            if selection is None:
                records.append(("session_selection", _record(
                    session_selection_id=ACTIVE_SESSION_SELECTION_ID,
                    active_workflow_session_id=sid, revision=1,
                )))
            def bind_copy(saved_store, _records):
                WorkbenchResourceStore(saved_store).bind_session_owner(sid, workflow_id)
                if copied_state is None:
                    return
                variables = ProgramVariableStore(saved_store)
                copied = variables.write_in_transaction(
                    sid, copied_state, expected_revision=0, key="copy:" + idempotency_key,
                    request={"kind": "copy_empty", "source": copy_from, "session_id": sid,
                             "data_revision": expected_data_revision},
                )["state"]
                variables.bind("workflow_commit", commit["commit_id"], sid, copied["revision"])
                variables.bind("state_snapshot", snapshot["state_snapshot_id"], sid, copied["revision"])

            if copy_key is None:
                store.save_bundle_prepared(lambda _: records, sid, operation="workflow.create_session",
                    expected_session_revisions={sid: 0},
                    expected_ref_heads={ref["workflow_ref_id"]: (0, None)},
                    expected_session_selection_revisions=(
                        {ACTIVE_SESSION_SELECTION_ID: 0} if selection is None else None
                    ), after_save=bind_copy,
                    request_digest="workflow-op-v1:" + content_digest({
                        "kind": "create_session", "session_id": sid, "workflow_id": workflow_id,
                    }).rsplit(":", 1)[1])
                return self._view(store, sid)
            store.save_bundle_prepared(lambda _: records, copy_key,
                operation="workflow.copy_empty",
                expected_session_revisions={sid: 0, **(
                    {copy_from: expected_source_revision} if copy_key else {}
                )},
                expected_ref_heads={ref["workflow_ref_id"]: (0, None)},
                expected_session_selection_revisions=(
                    {ACTIVE_SESSION_SELECTION_ID: 0} if selection is None else None
                ), request_digest=copy_digest, after_save=bind_copy)
            return self._view(store, sid)

    def copy_workbench_session(self, session_id, *, expected_source_revision,
                               expected_data_revision, idempotency_key, target_workflow_id=None):
        """Copy the current stable session, including live manual data, not its other sessions."""
        from .workbench_resources import require, resource_id, resource_revision
        resource_id(session_id)
        resource_id(idempotency_key)
        resource_revision(expected_source_revision)
        resource_revision(expected_data_revision, zero=True)
        with self._lock, closing(self._store()) as store:
            source = self._session(store, session_id)
            owner = target_workflow_id or WorkbenchResourceStore(store).session_owner(session_id)
            empty_receipt = store.read_receipt_with_digest("workflow.copy_empty", session_id + ":" + idempotency_key)
            refs = self._formal_refs(store, session_id)
            if not refs or empty_receipt is not None:
                return self.create_session(
                    copy_from=session_id, expected_source_revision=expected_source_revision,
                    expected_data_revision=expected_data_revision, idempotency_key=idempotency_key,
                    workflow_id=owner,
                )
            prior = store.read_receipt_with_digest("workflow.fork", session_id + ":" + idempotency_key)
            anchor = next((row for row in prior[1] if "fork_anchor_id" in row and "role" in row), None) if prior else None
            require(anchor is not None or refs[-1]["role"] == "assistant",
                    "Current session has no completed copy boundary")
            return self._create_branch(
                session_id, anchor["visible_message_id"] if anchor else refs[-1]["visible_message_id"], switch=False, candidate_id=None,
                expected_source_revision=expected_source_revision, idempotency_key=idempotency_key,
                inherit_current_data=expected_data_revision,
                target_workflow_id=owner,
            )

    def get_active_session(self) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            selection = self._session_selection(store)
            return {
                "active_workflow_session_id": (
                    selection["active_workflow_session_id"] if selection else None
                ),
                "revision": selection["revision"] if selection else 0,
            }

    def switch_session(
        self, session_id: str, *, idempotency_key: str,
        expected_selection_revision: int,
    ) -> dict[str, Any]:
        request = {
            "kind": "switch_session",
            "scope": {"kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID},
            "target": {"kind": "workflow_session", "id": session_id},
            "idempotency_key": idempotency_key,
            "expected_revisions": [{
                "kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID,
                "revision": expected_selection_revision,
            }],
        }
        request_digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        with self._lock, closing(self._store()) as store:
            self._check_open()
            key = ACTIVE_SESSION_SELECTION_ID + ":" + idempotency_key
            prior = store.read_receipt_with_digest("workflow.switch_session", key)
            if prior is not None:
                if prior[0] != request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                row = next(item for item in prior[1] if "session_selection_id" in item)
                return {"active_workflow_session_id": row["active_workflow_session_id"],
                        "revision": row["revision"]}
            self._session(store, session_id)
            current = self._session_selection(store)
            if current is None or current["revision"] != expected_selection_revision:
                raise ContractValidationError("Session selection revision conflict")
            operation = WorkflowOperation({
                "schema_version": 1, "operation_id": _uid(), **request, "payload": {},
            }).to_mapping()
            selection = copy.deepcopy(current)
            selection.update(
                active_workflow_session_id=session_id,
                revision=expected_selection_revision + 1,
            )
            store.save_bundle([
                ("workflow_operation", operation), ("session_selection", selection),
            ], key, operation="workflow.switch_session",
                expected_session_selection_revisions={
                    ACTIVE_SESSION_SELECTION_ID: expected_selection_revision,
                }, request_digest=request_digest)
            return {"active_workflow_session_id": session_id, "revision": selection["revision"]}

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            resources = WorkbenchResourceStore(store)
            return [{"workflow_session_id": row["workflow_session_id"], "created_at": row.get("created_at"),
                     "mode": self.mode, **({"workflow_id": owner} if (
                         owner := resources.session_owner(row["workflow_session_id"])
                     ) != "frontend:main-test" else {})}
                    for row in reversed(store.list_records("workflow_session"))]

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._view(store, session_id)

    def save_exposure_configuration(self, **request):
        from .exposure_configuration import ExposureConfigurationStore
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return ExposureConfigurationStore(store).write(**request)

    def get_exposure_configuration(self, identity, version=None):
        from .exposure_configuration import ExposureConfigurationStore
        with self._lock, closing(self._store()) as store:
            self._check_open()
            record = ExposureConfigurationStore(store).get(identity, version)
            if record is None:
                raise configuration_error("not_found", 404)
            return record

    def read_exposures(self, session_id, identity, version):
        from .exposure_configuration import ExposureConfigurationStore, read_registered_exposures
        with self._lock, closing(self._store()) as store:
            self._check_open()
            catalog = ExposureConfigurationStore(store)
            record = catalog.get(identity, version)
            if record is None:
                raise configuration_error("not_found", 404)
            if record["workflow_id"] != WorkbenchResourceStore(store).session_owner(session_id):
                raise context_error("Declaration belongs to another workflow", status=409, reason="ownership_mismatch")
            return read_registered_exposures(record, catalog.get(identity), self._view(store, session_id))

    def _selected_workbench_context(self, store, sid, binding, request):
        sessions = [row for row in store.list_records("workflow_session")
                    if row["workflow_session_id"] == sid]
        if not sessions:
            raise context_error("Context session was not found", status=404, reason="not_found")
        if len(sessions) != 1:
            raise ContractValidationError("Context session has duplicate records")
        session = validate_record("workflow_session", sessions[0])
        if session["workflow_definition_id"] != self._definition_id:
            raise context_error("Context session has another definition", status=409, reason="ownership_mismatch")
        head = validate_record("workflow_ref", self._workflow_ref(store, sid))
        if (
            session["revision"] != request["expected_session_revision"]
            or head["revision"] != request["expected_ref_revision"]
            or head["head_commit_id"] != request["expected_head_commit_id"]
        ):
            raise context_error("Context read revision fence changed", status=409, reason="stale_revision")
        commit = validate_record("workflow_commit", store.get_record(
            "workflow_commit", {"commit_id": head["head_commit_id"]},
        ))
        state = validate_record("state_snapshot", store.get_record(
            "state_snapshot", {"state_snapshot_id": commit["state_snapshot_id"]},
        ))
        if (
            commit["workflow_session_id"] != sid or state["workflow_session_id"] != sid
            or state["workflow_definition_id"] != session["workflow_definition_id"]
            or state["definition_revision"] != session["definition_revision"]
        ):
            raise ContractValidationError("Selected context head has another owner")
        nodes = [node for node in state["node_states"] if node["node_binding_id"] == binding]
        if len(nodes) != 1:
            raise ContractValidationError("Selected context head has no unique node")
        result = nodes[0]["selected_result"]
        if result is not None and result["kind"] != "agent_turn":
            raise ContractValidationError("Selected Agent context has a non-Agent result")
        parent = None if result is None else result["turn_id"]
        reader = SelectedArchiveReader(
            store, self._snapshot_components, allowed_bindings=(A_BINDING, B_BINDING),
        )
        archived = reader.read(
            sid, binding, parent, protect_protocol=True, include_ports=True,
            capacity=PromptAssemblyLimits(),
        )
        reader.validate_selected_refs(archived, state["selection_refs"])
        reader.validate_inherited_boundary(archived, sid, binding)
        scope = {
            "workflow_session_id": sid, "node_binding_id": binding,
            "session_revision": session["revision"], "ref_revision": head["revision"],
            "head_commit_id": head["head_commit_id"], "state_snapshot_id": state["state_snapshot_id"],
            "parent_turn_id": parent,
            "selected_chain_run_id": archived.turns[-1]["chain_run_id"] if archived.turns else None,
        }
        return scope, archived

    def _workbench_context(self, sid, binding, request, *, preview):
        validate_context_identity(sid, binding)
        request = validate_context_request(request, preview=preview)
        if binding not in (A_BINDING, B_BINDING):
            raise context_error("Context binding was not found", status=404, reason="not_found")
        try:
            with self._lock, closing(self._store()) as store:
                self._check_open()
                scope, archived = self._selected_workbench_context(store, sid, binding, request)
                if not preview:
                    return {
                        "schema_version": 1, "kind": "workflow_context_read", "scope": scope,
                        "messages": archived.messages, "logical_floors": archived.logical_floors,
                        "protected_blocks": archived.protected_blocks, "turns": archived.turns,
                    }
                stage = "A" if binding == A_BINDING else "B"
                default = self._resolved[stage][3]["payload"]["context"]
                config = materialize_prompt_draft(request["prompt_config"])
                if "schema_version" in default:
                    config = compose_selected_prompt_context(config, default)
                    config = bind_prompt_config_scope(config, workflow_session_id=sid, node_binding_id=binding)
                    if config["schema_version"] == 2 and stage == "A" and request["text"] is not None:
                        ephemeral = {
                            "schema_version": 1, "input_id": _uid(), "port_id": "request",
                            "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
                            "source": {"kind": "visible_message", "visible_message_id": _uid()},
                            "payload": {"text": request["text"]},
                        }
                        config["variables"] = prepare_variable_assignments(
                            config["variables"], config["variable_plan"], ephemeral,
                        )["snapshot"]
                if config["schema_version"] == 3:
                    self._resolve_global_programs(store, {stage: config})
                    config["preparation_state"] = self._program_current_state(store, sid)
                    self._bind_context_sources(store, sid, config, request)
                    archived = self._assembly_archive(config, binding, archived)
                try:
                    return preview_context(archived, scope, config, text=request["text"], stage=stage)
                except PromptProcessingError as exc:
                    error = context_error(str(exc), reason=exc.code)
                    error.node_id = exc.node_id
                    error.processing_diagnostic = exc.diagnostic()
                    raise error from exc
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) is not None:
                raise
            raise context_error(
                "Stored context cannot be projected", status=500, reason="storage_contract_violation",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise context_error(
                "Stored context has invalid structure", status=500, reason="storage_contract_violation",
            ) from exc

    def read_node_context(self, session_id: str, node_binding_id: str, **request) -> dict[str, Any]:
        """Read the formally selected archive without writes or model dispatch."""
        return self._workbench_context(session_id, node_binding_id, request, preview=False)

    def preview_node_context(self, session_id: str, node_binding_id: str, **request) -> dict[str, Any]:
        """Assemble an inline draft around the selected archive and ephemeral A input."""
        return self._workbench_context(session_id, node_binding_id, request, preview=True)

    def _program_current_state(self, store, sid):
        variables = ProgramVariableStore(store)
        current = variables.current(sid)
        if current["revision"] != 0:
            return current
        session = self._session(store, sid)
        if session["source"]["kind"] == "fork":
            reference = variables.reference("workflow_commit", self._workflow_ref(store, sid)["head_commit_id"])
            if reference is not None:
                inherited = variables.read(reference["session_id"], reference["revision"])
                inherited["revision"] = 0
                return inherited
        return current

    def _program_request_state(self, store, sid, request):
        fence_fields = {"expected_session_revision", "expected_ref_revision", "expected_head_commit_id"}
        fence = {key: request[key] for key in fence_fields}
        validate_context_request(fence)
        self._selected_workbench_context(store, sid, A_BINDING, fence)
        drafts = []
        if "prompt_selection" in request:
            configs = resolve_prompt_selection(
                request["prompt_selection"], PromptConfigStore(store),
                workflow_definition_id=self._definition_id, workflow_session_id=sid,
                node_bindings={"A": A_BINDING, "B": B_BINDING},
            )
            drafts = list(configs.values())
        else:
            drafts = [materialize_prompt_draft(request["prompt_config"])]
        definitions = {}
        for config in drafts:
            if config["schema_version"] != 3:
                continue
            for definition in program_definitions(config["preparation"]):
                previous = definitions.get(definition["name"])
                if previous is not None and previous != definition:
                    raise program_variable_error("variable_type_conflict", "A/B variable declarations differ")
                definitions[definition["name"]] = definition
        return merge_program_definitions(self._program_current_state(store, sid), list(definitions.values()))

    @staticmethod
    def _program_variable_response(sid, state):
        return {
            "schema_version": 1, "kind": "workflow_variable_read", "workflow_session_id": sid,
            "revision": state["revision"], "values": [{
                "name": name, "type": entry["type"], "assigned": "value" in entry,
                "source": entry["source"], **({"value": entry["value"]} if "value" in entry else {}),
            } for name, entry in sorted(state["values"].items())],
        }

    @staticmethod
    def _program_variable_write_response(sid, state, request):
        result = WorkflowService._program_variable_response(sid, state)
        result["write_receipt"] = {
            "idempotency_key": request["idempotency_key"],
            "name": request["name"],
            "expected_variable_revision": request["expected_variable_revision"],
        }
        return result

    def read_session_variables(self, sid, **request):
        self._validate_program_variable_request(request, write=False)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            try:
                return self._program_variable_response(sid, self._program_request_state(store, sid, request))
            except PromptProcessingError as exc:
                error = program_variable_error(exc.code, str(exc))
                error.processing_diagnostic = exc.diagnostic()
                raise error from exc

    def write_session_variable(self, sid, **request):
        self._validate_program_variable_request(request, write=True)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            try:
                store._connection.execute("BEGIN IMMEDIATE")
                self._session(store, sid)
                variables = ProgramVariableStore(store)
                receipt_request = {"session_id": sid, **request}
                receipt_key = "program:edit:" + request["idempotency_key"]
                existing = variables.receipt(sid, receipt_key, receipt_request)
                if existing is not None:
                    store._connection.execute("COMMIT")
                    return self._program_variable_write_response(sid, existing["state"], request)
                state = self._program_request_state(store, sid, request)
                name = validate_variable_name(request["name"])
                if name not in state["values"]:
                    raise program_variable_error("variable_not_registered", "Variable is not registered")
                entry = state["values"][name]
                validate_typed_value(entry["type"], request["value"])
                state["values"][name] = {"type": entry["type"], "source": "assignment", "value": request["value"]}
                result = variables.write_in_transaction(
                    sid, state, expected_revision=request["expected_variable_revision"],
                    key=receipt_key, request=receipt_request,
                )
                store._connection.execute("COMMIT")
                return self._program_variable_write_response(sid, result["state"], request)
            except Exception as exc:
                if store._connection.in_transaction:
                    store._connection.execute("ROLLBACK")
                if isinstance(exc, PromptProcessingError):
                    error = program_variable_error(exc.code, str(exc))
                    error.processing_diagnostic = exc.diagnostic()
                    raise error from exc
                raise

    @staticmethod
    def _validate_program_variable_request(request, *, write):
        fields = {"expected_session_revision", "expected_ref_revision", "expected_head_commit_id"}
        source = {"prompt_selection"} if "prompt_selection" in request else {"prompt_config"}
        if write:
            fields |= {"name", "value", "expected_variable_revision", "idempotency_key"}
        if type(request) is not dict or set(request) != fields | source:
            raise program_variable_error("invalid_request", "Variable request fields must be exact")
        if write and (type(request["expected_variable_revision"]) is not int
                      or request["expected_variable_revision"] < 0
                      or type(request["idempotency_key"]) is not str
                      or not 0 < len(request["idempotency_key"]) <= 100):
            raise program_variable_error("invalid_request", "Variable edit revision or key is invalid")

    def list_reply_candidate_floors(self, session_id: str) -> list[dict[str, Any]]:
        """Read every completed formal floor without changing its selection."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            self._session(store, session_id)
            return self._reply_candidate_floors(
                store, session_id, self._formal_refs(store, session_id),
                store.read_bundle(),
            )

    def _formal_refs(self, store, session_id):
        head = self._workflow_ref(store, session_id)
        commit = store.get_record("workflow_commit", {
            "commit_id": head["head_commit_id"],
        })
        snapshot = store.get_record("state_snapshot", {
            "state_snapshot_id": commit["state_snapshot_id"],
        })
        registry = [row for row in store.list_records("visible_message_ref")
                    if row["workflow_session_id"] == session_id]
        by_id = {row["visible_message_id"]: row for row in registry}
        selected = [
            copy.deepcopy(by_id[row["visible_message_id"]])
            for row in snapshot["visible_message_refs"]
        ]
        selected_ids = {row["visible_message_id"] for row in selected}
        last_sequence = max((row["sequence"] for row in selected), default=0)
        selected.extend(
            copy.deepcopy(row) for row in registry
            if row["role"] == "user"
            and row["visible_message_id"] not in selected_ids
            and row["sequence"] > last_sequence
        )
        return sorted(selected, key=lambda row: row["sequence"])

    def _reply_candidate_floors(self, store, session_id, refs, bundle, *, require_complete=False):
        session = self._session(store, session_id)
        inherited = {}
        if session["source"]["kind"] == "fork":
            anchor = store.get_record("fork_anchor", {
                "fork_anchor_id": session["source"]["fork_anchor_id"],
            })
            inherited = {
                row["user_visible_message_id"]: row
                for row in anchor.get("candidate_floors", [])
            }
        candidates = {
            row["candidate_id"]: row for row in bundle.get("workflow_candidate", [])
        }
        chains = {
            row["chain_run_id"]: row for row in bundle.get("chain_run", [])
        }
        floors = []
        for index, user in enumerate(refs[:-1]):
            assistant = refs[index + 1]
            if user["role"] != "user" or assistant["role"] != "assistant":
                continue
            user_id = user["visible_message_id"]
            if user_id in inherited:
                ids = copy.deepcopy(inherited[user_id]["candidate_ids"])
                descendants = set(ids)
                for origin in bundle.get("chain_input_origin", []):
                    if (origin["workflow_session_id"] == session_id
                            and origin["source_chain_run_id"] in {
                                candidates[candidate_id]["chain_run_id"]
                                for candidate_id in descendants
                            }):
                        descendants.update(
                            row["candidate_id"]
                            for row in bundle.get("workflow_candidate", [])
                            if row["workflow_session_id"] == session_id
                            and row["chain_run_id"] == origin["chain_run_id"]
                        )
                ids.extend(
                    row["candidate_id"]
                    for row in bundle.get("workflow_candidate", [])
                    if row["candidate_id"] in descendants
                    and row["candidate_id"] not in ids
                )
            elif (user["boundary"]["chain_run_id"] in chains
                  and chains[user["boundary"]["chain_run_id"]]["workflow_session_id"] == session_id):
                ids = [
                    row["candidate_id"]
                    for row in list_workflow_reply_candidates(bundle, session_id, user_id)
                ]
            elif require_complete:
                raise ContractValidationError(
                    "Inherited reply candidates have no frozen fork manifest"
                )
            else:
                continue  # Older branch seeds did not retain a candidate inventory.
            selected = [
                candidate_id for candidate_id in ids
                if candidates[candidate_id]["chain_run_id"] == assistant["boundary"]["chain_run_id"]
                and candidates[candidate_id]["output_id"] == assistant["boundary"]["output_id"]
            ]
            if len(selected) != 1:
                raise ContractValidationError("Formal reply is missing from its candidate floor")
            floors.append({
                "user_visible_message_id": user_id,
                "candidate_ids": ids,
                "selected_candidate_id": selected[0],
            })
        return floors

    def _view(self, store, session_id):
        session = self._session(store, session_id)
        bundle = store.read_bundle()
        refs = self._formal_refs(store, session_id)
        head = self._workflow_ref(store, session_id)
        messages = {row["visible_message_id"]: row for row in bundle.get("visible_message", [])}
        candidates = {
            row["candidate_id"]: row for row in bundle.get("workflow_candidate", [])
        }
        outputs = {
            row["output_id"]: row for row in bundle.get("workflow_output", [])
        }
        floors = {
            row["user_visible_message_id"]: row
            for row in self._reply_candidate_floors(store, session_id, refs, bundle)
        }
        projected_messages = []
        user_id = None
        for ref in refs:
            projected = {
                "visible_message_id": ref["visible_message_id"],
                "role": ref["role"],
                "payload": copy.deepcopy(messages[ref["visible_message_id"]]["payload"]),
                "sequence": ref["sequence"],
                "chain_run_id": ref["boundary"].get("chain_run_id"),
            }
            if ref["role"] == "user":
                user_id = ref["visible_message_id"]
            elif user_id in floors:
                floor = floors[user_id]
                projected["reply_candidates"] = [
                    {
                        "candidate_id": candidate_id,
                        "chain_run_id": candidates[candidate_id]["chain_run_id"],
                        "payload": copy.deepcopy(
                            outputs[candidates[candidate_id]["output_id"]]["payload"]
                        ),
                        "selected": candidate_id == floor["selected_candidate_id"],
                    }
                    for candidate_id in floor["candidate_ids"]
                ]
            projected_messages.append(projected)
        runs = [row for kind in ("run_record", "node_run") for row in bundle.get(kind, [])
                if row["workflow_session_id"] == session_id]
        chains = [row for row in bundle.get("chain_run", []) if row["workflow_session_id"] == session_id]
        current_chain = chains[-1]["chain_run_id"] if chains else None
        nodes = []
        for binding, label in ((A_BINDING, "A / Draft"), (B_BINDING, "B / Revision"), (OUTPUT_BINDING, "Output")):
            run = next((row for row in reversed(runs) if row["node_binding_id"] == binding
                        and row["chain_run_id"] == current_chain), None)
            projected_node = {"node_binding_id": binding, "label": label,
                              "status": run["status"] if run else "idle",
                              "run_id": run["run_id"] if run else None,
                              "revision": run["revision"] if run else None}
            if run is not None and run.get("profile") == "agent":
                facts = store.read_execution_facts(run["run_id"])
                projected_node["budget"] = {
                    **self._budget_limits(run, bundle.get("workflow_operation", [])),
                    "model_requests": sum(row["kind"] == "model_request" for row in facts),
                    "attempts": sum(row["kind"] == "model_attempt_started" for row in facts),
                }
            nodes.append(projected_node)
        error = self._errors.get(session_id)
        if error is None and any(row["status"] == "recovery_unavailable" for row in chains):
            error = {"code": "RECOVERY_UNAVAILABLE", "message": "The previous execution lost its runtime. End it explicitly, then choose continue workflow or reroll."}
        if error is None and any(row["status"] == "failed" for row in chains):
            error = {"code": "RUN_FAILED", "message": "The workflow stopped without a successful reply."}
        if error is None and chains and chains[-1]["status"] == "closed":
            closeout = next((row for row in bundle.get("execution_closeout", [])
                             if row["chain_run_id"] == chains[-1]["chain_run_id"]), None)
            if closeout is not None and closeout["diagnostic"]["category"] == "contract":
                error = {"code": closeout["diagnostic"]["code"],
                         "message": "The execution is closed. Its program or storage fault requires explicit repair; model continuation is unavailable."}
        active = self._futures.get(session_id)
        retry_archive = [
            row["run_id"] for row in runs
            if row.get("profile") == "agent" and (
                row["status"] in ("running", "final_ready")
                or row["status"] == "succeeded"
                and ResultPortStore(store, allowed_bindings=(A_BINDING, B_BINDING)).get_release(row["run_id"]) is None
            )
            and isinstance(self._active_results.get(row["run_id"]), dict)
            and self._active_results[row["run_id"]].get("session_id") == session_id
        ]
        retry_publish = any(
            isinstance(package, dict) and package.get("session_id") == session_id
            and package.get("stage") == "B" and package.get("retain_result")
            for package in self._active_results.values()
        ) or any(
            row["status"] == "pending" and row["target"] == {
                "kind": "ui", "workflow_session_id": session_id,
            }
            for row in bundle.get("output_delivery", [])
        )
        pending_ref = next((
            row for row in refs
            if row["role"] == "user"
            and row["boundary"]["input_status"] == "pending"
        ), None)
        pending_input = pending_ref is not None
        interruptible = any(
            row["workflow_session_id"] == session_id
            and row["status"] in ("prepared", "running")
            and row["run_id"] in self._active_runs
            and self._kernel_supports(row, "pause_resume")
            and row["run_id"] not in self._finalizing_runs
            and (row["status"] == "prepared" or active is not None and not active.done())
            for row in bundle.get("run_record", [])
        )
        resumable = extendable = False
        for row in bundle.get("run_record", []):
            if row["workflow_session_id"] != session_id or row["status"] not in ("paused", "failed"):
                continue
            chain = next(item for item in chains if item["chain_run_id"] == row["chain_run_id"])
            try:
                checkpoint = self._assert_safe_checkpoint(store, session_id, row, chain)
            except ContractValidationError:
                continue
            limits = self._budget_limits(row, bundle.get("workflow_operation", []))
            resumable = self._checkpoint_has_budget(checkpoint, limits)
            extendable = not resumable and (
                (checkpoint is None or checkpoint.model_requests < 64)
                and (checkpoint is None or checkpoint.attempts < 256)
            )
            break
        can_submit = (
            not pending_input and not retry_publish
            and all(row["status"] in ("succeeded", "superseded", "closed") for row in chains)
            and (active is None or active.done())
        )
        can_continue_pending = (
            pending_input and pending_ref == refs[-1]
            and error is None and not retry_archive and not retry_publish
            and not self._uncertain_tools.get(session_id)
            and all(row["status"] in ("succeeded", "superseded", "closed") for row in chains)
            and not any(row["workflow_session_id"] == session_id
                        and row["run_id"] in self._active_results
                        for row in bundle.get("run_record", []))
            and (active is None or active.done())
        )
        latest_floor = (
            floors.get(refs[-2]["visible_message_id"])
            if len(refs) >= 2 and refs[-2]["role"] == "user"
            and refs[-1]["role"] == "assistant" else None
        )
        can_select_candidates = (
            can_submit and error is None and not retry_archive
            and not self._uncertain_tools.get(session_id)
            and not any(
                run["workflow_session_id"] == session_id
                and run["run_id"] in self._active_results
                for run in bundle.get("run_record", [])
            )
            and latest_floor is not None
            and len(latest_floor["candidate_ids"]) > 1
        )
        can_reroll = False
        if (can_submit and error is None and refs and refs[-1]["role"] == "assistant"
                and not self._uncertain_tools.get(session_id)):
            try:
                source_id = refs[-1]["boundary"]["chain_run_id"]
                source = next(row for row in bundle["chain_run"]
                              if row["chain_run_id"] == source_id)
                if source["workflow_session_id"] == session_id:
                    inspect_completed_reroll_origin(bundle, session_id, source_id)
                else:
                    inspect_inherited_reroll_origin(bundle, session_id, source_id)
                self._assert_reroll_kernel_supports(store, source)
                can_reroll = True
            except ContractValidationError:
                pass
        if (not can_reroll and refs and refs[-1]["role"] == "user"
                and not retry_archive and not retry_publish
                and not self._uncertain_tools.get(session_id)
                and (active is None or active.done())):
            source_id = refs[-1]["boundary"].get("chain_run_id")
            if source_id is not None:
                try:
                    frozen = inspect_interrupted_reroll_origin(bundle, session_id, source_id)
                    source_run_id = frozen.stopped_run_id or frozen.source_run_id
                    workspace = self._active_runs.get(source_run_id)
                    if (workspace is not None and source_run_id in self._run_checkpoints
                            and source_run_id not in self._interrupting
                            and source_run_id not in self._finalizing_runs
                            and not self._reroll_tools_unsettled(store, source_run_id, workspace)
                            and not any(row["workflow_session_id"] == session_id
                                        and row["run_id"] in self._active_results
                                        for row in bundle["run_record"])):
                        source = store.get_record("chain_run", {"chain_run_id": source_id})
                        self._assert_reroll_kernel_supports(store, source)
                        can_reroll = True
                except ContractValidationError:
                    pass
        recovery_chain_id = None
        can_close_execution = can_continue_workflow = False
        if chains and (active is None or active.done()) and not retry_archive and not retry_publish:
            source = chains[-1]
            if source["status"] in ("failed", "recovery_unavailable", "prepared", "running", "paused"):
                try:
                    self._assert_closeable(store, session_id, source)
                    recovery_chain_id = source["chain_run_id"]
                    can_close_execution = True
                except ContractValidationError:
                    pass
            elif source["status"] == "closed":
                try:
                    inspect_closed_reroll_origin(bundle, session_id, source["chain_run_id"])
                    self._assert_reroll_kernel_supports(store, source)
                    recovery_chain_id = source["chain_run_id"]
                    can_reroll = True
                    self._continuation_basis(store, session_id, source["chain_run_id"])
                    can_continue_workflow = True
                except ContractValidationError:
                    pass
        model_plan = self._session_model_plan(store, session_id)
        view = {"workflow_session_id": session_id, "revision": session["revision"], "mode": self.mode,
            "model_selection": model_plan["selection"] if model_plan is not None else None,
            "ref_revision": head["revision"], "head_commit_id": head["head_commit_id"],
            "can_submit": can_submit, "can_reroll": can_reroll,
            "can_select_candidates": can_select_candidates,
            "can_close_execution": can_close_execution,
            "can_continue_workflow": can_continue_workflow,
            "recovery_chain_run_id": recovery_chain_id,
            "pending_input_id": pending_ref["boundary"]["input_id"] if can_continue_pending else None,
            "messages": projected_messages,
            "nodes": nodes, "chains": [{"chain_run_id": row["chain_run_id"], "status": row["status"]} for row in chains],
            "error": copy.deepcopy(error),
            "unresolved_tool_executions": copy.deepcopy(self._uncertain_tools.get(session_id, [])),
            "available_actions": (["retry_archive"] if retry_archive else
                                  ["retry_publish"] if retry_publish else
                                  (["resume"] if resumable else ["extend_budget"]) +
                                  (["reroll"] if can_reroll else []) +
                                  (["close_execution"] if can_close_execution else [])
                                  if resumable or extendable else
                                  ["close_execution"] if can_close_execution else
                                  (["continue_workflow", "reroll"] if can_continue_workflow else ["reroll"])
                                  if recovery_chain_id else
                                  ["continue_pending_input"] if can_continue_pending else
                                  ["interrupt"] if interruptible else [])}
        from .workflow_observation import project_observation
        view["observation"] = project_observation(
            store, view, bundle, bindings=(A_BINDING, B_BINDING, OUTPUT_BINDING),
        )
        return view

    def _check_open(self):
        if self._closed:
            raise ContractValidationError("Workflow service is closed")

    @staticmethod
    def _submission_receipt(records):
        chain = next(row for row in records if "chain_run_id" in row and "node_run_ids" in row)
        ref = next(row for row in records if row.get("role") == "user" and "boundary" in row)
        return {"workflow_session_id": chain["workflow_session_id"], "chain_run_id": chain["chain_run_id"],
                "visible_message_id": ref["visible_message_id"], "input_id": chain["input_id"], "status": "prepared"}

    def _assert_runtime_forkable(self, session_id: str, store: SqliteStore) -> None:
        future = self._futures.get(session_id)
        if future is not None and not future.done():
            raise ContractValidationError("Workflow session has an active execution")
        active_runs = {
            run_id for run_id in self._active_results
            if any(row["run_id"] == run_id and row["workflow_session_id"] == session_id
                   for row in store.list_records("run_record"))
        }
        if active_runs:
            raise ContractValidationError("Workflow session has an unarchived result")
        if any(row["workflow_session_id"] == session_id
               and row["status"] not in ("succeeded", "superseded", "closed")
               for row in store.list_records("chain_run")):
            raise ContractValidationError(
                "Workflow session has an unfinished execution; only a completed user boundary can be forked"
            )
        if any(row["status"] == "pending" and row["target"] == {
            "kind": "ui", "workflow_session_id": session_id,
        } for row in store.list_records("output_delivery")):
            raise ContractValidationError("Workflow session has an unpublished result")

    @staticmethod
    def _fork_receipt(records, switch: bool) -> dict[str, Any]:
        anchor = next(row for row in records if "fork_anchor_id" in row)
        child = next(row for row in records
                     if row.get("source", {}).get("kind") == "fork"
                      and row["source"]["fork_anchor_id"] == anchor["fork_anchor_id"]
                      and "workflow_session_id" in row)
        pending = anchor.get("pending_input")
        source_id = child["source"]["source_workflow_session_id"]
        selection = next((row for row in records if "session_selection_id" in row), None)
        branch_operation = next(
            row for row in records
            if row.get("kind") == "create_branch"
            and row.get("payload", {}).get("child_workflow_session_id")
            == child["workflow_session_id"]
        )
        result = {
            "workflow_session_id": child["workflow_session_id"],
            "source_workflow_session_id": source_id,
            "visible_message_id": child["source"]["visible_message_id"],
            "fork_anchor_id": child["source"]["fork_anchor_id"],
            "role": anchor["role"],
            "pending_input_id": pending["input_id"] if pending is not None else None,
            "active_workflow_session_id": (
                child["workflow_session_id"] if switch
                else branch_operation["payload"].get(
                    "active_workflow_session_id_at_creation", source_id,
                )
            ),
            "status": "pending" if pending is not None else "created",
        }
        if switch:
            result["selection_revision"] = selection["revision"]
        return result

    def create_branch(
        self, source_workflow_session_id: str, visible_message_id: str, *,
        idempotency_key: str, expected_source_revision: int,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        return self._create_branch(
            source_workflow_session_id, visible_message_id,
            idempotency_key=idempotency_key,
            expected_source_revision=expected_source_revision,
            switch=False, candidate_id=candidate_id,
        )

    def create_and_switch_branch(
        self, source_workflow_session_id: str, visible_message_id: str, *,
        idempotency_key: str, expected_source_revision: int,
        candidate_id: str | None = None,
        expected_selection_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._create_branch(
            source_workflow_session_id, visible_message_id,
            idempotency_key=idempotency_key,
            expected_source_revision=expected_source_revision,
            switch=True, candidate_id=candidate_id,
            expected_selection_revision=expected_selection_revision,
        )

    def _create_branch(
        self, source_workflow_session_id: str, visible_message_id: str, *,
        idempotency_key: str, expected_source_revision: int, switch: bool,
        candidate_id: str | None,
        expected_selection_revision: int | None = None,
        inherit_current_data: int | None = None,
        target_workflow_id: str | None = None,
    ) -> dict[str, Any]:
        request = LegacyControlRequest(
            "create_and_switch_branch" if switch else "create_branch",
            source_workflow_session_id, visible_message_id, idempotency_key,
            expected_source_revision,
        )
        request_digest = request.request_digest
        if candidate_id is not None or expected_selection_revision is not None or inherit_current_data is not None or target_workflow_id is not None:
            request_digest = "workflow-op-v1:" + content_digest({
                **request.to_mapping(),
                **({"candidate_id": candidate_id} if candidate_id is not None else {}),
                **({"expected_selection_revision": expected_selection_revision}
                   if expected_selection_revision is not None else {}),
                **({"inherit_current_data": inherit_current_data}
                   if inherit_current_data is not None else {}),
                **({"target_workflow_id": target_workflow_id} if target_workflow_id is not None else {}),
            }).rsplit(":", 1)[1]
        operation = "workflow.fork_switch" if switch else "workflow.fork"
        with self._lock, closing(self._store()) as store:
            self._check_open()
            source = self._session(store, source_workflow_session_id)
            key = source_workflow_session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest(operation, key)
            if receipt is not None:
                digest, saved = receipt
                if digest != request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return self._fork_receipt(saved, switch)
            if source["revision"] != expected_source_revision:
                raise ContractValidationError("Workflow session revision conflict")
            active_selection = self._session_selection(store)
            if switch and (active_selection is None or (
                expected_selection_revision is not None
                and active_selection["revision"] != expected_selection_revision
            )):
                raise ContractValidationError("Session selection revision conflict")
            self._assert_runtime_forkable(source_workflow_session_id, store)
            inherited_current = None
            if inherit_current_data is not None:
                inherited_current = self._program_current_state(store, source_workflow_session_id)
                if inherited_current["revision"] != inherit_current_data:
                    raise context_error("Source session data changed", status=409, reason="stale_revision")
            bundle = store.read_bundle()
            ref = next((row for row in self._formal_refs(store, source_workflow_session_id)
                        if row["visible_message_id"] == visible_message_id), None)
            if ref is None:
                raise ContractValidationError("Fork message was not found in the source session")
            message = next(row for row in bundle["visible_message"]
                           if row["visible_message_id"] == visible_message_id)
            if message["role"] != ref["role"]:
                raise ContractValidationError("Fork message role is inconsistent")
            if ref["role"] == "user" and ref["boundary"]["input_status"] != "completed":
                raise ContractValidationError("Only a completed user boundary can be forked")
            checkpoint_id = ref["boundary"].get("before_checkpoint_id", ref["boundary"].get("after_checkpoint_id"))
            if checkpoint_id is None:
                raise ContractValidationError("Fork message has no stable checkpoint")
            checkpoint = store.get_record("workflow_checkpoint", {"checkpoint_id": checkpoint_id})
            prefix = [row for row in self._formal_refs(store, source_workflow_session_id)
                      if row["sequence"] <= ref["sequence"]]
            floors = self._reply_candidate_floors(
                store, source_workflow_session_id, prefix, bundle,
                require_complete=True,
            )
            if candidate_id is not None and ref["role"] != "assistant":
                raise ContractValidationError("Candidate fork requires an assistant anchor")
            if ref["role"] == "assistant":
                if ref["boundary"].get("after_checkpoint_id") is None:
                    raise ContractValidationError("Assistant fork requires a completed output checkpoint")
                chain = store.get_record("chain_run", {"chain_run_id": ref["boundary"]["chain_run_id"]})
                if chain["status"] != "succeeded":
                    raise ContractValidationError("Assistant fork requires a completed workflow")
                if checkpoint["kind"] != "completed_output":
                    raise ContractValidationError("Assistant fork requires a completed output checkpoint")
                if not floors or not prefix or prefix[-1]["role"] != "assistant":
                    raise ContractValidationError("Assistant fork has no completed candidate floor")
                current_selected_id = floors[-1]["selected_candidate_id"]
                selected_id = candidate_id or current_selected_id
                if selected_id not in floors[-1]["candidate_ids"]:
                    raise ContractValidationError("Candidate is not in the frozen fork floor")
                selected_candidate = next(
                    row for row in bundle["workflow_candidate"]
                    if row["candidate_id"] == selected_id
                )
                result_commit = store.get_record("workflow_commit", {
                    "commit_id": selected_candidate["result_commit_id"],
                })
                result_state = store.get_record("state_snapshot", {
                    "state_snapshot_id": result_commit["state_snapshot_id"],
                })
                candidate_reply = result_state["visible_message_refs"][-1]
                if (candidate_reply["role"] != "assistant"
                        or result_state["visible_message_refs"][-2]["visible_message_id"]
                        != floors[-1]["user_visible_message_id"]
                        or candidate_reply["boundary"] != {
                            "chain_run_id": selected_candidate["chain_run_id"],
                            "output_id": selected_candidate["output_id"],
                            "after_checkpoint_id": selected_candidate["checkpoint_id"],
                        }):
                    raise ContractValidationError("Candidate differs from its fork floor")
                seed_checkpoint = store.get_record("workflow_checkpoint", {
                    "checkpoint_id": selected_candidate["checkpoint_id"],
                })
            chain = store.get_record("chain_run", {"chain_run_id": ref["boundary"]["chain_run_id"]})
            if chain["schema_version"] != 2:
                raise ContractValidationError("Legacy chain has no stable version anchor")
            if ref["role"] == "user":
                parent_commit_id = chain["base_commit_id"]
            else:
                parent_commit_id = selected_candidate["result_commit_id"]
            source_head = self._workflow_ref(store, source_workflow_session_id)
            anchor_id = _uid()
            child_id = _uid()
            child = _record(
                workflow_session_id=child_id,
                workflow_definition_id=source["workflow_definition_id"],
                definition_revision=source["definition_revision"], revision=1,
                source={"kind": "fork", "source_workflow_session_id": source_workflow_session_id,
                        "visible_message_id": visible_message_id, "fork_anchor_id": anchor_id},
            )
            child_nodes = []
            for node in (seed_checkpoint if ref["role"] == "assistant" else checkpoint)["nodes"]:
                child_nodes.append(_record(
                    workflow_session_id=child_id, node_binding_id=node["node_binding_id"],
                    data_version=node["data_version"], private_data=copy.deepcopy(node["private_data"]),
                ))
            child_checkpoint = _record(
                checkpoint_id=_uid(), workflow_session_id=child_id,
                workflow_definition_id=source["workflow_definition_id"],
                definition_revision=source["definition_revision"], revision=1, kind="initial",
                nodes=copy.deepcopy((seed_checkpoint if ref["role"] == "assistant" else checkpoint)["nodes"]),
                selection_refs=copy.deepcopy(
                    (seed_checkpoint if ref["role"] == "assistant" else checkpoint)["selection_refs"]
                ),
            )
            anchor_values = {
                "fork_anchor_id": anchor_id,
                "source_workflow_session_id": source_workflow_session_id,
                "visible_message_id": visible_message_id,
                "role": ref["role"], "checkpoint_id": checkpoint_id,
            }
            pending_input = None
            if ref["role"] == "user":
                pending_input = _record(
                    input_id=_uid(), port_id="request", payload_schema_ref=message["payload_schema_ref"],
                    source={"kind": "visible_message", "visible_message_id": visible_message_id},
                    payload=copy.deepcopy(message["payload"]),
                )
                anchor_values["pending_input"] = {
                    "input_id": pending_input["input_id"],
                    "source_visible_message_id": visible_message_id,
                    "port_id": pending_input["port_id"],
                    "payload_schema_ref": copy.deepcopy(pending_input["payload_schema_ref"]),
                    "payload": copy.deepcopy(pending_input["payload"]),
                }
            else:
                anchor_values.update(
                    chain_run_id=ref["boundary"]["chain_run_id"],
                    output_id=ref["boundary"]["output_id"],
                )
            if ref["role"] == "assistant":
                floors[-1]["selected_candidate_id"] = selected_id
            anchor_values["candidate_floors"] = floors
            anchor = _record(**anchor_values)
            validate_fork_request(bundle, anchor)
            child_refs = []
            for original in prefix:
                child_ref = copy.deepcopy(original)
                child_ref["workflow_session_id"] = child_id
                if original["visible_message_id"] == visible_message_id and ref["role"] == "user":
                    child_ref["boundary"] = copy.deepcopy(original["boundary"])
                    child_ref["boundary"].update(
                        input_id=pending_input["input_id"], input_status="pending", chain_run_id=None,
                    )
                child_refs.append(child_ref)
            if ref["role"] == "assistant" and selected_id != current_selected_id:
                child_refs[-1] = _record(
                    workflow_session_id=child_id,
                    visible_message_id=candidate_reply["visible_message_id"],
                    sequence=ref["sequence"], role="assistant",
                    boundary=copy.deepcopy(candidate_reply["boundary"]),
                )
            payload = {
                "requested_action": request.kind,
                "child_workflow_session_id": child_id,
                "fork_anchor_id": anchor_id,
                "parent_commit_id": parent_commit_id,
                "configuration_chain_run_id": (
                    selected_candidate["chain_run_id"] if ref["role"] == "assistant" else chain["chain_run_id"]
                ),
                "active_workflow_session_id_at_creation": (
                    active_selection["active_workflow_session_id"]
                    if active_selection is not None else source_workflow_session_id
                ),
            }
            if candidate_id is not None:
                payload["candidate_id"] = candidate_id
            branch_operation = self._operation(
                "create_branch", source_workflow_session_id, "visible_message",
                visible_message_id, idempotency_key, [
                    {"kind": "workflow_session", "id": source_workflow_session_id,
                     "revision": expected_source_revision},
                    {"kind": "workflow_ref", "id": source_head["workflow_ref_id"],
                     "revision": source_head["revision"]},
                ], payload,
                expected_head_commit_id=source_head["head_commit_id"],
            )
            child_snapshot = self._state_snapshot(
                child_id, child_checkpoint["nodes"], child_checkpoint["selection_refs"],
                child_refs, pending_input_id=pending_input["input_id"] if pending_input else None,
            )
            child_commit = _record(
                commit_id=_uid(), workflow_session_id=child_id,
                state_snapshot_id=child_snapshot["state_snapshot_id"],
                parent_commit_id=parent_commit_id,
                operation_id=branch_operation["operation_id"],
                source={"kind": "session_seed", "workflow_session_id": child_id},
            )
            child_head = _record(
                workflow_ref_id=_uid(), workflow_session_id=child_id,
                head_commit_id=child_commit["commit_id"], revision=1,
            )
            source_update = copy.deepcopy(source)
            source_update["revision"] += 1
            records = [("workflow_session", source_update), ("workflow_session", child),
                       *[("node_session", row) for row in child_nodes],
                       ("workflow_checkpoint", child_checkpoint), ("fork_anchor", anchor),
                        *[("visible_message_ref", row) for row in child_refs],
                        ("workflow_operation", branch_operation),
                        ("state_snapshot", child_snapshot), ("workflow_commit", child_commit),
                        ("workflow_ref", child_head)]
            if pending_input is not None:
                records.append(("node_input", pending_input))
            if switch:
                selection_revision = active_selection["revision"]
                selection_update = copy.deepcopy(active_selection)
                selection_update.update(
                    active_workflow_session_id=child_id,
                    revision=selection_revision + 1,
                )
                switch_operation = self._operation(
                    "create_and_switch_branch", source_workflow_session_id,
                    "visible_message", visible_message_id, idempotency_key, [
                        {"kind": "workflow_session", "id": source_workflow_session_id,
                         "revision": expected_source_revision},
                        {"kind": "workflow_ref", "id": source_head["workflow_ref_id"],
                         "revision": source_head["revision"]},
                        {"kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID,
                         "revision": selection_revision},
                    ], {"child_workflow_session_id": child_id},
                    expected_head_commit_id=source_head["head_commit_id"],
                )
                records.extend([
                    ("workflow_operation", switch_operation),
                    ("session_selection", selection_update),
                ])
            source_variable_ref = VariableStore(store).get_bound_state_ref("workflow_commit", parent_commit_id)
            source_program_ref = ProgramVariableStore(store).reference("workflow_commit", parent_commit_id)

            def bind_fork(saved_store, saved_records):
                resources = WorkbenchResourceStore(saved_store)
                resources.bind_session_owner(child_id, target_workflow_id or resources.session_owner(source_workflow_session_id))
                if source_program_ref is not None or inherited_current is not None:
                    program_variables = ProgramVariableStore(saved_store)
                    inherited = inherited_current if inherited_current is not None else program_variables.read(
                        source_program_ref["session_id"], source_program_ref["revision"],
                    )
                    copied = program_variables.write_in_transaction(
                        child_id, inherited, expected_revision=0,
                        key="program:fork:" + anchor_id,
                        request={"kind": "fork", "session_id": child_id, "source_ref": source_program_ref,
                                 "current_revision": inherit_current_data},
                    )
                    if source_program_ref is not None:
                        program_variables.bind("fork_anchor", anchor_id, source_program_ref["session_id"],
                                               source_program_ref["revision"])
                    program_variables.bind("state_snapshot", child_snapshot["state_snapshot_id"],
                                           child_id, copied["state"]["revision"])
                    program_variables.bind("workflow_commit", child_commit["commit_id"],
                                           child_id, copied["state"]["revision"])
                if source_variable_ref is None:
                    return
                variables = VariableStore(saved_store)
                reference = source_variable_ref
                if reference["workflow_session_id"] != source_workflow_session_id:
                    inherited = variables.read_snapshot(reference, node_binding_id=B_BINDING)
                    inherited["workflow_session_id"] = source_workflow_session_id
                    inherited["values"]["workflow_session_id"]["value"] = source_workflow_session_id
                    current = variables.get_current(
                        workflow_session_id=source_workflow_session_id, workflow_id=reference["workflow_id"],
                        registry_revision=reference["registry_revision"],
                    )
                    restored = variables.prepare_snapshot_in_transaction(
                        inherited, expected_revision=current["revision"] if current else 0,
                        idempotency_key="variables:anchor:" + anchor_id, source_ref=reference,
                    )
                    reference = _variable_ref(restored["after"])
                variables.bind_state_in_transaction("fork_anchor", anchor_id, reference)
                copied = variables.fork_in_transaction(
                    source_ref=reference, workflow_session_id=child_id,
                    expected_revision=0, idempotency_key="variables:fork:" + anchor_id,
                )
                reference = _variable_ref(copied["after"])
                variables.bind_state_in_transaction("state_snapshot", child_snapshot["state_snapshot_id"], reference)
                variables.bind_state_in_transaction("workflow_commit", child_commit["commit_id"], reference)

            saved = store.save_bundle_prepared(
                lambda _: records, key, operation=operation,
                expected_session_revisions={source_workflow_session_id: expected_source_revision,
                                            child_id: 0},
                 expected_ref_heads={child_head["workflow_ref_id"]: (0, None)},
                expected_session_selection_revisions=(
                    {ACTIVE_SESSION_SELECTION_ID: active_selection["revision"]}
                    if switch else None
                ),
                request_digest=request_digest, after_save=bind_fork,
            )
            return self._fork_receipt(saved, switch)

    def continue_pending_input(
        self, session_id: str, input_id: str, *,
        idempotency_key: str, expected_session_revision: int,
    ) -> dict[str, Any]:
        request = LegacyControlRequest(
            "continue_pending_input", session_id, input_id, idempotency_key,
            expected_session_revision,
        )
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest("workflow.pending", key)
            if receipt is not None:
                digest, saved = receipt
                if digest != request.request_digest:
                    # A pre-fingerprint receipt still proves its input and initial revision.
                    legacy_input = next((row for row in saved if row.get("input_id") == input_id
                                         and "node_run_ids" in row), None)
                    legacy_session = next((row for row in saved
                                           if row.get("workflow_session_id") == session_id
                                           and "revision" in row), None)
                    if (not digest.startswith("json-v1:sha256:") or legacy_input is None
                            or legacy_session is None
                            or legacy_session["revision"] != expected_session_revision + 1):
                        raise ContractValidationError(
                            "Idempotency key has different workflow operation request"
                        )
                return self._submission_receipt(saved)
            if session["revision"] != expected_session_revision:
                raise ContractValidationError("Workflow session revision conflict")
            self._assert_runtime_forkable(session_id, store)
            refs = [row for row in store.list_records("visible_message_ref")
                    if row["workflow_session_id"] == session_id and row["role"] == "user"
                    and row["boundary"]["input_id"] == input_id]
            if len(refs) != 1 or refs[0]["boundary"]["input_status"] != "pending":
                raise ContractValidationError("Pending input is not available")
            node_input = store.get_record("node_input", {"input_id": input_id})
            nodes = self._node_states(store, session_id)
            head = self._workflow_ref(store, session_id)
            before = self._checkpoint(session_id, nodes, self._selections(store, session_id),
                kind="before_input", input_id=input_id, revision=session["revision"] + 1)
            chain_id = _uid()
            operation = self._operation(
                "continue_pending_input", session_id, "node_input", input_id,
                idempotency_key, [{
                    "kind": "workflow_session", "id": session_id,
                    "revision": expected_session_revision,
                }], {"chain_run_id": chain_id, "base_commit_id": head["head_commit_id"],
                     "base_ref_revision": head["revision"]},
            )
            ref = copy.deepcopy(refs[0])
            ref["boundary"].update(input_status="running", chain_run_id=chain_id)
            def build(saved_store):
                source_chain = self._session_configuration_chain(saved_store, session_id)
                model_plan = self._chain_model_plan(saved_store, source_chain) if source_chain is not None else None
                prompt_plan = self._chain_prompt_selection_plan(saved_store, source_chain) if source_chain is not None else None
                self._verify_model_plan(model_plan, store=saved_store)
                snapshot, prepared_run = self._prepare_node(
                    saved_store, session_id, chain_id, "A", node_input,
                    model_selection_plan=model_plan,
                    context_config=prompt_plan["nodes"].get("A") if prompt_plan is not None else None,
                    prompt_selection_plan=prompt_plan,
                )
                chain = _record(schema_version=2, chain_run_id=chain_id, workflow_session_id=session_id,
                    input_id=input_id, status="prepared", node_run_ids=[prepared_run["run_id"]], output_id=None,
                    base_commit_id=head["head_commit_id"], base_ref_revision=head["revision"],
                    operation_id=operation["operation_id"])
                return [
                    ("workflow_checkpoint", before), ("input_snapshot", snapshot),
                    ("run_record", prepared_run), ("chain_run", chain), ("workflow_operation", operation),
                    ("visible_message_ref", ref),
                ]
            records = self._save_prepared(
                store, session_id, build, key, operation="workflow.pending", request_digest=request.request_digest,
            )
            run = next(row for row in records if row.get("profile") == "agent")
            self._active_runs[run["run_id"]] = ActiveRun(
                run["run_id"], chain_id, session_id, head["head_commit_id"],
            )
            try:
                self._futures[session_id] = self._executor.submit(
                    self._execute_chain, session_id, chain_id, run["run_id"],
                )
            except Exception:
                self._fail(session_id, chain_id, run["run_id"], "DISPATCH_FAILED")
            return self._submission_receipt(records)

    def retry_archive(
        self, session_id: str, run_id: str, *, idempotency_key: str,
        expected_session_revision: int,
    ) -> dict[str, Any]:
        """Retry only the durable closeout of an accepted in-process final.

        The accepted model result is retained in ``_active_results``. This
        method never reruns that node's adapter or kernel. After an A closeout,
        the first B execution still runs through the normal chain owner.
        Recovery after process restart is intentionally outside this first
        implementation; the persisted state becomes recovery-unavailable.
        """
        request = LegacyControlRequest(
            "retry_archive", session_id, run_id, idempotency_key, expected_session_revision,
        )
        receipt_key = (session_id, idempotency_key)
        with self._lock:
            self._check_open()
            previous = self._archive_retry_receipts.get(receipt_key)
            if previous is not None:
                if previous[0] != request.request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return copy.deepcopy(previous[1])
            package = self._active_results.get(run_id)
            if not isinstance(package, dict) or package.get("session_id") != session_id:
                raise ContractValidationError("Accepted final is not available in this process")
            with closing(self._store()) as store:
                session = self._session(store, session_id)
                if session["revision"] != expected_session_revision:
                    raise ContractValidationError("Workflow session revision conflict")
                run = store.get_record("run_record", {"run_id": run_id})
                ports = ResultPortStore(store, allowed_bindings=(A_BINDING, B_BINDING))
                if (run["status"] not in ("running", "final_ready", "succeeded")
                        or run["status"] == "succeeded" and ports.get_release(run_id) is not None):
                    raise ContractValidationError("Only an accepted unreleased result can retry archive")
                if package["run"]["run_id"] != run_id:
                    raise ContractValidationError("Accepted final identity differs from the run")
                accept_retry(
                    self, store, session_id=session_id, run_id=run_id,
                    chain_run_id=package["chain_run_id"],
                )

            output = self._archive_accepted_node(package)
            result = {
                "workflow_session_id": session_id, "run_id": run_id,
                "chain_run_id": package["chain_run_id"],
                "status": "accepted", "completion": "unconfirmed",
            }
            self._archive_retry_receipts[receipt_key] = (request.request_digest, copy.deepcopy(result))
            if package["stage"] == "A":
                completed = self._execute_chain(
                    session_id, package["chain_run_id"], run_id, archived_output=output,
                )
                if not completed:
                    return result
            else:
                self._publish(session_id, package["chain_run_id"], output)
                self._active_results.pop(run_id, None)
                self._errors.pop(session_id, None)
                self._execution_failures.pop(session_id, None)
            result = {
                "workflow_session_id": session_id, "run_id": run_id,
                "chain_run_id": package["chain_run_id"], "status": "succeeded",
            }
            self._archive_retry_receipts[receipt_key] = (request.request_digest, copy.deepcopy(result))
            return result

    def retry_publish(
        self, session_id: str, run_id: str, *, idempotency_key: str,
        expected_session_revision: int,
    ) -> dict[str, Any]:
        """Retry publishing an already archived B result to the UI projection."""
        request = LegacyControlRequest(
            "retry_publish", session_id, run_id, idempotency_key, expected_session_revision,
        )
        receipt_key = (session_id, idempotency_key)
        with self._lock:
            self._check_open()
            previous = self._publish_retry_receipts.get(receipt_key)
            if previous is not None:
                if previous[0] != request.request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return copy.deepcopy(previous[1])
            package = self._active_results.get(run_id)
            with closing(self._store()) as store:
                session = self._session(store, session_id)
                if session["revision"] != expected_session_revision:
                    raise ContractValidationError("Workflow session revision conflict")
                run = store.get_record("run_record", {"run_id": run_id})
                chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
                candidates = [row for row in store.list_records("workflow_candidate")
                              if row["chain_run_id"] == chain["chain_run_id"]]
                if (run["workflow_session_id"] != session_id or run["node_binding_id"] != B_BINDING
                        or run["status"] != "succeeded"
                        or chain["status"] not in ("running", "succeeded")
                        or (chain["status"] == "succeeded" and len(candidates) != 1)):
                    raise ContractValidationError("Only an unpublished completed output can retry publish")
                submission = ResultPortStore(store, allowed_bindings=(A_BINDING, B_BINDING)).get_submission(run_id)
                if submission is not None and ResultPortStore(
                    store, allowed_bindings=(A_BINDING, B_BINDING),
                ).read_port(run_id, "final") is None:
                    raise ContractValidationError("Result ports must be released before workflow publication")
                if candidates:
                    reroll = any(
                        row["chain_run_id"] == chain["chain_run_id"]
                        for row in store.list_records("chain_input_origin")
                    )
                    delivery = next(
                        (row for row in store.list_records("output_delivery")
                         if row["output_id"] == candidates[0]["output_id"]
                         and row["target"] == {"kind": "ui", "workflow_session_id": session_id}),
                        None,
                    )
                    if reroll:
                        operation = store.get_record("workflow_operation", {
                            "operation_id": chain["operation_id"],
                        })
                        expected_head = operation["expected_head_commit_id"]
                        if (delivery is not None
                                or self._workflow_ref(store, session_id)["head_commit_id"]
                                != expected_head):
                            raise ContractValidationError(
                                "Only an unpublished output can retry publish"
                            )
                    elif delivery is None or delivery["status"] != "pending":
                        raise ContractValidationError("Only a pending output can retry publish")
                    upstream = next(
                        row for row in store.list_records("workflow_output")
                        if row["source"]["run_id"] == run_id
                    )
                else:
                    if (not isinstance(package, dict) or package.get("session_id") != session_id
                            or package.get("stage") != "B" or not package.get("retain_result")):
                        raise ContractValidationError("Accepted output is not available in this process")
                    upstream = package["output"]
                accept_retry(
                    self, store, session_id=session_id, run_id=run_id,
                    chain_run_id=chain["chain_run_id"],
                )
            self._publish(session_id, chain["chain_run_id"], upstream)
            self._active_results.pop(run_id, None)
            self._errors.pop(session_id, None)
            self._execution_failures.pop(session_id, None)
            result = {
                "workflow_session_id": session_id, "run_id": run_id,
                "chain_run_id": chain["chain_run_id"], "status": "succeeded",
            }
            self._publish_retry_receipts[receipt_key] = (request.request_digest, copy.deepcopy(result))
            return result

    @staticmethod
    def _budget_limits(run, operations):
        initial = run["initial_budget"]
        requests = initial["max_model_requests"]
        attempts = initial.get("max_model_attempts", requests * 4)
        for operation in operations:
            if (operation["kind"] == "extend_budget"
                    and operation["target"] == {"kind": "run_record", "id": run["run_id"]}):
                requests += operation["payload"]["additional_model_requests"]
                attempts += operation["payload"]["additional_model_attempts"]
        return {"max_model_requests": requests, "max_model_attempts": attempts}

    @staticmethod
    def _checkpoint_has_budget(checkpoint, limits):
        if checkpoint is not None and any(
            tool.name == "final_answer" for tool in checkpoint.pending_tools
        ):
            return True
        requests = checkpoint.model_requests if checkpoint is not None else 0
        attempts = checkpoint.attempts if checkpoint is not None else 0
        return (requests < limits["max_model_requests"]
                and attempts < limits["max_model_attempts"])

    def _assert_safe_checkpoint(self, store, session_id, run, chain):
        if not self._kernel_supports(run, "pause_resume"):
            raise control_error("Kernel does not support same-run controls", "unsupported")
        run_id = run["run_id"]
        future = self._futures.get(session_id)
        active = self._active_runs.get(run_id)
        if (run["status"] not in ("paused", "failed")
                or chain["status"] != run["status"]
                or run_id not in self._run_checkpoints or active is None
                or (future is not None and not future.done())
                or run_id in self._finalizing_runs
                or any(tool.status in ("started", "unknown")
                       for tool in active.snapshot().pending_tools)):
            raise ContractValidationError("Run has no safe in-process checkpoint")
        if run["status"] == "paused" and session_id in self._execution_failures:
            raise ContractValidationError("Paused runtime has an explicit execution fault")
        checkpoint = self._run_checkpoints[run_id]
        facts = store.read_execution_facts(run_id)
        if run["status"] == "failed":
            failure = self._execution_failures.get(session_id)
            failures = [row["payload"] for row in facts if row["kind"] == "execution_failed"]
            if (not isinstance(failure, RunFailed) or isinstance(failure, KernelContractError)
                    or checkpoint is None or failure.checkpoint is not checkpoint
                    or not failures or failures[-1]["code"] != failure.code
                    or failures[-1]["category"] not in ("model", "protocol")):
                raise ContractValidationError("Failure has no trusted recoverable model checkpoint")
        accepted = [row["payload"]["message"] for row in facts if row["kind"] == "message_accepted"]
        requests = sum(row["kind"] == "model_request" for row in facts)
        attempts = sum(row["kind"] == "model_attempt_started" for row in facts)
        if checkpoint is None:
            if facts or active.snapshot().accepted_progress != AcceptedProgress():
                raise ContractValidationError("Unstarted checkpoint has execution facts")
        else:
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
            _, kernel, _, _ = self._snapshot_components(snapshot)
            validate = getattr(kernel.implementation, "validate_checkpoint", None)
            if not callable(validate):
                raise ContractValidationError("Kernel does not support checkpoint validation")
            validate(checkpoint, copy.deepcopy(snapshot))
            if (checkpoint.model_requests != requests or checkpoint.attempts != attempts
                    or canonical_bytes(list(checkpoint.messages)) != canonical_bytes(accepted)
                    or {row["payload"]["attempt_id"] for row in facts
                        if row["kind"] == "model_attempt_started"} !=
                    {row["payload"]["attempt_id"] for row in facts
                        if row["kind"] == "model_attempt_finished"}):
                raise ContractValidationError("Checkpoint differs from saved execution facts")
        return checkpoint

    @staticmethod
    def _run_control_receipt(records, status):
        run = next(row for row in records if row.get("profile") == "agent"
                   and "run_id" in row and "chain_run_id" in row)
        return {
            "workflow_session_id": run["workflow_session_id"],
            "chain_run_id": run["chain_run_id"],
            "run_id": run["run_id"],
            "status": status,
        }

    def interrupt(
        self, session_id: str, run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_run_revision: int,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            run = store.get_record("run_record", {"run_id": run_id})
            if run["workflow_session_id"] != session_id:
                raise ContractValidationError("Run belongs to another workflow session")
            chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
            request = RunControlRequest(
                "interrupt", session_id, chain["chain_run_id"], run_id,
                idempotency_key, expected_session_revision, expected_run_revision,
            )
            key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest("workflow.interrupt", key)
            if receipt is not None:
                if receipt[0] != request.request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                saved = next(row for row in receipt[1] if row.get("run_id") == run_id)
                return self._run_control_receipt(receipt[1], saved["status"])
            if (session["revision"] != expected_session_revision
                    or run["revision"] != expected_run_revision):
                raise ContractValidationError("Workflow or run revision conflict")
            if not self._kernel_supports(run, "pause_resume"):
                raise control_error("Kernel does not support same-run controls", "unsupported")
            if (run_id not in self._active_runs or run_id in self._finalizing_runs
                    or run["status"] not in ("prepared", "running")
                    or chain["status"] not in ("prepared", "running")
                    or (run["status"] == "running"
                        and (self._futures.get(session_id) is None
                             or self._futures[session_id].done()))):
                raise ContractValidationError("Run is not interruptible")
            immediate = run["status"] == "prepared"
            previous_status = run["status"]
            run["status"] = "paused" if immediate else "pausing"
            run["revision"] += 1
            records = [("run_record", run)]
            if immediate:
                chain["status"] = "paused"
                records.append(("chain_run", chain))
            operation = self._operation(
                "interrupt", session_id, "run_record", run_id, idempotency_key,
                request.to_mapping()["expected_revisions"],
                {"chain_run_id": chain["chain_run_id"]},
            )
            saved = self._save(
                store, session_id, [*records, ("workflow_operation", operation)],
                key, operation="workflow.interrupt",
                request_digest=request.request_digest,
            )
            self._interrupting.add(run_id)
            self._ensure_event_source(store, run)
            self._publish_control_event(
                run, next(row for row in saved if row.get("kind") == "interrupt"),
            )
            self._clear_event_preview(run_id, "pause_requested")
            if immediate:
                self._run_checkpoints[run_id] = None
                active = self._active_runs[run_id]
                view = active.snapshot()
                active.fence(expected_generation=view.generation, expected_revision=view.revision)
                self._sync_event_generation(run_id)
            self._publish_run_state(run, previous_status, reason_code="pause_requested")
            return self._run_control_receipt(saved, run["status"])

    def resume(
        self, session_id: str, run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_run_revision: int,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            run = store.get_record("run_record", {"run_id": run_id})
            if run["workflow_session_id"] != session_id:
                raise ContractValidationError("Run belongs to another workflow session")
            chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
            request = RunControlRequest(
                "resume", session_id, chain["chain_run_id"], run_id,
                idempotency_key, expected_session_revision, expected_run_revision,
            )
            key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest("workflow.resume", key)
            if receipt is not None:
                if receipt[0] != request.request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return self._run_control_receipt(receipt[1], "running")
            if (session["revision"] != expected_session_revision
                    or run["revision"] != expected_run_revision):
                raise ContractValidationError("Workflow or run revision conflict")
            checkpoint = self._assert_safe_checkpoint(store, session_id, run, chain)
            self._verify_model_plan(self._chain_model_plan(store, chain))
            limits = self._budget_limits(run, store.list_records("workflow_operation"))
            if not self._checkpoint_has_budget(checkpoint, limits):
                raise control_error("Run budget is exhausted", "budget_blocked")
            payload = {"chain_run_id": chain["chain_run_id"]}
            previous_status = run["status"]
            if run["status"] == "failed":
                payload["recovery_code"] = self._execution_failures[session_id].code
            run["status"] = "running"
            run["revision"] += 1
            chain["status"] = "running"
            operation = self._operation(
                "resume", session_id, "run_record", run_id, idempotency_key,
                request.to_mapping()["expected_revisions"],
                payload,
            )
            saved = self._save(
                store, session_id, [
                    ("run_record", run), ("chain_run", chain),
                    ("workflow_operation", operation),
                ], key, operation="workflow.resume",
                request_digest=request.request_digest,
            )
            self._interrupting.discard(run_id)
            self._errors.pop(session_id, None)
            self._ensure_event_source(store, run)
            self._sync_event_generation(run_id)
            self._clear_event_preview(run_id, "resumed")
            self._publish_control_event(
                run, next(row for row in saved if row.get("kind") == "resume"),
            )
            self._publish_run_state(run, previous_status, reason_code="resumed")
            stage = "A" if run["node_binding_id"] == A_BINDING else "B"
            try:
                self._futures[session_id] = self._executor.submit(
                    self._execute_chain, session_id, chain["chain_run_id"], run_id, stage,
                )
            except Exception:
                self._fail(session_id, chain["chain_run_id"], run_id, "DISPATCH_FAILED")
            return self._run_control_receipt(saved, "running")

    def extend_budget(
        self, session_id: str, run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_run_revision: int,
        additional_model_requests: int = 0, additional_model_attempts: int = 0,
    ) -> dict[str, Any]:
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            run = store.get_record("run_record", {"run_id": run_id})
            if run["workflow_session_id"] != session_id:
                raise ContractValidationError("Run belongs to another workflow session")
            chain = store.get_record("chain_run", {"chain_run_id": run["chain_run_id"]})
            request = BudgetControlRequest(
                "extend_budget", session_id, chain["chain_run_id"], run_id,
                idempotency_key, expected_session_revision, expected_run_revision,
                additional_model_requests, additional_model_attempts,
            )
            key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest("workflow.extend_budget", key)
            if receipt is not None:
                if receipt[0] != request.request_digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return self._budget_receipt(receipt[1])
            if (session["revision"] != expected_session_revision
                    or run["revision"] != expected_run_revision):
                raise ContractValidationError("Workflow or run revision conflict")
            checkpoint = self._assert_safe_checkpoint(store, session_id, run, chain)
            limits = self._budget_limits(run, store.list_records("workflow_operation"))
            if self._checkpoint_has_budget(checkpoint, limits):
                raise control_error("Run budget is not exhausted", "budget_blocked")
            limits["max_model_requests"] += additional_model_requests
            limits["max_model_attempts"] += additional_model_attempts
            if limits["max_model_requests"] > 64 or limits["max_model_attempts"] > 256:
                raise control_error("Run budget extension exceeds its bounded limit", "budget_blocked")
            run["revision"] += 1
            operation = self._operation(
                "extend_budget", session_id, "run_record", run_id, idempotency_key,
                request.to_mapping()["expected_revisions"],
                {"chain_run_id": chain["chain_run_id"],
                 "additional_model_requests": additional_model_requests,
                 "additional_model_attempts": additional_model_attempts, **limits},
            )
            saved = self._save(store, session_id, [
                ("run_record", run), ("workflow_operation", operation),
            ], key, operation="workflow.extend_budget", request_digest=request.request_digest)
            self._ensure_event_source(store, run)
            self._sync_event_generation(run_id)
            saved_operation = next(row for row in saved if row.get("kind") == "extend_budget")
            self._publish_control_event(run, saved_operation)
            for kind, added, used in (
                ("model_requests", additional_model_requests,
                 checkpoint.model_requests if checkpoint is not None else 0),
                ("model_attempts", additional_model_attempts,
                 checkpoint.attempts if checkpoint is not None else 0),
            ):
                if added:
                    self._publish_event(run_id, "budget_changed", {
                        "budget_kind": kind, "additional": added,
                        "limit": limits["max_" + kind], "used": used,
                        "reason_code": "manual_extension",
                    }, once="budget:" + saved_operation["operation_id"] + ":" + kind,
                        visibility="business_candidate")
            return self._budget_receipt(saved)

    @staticmethod
    def _budget_receipt(records):
        operation = next(row for row in records if row.get("kind") == "extend_budget")
        run = next(row for row in records if row.get("profile") == "agent")
        return {
            "workflow_session_id": run["workflow_session_id"],
            "chain_run_id": run["chain_run_id"], "run_id": run["run_id"],
            "status": run["status"], "revision": run["revision"],
            "budget": {field: operation["payload"][field]
                       for field in ("max_model_requests", "max_model_attempts")},
        }

    def submit(
        self, session_id: str, text: str, idempotency_key: str, *,
        prompt_selection: dict[str, Any] | None = None,
        model_selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if type(text) is not str or not text.strip() or len(text) > 12000:
            raise ContractValidationError("Input must contain 1 to 12000 characters")
        if type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 128:
            raise ContractValidationError("A bounded idempotency key is required")
        if prompt_selection is not None:
            prompt_selection = validate_prompt_selection(prompt_selection)
        if model_selection is not None:
            model_selection = validate_model_selection(model_selection)
        request = {
            "workflow_session_id": session_id, "text": text,
            "prompt_selection": prompt_selection,
        }
        # Preserve historical request fingerprints for callers without this field.
        if model_selection is not None:
            request["model_selection"] = model_selection
        digest = _request_digest(request)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            self._session(store, session_id)
            key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest("workflow.submit", key)
            if receipt is not None:
                original = next(row for row in receipt[1] if row.get("role") == "user" and "payload" in row)
                if (original["payload"] != {"text": text}
                        or receipt[0].startswith("workflow-op-v1:") and receipt[0] != digest
                        or not receipt[0].startswith("workflow-op-v1:")
                        and (prompt_selection is not None or model_selection is not None)):
                    raise control_error("Idempotency key has different input content", "idempotency_conflict")
                return self._submission_receipt(receipt[1])
            view = self._view(store, session_id)
            if not view["can_submit"]:
                raise ContractValidationError("Workflow session has an unfinished execution")
            if model_selection is None and view["model_selection"] is not None:
                raise configuration_error("model_selection_required", 409)
            head = self._workflow_ref(store, session_id)
            input_id, visible_id, chain_id = _uid(), _uid(), _uid()
            node_input = _record(input_id=input_id, port_id="request", payload_schema_ref=TEXT_SCHEMA_REF,
                source={"kind": "visible_message", "visible_message_id": visible_id}, payload={"text": text})
            user = _record(visible_message_id=visible_id, origin_workflow_session_id=session_id,
                role="user", payload_schema_ref=TEXT_SCHEMA_REF, payload={"text": text},
                source={"kind": "workflow_input", "input_id": input_id})
            nodes = self._node_states(store, session_id)
            before = self._checkpoint(session_id, nodes, self._selections(store, session_id),
                kind="before_input", input_id=input_id, revision=view["revision"])
            operation = self._operation(
                "submit", session_id, "workflow_session", session_id,
                idempotency_key, [{
                    "kind": "workflow_session", "id": session_id,
                    "revision": view["revision"],
                }], {"chain_run_id": chain_id, "base_commit_id": head["head_commit_id"],
                     "base_ref_revision": head["revision"]},
            )
            ref = _record(workflow_session_id=session_id, visible_message_id=visible_id,
                role="user", sequence=max(
                    (row["sequence"] for row in store.list_records("visible_message_ref")
                     if row["workflow_session_id"] == session_id),
                    default=0,
                ) + 1,
                boundary={"before_checkpoint_id": before["checkpoint_id"], "input_id": input_id,
                          "input_status": "running", "chain_run_id": chain_id})
            def build(saved_store):
                if model_selection is not None and self.mode == "offline" and self._factory is None:
                    record = ModelConfigurationStore(saved_store).get_revision(
                        "model", model_selection["config_id"], model_selection["revision"],
                    )
                    if record and record["nodes"]:
                        raise configuration_error("offline_model_unsupported", 409)
                model_plan = (
                    resolve_model_selection(model_selection, ModelConfigurationStore(saved_store))
                    if model_selection is not None else None
                )
                if model_plan and model_plan["nodes"]:
                    if not self._supports_selected_model():
                        raise configuration_error("model_factory_unsupported", 409)
                    for binding_config in model_plan["nodes"].values():
                        try:
                            self._registry.resolve(
                                "adapter", ADAPTER_COMPONENT, VERSION,
                                required_capabilities=("canonical_messages", "native_tools", "non_streaming"),
                                config=_config(ADAPTER_COMPONENT, binding_config["parameters"]),
                            )
                        except ContractValidationError:
                            raise configuration_error("model_parameters_unsupported", 409) from None
                selected = (
                    resolve_prompt_selection(
                        prompt_selection, PromptConfigStore(saved_store),
                        workflow_definition_id=self._definition_id,
                        workflow_session_id=session_id,
                        node_bindings={"A": A_BINDING, "B": B_BINDING},
                    ) if prompt_selection is not None else {}
                )
                selected = {
                    stage: compose_selected_prompt_context(
                        materialized,
                        self._resolved[stage][3]["payload"]["context"]
                        if PREPARATION_CAPABILITY in self._resolved[stage][0].descriptor["capabilities"]
                        else None,
                    )
                    for stage, materialized in selected.items()
                }
                self._resolve_global_programs(saved_store, selected)
                snapshot, prepared_run = self._prepare_node(
                    saved_store, session_id, chain_id, "A", node_input,
                    context_config=selected.get("A"),
                    prompt_selection_plan=make_prompt_selection_plan(selected) if prompt_selection is not None else None,
                    model_selection_plan=model_plan,
                )
                chain = _record(schema_version=2, chain_run_id=chain_id, workflow_session_id=session_id,
                    input_id=input_id, status="prepared", node_run_ids=[prepared_run["run_id"]], output_id=None,
                    base_commit_id=head["head_commit_id"], base_ref_revision=head["revision"],
                    operation_id=operation["operation_id"])
                return [
                    ("node_input", node_input), ("visible_message", user), ("visible_message_ref", ref),
                    ("workflow_checkpoint", before), ("input_snapshot", snapshot), ("run_record", prepared_run),
                    ("chain_run", chain), ("workflow_operation", operation),
                ]
            records = self._save_prepared(
                store, session_id, build, key, operation="workflow.submit", request_digest=digest,
            )
            run = next(row for row in records if row.get("profile") == "agent")
            self._active_runs[run["run_id"]] = ActiveRun(
                run["run_id"], chain_id, session_id, head["head_commit_id"],
            )
            # No adapter/tool invocation is allowed above this durable boundary.
            try:
                self._futures[session_id] = self._executor.submit(
                    self._execute_chain, session_id, chain_id, run["run_id"],
                )
            except Exception:
                # The receipt is already durable, so convert a scheduler failure
                # into the same terminal input failure used by execution errors.
                self._fail(session_id, chain_id, run["run_id"], "DISPATCH_FAILED")
            return self._submission_receipt(records)

    @staticmethod
    def _recovery_request(
        kind, session_id, chain_id, idempotency_key, expected_session_revision,
        expected_ref_revision, expected_head_commit_id,
    ):
        if type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 128:
            raise ContractValidationError("A bounded idempotency key is required")
        if (type(expected_session_revision) is not int or expected_session_revision < 1
                or type(expected_ref_revision) is not int or expected_ref_revision < 1
                or type(expected_head_commit_id) is not str):
            raise ContractValidationError("Expected workflow revisions and Head are required")
        return "workflow-op-v1:" + content_digest({
            "kind": kind, "workflow_session_id": session_id,
            "source_chain_run_id": chain_id, "idempotency_key": idempotency_key,
            "expected_session_revision": expected_session_revision,
            "expected_ref_revision": expected_ref_revision,
            "expected_head_commit_id": expected_head_commit_id,
        }).rsplit(":", 1)[1]

    def _assert_closeable(self, store, sid, chain):
        future = self._futures.get(sid)
        if chain["workflow_session_id"] != sid or chain["status"] not in (
            "failed", "recovery_unavailable", "prepared", "running", "paused",
        ):
            raise ContractValidationError("Execution cannot be closed from its current state")
        if future is not None and not future.done():
            raise ContractValidationError("Execution worker has not stopped")
        if chain["status"] == "paused" and (
            sid not in self._execution_failures
            or any(run_id in self._run_checkpoints for run_id in chain["node_run_ids"])
        ):
            raise ContractValidationError("A recoverable paused execution cannot be closed")
        if any(row["run_id"] in self._active_results
               for row in store.list_records("run_record")
               if row["workflow_session_id"] == sid):
            raise ContractValidationError("An accepted result must be archived or published")
        if any(row["target"] == {"kind": "ui", "workflow_session_id": sid}
               and row["status"] not in ("succeeded", "failed")
               for row in store.list_records("output_delivery")):
            raise ContractValidationError("Execution has an unsettled delivery")
        if chain["status"] in ("prepared", "running") and sid not in self._execution_failures:
            if any(run_id in self._active_runs or run_id in self._run_checkpoints
                   for run_id in chain["node_run_ids"]):
                raise ContractValidationError("Execution still has a live workspace")

    def _closeout_records(self, store, sid, chain, reason):
        runs = [
            store.get_record("run_record", {"run_id": run_id})
            for run_id in chain["node_run_ids"]
            if any(row["run_id"] == run_id for row in store.list_records("run_record"))
        ]
        unfinished = [row for row in runs if row["status"] != "succeeded"]
        refs = []
        diagnostic = {"code": "EXECUTION_INTERRUPTED", "category": "interrupted"}
        for run in runs:
            facts = store.read_execution_facts(run["run_id"])
            if run in unfinished:
                refs.append(fact_reference(run["run_id"], facts))
            failures = [row["payload"] for row in facts if row["kind"] == "execution_failed"]
            if failures:
                diagnostic = {field: failures[-1][field] for field in ("code", "category")}
        failure = self._execution_failures.get(sid)
        if getattr(failure, "recording_failed", False):
            diagnostic = {
                "code": (failure.code if failure.code == "execution_interrupted"
                         else "fact_persistence_error"),
                "category": "contract",
            }
        elif isinstance(failure, KernelContractError):
            diagnostic = {"code": failure.code, "category": "contract"}
        elif isinstance(failure, _HOST_INTERRUPTION_SIGNALS):
            diagnostic = {"code": "HOST_INTERRUPTED", "category": "interrupted"}
        elif failure is not None and not isinstance(failure, RunFailed):
            diagnostic = {"code": "PROGRAM_OR_STORAGE_FAILED", "category": "contract"}
        closeout = _record(
            closeout_id=_uid(), workflow_session_id=sid,
            chain_run_id=chain["chain_run_id"], reason=reason,
            run_ids=[row["run_id"] for row in unfinished],
            fact_refs=refs, diagnostic=diagnostic,
        )
        closed_chain = copy.deepcopy(chain)
        closed_chain["status"] = "closed"
        records = [("chain_run", closed_chain), ("execution_closeout", closeout)]
        for run in unfinished:
            run.update(status="closed", revision=run["revision"] + 1)
            records.append(("run_record", run))
        return records, closeout

    def _discard_closed_runtime(self, sid, run_ids):
        for run_id in run_ids:
            self._clear_event_preview(run_id, "execution_closed")
            active = self._active_runs.pop(run_id, None)
            if active is not None:
                view = active.snapshot()
                fenced = active.fence(expected_generation=view.generation, expected_revision=view.revision)
                self._event_generations[run_id] = fenced.generation
                source = self._event_sources.get(run_id)
                if source is not None:
                    source.advance_generation(fenced.generation)
            with closing(self._store()) as store:
                run = self._find_event_run(store, run_id)
                self._ensure_event_source(store, run)
                self._publish_run_state(run, None, reason_code="execution_closed")
                self._event_sources[run_id].seal()
                self._release_event_source(run_id)
            self._run_checkpoints.pop(run_id, None)
            self._fact_sequences.pop(run_id, None)
            self._interrupting.discard(run_id)
            self._finalizing_runs.discard(run_id)
        self._errors.pop(sid, None)
        self._execution_failures.pop(sid, None)
        self._uncertain_tools.pop(sid, None)
        self._progress.pop(sid, None)

    def _restore_stable_nodes(self, store, sid):
        head = self._workflow_ref(store, sid)
        commit = store.get_record("workflow_commit", {"commit_id": head["head_commit_id"]})
        state = store.get_record("state_snapshot", {"state_snapshot_id": commit["state_snapshot_id"]})
        records = []
        for frozen in state["node_states"]:
            node = store.get_record("node_session", {
                "workflow_session_id": sid, "node_binding_id": frozen["node_binding_id"],
            })
            if node["private_data"] != frozen["private_data"]:
                node["data_version"] += 1
                node["private_data"] = copy.deepcopy(frozen["private_data"])
                records.append(("node_session", node))
        return records

    @staticmethod
    def _closeout_receipt(records):
        closed = next(row for row in records if "closeout_id" in row and "fact_refs" in row)
        return {
            "workflow_session_id": closed["workflow_session_id"],
            "chain_run_id": closed["chain_run_id"],
            "closeout_id": closed["closeout_id"], "status": "closed",
        }

    def close_execution(
        self, session_id, source_chain_run_id, *, idempotency_key,
        expected_session_revision, expected_ref_revision, expected_head_commit_id,
    ):
        """End an unavailable runtime atomically; this operation never dispatches."""
        digest = self._recovery_request(
            "close_execution", session_id, source_chain_run_id, idempotency_key,
            expected_session_revision, expected_ref_revision, expected_head_commit_id,
        )
        key = session_id + ":" + idempotency_key
        with self._lock, closing(self._store()) as store:
            self._check_open()
            prior = store.read_receipt_with_digest("workflow.close_execution", key)
            if prior is not None:
                if prior[0] != digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                return self._closeout_receipt(prior[1])
            session = self._session(store, session_id)
            head = self._workflow_ref(store, session_id)
            if session["revision"] != expected_session_revision:
                raise ContractValidationError("Workflow session revision conflict")
            if (head["revision"] != expected_ref_revision
                    or head["head_commit_id"] != expected_head_commit_id):
                raise ContractValidationError("Workflow ref head conflict")
            chain = store.get_record("chain_run", {"chain_run_id": source_chain_run_id})
            self._assert_closeable(store, session_id, chain)
            records, closeout = self._closeout_records(
                store, session_id, chain,
                "lost_runtime" if chain["status"] == "recovery_unavailable" else "execution_failed",
            )
            operation = self._operation(
                "close_execution", session_id, "chain_run", source_chain_run_id,
                idempotency_key, [
                    {"kind": "workflow_session", "id": session_id, "revision": expected_session_revision},
                    {"kind": "workflow_ref", "id": head["workflow_ref_id"], "revision": expected_ref_revision},
                ], {"closeout_id": closeout["closeout_id"]},
                expected_head_commit_id=expected_head_commit_id,
            )
            records.extend(self._restore_stable_nodes(store, session_id))
            for ref in store.list_records("visible_message_ref"):
                if (ref["workflow_session_id"] == session_id and ref["role"] == "user"
                        and ref["boundary"]["chain_run_id"] == source_chain_run_id
                        and ref["boundary"]["input_status"] == "running"):
                    ref["boundary"]["input_status"] = "failed"
                    records.append(("visible_message_ref", ref))
            saved = self._save(
                store, session_id, [*records, ("workflow_operation", operation)], key,
                operation="workflow.close_execution", request_digest=digest,
                expected_ref_heads={head["workflow_ref_id"]: (
                    expected_ref_revision, expected_head_commit_id,
                )},
            )
            self._discard_closed_runtime(session_id, closeout["run_ids"])
            return self._closeout_receipt(saved)

    def _continuation_basis(self, store, sid, chain_id):
        frozen = inspect_closed_reroll_origin(store.read_bundle(), sid, chain_id)
        chain = store.get_record("chain_run", {"chain_run_id": chain_id})
        closeouts = [row for row in store.list_records("execution_closeout")
                     if row["chain_run_id"] == chain_id]
        if len(closeouts) != 1:
            raise ContractValidationError("Closed execution has no unique evidence")
        closeout = closeouts[0]
        source = next((
            store.get_record("run_record", {"run_id": run_id})
            for run_id in reversed(closeout["run_ids"])
            if store.get_record("run_record", {"run_id": run_id})["status"] == "closed"
        ), None)
        if source is None or source["node_binding_id"] not in (A_BINDING, B_BINDING):
            raise ContractValidationError("No interrupted Agent boundary is available for continuation")
        old_snapshot = store.get_record("input_snapshot", {"snapshot_id": source["snapshot_id"]})
        old_input = store.get_record("node_input", {"input_id": source["input_id"]})
        prior = next((row for row in store.list_records("execution_continuation")
                      if row["chain_run_id"] == chain_id), None)
        members = (prior["reused_run_ids"] if prior is not None else []) + chain["node_run_ids"]
        reused = [
            store.get_record("run_record", {"run_id": run_id})
            for run_id in members
            if any(row["run_id"] == run_id for row in store.list_records("run_record"))
        ]
        reused = [row for row in reused if row["status"] == "succeeded"
                  and row["node_binding_id"] == A_BINDING]
        if source["node_binding_id"] == B_BINDING:
            if len(reused) != 1:
                raise ContractValidationError("Continuation needs one verified completed A")
            output = store.get_record("workflow_output", {
                "output_id": old_input["source"].get("output_id"),
            })
            if (output["source"]["run_id"] != reused[0]["run_id"]
                    or old_input["source"]["kind"] != "upstream_output"):
                raise ContractValidationError("Continuation B has no verified A output")
        else:
            reused = []
        histories = {ref["run_id"]: store.read_execution_facts(ref["run_id"])
                     for ref in closeout["fact_refs"]}
        evidence = interruption_evidence(closeout, source["run_id"], histories)
        return frozen, source, old_input, old_snapshot, reused, closeout, evidence

    def continue_workflow(
        self, session_id, source_chain_run_id, *, idempotency_key,
        expected_session_revision, expected_ref_revision, expected_head_commit_id,
    ):
        """Manually start a fresh execution with interruption evidence and verified outputs."""
        digest = self._recovery_request(
            "continue_workflow", session_id, source_chain_run_id, idempotency_key,
            expected_session_revision, expected_ref_revision, expected_head_commit_id,
        )
        key = session_id + ":" + idempotency_key
        with self._lock, closing(self._store()) as store:
            self._check_open()
            prior = store.read_receipt_with_digest("workflow.continue_workflow", key)
            if prior is not None:
                if prior[0] != digest:
                    raise ContractValidationError("Idempotency key has different workflow operation request")
                receipt = self._reroll_receipt(prior[1])
            else:
                session = self._session(store, session_id)
                head = self._workflow_ref(store, session_id)
                if session["revision"] != expected_session_revision:
                    raise ContractValidationError("Workflow session revision conflict")
                if (head["revision"] != expected_ref_revision
                        or head["head_commit_id"] != expected_head_commit_id):
                    raise ContractValidationError("Workflow ref head conflict")
                self._assert_runtime_forkable(session_id, store)
                frozen, source, old_input, old_snapshot, reused, closeout, evidence = (
                    self._continuation_basis(store, session_id, source_chain_run_id)
                )
                self._verify_model_plan(self._chain_model_plan(
                    store, store.get_record("chain_run", {"chain_run_id": source_chain_run_id}),
                ))
                chain_id, run_id = _uid(), _uid()
                user_input, _ = frozen.clone_frozen_start(
                    input_id=_uid(), snapshot_id=_uid(), created_at=_now(),
                )
                node_input = copy.deepcopy(old_input)
                node_input.update(input_id=user_input["input_id"] if not reused else _uid(), created_at=_now())
                message = {
                    "schema_version": 2, "message_id": _uid(), "role": "system",
                    "source": {"kind": "runtime_execution_observation",
                               "closeout_id": closeout["closeout_id"], "source_run_id": source["run_id"]},
                    "blocks": [{"kind": "text", "text": evidence}],
                }
                snapshot = copy.deepcopy(old_snapshot)
                snapshot.update(
                    snapshot_id=_uid(), input_id=node_input["input_id"],
                    created_at=_now(), s0=[*snapshot["s0"], message],
                )
                check_prepared_request_capacity(snapshot)
                run = _record(
                    profile="agent", run_id=run_id, workflow_session_id=session_id,
                    node_binding_id=source["node_binding_id"], chain_run_id=chain_id,
                    input_id=node_input["input_id"], snapshot_id=snapshot["snapshot_id"],
                    source_run_id=None, status="prepared", revision=1,
                    initial_budget=copy.deepcopy(source["initial_budget"]),
                    result_turn_id=None, superseded_by_run_id=None,
                )
                operation = self._operation(
                    "continue_workflow", session_id, "chain_run", source_chain_run_id,
                    idempotency_key, [
                        {"kind": "workflow_session", "id": session_id, "revision": expected_session_revision},
                        {"kind": "workflow_ref", "id": head["workflow_ref_id"], "revision": expected_ref_revision},
                    ], {
                        "chain_run_id": chain_id, "source_chain_run_id": source_chain_run_id,
                        "closeout_id": closeout["closeout_id"],
                        "base_commit_id": frozen.base_commit_id, "base_ref_revision": frozen.base_ref_revision,
                    }, expected_head_commit_id=expected_head_commit_id,
                )
                chain = _record(
                    schema_version=2, chain_run_id=chain_id, workflow_session_id=session_id,
                    input_id=user_input["input_id"], status="prepared", node_run_ids=[run_id],
                    output_id=None, base_commit_id=frozen.base_commit_id,
                    base_ref_revision=frozen.base_ref_revision, operation_id=operation["operation_id"],
                )
                origin = _record(
                    chain_run_id=chain_id, workflow_session_id=session_id,
                    input_id=user_input["input_id"],
                    visible_message_id=user_input["source"]["visible_message_id"],
                    source_chain_run_id=source_chain_run_id,
                )
                continuation = _record(
                    chain_run_id=chain_id, workflow_session_id=session_id,
                    source_chain_run_id=source_chain_run_id, closeout_id=closeout["closeout_id"],
                    source_run_id=source["run_id"],
                    reused_run_ids=[row["run_id"] for row in reused], evidence_message=message,
                )
                records = [
                    ("node_input", node_input), ("input_snapshot", snapshot),
                    ("run_record", run), ("chain_run", chain), ("chain_input_origin", origin),
                    ("execution_continuation", continuation), ("workflow_operation", operation),
                ]
                if reused:
                    records.append(("node_input", user_input))
                saved = self._save(
                    store, session_id, records, key, operation="workflow.continue_workflow",
                    request_digest=digest,
                    expected_ref_heads={head["workflow_ref_id"]: (
                        expected_ref_revision, expected_head_commit_id,
                    )},
                )
                receipt = self._reroll_receipt(saved)
            run = store.get_record("run_record", {"run_id": receipt["run_id"]})
            chain = store.get_record("chain_run", {"chain_run_id": receipt["chain_run_id"]})
            if run["status"] == "prepared" and run["run_id"] not in self._active_runs:
                self._active_runs[run["run_id"]] = ActiveRun(
                    run["run_id"], chain["chain_run_id"], session_id, chain["base_commit_id"],
                )
                try:
                    self._futures[session_id] = self._executor.submit(
                        self._execute_chain, session_id, chain["chain_run_id"], run["run_id"],
                        "A" if run["node_binding_id"] == A_BINDING else "B",
                    )
                except Exception:
                    self._fail(session_id, chain["chain_run_id"], run["run_id"], "DISPATCH_FAILED")
            return receipt

    @staticmethod
    def _reroll_tools_unsettled(store, run_id, workspace):
        pending = workspace.snapshot().pending_tools
        if not pending:
            return False
        run = store.get_record("run_record", {"run_id": run_id})
        if run["node_binding_id"] != B_BINDING or any(tool.status != "pending" for tool in pending):
            return True
        facts = store.read_execution_facts(run_id)
        accepted_calls = {
            block["tool_call_id"]
            for fact in facts if fact["kind"] == "message_accepted"
            for block in fact["payload"]["message"]["blocks"] if block["kind"] == "tool_call"
        }
        dispatches = {fact["payload"]["tool_call_id"] for fact in facts if fact["kind"] == "tool_dispatch"}
        if any(tool.tool_call_id not in accepted_calls or tool.tool_call_id in dispatches
               or tool.tool_execution_id is not None for tool in pending):
            raise ContractValidationError("Pending tools differ from saved dispatch evidence")
        return False

    def _assert_reroll_kernel_supports(self, store, chain):
        for run in self._agent_members(store, chain):
            if not self._kernel_supports(run, "reroll"):
                raise control_error("Kernel does not support reroll", "unsupported")

    def _accept_reroll(
        self, session_id: str, source_chain_run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_ref_revision: int,
        expected_head_commit_id: str,
    ) -> dict[str, Any]:
        """Durably prepare an isolated reroll A, without dispatch or projection."""
        if type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 128:
            raise ContractValidationError("A bounded idempotency key is required")
        if (type(expected_session_revision) is not int or expected_session_revision < 1
                or type(expected_ref_revision) is not int or expected_ref_revision < 1
                or type(expected_head_commit_id) is not str):
            raise ContractValidationError("Expected workflow revisions and Head are required")
        request = {
            "kind": "reroll", "workflow_session_id": session_id,
            "source_chain_run_id": source_chain_run_id,
            "idempotency_key": idempotency_key,
            "expected_session_revision": expected_session_revision,
            "expected_ref_revision": expected_ref_revision,
            "expected_head_commit_id": expected_head_commit_id,
        }
        request_digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        receipt_key = session_id + ":" + idempotency_key
        with self._lock, closing(self._store()) as store:
            self._check_open()
            self._session(store, session_id)
            receipt = store.read_receipt_with_digest("workflow.reroll", receipt_key)
            if receipt is not None:
                if receipt[0] != request_digest:
                    raise ContractValidationError(
                        "Idempotency key has different workflow operation request"
                    )
                return self._reroll_receipt(receipt[1])
            session = self._session(store, session_id)
            ref = self._workflow_ref(store, session_id)
            if session["revision"] != expected_session_revision:
                raise ContractValidationError("Workflow session revision conflict")
            if (ref["revision"] != expected_ref_revision
                    or ref["head_commit_id"] != expected_head_commit_id):
                raise ContractValidationError("Workflow ref head conflict")
            source = next(
                (row for row in store.list_records("chain_run")
                 if row["chain_run_id"] == source_chain_run_id), None,
            )
            interrupted = source is not None and source["workflow_session_id"] == session_id and source["status"] == "paused"
            closed_source = source is not None and source["workflow_session_id"] == session_id and source["status"] == "closed"
            if interrupted:
                frozen = inspect_interrupted_reroll_origin(
                    store.read_bundle(), session_id, source_chain_run_id,
                )
                future = self._futures.get(session_id)
                source_run_id = frozen.stopped_run_id or frozen.source_run_id
                workspace = self._active_runs.get(source_run_id)
                if (future is not None and not future.done()
                        or workspace is None or source_run_id not in self._run_checkpoints
                        or source_run_id in self._finalizing_runs
                        or source_run_id in self._interrupting
                        or any(
                            row["workflow_session_id"] == session_id
                            and row["run_id"] in self._active_results
                            for row in store.list_records("run_record")
                        )
                        or self._uncertain_tools.get(session_id)
                        or self._reroll_tools_unsettled(store, source_run_id, workspace)):
                    raise ContractValidationError("Interrupted run has not safely stopped and settled")
            else:
                self._assert_runtime_forkable(session_id, store)
            if source is None or source["workflow_session_id"] == session_id:
                if closed_source:
                    frozen = inspect_closed_reroll_origin(
                        store.read_bundle(), session_id, source_chain_run_id,
                    )
                elif not interrupted:
                    frozen = inspect_completed_reroll_origin(
                        store.read_bundle(), session_id, source_chain_run_id,
                    )
            else:
                frozen = inspect_inherited_reroll_origin(
                    store.read_bundle(), session_id, source_chain_run_id,
                )
            source = store.get_record("chain_run", {
                "chain_run_id": source_chain_run_id,
            })
            self._assert_reroll_kernel_supports(store, source)
            self._verify_model_plan(self._chain_model_plan(store, source))
            chain_id, input_id, snapshot_id, run_id = _uid(), _uid(), _uid(), _uid()
            node_input, snapshot = frozen.clone_frozen_start(
                input_id=input_id, snapshot_id=snapshot_id, created_at=_now(),
            )
            original_run = store.get_record("run_record", {
                "run_id": frozen.source_run_id,
            })
            run = _record(
                profile="agent", run_id=run_id, workflow_session_id=session_id,
                node_binding_id=A_BINDING, chain_run_id=chain_id,
                input_id=input_id, snapshot_id=snapshot_id,
                source_run_id=frozen.source_run_id
                if interrupted and source_run_id == frozen.source_run_id else None,
                status="prepared", revision=1,
                initial_budget=copy.deepcopy(original_run["initial_budget"]),
                result_turn_id=None, superseded_by_run_id=None,
            )
            closed = []
            closeout = None
            if interrupted and source_run_id != frozen.source_run_id:
                closed, closeout = self._closeout_records(
                    store, session_id, source, "paused_reroll",
                )
                closed.extend(self._restore_stable_nodes(store, session_id))
            operation = self._operation(
                "reroll", session_id, "chain_run", source_chain_run_id,
                idempotency_key, [
                    {"kind": "workflow_session", "id": session_id,
                     "revision": expected_session_revision},
                    {"kind": "workflow_ref", "id": ref["workflow_ref_id"],
                     "revision": expected_ref_revision},
                ], {
                    "chain_run_id": chain_id, "source_chain_run_id": source_chain_run_id,
                    "base_commit_id": frozen.base_commit_id,
                    "base_ref_revision": frozen.base_ref_revision,
                    **({"closeout_id": closeout["closeout_id"]} if closeout is not None else {}),
                }, expected_head_commit_id=expected_head_commit_id,
            )
            chain = _record(
                schema_version=2, chain_run_id=chain_id,
                workflow_session_id=session_id, input_id=input_id,
                status="prepared", node_run_ids=[run_id], output_id=None,
                base_commit_id=frozen.base_commit_id,
                base_ref_revision=frozen.base_ref_revision,
                operation_id=operation["operation_id"],
            )
            origin = _record(
                chain_run_id=chain_id, workflow_session_id=session_id,
                visible_message_id=node_input["source"]["visible_message_id"],
                input_id=input_id, source_chain_run_id=source["chain_run_id"],
            )
            if interrupted and closeout is None:
                old_run = copy.deepcopy(original_run)
                old_run.update(status="superseded", revision=old_run["revision"] + 1,
                               superseded_by_run_id=run_id)
                old_chain = copy.deepcopy(source)
                old_chain["status"] = "superseded"
                closed = [("run_record", old_run), ("chain_run", old_chain)]
            saved = self._save(
                store, session_id, [
                    *closed,
                    ("node_input", node_input), ("input_snapshot", snapshot),
                    ("run_record", run), ("chain_run", chain),
                    ("chain_input_origin", origin), ("workflow_operation", operation),
                ], receipt_key, operation="workflow.reroll",
                request_digest=request_digest,
                expected_ref_heads={ref["workflow_ref_id"]: (
                    expected_ref_revision, expected_head_commit_id,
                )},
            )
            if interrupted:
                self._discard_closed_runtime(session_id, [source_run_id])
            return self._reroll_receipt(saved)

    @staticmethod
    def _reroll_receipt(records: list[dict[str, Any]]) -> dict[str, Any]:
        origin = next(row for row in records if "source_chain_run_id" in row
                      and "visible_message_id" in row)
        chain = next(row for row in records if "node_run_ids" in row
                     and row["chain_run_id"] == origin["chain_run_id"])
        return {
            "workflow_session_id": chain["workflow_session_id"],
            "source_chain_run_id": origin["source_chain_run_id"],
            "chain_run_id": chain["chain_run_id"],
            "run_id": chain["node_run_ids"][0],
            "input_id": chain["input_id"],
            "status": chain["status"],
        }

    def _reroll(
        self, session_id: str, source_chain_run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_ref_revision: int,
        expected_head_commit_id: str,
    ) -> dict[str, Any]:
        """Start an accepted replacement chain after the workflow control gate."""
        with self._lock:
            receipt = self._accept_reroll(
                session_id, source_chain_run_id, idempotency_key=idempotency_key,
                expected_session_revision=expected_session_revision,
                expected_ref_revision=expected_ref_revision,
                expected_head_commit_id=expected_head_commit_id,
            )
            run_id = receipt["run_id"]
            with closing(self._store()) as store:
                run = store.get_record("run_record", {"run_id": run_id})
                chain = store.get_record("chain_run", {
                    "chain_run_id": receipt["chain_run_id"],
                })
            if run["status"] == "prepared" and run_id not in self._active_runs:
                self._active_runs[run_id] = ActiveRun(
                    run_id, chain["chain_run_id"], session_id,
                    chain["base_commit_id"],
                )
                try:
                    self._futures[session_id] = self._executor.submit(
                        self._execute_chain, session_id, chain["chain_run_id"], run_id,
                    )
                except Exception:
                    self._fail(session_id, chain["chain_run_id"], run_id,
                               "DISPATCH_FAILED")
            return receipt

    def reroll(
        self, session_id: str, source_chain_run_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_ref_revision: int,
        expected_head_commit_id: str,
    ) -> dict[str, Any]:
        """Start a new whole-chain candidate while retaining the prior result."""
        return self._reroll(
            session_id, source_chain_run_id, idempotency_key=idempotency_key,
            expected_session_revision=expected_session_revision,
            expected_ref_revision=expected_ref_revision,
            expected_head_commit_id=expected_head_commit_id,
        )

    @staticmethod
    def _node_states(store, session_id):
        return [row for row in store.list_records("node_session") if row["workflow_session_id"] == session_id]

    @staticmethod
    def _selections(store, session_id):
        head = WorkflowService._workflow_ref(store, session_id)
        commit = store.get_record("workflow_commit", {"commit_id": head["head_commit_id"]})
        snapshot = store.get_record("state_snapshot", {"state_snapshot_id": commit["state_snapshot_id"]})
        return copy.deepcopy(snapshot["selection_refs"])

    def _checkpoint(self, sid, nodes, selections, *, kind, revision, **boundary):
        return _record(checkpoint_id=_uid(), workflow_session_id=sid, workflow_definition_id=self._definition_id,
            definition_revision=1, revision=revision, kind=kind, selection_refs=selections, **boundary,
            nodes=[{"node_binding_id": row["node_binding_id"], "data_version": row["data_version"],
                    "private_data": copy.deepcopy(row["private_data"]),
                    "selected_turn_id": row["private_data"]["payload"].get("head_turn_id")} for row in nodes])

    def _prepare_node(
        self, store, sid, chain_id, stage, node_input,
        *, parent_turn_id=_CURRENT_PARENT, frozen_snapshot=None, context_config=None,
        prompt_selection_plan=None,
        model_selection_plan=None,
        event_declarations=None,
    ):
        binding = A_BINDING if stage == "A" else B_BINDING
        state = store.get_record("node_session", {"workflow_session_id": sid, "node_binding_id": binding})
        parent = (state["private_data"]["payload"]["head_turn_id"]
                  if parent_turn_id is _CURRENT_PARENT else parent_turn_id)
        context, kernel, _, config = (
            self._snapshot_components(frozen_snapshot, store=store)
            if frozen_snapshot is not None else self._resolved[stage]
        )
        config = copy.deepcopy(config)
        if model_selection_plan is not None:
            if frozen_snapshot is not None:
                raise ContractValidationError("Frozen model cannot be replaced by a new selection")
            plan = validate_model_plan(model_selection_plan)
            config["payload"]["model_selection_plan"] = plan
            if stage in plan["nodes"]:
                binding_config = plan["nodes"][stage]
                adapter_config = copy.deepcopy(binding_config["parameters"])
                adapter = self._registry.resolve(
                    "adapter", ADAPTER_COMPONENT, VERSION,
                    required_capabilities=("canonical_messages", "native_tools", "non_streaming"),
                    config=_config(ADAPTER_COMPONENT, adapter_config),
                )
                config["payload"]["adapter"] = adapter_config
                config["payload"]["resolved"]["adapter"] = adapter.descriptor
                config["payload"]["model_binding"] = binding_config
        if "run_events" not in config["payload"]:
            declarations = (
                FrozenEventDeclarations.from_json(event_declarations)
                if event_declarations is not None else
                self._frozen_event_declarations(frozen_snapshot, kernel.descriptor)
                if frozen_snapshot is not None else self._new_event_declarations(kernel.descriptor)
            )
            config["payload"]["run_events"] = declarations.to_json()
        if stage == "A" and frozen_snapshot is None:
            config["payload"]["run_event_plan"] = self._new_event_plan()
        if context_config is not None:
            if frozen_snapshot is not None:
                raise ContractValidationError("Frozen Context cannot be replaced by a new selection")
            context = self._registry.resolve(
                "context", PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
                required_capabilities=("sources", "paired_history", "frozen_projection",
                                       PREPARATION_CAPABILITY),
                config=_config(PREPARED_CONTEXT_COMPONENT, context_config),
            )
            context.require_api()
            config["payload"]["context"] = copy.deepcopy(context_config)
            config["payload"]["resolved"]["context"] = context.descriptor
        if prompt_selection_plan is not None:
            config["payload"]["prompt_selection_plan"] = validate_prompt_selection_plan(prompt_selection_plan)
        routes = config["payload"].get(
            "result_port_routes",
            make_result_port_routes(binding, allowed_bindings=(A_BINDING, B_BINDING)),
        )
        routes = validate_result_port_routes(routes, allowed_bindings=(A_BINDING, B_BINDING))
        if routes["node_binding_id"] != binding:
            raise ContractValidationError("Result routes belong to another preparation node")
        config["payload"]["result_port_routes"] = routes
        archived = SelectedArchiveReader(
            store, self._snapshot_components, allowed_bindings=(A_BINDING, B_BINDING),
        ).read(sid, binding, parent)
        history = archived.messages
        logical_floors = archived.logical_floors
        protected_blocks = archived.protected_blocks
        prepared = PREPARATION_CAPABILITY in context.descriptor["capabilities"]
        if prepared:
            runtime_context_config = bind_prompt_config_scope(
                config["payload"]["context"], workflow_session_id=sid, node_binding_id=binding,
            )
            session = store.get_record("workflow_session", {"workflow_session_id": sid})
            validate_prompt_registry_owner(
                runtime_context_config, workflow_definition_id=session["workflow_definition_id"],
                definition_revision=session["definition_revision"],
            )
            if runtime_context_config["schema_version"] == 2:
                variables, variable_evidence = self._prepare_runtime_variables(
                    store, sid, binding, chain_id, node_input, runtime_context_config,
                    frozen_snapshot=frozen_snapshot,
                )
                runtime_context_config["variables"] = variables
                config["payload"]["variable_preparation"] = variable_evidence
            program_variables = None
            if runtime_context_config["schema_version"] == 3:
                self._validate_data_program(store, runtime_context_config["preparation"])
                head = self._workflow_ref(store, sid)
                self._bind_context_sources(store, sid, runtime_context_config, {
                    "expected_session_revision": session["revision"],
                    "expected_ref_revision": head["revision"],
                    "expected_head_commit_id": head["head_commit_id"],
                })
                archived = self._assembly_archive(runtime_context_config, binding, archived)
                history, logical_floors, protected_blocks = (
                    archived.messages, archived.logical_floors, archived.protected_blocks,
                )
                program_variables = ProgramVariableStore(store)
                runtime_context_config["preparation_state"] = self._program_current_state(store, sid)
                current = program_variables.current(sid)
                head_commit_id = self._workflow_ref(store, sid)["head_commit_id"]
                if frozen_snapshot is None and binding == A_BINDING:
                    if current["revision"] == 0:
                        initial = merge_program_definitions(
                            runtime_context_config["preparation_state"],
                            program_definitions(runtime_context_config["preparation"]),
                        )
                        current = program_variables.write_in_transaction(
                            sid, initial, expected_revision=0,
                            key="program:seed:" + sid,
                            request={"kind": "seed", "session_id": sid,
                                     "definitions": program_definitions(runtime_context_config["preparation"])},
                        )["state"]
                        runtime_context_config["preparation_state"] = current
                    if program_variables.reference("workflow_commit", head_commit_id) is None:
                        program_variables.bind("workflow_commit", head_commit_id, sid, current["revision"])
                runtime_context_config["preparation_cache"] = program_variables.cached(chain_id)
                if frozen_snapshot is not None:
                    original = frozen_snapshot["config"]["payload"]["context_preparation"]
                    runtime_context_config["preparation_state"] = copy.deepcopy(original["program"]["state"])
                    node_configs = {node["node_id"]: node for node in runtime_context_config["preparation"]["nodes"]}
                    refreshed = set()
                    for node in runtime_context_config["preparation"]["nodes"]:
                        if (node["kind"] == "text" and node["config"].get("source") == "root_input"
                                or node["kind"] == "context-source"
                                or any(target in refreshed for target in node["inputs"].values())):
                            refreshed.add(node["node_id"])
                    runtime_context_config["preparation_cache"].update({
                        stage["node_id"]: {
                            "config_digest": content_digest(node_configs[stage["node_id"]]),
                            "output": copy.deepcopy(stage["output"]),
                        } for stage in original["program"]["stages"]
                        if stage["node_id"] not in refreshed and (
                            stage["output"]["kind"] != "prompt" or stage["output"]["view"] is None
                        )
                    })
            preparation = context.implementation.prepare_context(
                copy.deepcopy(node_input), copy.deepcopy(history),
                config=copy.deepcopy(runtime_context_config), workflow_session_id=sid,
                node_binding_id=binding, parent_turn_id=parent,
                logical_floors=copy.deepcopy(logical_floors),
                protected_blocks=copy.deepcopy(protected_blocks),
            )
            preparation = validate_context_preparation(
                preparation, node_input=node_input, history=history,
                workflow_session_id=sid, node_binding_id=binding, parent_turn_id=parent,
                logical_floors=logical_floors, protected_blocks=protected_blocks,
                config=runtime_context_config,
            )
            if program_variables is not None:
                current = program_variables.current(sid)
                result_state = preparation["program"]["state"]
                receipt_key = "program:run:" + node_input["input_id"]
                receipt = program_variables.write_in_transaction(
                    sid, result_state, expected_revision=current["revision"],
                    key=receipt_key, request={
                        "kind": "run_preparation", "session_id": sid, "input_id": node_input["input_id"],
                        "program_digest": preparation["program"]["evidence_digest"],
                    },
                    advance_head=frozen_snapshot is None,
                )
                program_variables.save_outputs(chain_id, runtime_context_config["preparation"],
                                               preparation["program"])
                config["payload"]["program_variable_preparation"] = {
                    "schema_version": 1, "kind": "program_variable_preparation",
                    "session_id": sid, "revision": receipt["state"]["revision"],
                    "receipt_key": receipt_key,
                }
            config = copy.deepcopy(config)
            config["payload"]["context"] = preparation["config"]
            config["payload"]["context_preparation"] = preparation
            s0 = preparation["s0"]
        else:
            s0 = context.implementation.prepare(
                copy.deepcopy(node_input), copy.deepcopy(history),
                config=copy.deepcopy(config["payload"]["context"]),
            )
        validate_message_history(s0)
        expected_source = (
            {"kind": "human", "visible_message_id": node_input["source"]["visible_message_id"]}
            if node_input["source"]["kind"] == "visible_message"
            else {"kind": "upstream_node", "output_id": node_input["source"]["output_id"]}
        )
        history_ids = {message["message_id"] for message in history}
        preserved_history = [message for message in s0 if message["message_id"] in history_ids]
        roots = [index for index, message in enumerate(s0)
                 if message["role"] == "user" and message["source"] == expected_source]
        if not prepared and (len(roots) != 1 or preserved_history != history
                or s0[roots[0]]["blocks"] != [{
                    "kind": "text", "text": dumps_pretty(node_input["payload"]),
                }] or any(message["message_id"] in history_ids for message in s0[roots[0] + 1:])
                or any(message["source"]["kind"] != "prompt" for message in s0[roots[0] + 1:])):
            raise ContractValidationError("Context must preserve frozen input and accepted history sources")
        snapshot = build_snapshot(workflow_session_id=sid, node_binding_id=binding, node_input=node_input,
            parent_turn_id=parent, node_definition=self._agent_definition, config=config, s0=s0,
            tools=self._tools(), model_parameters=(
                frozen_snapshot["model_parameters"] if frozen_snapshot is not None
                else config["payload"]["adapter"]
            ), output_schema=(
                frozen_snapshot["output_schema"] if frozen_snapshot is not None else OUTPUT_SCHEMA
            ), projection_version=PREPARED_PROJECTION_VERSION if prepared else 1)
        check_prepared_request_capacity(snapshot)
        run = _record(profile="agent", run_id=_uid(), workflow_session_id=sid, node_binding_id=binding,
            chain_run_id=chain_id, input_id=node_input["input_id"], snapshot_id=snapshot["snapshot_id"],
            source_run_id=None, status="prepared", revision=1, initial_budget=config["payload"].get(
                "budget", config["payload"]["kernel"],
            ),
            result_turn_id=None, superseded_by_run_id=None)
        return snapshot, run

    def _prepare_runtime_variables(self, store, sid, binding, chain_id, node_input, config, *, frozen_snapshot):
        variables = VariableStore(store)
        registry = config["variables"]["registry"]
        variables.register_registry_in_transaction(registry)
        identity = {
            "workflow_session_id": sid, "workflow_id": registry["workflow_id"],
            "registry_revision": registry["revision"],
        }
        current = variables.get_current(**identity)
        key = "variables:" + node_input["input_id"]
        seed = None
        if frozen_snapshot is not None:
            original = frozen_snapshot["config"]["payload"].get("variable_preparation")
            if original is None:
                raise ContractValidationError("Frozen variable rules have no saved preparation")
            frozen = validate_frozen_variable_preparation(original["frozen"])
            rederived = rederive_root_variable_assignments(
                frozen, node_input, workflow_session_id=sid, node_binding_id=binding,
            )
            receipt = variables.prepare_snapshot_in_transaction(
                rederived["snapshot"], expected_revision=current["revision"] if current else 0,
                idempotency_key=key, source_ref=original["state_ref"],
                preparation_digest=content_digest({"frozen": frozen, "rederivation": rederived}),
            )
        else:
            if current is None:
                current = variables.prepare_in_transaction(
                    **identity, expected_revision=0, assignments=[],
                    idempotency_key="variables:seed:" + sid,
                )["after"]
                head = self._workflow_ref(store, sid)
                seed = {"commit_id": head["head_commit_id"], "state_ref": _variable_ref(current)}
            basis = variables.snapshot_from_state(current, node_binding_id=binding)
            basis_ref = _variable_ref(current)
            if binding == A_BINDING:
                selected_ref = variables.get_bound_state_ref(
                    "workflow_commit", self._workflow_ref(store, sid)["head_commit_id"],
                )
                if selected_ref is not None:
                    basis = variables.read_snapshot(selected_ref, node_binding_id=binding)
                    basis_ref = selected_ref
            frozen = prepare_variable_assignments(basis, config["variable_plan"], node_input)
            if basis_ref == _variable_ref(current):
                receipt = variables.prepare_in_transaction(
                    **identity, expected_revision=current["revision"],
                    assignments=frozen["resolved_assignments"], idempotency_key=key,
                    preparation_digest=content_digest({"frozen": frozen, "rederivation": None}),
                )
            else:
                receipt = variables.prepare_snapshot_in_transaction(
                    frozen["snapshot"], expected_revision=current["revision"],
                    idempotency_key=key, source_ref=basis_ref,
                    preparation_digest=content_digest({"frozen": frozen, "rederivation": None}),
                )
            rederived = None
        snapshot = variables.snapshot_from_state(receipt["after"], node_binding_id=binding)
        expected = rederived["snapshot"] if rederived is not None else frozen["snapshot"]
        if canonical_bytes(snapshot) != canonical_bytes(expected):
            raise ContractValidationError("Saved variable state differs from prepared values")
        return snapshot, {
            "schema_version": 1, "kind": "workflow_variable_preparation",
            "state_ref": _variable_ref(receipt["after"]), "receipt_key": key,
            "frozen": frozen, "rederivation": rederived, "seed": seed,
        }

    def _execute_chain(
        self, sid, chain_id, first_run_id, start_stage="A", *, archived_output=None,
    ):
        run_id = first_run_id
        with self._lock:
            self._execution_failures.pop(sid, None)
            if archived_output is not None:
                self._errors.pop(sid, None)
        try:
            if start_stage == "A":
                output = (archived_output if archived_output is not None
                          else self._execute_node(sid, chain_id, "A", run_id))
                self._prepare_next_node(sid, chain_id, output)
                run_id = self._chain_next_run(sid, chain_id)
            output = self._execute_node(sid, chain_id, "B", run_id, retain_result=True)
            self._publish(sid, chain_id, output)
            with self._lock:
                self._active_results.pop(run_id, None)
                self._errors.pop(sid, None)
                self._execution_failures.pop(sid, None)
            return True
        except _ExecutionPaused:
            return False
        except RunFailed as exc:
            with self._lock:
                self._execution_failures[sid] = exc
            self._fail(sid, chain_id, run_id, exc.code, failure=exc)
        except Exception as exc:
            # Successful nodes and an accepted final are never rewritten as a
            # model failure when a later archive/publication transaction fails.
            with self._lock:
                self._execution_failures[sid] = exc
                self._errors[sid] = {"code": "STORAGE_OR_COORDINATION_FAILED",
                    "message": "The workflow stopped before durable completion. No successful reply was published."}
                try:
                    self._persist_execution_failure(run_id, "PROGRAM_OR_STORAGE_FAILED", "contract")
                finally:
                    self._revoke_failed_callbacks(run_id, "PROGRAM_OR_STORAGE_FAILED", "contract")
        except BaseException as exc:
            with self._lock:
                interrupted = isinstance(exc, _HOST_INTERRUPTION_SIGNALS)
                code = "HOST_INTERRUPTED" if interrupted else "PROGRAM_OR_STORAGE_FAILED"
                category = "interrupted" if interrupted else "contract"
                self._execution_failures[sid] = exc
                self._errors[sid] = {
                    "code": "HOST_INTERRUPTED" if interrupted else "STORAGE_OR_COORDINATION_FAILED",
                    "message": (
                        "The host stopped execution. Explicit closeout is required before new work."
                        if interrupted else
                        "The workflow stopped before durable completion. No successful reply was published."
                    ),
                }
                try:
                    try:
                        self._persist_execution_failure(run_id, code, category)
                    finally:
                        self._revoke_failed_callbacks(run_id, code, category)
                except BaseException:
                    pass  # Preserve the original exit signal even if diagnosis fails.
            raise
        return False

    def _prepare_next_node(self, sid: str, chain_id: str, output: dict[str, Any]) -> None:
        with self._lock, closing(self._store()) as store:
            node_input = _record(input_id=_uid(), port_id="request", payload_schema_ref=output["payload_schema_ref"],
                source={"kind": "upstream_output", "output_id": output["output_id"]}, payload=output["payload"])
            chain = store.get_record("chain_run", {"chain_run_id": chain_id})
            origin = next(
                (row for row in store.list_records("chain_input_origin")
                 if row["chain_run_id"] == chain_id), None,
            )
            frozen_b = None
            parent = _CURRENT_PARENT
            if origin is not None:
                source = store.get_record("chain_run", {
                    "chain_run_id": origin["source_chain_run_id"],
                })
                source_b = next((
                    row for row in self._agent_members(store, source)
                    if row["node_binding_id"] == B_BINDING
                ), None)
                if source_b is None:
                    base = store.get_record("workflow_commit", {"commit_id": chain["base_commit_id"]})
                    state = store.get_record("state_snapshot", {"state_snapshot_id": base["state_snapshot_id"]})
                    selected = next(row for row in state["node_states"]
                                    if row["node_binding_id"] == B_BINDING)["selected_result"]
                    parent = selected["turn_id"] if selected is not None else None
                else:
                    frozen_b = store.get_record("input_snapshot", {
                        "snapshot_id": source_b["snapshot_id"],
                    })
                    parent = frozen_b["parent_turn_id"]
            plan = self._chain_prompt_selection_plan(store, chain)
            event_plan = self._chain_event_plan(store, chain)
            model_plan = self._chain_model_plan(store, chain)
            self._verify_model_plan(model_plan)

            def build(saved_store):
                snapshot, prepared_run = self._prepare_node(
                    saved_store, sid, chain_id, "B", node_input, parent_turn_id=parent,
                    frozen_snapshot=frozen_b,
                    context_config=plan["nodes"].get("B") if frozen_b is None and plan is not None else None,
                    event_declarations=event_plan["nodes"]["B"] if frozen_b is None and event_plan is not None else None,
                    model_selection_plan=model_plan if frozen_b is None else None,
                )
                changed = copy.deepcopy(chain)
                changed["node_run_ids"].append(prepared_run["run_id"])
                return [("node_input", node_input), ("input_snapshot", snapshot),
                        ("run_record", prepared_run), ("chain_run", changed)]
            saved = self._save_prepared(
                store, sid, build, "prepare-b:" + chain_id, operation="workflow.update",
                request_digest=_request_digest({"chain_run_id": chain_id, "upstream_output_id": output["output_id"]}),
            )
            run = next(row for row in saved if row.get("profile") == "agent")
            self._active_runs[run["run_id"]] = ActiveRun(
                run["run_id"], chain_id, sid, chain["base_commit_id"],
            )

    def _session_configuration_chain(self, store, sid):
        refs = self._formal_refs(store, sid)
        user = next((ref for ref in reversed(refs) if ref["role"] == "user"), None)
        if user is None:
            return None
        chain_id = user["boundary"].get("chain_run_id")
        if chain_id is None:
            session = self._session(store, sid)
            if session["source"]["kind"] == "fork":
                anchor = store.get_record("fork_anchor", {
                    "fork_anchor_id": session["source"]["fork_anchor_id"],
                })
                branch = next((operation for operation in store.list_records("workflow_operation")
                               if operation["kind"] == "create_branch"
                               and operation["payload"].get("child_workflow_session_id") == sid), None)
                chain_id = branch["payload"].get("configuration_chain_run_id") if branch else None
                if chain_id is None:
                    source = next((ref for ref in store.list_records("visible_message_ref")
                                   if ref["workflow_session_id"] == anchor["source_workflow_session_id"]
                                   and ref["visible_message_id"] == user["visible_message_id"]), None)
                    chain_id = source["boundary"].get("chain_run_id") if source else None
        if chain_id is None:
            return None
        return store.get_record("chain_run", {"chain_run_id": chain_id})

    def _session_model_plan(self, store, sid):
        chain = self._session_configuration_chain(store, sid)
        return self._chain_model_plan(store, chain) if chain is not None else None

    def _chain_model_plan(self, store, chain):
        cursor, seen = chain, set()
        while cursor["chain_run_id"] not in seen:
            seen.add(cursor["chain_run_id"])
            first = next((run for run in self._agent_members(store, cursor)
                          if run["node_binding_id"] == A_BINDING), None)
            if first is not None:
                snapshot = store.get_record("input_snapshot", {"snapshot_id": first["snapshot_id"]})
                plan = snapshot["config"]["payload"].get("model_selection_plan")
                if plan is not None:
                    return validate_model_plan(plan)
            origin = next((row for row in store.list_records("chain_input_origin")
                           if row["chain_run_id"] == cursor["chain_run_id"]), None)
            if origin is None:
                return None
            cursor = store.get_record("chain_run", {"chain_run_id": origin["source_chain_run_id"]})
        raise ContractValidationError("Model selection origin is cyclic")

    def _chain_event_plan(self, store, chain):
        cursor = chain
        seen = set()
        while cursor["chain_run_id"] not in seen:
            seen.add(cursor["chain_run_id"])
            first = next((run for run in self._agent_members(store, cursor)
                          if run["node_binding_id"] == A_BINDING), None)
            if first is not None:
                snapshot = store.get_record("input_snapshot", {"snapshot_id": first["snapshot_id"]})
                plan = snapshot["config"]["payload"].get("run_event_plan")
                if plan is not None:
                    return self._validate_event_plan(plan)
            origin = next((row for row in store.list_records("chain_input_origin")
                           if row["chain_run_id"] == cursor["chain_run_id"]), None)
            if origin is None:
                return None
            cursor = store.get_record("chain_run", {"chain_run_id": origin["source_chain_run_id"]})
        raise ContractValidationError("Frozen workflow event plan has cyclic provenance")

    def _chain_prompt_selection_plan(self, store, chain):
        cursor = chain
        seen = set()
        while cursor["chain_run_id"] not in seen:
            seen.add(cursor["chain_run_id"])
            first = next((run for run in self._agent_members(store, cursor)
                          if run["node_binding_id"] == A_BINDING), None)
            if first is not None:
                snapshot = store.get_record("input_snapshot", {"snapshot_id": first["snapshot_id"]})
                plan = snapshot["config"]["payload"].get("prompt_selection_plan")
                if plan is not None:
                    return validate_prompt_selection_plan(plan)
            origin = next((row for row in store.list_records("chain_input_origin")
                           if row["chain_run_id"] == cursor["chain_run_id"]), None)
            if origin is None:
                return None
            cursor = store.get_record("chain_run", {"chain_run_id": origin["source_chain_run_id"]})
        raise ContractValidationError("Prompt selection origin is cyclic")

    def _chain_next_run(self, sid: str, chain_id: str) -> str:
        with self._lock, closing(self._store()) as store:
            chain = store.get_record("chain_run", {"chain_run_id": chain_id})
            runs = [row for row in store.list_records("run_record")
                    if row["workflow_session_id"] == sid and row["chain_run_id"] == chain_id]
            return next(run["run_id"] for run in runs if run["run_id"] == chain["node_run_ids"][-1])

    @staticmethod
    def _agent_members(store, chain):
        continuation = next((
            row for row in store.list_records("execution_continuation")
            if row["chain_run_id"] == chain["chain_run_id"]
        ), None)
        members = (continuation["reused_run_ids"] if continuation else []) + chain["node_run_ids"]
        agent_runs = {row["run_id"]: row for row in store.list_records("run_record")}
        return [agent_runs[run_id] for run_id in members if run_id in agent_runs]

    def _archive_accepted_node(self, package: dict[str, Any]) -> dict[str, Any]:
        sid = package["session_id"]
        run_id = package["run"]["run_id"]
        with self._lock, closing(self._store()) as store:
            ports = ResultPortStore(store, allowed_bindings=(A_BINDING, B_BINDING))
            snapshot = store.get_record("input_snapshot", {"snapshot_id": package["run"]["snapshot_id"]})
            submission = make_archive_submission(
                make_result_package(package["turn"], package["group"], package["run"], package["output"]),
                snapshot["config"]["payload"].get(
                    "result_port_routes",
                    make_result_port_routes(package["run"]["node_binding_id"],
                                            allowed_bindings=(A_BINDING, B_BINDING)),
                ), allowed_bindings=(A_BINDING, B_BINDING),
            )
            current = store.get_record("run_record", {"run_id": run_id})
            if current["status"] == "running":
                final_ready = copy.deepcopy(current)
                final_ready.update(status="final_ready", revision=current["revision"] + 1)
                if package["run"]["revision"] != final_ready["revision"] + 1:
                    raise ContractValidationError("Accepted final lost its original preparation boundary")
                self._save(
                    store, sid, [("run_record", final_ready)], "final-ready:" + run_id,
                    after_save=lambda *_: ports.put_submission_in_transaction(submission),
                )
                self._publish_run_state(final_ready, "running")
            elif ports.get_submission(run_id) is None:
                store.save_bundle_prepared(
                    lambda _: [], "accepted:" + run_id,
                    request_digest=_request_digest(submission),
                    after_save=lambda *_: ports.put_submission_in_transaction(submission),
                )
            else:
                if canonical_bytes(ports.get_submission(run_id)) != canonical_bytes(submission):
                    raise ContractValidationError("Accepted result changed before archive retry")
            self._clear_event_preview(run_id, "final_accepted")
            self._publish_event(run_id, "final_ready", {
                "result_id": package["turn"]["turn_id"], "turn_id": package["turn"]["turn_id"],
                "snapshot_id": package["turn"]["snapshot_id"],
            }, once="final_ready", visibility="business_candidate")
            try:
                saved_archive = store.archive_success(
                    [("turn", package["turn"]), ("candidate_group", package["group"]),
                     ("run_record", package["run"])],
                    "archive:" + run_id,
                )
            except Exception:
                self._publish_event(run_id, "archive_result", {
                    "result_id": package["turn"]["turn_id"], "status": "failed",
                    "turn_id": None, "reason_code": "archive_write_failed",
                }, visibility="business_candidate")
                raise
            receipt = make_archive_receipt(submission, saved_archive, allowed_bindings=(A_BINDING, B_BINDING))
            self._publish_event(run_id, "archive_result", {
                "result_id": package["turn"]["turn_id"], "status": "succeeded",
                "turn_id": package["turn"]["turn_id"], "reason_code": None,
            }, once="archive_succeeded", visibility="business_candidate")
            previous = store.read_receipt("workflow.update", "output:" + run_id)
            if previous is not None:
                released = ports.read_port(run_id, "final")
                if released is None or released["value"] != package["output"]["payload"]:
                    raise ContractValidationError("Saved output has no corresponding released result")
                self._publish_event(run_id, "ports_released", {
                    "result_id": package["turn"]["turn_id"], "turn_id": package["turn"]["turn_id"],
                    "output_id": package["output"]["output_id"], "status": "succeeded",
                    "ports": ["final", "context_delta"], "reason_code": None,
                }, once="ports_released", visibility="business_candidate")
                self._publish_run_state(package["run"], "final_ready")
                self._event_sources[run_id].seal()
                self._release_event_source(run_id)
                self._active_runs.pop(run_id, None)
                self._fact_sequences.pop(run_id, None)
                return copy.deepcopy(package["output"])
            chain = store.get_record("chain_run", {"chain_run_id": package["chain_run_id"]})
            reroll = any(row["chain_run_id"] == chain["chain_run_id"]
                         for row in store.list_records("chain_input_origin"))
            if chain["schema_version"] == 1:
                store.select_candidate(_record(
                    candidate_group_id=package["group"]["candidate_group_id"],
                    selected_turn_id=package["turn"]["turn_id"], revision=1,
                ), expected_revision=0)
            records = [("workflow_output", package["output"])]
            if not reroll:
                node = store.get_record("node_session", {
                    "workflow_session_id": sid,
                    "node_binding_id": package["run"]["node_binding_id"],
                })
                node["data_version"] += 1
                node["private_data"]["payload"]["head_turn_id"] = package["turn"]["turn_id"]
                records.insert(0, ("node_session", node))
            try:
                self._save(
                    store, sid, records, "output:" + run_id,
                    after_save=lambda *_: ports.release_in_transaction(submission, receipt),
                )
            except Exception:
                self._publish_event(run_id, "ports_released", {
                    "result_id": package["turn"]["turn_id"], "turn_id": package["turn"]["turn_id"],
                    "output_id": None, "status": "failed", "ports": [],
                    "reason_code": "ports_write_failed",
                }, visibility="business_candidate")
                raise
            if not package["retain_result"]:
                self._active_results.pop(run_id, None)
            self._active_runs.pop(run_id, None)
            self._fact_sequences.pop(run_id, None)
            final = ports.read_port(run_id, "final")
            if final is None or canonical_bytes(final["value"]) != canonical_bytes(package["output"]["payload"]):
                raise ContractValidationError("Formal business output has not been released")
            self._publish_event(run_id, "ports_released", {
                "result_id": package["turn"]["turn_id"], "turn_id": package["turn"]["turn_id"],
                "output_id": package["output"]["output_id"], "status": "succeeded",
                "ports": ["final", "context_delta"], "reason_code": None,
            }, once="ports_released", visibility="business_candidate")
            self._publish_run_state(package["run"], "final_ready")
            self._event_sources[run_id].seal()
            self._release_event_source(run_id)
            return copy.deepcopy(package["output"])

    def _execute_node(self, sid, chain_id, stage, run_id, *, retain_result=False):
        with self._lock, closing(self._store()) as store:
            run = store.get_record("run_record", {"run_id": run_id})
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
            chain = store.get_record("chain_run", {"chain_run_id": chain_id})
            if run["status"] == "paused" or run_id in self._interrupting:
                raise _ExecutionPaused()
            if run["status"] == "prepared":
                run.update(status="running", revision=run["revision"] + 1)
                chain["status"] = "running"
                self._save(store, sid, [("run_record", run), ("chain_run", chain)], "start:" + run_id)
            elif run["status"] != "running" or run_id not in self._run_checkpoints:
                raise ContractValidationError("Run cannot enter execution from its current state")
            active = self._active_runs[run_id]
            generation = active.snapshot().generation
            self._ensure_event_source(store, run)
            self._sync_event_generation(run_id)
            initial_start = (run_id, "run_started") not in self._event_once
            if run_id not in self._event_execution_fixed:
                self._event_registry.restore(self._event_declarations[run_id].to_json())
                self._event_execution_fixed.add(run_id)
            if initial_start:
                origin = next((row for row in store.list_records("chain_input_origin")
                               if row["chain_run_id"] == chain_id), None)
                source_run = None
                if run["source_run_id"] is not None:
                    source_run = store.get_record("run_record", {"run_id": run["source_run_id"]})
                elif origin is not None:
                    source_chain = store.get_record("chain_run", {"chain_run_id": origin["source_chain_run_id"]})
                    source_run = next((member for member in reversed(self._agent_members(store, source_chain))
                                       if member["node_binding_id"] == run["node_binding_id"]), None)
                self._publish_event(run_id, "run_started", {
                    "chain_run_id": chain_id, "input_id": run["input_id"],
                    "input_snapshot_id": snapshot["snapshot_id"],
                    "source_chain_run_id": origin["source_chain_run_id"] if origin is not None else None,
                    "source_run_id": source_run["run_id"] if source_run is not None else None,
                    "source_turn_id": source_run["result_turn_id"] if source_run is not None else None,
                    "parent_turn_id": snapshot["parent_turn_id"],
                }, once="run_started", visibility="business_candidate")
                self._publish_run_state(run, "prepared")
            checkpoint = self._run_checkpoints.get(run_id)
            limits = self._budget_limits(run, store.list_records("workflow_operation"))
        _, kernel, adapter, _ = self._snapshot_components(snapshot)
        model_binding = snapshot["config"]["payload"].get("model_binding")
        model = adapter.implementation(stage, snapshot["model_parameters"], **(
            {"model_binding": model_binding} if model_binding is not None else {}
        ))
        if validate_frozen_preparation(snapshot) is not None:
            model = PreparedRequestAdapter(model, snapshot)
        kernel_snapshot = copy.deepcopy(snapshot)
        paused_checkpoint = None
        try:
            try:
                event_arguments = {}
                if "events" in kernel.descriptor["capabilities"]:
                    event_arguments["on_event"] = _BehaviorReporter(
                        lambda event_type, ref, payload: self._report_behavior(
                            run_id, generation, event_type, ref, payload,
                        ),
                        lambda text: self._report_preview(run_id, generation, text),
                    )
                result = kernel.implementation.run(
                    kernel_snapshot, self._tools(), model,
                    max_model_requests=limits["max_model_requests"],
                    max_model_attempts=limits["max_model_attempts"],
                    checkpoint=checkpoint,
                    on_progress=lambda progress: self._on_progress(
                        sid, run_id, generation, progress,
                    ),
                    on_boundary=lambda kind: self._run_boundary(
                        run_id, generation, kind,
                    ),
                    on_tool_event=lambda kind, call_id, execution_id, outcome:
                        self._run_tool_event(
                            run_id, generation, kind, call_id, execution_id, outcome,
                        ),
                    on_fact=lambda event: self._on_fact(run_id, generation, event),
                    **event_arguments,
                )
            except KernelPaused as paused:
                paused_checkpoint = paused.checkpoint
        finally:
            primary = sys.exc_info()[1]
            close = getattr(model, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as cleanup_error:
                    preserve_exit = primary is not None and not isinstance(primary, Exception)
                    if not isinstance(cleanup_error, Exception) and not preserve_exit:
                        raise
                    if preserve_exit or isinstance(primary, KernelContractError):
                        add_note = getattr(primary, "add_note", None)
                        if callable(add_note):
                            add_note("Adapter cleanup also failed.")
                        else:
                            primary.__notes__ = [*getattr(primary, "__notes__", ()),
                                                 "Adapter cleanup also failed."]
                    elif isinstance(primary, RunFailed) or paused_checkpoint is not None:
                        state = primary if isinstance(primary, RunFailed) else paused_checkpoint
                        raise KernelContractError(
                            "adapter_close_error", list(state.messages),
                            state.model_requests, state.attempts,
                        ) from (primary or cleanup_error)
                    elif primary is None:
                        raise
        if paused_checkpoint is not None:
            with self._lock, closing(self._store()) as store:
                view = active.snapshot()
                progress = AcceptedProgress(
                    paused_checkpoint.model_requests, paused_checkpoint.attempts,
                    len(paused_checkpoint.messages),
                )
                if progress != view.accepted_progress:
                    active.accept_progress(
                        progress, expected_generation=generation,
                        expected_revision=view.revision,
                    )
                current = store.get_record("run_record", {"run_id": run_id})
                if current["status"] != "pausing":
                    raise ContractValidationError("Only an interrupted run can pause")
                current.update(status="paused", revision=current["revision"] + 1)
                current_chain = store.get_record("chain_run", {"chain_run_id": chain_id})
                current_chain["status"] = "paused"
                self._save(store, sid, [
                    ("run_record", current), ("chain_run", current_chain),
                ], "paused:" + run_id + ":" + str(current["revision"]))
                self._clear_event_preview(run_id, "paused")
                self._interrupting.discard(run_id)
                view = active.snapshot()
                active.fence(
                    expected_generation=view.generation, expected_revision=view.revision,
                )
                self._run_checkpoints[run_id] = paused_checkpoint
                self._sync_event_generation(run_id)
                self._publish_run_state(current, "pausing", reason_code="paused")
            raise _ExecutionPaused() from None
        with self._lock, closing(self._store()) as store:
            current = store.get_record("run_record", {"run_id": run_id})
            if current["status"] != "running" or run_id in self._interrupting:
                raise ContractValidationError("Final result lost its run eligibility")
            if canonical_bytes(kernel_snapshot) != canonical_bytes(snapshot):
                raise ContractValidationError("Kernel changed its frozen input snapshot")
            turn = self._validate_kernel_result(store, run, snapshot, result, limits)
            current.update(status="final_ready", revision=current["revision"] + 1)
            self._finalizing_runs.discard(run_id)
            self._run_checkpoints.pop(run_id, None)
            group = _record(candidate_group_id=_uid(), workflow_session_id=sid, node_binding_id=run["node_binding_id"],
                logical_input_id=run["input_id"], parent_turn_id=turn["parent_turn_id"],
                frozen_snapshot_id=snapshot["snapshot_id"], candidate_refs=[{"run_id": run_id, "turn_id": turn["turn_id"]}])
            output = _record(output_id=_uid(), workflow_session_id=sid, chain_run_id=chain_id,
                source={"kind": "node_run", "node_binding_id": run["node_binding_id"], "run_id": run_id, "turn_id": turn["turn_id"]},
                port_id="reply", payload_schema_ref=TEXT_SCHEMA_REF, payload=copy.deepcopy(turn["final"]["value"]))
            run_success = copy.deepcopy(current)
            run_success.update(status="succeeded", revision=current["revision"] + 1,
                               result_turn_id=turn["turn_id"])
            self._active_results[run_id] = {
                "session_id": sid, "chain_run_id": chain_id, "stage": stage,
                "retain_result": retain_result, "turn": turn, "group": group,
                "run": run_success, "output": output,
            }
            submission = make_archive_submission(
                make_result_package(turn, group, run_success, output),
                snapshot["config"]["payload"]["result_port_routes"],
                allowed_bindings=(A_BINDING, B_BINDING),
            )
            self._save(
                store, sid, [("run_record", current)], "final-ready:" + run_id,
                after_save=lambda *_: ResultPortStore(
                    store, allowed_bindings=(A_BINDING, B_BINDING),
                ).put_submission_in_transaction(submission),
            )
            self._clear_event_preview(run_id, "final_accepted")
            self._publish_event(run_id, "final_ready", {
                "result_id": turn["turn_id"], "turn_id": turn["turn_id"],
                "snapshot_id": snapshot["snapshot_id"],
            }, once="final_ready", visibility="business_candidate")
            self._publish_run_state(current, "running")
        output = self._archive_accepted_node(self._active_results[run_id])
        with self._lock:
            self._active_runs.pop(run_id, None)
            self._fact_sequences.pop(run_id, None)
        return output

    def _validate_kernel_result(self, store, run, snapshot, result, limits):
        """A replacement must return exactly the accepted, closed execution facts."""
        try:
            requests, attempts = result.model_requests, result.attempts
            if (type(requests) is not int or type(attempts) is not int
                    or not 1 <= requests <= limits["max_model_requests"]
                    or not requests <= attempts <= limits["max_model_attempts"]):
                raise ContractValidationError("Kernel result has invalid cumulative counters")
            turn = validate_record("turn", _record(
                turn_id=_uid(), parent_turn_id=snapshot["parent_turn_id"], run_id=run["run_id"],
                snapshot_id=snapshot["snapshot_id"], input_id=run["input_id"],
                messages=result.messages, final=result.final,
                projection_version=snapshot["projection_version"],
            ))
            validate_message_history(snapshot["s0"] + turn["messages"])
            validate_turn_final(turn, snapshot)
            facts = store.read_execution_facts(run["run_id"])
            accepted = [row["payload"]["message"] for row in facts if row["kind"] == "message_accepted"]
            started = {row["payload"]["attempt_id"] for row in facts if row["kind"] == "model_attempt_started"}
            finished = {row["payload"]["attempt_id"] for row in facts if row["kind"] == "model_attempt_finished"}
            view = self._active_runs[run["run_id"]].snapshot()
            if (requests != sum(row["kind"] == "model_request" for row in facts)
                    or attempts != len(started) or started != finished
                    or canonical_bytes(turn["messages"]) != canonical_bytes(accepted)
                    or view.accepted_progress != AcceptedProgress(requests, attempts, len(accepted))
                    or view.pending_tools or run["run_id"] not in self._finalizing_runs):
                raise ContractValidationError("Kernel result differs from accepted execution facts or final boundary")
            return turn
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise ContractValidationError("Kernel returned an invalid public result") from exc

    def _run_boundary(self, run_id, generation, kind):
        with self._lock:
            if self._callbacks_revoked:
                return False
            active = self._active_runs.get(run_id)
            if active is None or active.snapshot().generation != generation:
                return False
            if run_id in self._interrupting:
                return False
            if kind == "before_final":
                self._finalizing_runs.add(run_id)
            return True

    def _run_tool_event(self, run_id, generation, kind, call_id, execution_id, outcome):
        with self._lock:
            if self._callbacks_revoked:
                raise KernelPauseRequested()
            active = self._active_runs[run_id]
            view = active.snapshot()
            if view.generation != generation:
                raise KernelPauseRequested()
            guard = {"expected_generation": generation, "expected_revision": view.revision}
            if kind == "queue":
                if execution_id is not None or outcome is not None:
                    raise ContractValidationError("Queued tool cannot have an execution or outcome")
                active.queue_tool(call_id, **guard)
            elif kind == "start":
                if outcome is not None:
                    raise ContractValidationError("Started tool cannot have an outcome")
                if run_id in self._interrupting:
                    raise KernelPauseRequested()
                active.start_tool(call_id, execution_id, **guard)
            elif kind == "settle":
                tool = next((tool for tool in view.tools if tool.tool_call_id == call_id), None)
                if tool is None or execution_id != tool.tool_execution_id:
                    raise ContractValidationError("Settled tool execution identity does not match")
                active.settle_tool(call_id, outcome, **guard)
            elif kind == "skip":
                if execution_id is not None or outcome != "never_started":
                    raise ContractValidationError("Skipped tool must be never-started without an execution")
                active.skip_tool(call_id, **guard)
            else:
                raise ContractValidationError("Unknown kernel tool event")
            current = active.snapshot()
            order = next(index + 1 for index, tool in enumerate(current.tools)
                         if tool.tool_call_id == call_id)
            value = "started" if kind == "start" else outcome
            status = ("pending" if kind == "queue" else
                      "uncertain" if value in {"started", "unknown", "outcome_unknown", "interrupted"}
                      else "settled")
            self._publish_event(run_id, "tool_progress", {
                "tool_call_id": call_id, "tool_execution_id": execution_id,
                "model_order": order, "status": status, "outcome": value,
            })

    def _on_progress(self, sid, run_id, generation, progress):
        with self._lock:
            if self._callbacks_revoked:
                raise KernelPauseRequested()
            active = self._active_runs[run_id]
            view = active.snapshot()
            if view.generation != generation:
                raise KernelPauseRequested()
            counters = AcceptedProgress(
                progress["model_requests"], progress["attempts"],
                progress["accepted_messages"],
            )
            if counters != view.accepted_progress:
                active.accept_progress(
                    counters, expected_generation=generation,
                    expected_revision=view.revision,
                )
            self._progress[sid] = copy.deepcopy(progress)

    def _on_fact(self, run_id: str, generation: str, event: dict[str, Any]) -> None:
        with self._lock, closing(self._callback_store()) as store:
            if self._callbacks_revoked:
                raise ContractValidationError("Workflow fact owner is closed")
            active = self._active_runs.get(run_id)
            if active is None or active.snapshot().generation != generation:
                raise ContractValidationError("Stale execution fact generation")
            if type(event) is not dict or set(event) != {"kind", "payload"}:
                raise ContractValidationError("Invalid execution fact event")
            run = store.get_record("run_record", {"run_id": run_id})
            if run["status"] not in {"running", "pausing"}:
                raise ContractValidationError("Execution facts require a live run")
            if event["kind"] == "model_request":
                snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
                check_prepared_request_capacity(snapshot, messages=event["payload"]["messages"])
            sequence = self._fact_sequences.get(run_id)
            if sequence is None:
                previous = store.read_execution_facts(run_id)
                sequence = previous[-1]["sequence"] if previous else 0
            stored = store.append_execution_fact(_record(
                fact_id=_uid(), run_id=run_id, chain_run_id=run["chain_run_id"],
                workflow_session_id=run["workflow_session_id"],
                node_binding_id=run["node_binding_id"], snapshot_id=run["snapshot_id"],
                generation=generation, sequence=sequence + 1,
                kind=event["kind"], payload=copy.deepcopy(event["payload"]),
            ), expected_sequence=sequence)
            self._fact_sequences[run_id] = stored["sequence"]
            if stored["kind"] in {"model_attempt_started", "model_attempt_finished"}:
                payload = stored["payload"]
                started = payload
                if stored["kind"] == "model_attempt_finished":
                    started = next(
                        row["payload"] for row in store.read_execution_facts(run_id)
                        if row["kind"] == "model_attempt_started"
                        and row["payload"]["attempt_id"] == payload["attempt_id"]
                    )
                else:
                    self._clear_event_preview(run_id, "request_attempt")
                self._publish_event(run_id, "request_attempt", {
                    "request_id": payload["request_id"], "request_index": started["request_index"],
                    "attempt_id": payload["attempt_id"], "attempt_index": started["attempt_index"],
                    "outcome": "started" if stored["kind"] == "model_attempt_started" else payload["outcome"],
                    "phase": "requesting" if stored["kind"] == "model_attempt_started" else "attempt_settled",
                })
                if stored["kind"] == "model_attempt_finished" and payload["outcome"] != "responded":
                    self._clear_event_preview(run_id, "attempt_rejected")

    def _persist_execution_failure(self, run_id, code, category):
        """Save coordinator diagnostics without changing any successful node fact."""
        try:
            with closing(self._store()) as store:
                run = store.get_record("run_record", {"run_id": run_id})
                facts = store.read_execution_facts(run_id)
                failures = [row["payload"] for row in facts if row["kind"] == "execution_failed"]
                if failures and failures[-1]["code"] == code and failures[-1]["category"] == category:
                    return
                active = self._active_runs.get(run_id)
                generation = (active.snapshot().generation if active is not None else
                              facts[-1]["generation"] if facts else _uid())
                sequence = len(facts)
                store.append_execution_fact(_record(
                    fact_id=_uid(), run_id=run_id, chain_run_id=run["chain_run_id"],
                    workflow_session_id=run["workflow_session_id"],
                    node_binding_id=run["node_binding_id"], snapshot_id=run["snapshot_id"],
                    generation=generation, sequence=sequence + 1, kind="execution_failed",
                    payload={
                        "code": code, "category": category,
                        "model_requests": sum(row["kind"] == "model_request" for row in facts),
                        "attempts": sum(row["kind"] == "model_attempt_started" for row in facts),
                    },
                ), expected_sequence=sequence)
                self._fact_sequences[run_id] = sequence + 1
        except Exception:
            # A failed diagnostic write never authorizes further dispatch or
            # replaces the original failure with fabricated execution evidence.
            return

    def _fail(self, sid, chain_id, run_id, code, *, failure=None):
        with self._lock:
            contract_fault = isinstance(failure, KernelContractError)
            recording_failed = getattr(failure, "recording_failed", False)
            if recording_failed:
                if code != "execution_interrupted":
                    code = "fact_persistence_error"
                contract_fault = True
            if code == "DISPATCH_FAILED":
                message = ("The workflow could not be scheduled after durable acceptance. "
                           "No model or tool was dispatched.")
            elif code == "execution_interrupted":
                message = ("The host interrupted execution before the tool batch could close. "
                           "No automatic continuation occurred; started effects may require verification.")
            elif code == "prompt_capacity_exceeded":
                message = ("The complete model request exceeded its frozen prompt capacity. "
                           "No oversized request was dispatched; accepted execution facts remain saved.")
            elif contract_fault:
                message = ("A program contract was violated. The workflow stopped without "
                           "fabricating a tool observation or successful history.")
            else:
                message = ("The model or protocol failed. The workflow stopped; "
                           "no failed content entered successful history.")
            category = ("contract" if contract_fault else
                        "interrupted" if code in ("execution_interrupted", "DISPATCH_FAILED") else
                        "protocol" if code in ("protocol_error", "invalid_final") else "model")
            self._persist_execution_failure(run_id, code, category)
            self._errors[sid] = {"code": code, "message": message}
            if recording_failed:
                self._errors[sid]["secondary_code"] = "fact_persistence_error"
                self._errors[sid]["message"] += " Failure facts could not be saved."
            active = self._active_runs.get(run_id)
            if active is not None:
                uncertain = [
                    {"tool_call_id": tool.tool_call_id,
                     "tool_execution_id": tool.tool_execution_id}
                    for tool in active.snapshot().tools if tool.status == "unknown"
                ]
                if uncertain:
                    self._uncertain_tools[sid] = uncertain
            try:
                with closing(self._store()) as store:
                    run = store.get_record("run_record", {"run_id": run_id})
                    previous_status = run["status"]
                    run.update(status="failed", revision=run["revision"] + 1)
                    chain = store.get_record("chain_run", {"chain_run_id": chain_id})
                    chain["status"] = "failed"
                    records = [("run_record", run), ("chain_run", chain)]
                    if not any(
                        row["chain_run_id"] == chain_id
                        for row in store.list_records("chain_input_origin")
                    ):
                        ref = next(row for row in store.list_records("visible_message_ref")
                                   if row["workflow_session_id"] == sid and row["role"] == "user"
                                   and row["boundary"]["chain_run_id"] == chain_id)
                        if ref["boundary"]["input_status"] != "failed":
                            ref["boundary"]["input_status"] = "failed"
                            records.append(("visible_message_ref", ref))
                    self._save(store, sid, records,
                               "failed:" + run_id + ":" + str(run["revision"]))
                    self._clear_event_preview(run_id, "execution_failed")
                    self._publish_event(run_id, "diagnostic", {
                        "code": code, "category": category,
                    })
                    self._publish_run_state(run, previous_status, reason_code=code)
                    self._interrupting.discard(run_id)
                    self._finalizing_runs.discard(run_id)
                    checkpoint = getattr(failure, "checkpoint", None)
                    if checkpoint is not None and active is not None:
                        self._run_checkpoints[run_id] = checkpoint
                        view = active.snapshot()
                        progress = AcceptedProgress(
                            checkpoint.model_requests, checkpoint.attempts,
                            len(checkpoint.messages),
                        )
                        if progress != view.accepted_progress:
                            active.accept_progress(
                                progress, expected_generation=view.generation,
                                expected_revision=view.revision,
                            )
                        view = active.snapshot()
                        active.fence(
                            expected_generation=view.generation, expected_revision=view.revision,
                        )
                        self._sync_event_generation(run_id)
                        self._errors[sid]["message"] += " Same-run continuation requires a manual command."
                    else:
                        self._active_runs.pop(run_id, None)
                        self._run_checkpoints.pop(run_id, None)
                        self._fact_sequences.pop(run_id, None)
                        self._release_event_source(run_id)
            except Exception as exc:
                self._execution_failures[sid] = exc
                self._persist_execution_failure(run_id, "PROGRAM_OR_STORAGE_FAILED", "contract")
                self._errors[sid] = {"code": "ERROR_RECORD_NOT_SAVED", "message": "The workflow stopped and its error status could not be saved."}

    def _publish(self, sid, chain_id, upstream):
        with self._lock, closing(self._store()) as store:
            candidates = [row for row in store.list_records("workflow_candidate")
                          if row["chain_run_id"] == chain_id]
            if not candidates:
                candidate = self._archive_workflow_candidate(store, sid, chain_id, upstream)
            elif len(candidates) == 1 and candidates[0]["workflow_session_id"] == sid:
                candidate = candidates[0]
            else:
                raise ContractValidationError("Chain has an ambiguous workflow candidate")
            try:
                self._select_workflow_candidate(store, sid, candidate)
            except ContractValidationError as exc:
                if ("Workflow ref head conflict" not in str(exc)
                        or not any(row["chain_run_id"] == chain_id
                                   for row in store.list_records("chain_input_origin"))):
                    self._publish_delivery_event(store, candidate, "failed", "delivery_write_failed")
                    raise
                # A sibling result is durable even when another Head wins the CAS.
            except Exception:
                self._publish_delivery_event(store, candidate, "failed", "delivery_write_failed")
                raise

    def _publish_delivery_event(self, store, candidate, status, reason_code=None):
        output = store.get_record("workflow_output", {"output_id": candidate["output_id"]})
        run = store.get_record("node_run", {"run_id": output["source"]["run_id"]})
        deliveries = [
            row for row in store.list_records("output_delivery")
            if row["output_id"] == candidate["output_id"]
            and row["target"] == {"kind": "ui", "workflow_session_id": candidate["workflow_session_id"]}
        ]
        if not deliveries:
            return  # Unselected reroll candidates have no UI delivery to report.
        if len(deliveries) != 1:
            raise ContractValidationError("Workflow event has ambiguous output delivery")
        delivery = deliveries[0]
        if status == "succeeded" and delivery["status"] != "succeeded":
            raise ContractValidationError("Workflow event cannot invent successful delivery")
        message = next(
            (row for row in store.list_records("visible_message")
             if row["role"] == "assistant" and row["source"] == {
                 "kind": "workflow_output", "output_id": output["output_id"],
             }), None,
        )
        if status == "succeeded" and message is None:
            raise ContractValidationError("Successful workflow delivery has no visible message")
        self._ensure_event_source(store, run)
        self._publish_event(run["run_id"], "workflow_published", {
            "chain_run_id": candidate["chain_run_id"], "output_id": output["output_id"],
            "delivery_id": delivery["delivery_id"],
            "visible_message_id": message["visible_message_id"] if status == "succeeded" else None,
            "status": status, "reason_code": reason_code,
        }, once="delivery:" + delivery["delivery_id"] + ":" + status if status != "failed" else None,
            visibility="business_candidate")
        if status == "succeeded":
            self._event_sources[run["run_id"]].seal()
            self._release_event_source(run["run_id"])

    def _archive_workflow_candidate(self, store, sid, chain_id, upstream):
        chain = store.get_record("chain_run", {"chain_run_id": chain_id})
        if chain["schema_version"] != 2 or chain["status"] != "running":
            raise ContractValidationError("Only a running versioned chain can archive a candidate")
        origin = next((row for row in store.list_records("chain_input_origin")
                       if row["chain_run_id"] == chain_id), None)
        input_id, run_id, output_id, visible_id = _uid(), _uid(), _uid(), _uid()
        node_input = _record(input_id=input_id, port_id="request",
            payload_schema_ref=upstream["payload_schema_ref"],
            source={"kind": "upstream_output", "output_id": upstream["output_id"]},
            payload=upstream["payload"])
        output = _record(output_id=output_id, workflow_session_id=sid, chain_run_id=chain_id,
            source={"kind": "node_run", "node_binding_id": OUTPUT_BINDING,
                    "run_id": run_id, "turn_id": None},
            port_id="reply", payload_schema_ref=TEXT_SCHEMA_REF,
            payload=self._emit_output(node_input))
        run = _record(profile="node", run_id=run_id, workflow_session_id=sid,
            node_binding_id=OUTPUT_BINDING, chain_run_id=chain_id, input_id=input_id,
            input_snapshot_id=None, source_run_id=None, status="succeeded", revision=1,
            result_output_id=output_id, superseded_by_run_id=None)
        node = store.get_record("node_session", {
            "workflow_session_id": sid, "node_binding_id": OUTPUT_BINDING,
        })
        node["data_version"] += 1
        node["private_data"]["payload"]["last_output_id"] = output_id
        nodes = [node if row["node_binding_id"] == OUTPUT_BINDING else row
                 for row in self._node_states(store, sid)]
        if origin is None:
            selections = self._selections(store, sid)
        else:
            base = store.get_record("workflow_commit", {
                "commit_id": chain["base_commit_id"],
            })
            base_snapshot = store.get_record("state_snapshot", {
                "state_snapshot_id": base["state_snapshot_id"],
            })
            selections = copy.deepcopy(base_snapshot["selection_refs"])
            paths = {}
            for member in self._agent_members(store, chain):
                member_snapshot = store.get_record("input_snapshot", {
                    "snapshot_id": member["snapshot_id"],
                })
                path = set()
                cursor = member_snapshot["parent_turn_id"]
                while cursor is not None:
                    path.add(cursor)
                    cursor = store.get_record("turn", {"turn_id": cursor})["parent_turn_id"]
                paths[member["node_binding_id"]] = path
            by_group = {
                row["candidate_group_id"]: row
                for row in store.list_records("candidate_group")
            }
            selections = [
                row for row in selections
                if row["selected_turn_id"] in paths.get(
                    by_group[row["candidate_group_id"]]["node_binding_id"], set(),
                )
            ]
        selected_groups = {row["candidate_group_id"] for row in selections}
        groups = store.list_records("candidate_group")
        for member in self._agent_members(store, chain):
            member_id = member["run_id"]
            group = next((row for row in groups if any(
                ref["run_id"] == member_id for ref in row["candidate_refs"]
            )), None)
            if group is None or member["result_turn_id"] is None:
                raise ContractValidationError("Completed node is missing its candidate")
            if origin is not None:
                selected_node = next(row for row in nodes
                                     if row["node_binding_id"] == member["node_binding_id"])
                selected_node["data_version"] += 1
                selected_node["private_data"]["payload"]["head_turn_id"] = member["result_turn_id"]
            if group["candidate_group_id"] not in selected_groups:
                selections.append({
                    "candidate_group_id": group["candidate_group_id"],
                    "selected_turn_id": member["result_turn_id"],
                })
                selected_groups.add(group["candidate_group_id"])
        checkpoint = self._checkpoint(
            sid, nodes, selections, kind="completed_output", output_id=output_id,
            revision=self._session(store, sid)["revision"] + 1,
        )
        chain.update(status="succeeded", output_id=output_id)
        chain["node_run_ids"].append(run_id)
        all_refs = [row for row in store.list_records("visible_message_ref")
                    if row["workflow_session_id"] == sid]
        indexed_refs = {row["visible_message_id"]: row for row in all_refs}
        if origin is None:
            head = self._workflow_ref(store, sid)
            prior_commit = store.get_record("workflow_commit", {
                "commit_id": head["head_commit_id"],
            })
        else:
            operation = store.get_record("workflow_operation", {"operation_id": chain["operation_id"]})
            prior_commit_id = operation["expected_head_commit_id"]
            prior_commit = store.get_record("workflow_commit", {
                "commit_id": prior_commit_id,
            })
        prior_snapshot = store.get_record("state_snapshot", {
            "state_snapshot_id": prior_commit["state_snapshot_id"],
        })
        refs = [copy.deepcopy(indexed_refs[item["visible_message_id"]])
                for item in prior_snapshot["visible_message_refs"]]
        if origin is None:
            user = next(row for row in all_refs
                        if row["role"] == "user"
                        and row["boundary"]["chain_run_id"] == chain_id)
            user["boundary"]["input_status"] = "completed"
            refs = [row for row in refs if row["visible_message_id"] != user["visible_message_id"]]
            refs.append(user)
        else:
            user = next(row for row in all_refs if row["role"] == "user"
                        and row["visible_message_id"] == origin["visible_message_id"])
            user["boundary"]["input_status"] = "completed"
            if not any(row["visible_message_id"] == user["visible_message_id"] for row in refs):
                refs.append(user)
            elif refs and refs[-1]["role"] == "assistant":
                if len(refs) < 2 or refs[-2]["visible_message_id"] != user["visible_message_id"]:
                    raise ContractValidationError("Replacement source has no selected terminal floor")
                refs.pop()
                refs[-1] = user
            elif refs[-1]["visible_message_id"] == user["visible_message_id"]:
                refs[-1] = user
            else:
                raise ContractValidationError("Replacement source is not the latest user floor")
        message = _record(
            visible_message_id=visible_id, origin_workflow_session_id=sid,
            role="assistant", payload_schema_ref=TEXT_SCHEMA_REF,
            payload=output["payload"],
            source={"kind": "workflow_output", "output_id": output_id},
        )
        assistant_ref = _record(
            workflow_session_id=sid, visible_message_id=visible_id,
            sequence=max((row["sequence"] for row in all_refs), default=0) + 1,
            role="assistant",
            boundary={"chain_run_id": chain_id, "output_id": output_id,
                      "after_checkpoint_id": checkpoint["checkpoint_id"]},
        )
        snapshot = self._state_snapshot(
            sid, checkpoint["nodes"], selections, [*refs, assistant_ref],
        )
        candidate_id, result_id = _uid(), _uid()
        result = _record(
            commit_id=result_id, workflow_session_id=sid,
            state_snapshot_id=snapshot["state_snapshot_id"],
            parent_commit_id=chain["base_commit_id"],
            operation_id=chain["operation_id"],
            source={"kind": "candidate", "candidate_id": candidate_id},
        )
        candidate = _record(
            candidate_id=candidate_id, workflow_session_id=sid,
            chain_run_id=chain_id, base_commit_id=chain["base_commit_id"],
            result_commit_id=result_id, output_id=output_id,
            checkpoint_id=checkpoint["checkpoint_id"],
        )
        archived = [
            ("node_input", node_input), ("node_run", run),
            ("workflow_output", output), ("workflow_checkpoint", checkpoint),
            ("chain_run", chain), ("visible_message", message),
            ("state_snapshot", snapshot),
            ("workflow_commit", result), ("workflow_candidate", candidate),
        ]
        if origin is None:
            archived.append(("node_session", node))
            archived.append(("output_delivery", _record(
                delivery_id=_uid(), output_id=output_id,
                target={"kind": "ui", "workflow_session_id": sid},
                status="pending", idempotency_key="ui:" + chain_id,
            )))
        else:
            archived.append(("visible_message_ref", assistant_ref))
        self._save(store, sid, archived, "archive-candidate:" + chain_id,
                   operation="workflow.archive_candidate")
        self._ensure_event_source(store, run)
        self._release_event_source(run["run_id"])
        self._publish_delivery_event(store, candidate, "pending")
        return candidate

    @staticmethod
    def _candidate_floor(store, candidate):
        chain_id = candidate["chain_run_id"]
        origin = next((row for row in store.list_records("chain_input_origin")
                       if row["chain_run_id"] == chain_id), None)
        if origin is not None:
            return origin["visible_message_id"]
        refs = [
            row for row in store.list_records("visible_message_ref")
            if row["workflow_session_id"] == candidate["workflow_session_id"]
            and row["role"] == "user"
            and row["boundary"]["chain_run_id"] == chain_id
        ]
        if len(refs) != 1:
            raise ContractValidationError("Candidate has no unique user floor")
        return refs[0]["visible_message_id"]

    def _same_floor_sibling(self, store, sid, selected_commit_id, target):
        selected_commit = store.get_record("workflow_commit", {
            "commit_id": selected_commit_id,
        })
        source = selected_commit["source"]
        if source["kind"] != "candidate":
            session = self._session(store, sid)
            if session["source"]["kind"] != "fork":
                return False
            current_snapshot = store.get_record("state_snapshot", {
                "state_snapshot_id": selected_commit["state_snapshot_id"],
            })
            current_refs = current_snapshot["visible_message_refs"]
            target_commit = store.get_record("workflow_commit", {
                "commit_id": target["result_commit_id"],
            })
            target_snapshot = store.get_record("state_snapshot", {
                "state_snapshot_id": target_commit["state_snapshot_id"],
            })
            target_refs = target_snapshot["visible_message_refs"]
            if (len(current_refs) < 2 or current_refs[-2]["role"] != "user"
                    or current_refs[-1]["role"] != "assistant"
                    or current_refs[:-1] != target_refs[:-1]):
                return False
            floors = self._reply_candidate_floors(
                store, sid, self._formal_refs(store, sid), store.read_bundle(),
                require_complete=True,
            )
            return bool(floors) and (
                floors[-1]["user_visible_message_id"] == current_refs[-2]["visible_message_id"]
                and target["candidate_id"] in floors[-1]["candidate_ids"]
                and target["candidate_id"] != floors[-1]["selected_candidate_id"]
            )
        current = store.get_record("workflow_candidate", {
            "candidate_id": source["candidate_id"],
        })
        if (current["workflow_session_id"] != sid
                or current["candidate_id"] == target["candidate_id"]
                or current["base_commit_id"] != target["base_commit_id"]
                or self._candidate_floor(store, current) != self._candidate_floor(store, target)):
            return False
        current_snapshot = store.get_record("state_snapshot", {
            "state_snapshot_id": selected_commit["state_snapshot_id"],
        })
        target_commit = store.get_record("workflow_commit", {
            "commit_id": target["result_commit_id"],
        })
        target_snapshot = store.get_record("state_snapshot", {
            "state_snapshot_id": target_commit["state_snapshot_id"],
        })
        current_refs = current_snapshot["visible_message_refs"]
        target_refs = target_snapshot["visible_message_refs"]
        return (
            bool(current_refs) and bool(target_refs)
            and current_refs[-1]["role"] == target_refs[-1]["role"] == "assistant"
            and current_refs[:-1] == target_refs[:-1]
            and current_refs[-1]["boundary"]["chain_run_id"] == current["chain_run_id"]
            and target_refs[-1]["boundary"]["chain_run_id"] == target["chain_run_id"]
        )

    def select_candidate(
        self, session_id: str, candidate_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_ref_revision: int,
        expected_head_commit_id: str,
    ) -> dict[str, Any]:
        if type(expected_session_revision) is not int or expected_session_revision < 1:
            raise ContractValidationError("Expected session revision must be positive")
        if type(expected_head_commit_id) is not str:
            raise ContractValidationError("Expected Head commit is required")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            existing_receipt = store.read_receipt_with_digest(
                "workflow.explicit_select_candidate",
                session_id + ":" + idempotency_key,
            )
            if existing_receipt is not None and any(
                row.get("kind") == "select_candidate"
                and row.get("target") != {"kind": "candidate", "id": candidate_id}
                for row in existing_receipt[1]
                if "operation_id" in row
            ):
                raise ContractValidationError(
                    "Idempotency key has different workflow operation request"
                )
            candidate = store.get_record("workflow_candidate", {"candidate_id": candidate_id})
            if candidate["workflow_session_id"] != session_id:
                source = session["source"]
                if source["kind"] != "fork":
                    raise ContractValidationError("Candidate belongs to another workflow session")
                anchor = store.get_record("fork_anchor", {
                    "fork_anchor_id": source["fork_anchor_id"],
                })
                if (anchor["role"] != "assistant" or not anchor.get("candidate_floors")
                        or candidate_id not in anchor["candidate_floors"][-1]["candidate_ids"]):
                    raise ContractValidationError("Candidate belongs to another workflow session")
                return self.select_inherited_candidate(
                    session_id, candidate_id, idempotency_key=idempotency_key,
                    expected_session_revision=expected_session_revision,
                    expected_ref_revision=expected_ref_revision,
                    expected_head_commit_id=expected_head_commit_id,
                )
            ref = self._workflow_ref(store, session_id)
            operation = self._operation(
                "select_candidate", session_id, "candidate", candidate_id,
                idempotency_key, [{
                    "kind": "workflow_ref", "id": ref["workflow_ref_id"],
                    "revision": expected_ref_revision,
                }], {"expected_session_revision": expected_session_revision},
                expected_head_commit_id=expected_head_commit_id,
            )
            request = {field: value for field, value in operation.items()
                       if field != "operation_id"}
            request_digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
            receipt_key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest(
                "workflow.explicit_select_candidate", receipt_key,
            )
            if receipt is not None:
                if receipt[0] != request_digest:
                    raise ContractValidationError(
                        "Idempotency key has different workflow operation request"
                    )
                saved = receipt[1]
            else:
                if session["revision"] != expected_session_revision:
                    raise ContractValidationError("Workflow session revision conflict")
                if (ref["revision"] != expected_ref_revision
                        or ref["head_commit_id"] != expected_head_commit_id):
                    raise ContractValidationError("Workflow ref head conflict")
                chain = store.get_record("chain_run", {"chain_run_id": candidate["chain_run_id"]})
                origin = next((row for row in store.list_records("chain_input_origin")
                               if row["chain_run_id"] == chain["chain_run_id"]), None)
                if origin is None:
                    expected_base = candidate["base_commit_id"]
                    expected_base_revision = chain["base_ref_revision"]
                else:
                    reroll_operation = store.get_record("workflow_operation", {
                        "operation_id": chain["operation_id"],
                    })
                    expected_base = reroll_operation["expected_head_commit_id"]
                    expected_base_revision = next(
                        item["revision"] for item in reroll_operation["expected_revisions"]
                        if item["kind"] == "workflow_ref"
                    )
                sibling = self._same_floor_sibling(
                    store, session_id, expected_head_commit_id, candidate,
                )
                if (chain["schema_version"] != 2 or chain["status"] != "succeeded"
                        or chain["workflow_session_id"] != session_id
                        or chain["base_commit_id"] != candidate["base_commit_id"]
                        or not (sibling or (
                            expected_base_revision == expected_ref_revision
                            and expected_base == expected_head_commit_id
                        ))):
                    raise ContractValidationError("Candidate is not based on the current Head")
                future = self._futures.get(session_id)
                if (future is not None and not future.done()) or any(
                    row["workflow_session_id"] == session_id
                    and row["status"] not in ("succeeded", "superseded", "closed")
                    for row in store.list_records("chain_run")
                ):
                    raise ContractValidationError("Workflow session has an unfinished execution")
                pending = [
                    row for row in store.list_records("output_delivery")
                    if row["target"] == {"kind": "ui", "workflow_session_id": session_id}
                    and row["status"] == "pending"
                ]
                if sibling:
                    if pending:
                        raise ContractValidationError("Workflow has an unpublished output")
                    matching = [
                        row for row in store.list_records("output_delivery")
                        if row["output_id"] == candidate["output_id"]
                        and row["target"] == {
                            "kind": "ui", "workflow_session_id": session_id,
                        }
                    ]
                    if len(matching) > 1 or matching and matching[0]["status"] != "succeeded":
                        raise ContractValidationError("Candidate has an unsettled output delivery")
                elif origin is None:
                    if len(pending) != 1 or pending[0]["output_id"] != candidate["output_id"]:
                        raise ContractValidationError("Candidate has no exclusive pending output delivery")
                elif pending or any(
                    row["output_id"] == candidate["output_id"]
                    for row in store.list_records("output_delivery")
                ):
                    raise ContractValidationError("Reroll candidate already has an output delivery")
                saved = self._select_workflow_candidate(
                    store, session_id, candidate,
                    selection_operation=operation,
                    receipt_key=receipt_key,
                    request_digest=request_digest,
                    expected_session_revision=expected_session_revision,
                    expected_ref_revision=expected_ref_revision,
                    expected_head_commit_id=expected_head_commit_id,
                    sibling=sibling,
                )
                for run_id, package in list(self._active_results.items()):
                    if (isinstance(package, dict)
                            and package.get("session_id") == session_id
                            and package.get("chain_run_id") == candidate["chain_run_id"]
                            and package.get("stage") == "B"):
                        self._active_results.pop(run_id, None)
                self._errors.pop(session_id, None)
                self._execution_failures.pop(session_id, None)
            selected = next(row for row in saved if "workflow_ref_id" in row)
            updated_session = next(
                row for row in saved
                if row.get("workflow_session_id") == session_id
                and "definition_revision" in row and "revision" in row
                and "source" in row
            )
            return {
                "workflow_session_id": session_id,
                "candidate_id": candidate_id,
                "head_commit_id": selected["head_commit_id"],
                "ref_revision": selected["revision"],
                "session_revision": updated_session["revision"],
                "status": "succeeded",
            }

    def select_inherited_candidate(
        self, session_id: str, candidate_id: str, *, idempotency_key: str,
        expected_session_revision: int, expected_ref_revision: int,
        expected_head_commit_id: str,
    ) -> dict[str, Any]:
        """Select a frozen terminal reply through a child-local version commit."""
        if type(expected_session_revision) is not int or expected_session_revision < 1:
            raise ContractValidationError("Expected session revision must be positive")
        if type(expected_head_commit_id) is not str:
            raise ContractValidationError("Expected Head commit is required")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            session = self._session(store, session_id)
            source = session["source"]
            if source["kind"] != "fork":
                raise ContractValidationError("Session has no inherited candidate floor")
            anchor = store.get_record("fork_anchor", {
                "fork_anchor_id": source["fork_anchor_id"],
            })
            floors = anchor.get("candidate_floors", [])
            if (anchor["role"] != "assistant" or not floors
                    or candidate_id not in floors[-1]["candidate_ids"]):
                raise ContractValidationError("Candidate is not in the frozen fork floor")
            candidate = store.get_record("workflow_candidate", {
                "candidate_id": candidate_id,
            })
            if candidate["workflow_session_id"] == session_id:
                raise ContractValidationError("Inherited candidate belongs to the child")
            ref = self._workflow_ref(store, session_id)
            operation = self._operation(
                "select_candidate", session_id, "candidate", candidate_id,
                idempotency_key, [{
                    "kind": "workflow_ref", "id": ref["workflow_ref_id"],
                    "revision": expected_ref_revision,
                }], {
                    "expected_session_revision": expected_session_revision,
                    "inherited_candidate_id": candidate_id,
                }, expected_head_commit_id=expected_head_commit_id,
            )
            request = {field: value for field, value in operation.items()
                       if field != "operation_id"}
            request_digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
            receipt_key = session_id + ":" + idempotency_key
            receipt = store.read_receipt_with_digest(
                "workflow.explicit_select_candidate", receipt_key,
            )
            if receipt is not None:
                if receipt[0] != request_digest:
                    raise ContractValidationError(
                        "Idempotency key has different workflow operation request"
                    )
                saved = receipt[1]
            else:
                if session["revision"] != expected_session_revision:
                    raise ContractValidationError("Workflow session revision conflict")
                if (ref["revision"] != expected_ref_revision
                        or ref["head_commit_id"] != expected_head_commit_id):
                    raise ContractValidationError("Workflow ref head conflict")
                self._assert_runtime_forkable(session_id, store)
                refs = self._formal_refs(store, session_id)
                if (len(refs) < 2 or refs[-2]["role"] != "user"
                        or refs[-1]["role"] != "assistant"
                        or refs[-2]["visible_message_id"] != floors[-1]["user_visible_message_id"]):
                    raise ContractValidationError(
                        "Inherited candidate floor is not the terminal formal reply"
                    )
                current_commit = store.get_record("workflow_commit", {
                    "commit_id": ref["head_commit_id"],
                })
                current_state = store.get_record("state_snapshot", {
                    "state_snapshot_id": current_commit["state_snapshot_id"],
                })
                current_floor = self._reply_candidate_floors(
                    store, session_id, refs, store.read_bundle(),
                    require_complete=True,
                )[-1]
                if current_floor["selected_candidate_id"] == candidate_id:
                    raise ContractValidationError("Candidate is already the current Head")
                target_commit = store.get_record("workflow_commit", {
                    "commit_id": candidate["result_commit_id"],
                })
                target_state = store.get_record("state_snapshot", {
                    "state_snapshot_id": target_commit["state_snapshot_id"],
                })
                target_refs = target_state["visible_message_refs"]
                if (len(target_refs) < 2
                        or target_refs[-2]["visible_message_id"] != refs[-2]["visible_message_id"]
                        or target_refs[-1]["role"] != "assistant"
                        or target_refs[-1]["boundary"] != {
                            "chain_run_id": candidate["chain_run_id"],
                            "output_id": candidate["output_id"],
                            "after_checkpoint_id": candidate["checkpoint_id"],
                        }):
                    raise ContractValidationError("Candidate differs from its frozen reply floor")
                registry = [
                    row for row in store.list_records("visible_message_ref")
                    if row["workflow_session_id"] == session_id
                ]
                target_ref = next(
                    (row for row in registry
                     if row["visible_message_id"] == target_refs[-1]["visible_message_id"]),
                    None,
                )
                new_ref = None
                if target_ref is None:
                    new_ref = _record(
                        workflow_session_id=session_id,
                        visible_message_id=target_refs[-1]["visible_message_id"],
                        sequence=max(row["sequence"] for row in registry) + 1,
                        role="assistant",
                        boundary=copy.deepcopy(target_refs[-1]["boundary"]),
                    )
                    target_ref = new_ref
                elif target_ref["boundary"] != target_refs[-1]["boundary"]:
                    raise ContractValidationError("Inherited candidate boundary changed")
                previous_ids = [
                    row["visible_message_id"]
                    for row in current_state["visible_message_refs"][:-1]
                ]
                by_id = {row["visible_message_id"]: row for row in registry}
                prefix = [by_id[message_id] for message_id in previous_ids]
                target_nodes = {
                    row["node_binding_id"]: row for row in target_state["node_states"]
                }
                node_updates = []
                for node in self._node_states(store, session_id):
                    frozen = target_nodes[node["node_binding_id"]]
                    node["data_version"] += 1
                    node["private_data"] = copy.deepcopy(frozen["private_data"])
                    node_updates.append(node)
                snapshot = self._state_snapshot(
                    session_id, node_updates, target_state["selection_refs"],
                    [*prefix, target_ref],
                )
                commit = _record(
                    commit_id=_uid(), workflow_session_id=session_id,
                    state_snapshot_id=snapshot["state_snapshot_id"],
                    parent_commit_id=ref["head_commit_id"],
                    operation_id=operation["operation_id"],
                    source={"kind": "control_operation",
                            "operation_id": operation["operation_id"]},
                )
                selected = copy.deepcopy(ref)
                selected["head_commit_id"] = commit["commit_id"]
                selected["revision"] += 1
                session["revision"] += 1
                records = [
                    ("workflow_session", session), ("workflow_ref", selected),
                    *[("node_session", row) for row in node_updates],
                    *([("visible_message_ref", new_ref)] if new_ref is not None else []),
                    ("workflow_operation", operation),
                    ("state_snapshot", snapshot), ("workflow_commit", commit),
                ]
                saved = store.save_bundle_prepared(
                    lambda _: records, receipt_key,
                    operation="workflow.explicit_select_candidate",
                    expected_session_revisions={
                        session_id: expected_session_revision,
                    },
                    expected_ref_heads={
                        ref["workflow_ref_id"]:
                        (expected_ref_revision, expected_head_commit_id),
                    },
                    request_digest=request_digest, after_save=self._bind_saved_variables,
                )
            selected = next(row for row in saved if "workflow_ref_id" in row)
            updated_session = next(
                row for row in saved
                if row.get("workflow_session_id") == session_id
                and "definition_revision" in row and "revision" in row
                and "source" in row
            )
            return {
                "workflow_session_id": session_id,
                "candidate_id": candidate_id,
                "head_commit_id": selected["head_commit_id"],
                "ref_revision": selected["revision"],
                "session_revision": updated_session["revision"],
                "status": "succeeded",
            }

    def _select_workflow_candidate(
        self, store, sid, candidate, *, selection_operation=None,
        receipt_key=None, request_digest=None,
        expected_session_revision=None, expected_ref_revision=None,
        expected_head_commit_id=None, sibling=False,
    ):
        chain = store.get_record("chain_run", {"chain_run_id": candidate["chain_run_id"]})
        origin = next((row for row in store.list_records("chain_input_origin")
                       if row["chain_run_id"] == chain["chain_run_id"]), None)
        ref = self._workflow_ref(store, sid)
        if origin is None:
            source_head = chain["base_commit_id"]
            source_revision = chain["base_ref_revision"]
        else:
            reroll_operation = store.get_record("workflow_operation", {
                "operation_id": chain["operation_id"],
            })
            source_head = reroll_operation["expected_head_commit_id"]
            source_revision = next(
                item["revision"] for item in reroll_operation["expected_revisions"]
                if item["kind"] == "workflow_ref"
            )
        if not sibling and (ref["revision"] != source_revision
                            or ref["head_commit_id"] != source_head):
            raise ContractValidationError("Workflow ref head conflict")
        selected = copy.deepcopy(ref)
        selected["head_commit_id"] = candidate["result_commit_id"]
        selected["revision"] += 1
        commit = store.get_record("workflow_commit", {
            "commit_id": candidate["result_commit_id"],
        })
        snapshot = store.get_record("state_snapshot", {
            "state_snapshot_id": commit["state_snapshot_id"],
        })
        current_refs = {row["visible_message_id"]: row
                        for row in store.list_records("visible_message_ref")
                        if row["workflow_session_id"] == sid}
        final_refs = snapshot["visible_message_refs"]
        projected_refs = []
        for item in final_refs:
            current = current_refs.get(item["visible_message_id"])
            if current is None or current["boundary"] != item["boundary"]:
                projected = copy.deepcopy(current) if current is not None else _record(
                    workflow_session_id=sid, visible_message_id=item["visible_message_id"],
                    role=item["role"], sequence=item["sequence"],
                    boundary=copy.deepcopy(item["boundary"]),
                )
                projected["boundary"] = copy.deepcopy(item["boundary"])
                projected_refs.append(projected)
        group_ids = {row["candidate_group_id"] for row in store.list_records("candidate_group")
                     if row["workflow_session_id"] == sid}
        selections = [
            _record(candidate_group_id=row["candidate_group_id"],
                    selected_turn_id=row["selected_turn_id"], revision=1)
            for row in snapshot["selection_refs"]
            if row["candidate_group_id"] in group_ids
            and not any(
                previous["candidate_group_id"] == row["candidate_group_id"]
                for previous in store.list_records("candidate_selection")
            )
        ]
        deliveries = [
            row for row in store.list_records("output_delivery")
            if row["output_id"] == candidate["output_id"]
            and row["target"] == {"kind": "ui", "workflow_session_id": sid}
        ]
        if sibling and deliveries:
            if len(deliveries) != 1 or deliveries[0]["status"] != "succeeded":
                raise ContractValidationError("Candidate has an unsettled output delivery")
            delivery = None
        elif origin is None:
            if len(deliveries) != 1 or deliveries[0]["status"] != "pending":
                raise ContractValidationError("Candidate has no pending output delivery")
            delivery = deliveries[0]
            delivery["status"] = "succeeded"
        else:
            if deliveries:
                raise ContractValidationError("Reroll candidate already has an output delivery")
            delivery = _record(
                delivery_id=_uid(), output_id=candidate["output_id"],
                target={"kind": "ui", "workflow_session_id": sid},
                status="succeeded", idempotency_key="ui:" + chain["chain_run_id"],
            )
        projected_nodes = []
        if origin is not None or sibling:
            for frozen in snapshot["node_states"]:
                current = store.get_record("node_session", {
                    "workflow_session_id": sid,
                    "node_binding_id": frozen["node_binding_id"],
                })
                if not sibling and frozen["data_version"] != current["data_version"] + 1:
                    raise ContractValidationError("Reroll node state changed before selection")
                current["data_version"] += 1
                current["private_data"] = copy.deepcopy(frozen["private_data"])
                projected_nodes.append(current)
        session = self._session(store, sid)
        old_revision = session["revision"]
        session["revision"] += 1
        explicit = selection_operation is not None
        records = [
            ("workflow_session", session), ("workflow_ref", selected),
            *[("candidate_selection", row) for row in selections],
            *[("visible_message_ref", row) for row in projected_refs],
            *[("node_session", row) for row in projected_nodes],
            *([("output_delivery", delivery)] if delivery is not None else []),
            *([("workflow_operation", selection_operation)] if explicit else []),
        ]
        options = {
            "operation": ("workflow.explicit_select_candidate" if explicit
                          else "workflow.select_candidate"),
            "expected_session_revisions": {
                sid: expected_session_revision if explicit else old_revision,
            },
            "expected_ref_heads": {ref["workflow_ref_id"]:
                                   ((expected_ref_revision, expected_head_commit_id) if explicit
                                    else (source_revision, source_head))},
            "expected_selection_revisions": {
                row["candidate_group_id"]: 0 for row in selections
            },
            "request_digest": request_digest,
        }
        if explicit:
            saved = store.save_bundle_prepared(
                lambda _: records, receipt_key, **options,
                after_save=self._restore_program_selected_values,
            )
        else:
            saved = store.save_bundle(
                records, "select-candidate:" + chain["chain_run_id"], **options,
            )
        self._publish_delivery_event(store, candidate, "succeeded")
        return saved

    def wait_for_idle(self, session_id: str, timeout: float = 20) -> None:
        with self._lock:
            future = self._futures.get(session_id)
        if future is not None:
            future.result(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)
        with self._lock:
            self._callbacks_revoked = True
            for active in self._active_runs.values():
                view = active.snapshot()
                active.fence(expected_generation=view.generation, expected_revision=view.revision)
            self._active_runs.clear()
            self._active_results.clear()
            self._run_checkpoints.clear()
            self._finalizing_runs.clear()
            self._interrupting.clear()
            self._progress.clear()
            self._execution_failures.clear()
            self._fact_sequences.clear()
            for source in self._event_sources.values():
                source.close()
            self._event_sources.clear()
            self._event_declarations.clear()
            self._event_states.clear()
            self._event_generations.clear()
            self._event_once.clear()
            self._event_ephemeral_order.clear()
            self._event_previews.clear()
            self._event_execution_fixed.clear()
            self._event_terminal_order.clear()
        self._lease.close()
