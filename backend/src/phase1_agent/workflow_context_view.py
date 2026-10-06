"""Read-only selected archive projection and detached workbench preparation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID, uuid4

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import dumps_pretty, validate_json_value
from .contracts_v2 import validate_record
from .prepared_context import validate_frozen_preparation
from .prompt_assembly import PromptAssemblyLimits, message_content_chars
from .prompt_config import validate_prompt_record
from .prompt_preparation import make_prompt_context_config, prepare_prompt_context
from .prompt_values import collection_from_config
from .prompt_errors import PromptProcessingError
from .workflow_result_ports import ResultPortStore


_REVISION_FIELDS = {
    "expected_session_revision", "expected_ref_revision", "expected_head_commit_id",
}
_MAX_REVISION = 2**63 - 1


def context_error(
    message: str, *, status: int = 400, reason: str = "invalid_request",
) -> ContractValidationError:
    error = ContractValidationError(message)
    error.status_code, error.reason_code = status, reason
    return error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def validate_context_request(value: Any, *, preview: bool = False) -> dict[str, Any]:
    """Accept only explicit revision fences, never a client-selected turn."""
    expected = _REVISION_FIELDS | ({"prompt_config", "text"} if preview else set())
    try:
        validate_json_value(value)
        valid = (
            type(value) is dict and set(value) == expected
            and all(type(value[field]) is int and 1 <= value[field] <= _MAX_REVISION
                    for field in ("expected_session_revision", "expected_ref_revision"))
            and _uuid(value["expected_head_commit_id"])
        )
        if not valid:
            raise context_error("Context read requires an exact revision fence")
        if preview:
            if not (value["text"] is None or type(value["text"]) is str):
                raise context_error("Preview text must be text or null")
            materialize_prompt_draft(value["prompt_config"])
    except ContractValidationError as exc:
        if isinstance(exc, PromptProcessingError):
            raise
        if getattr(exc, "reason_code", None) is not None:
            raise
        raise context_error("Invalid context request") from exc
    return copy.deepcopy(value)


def validate_context_identity(session_id: Any, binding: Any) -> None:
    if not _uuid(session_id) or not _uuid(binding):
        raise context_error("Context scope must use canonical UUID4 identities")


def materialize_prompt_draft(value: Any) -> dict[str, Any]:
    """Resolve exact inline v1 definitions, without reading or saving a catalog."""
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {"items", "groups", "config"},
             "Prompt draft fields must be exact")
    definitions: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for kind in ("item", "group"):
        values = value[kind + "s"]
        _require(type(values) is list and len(values) <= 512, "Too many inline definitions")
        field = kind + "_id"
        definitions[kind] = {}
        for record in values:
            record = validate_prompt_record(kind, record)
            identity = record[field], record["revision"]
            _require(identity not in definitions[kind], "Duplicate inline exact definition")
            definitions[kind][identity] = record
    config = validate_prompt_record("config", value["config"])

    def resolve(kind: str, identity: str, revision: int) -> dict[str, Any]:
        result = definitions[kind].get((identity, revision))
        _require(result is not None, "Missing inline exact definition")
        return copy.deepcopy(result)

    collection = collection_from_config(
        config, lambda identity, revision: resolve("item", identity, revision),
        lambda identity, revision: resolve("group", identity, revision),
    )
    return make_prompt_context_config(collection, prompt_config=value,
                                      preparation=config.get("preparation"))


@dataclass(frozen=True)
class ArchivedContext:
    messages: list[dict[str, Any]]
    logical_floors: list[list[str]]
    protected_blocks: list[dict[str, Any]]
    turns: list[dict[str, Any]]


class SelectedArchiveReader:
    """Project each selected root/delta exactly once using its frozen Context."""

    def __init__(
        self, store: Any, snapshot_components: Callable[..., Any],
        *, allowed_bindings: tuple[str, str],
    ) -> None:
        self._store = store
        self._snapshot_components = snapshot_components
        self._bindings = allowed_bindings

    def _record(self, kind: str, **identity: Any) -> dict[str, Any]:
        value = self._store.get_record(kind, identity)
        # Frozen component resolution and project_turn validate these large records.
        return value if kind in ("input_snapshot", "node_input", "turn") else validate_record(kind, value)

    def _ancestors(self, session_id: str) -> set[str]:
        found: set[str] = set()
        definition = None
        while True:
            _require(session_id not in found, "Workflow session ancestry is cyclic")
            found.add(session_id)
            session = self._record("workflow_session", workflow_session_id=session_id)
            current = session["workflow_definition_id"], session["definition_revision"]
            if definition is None:
                definition = current
            _require(current == definition, "Inherited session has another workflow definition")
            if session["source"]["kind"] == "new":
                return found
            session_id = session["source"]["source_workflow_session_id"]

    def read(
        self, session_id: str, binding: str, parent: str | None, *,
        protect_protocol: bool = False, include_ports: bool = False,
        capacity: PromptAssemblyLimits | None = None,
    ) -> ArchivedContext:
        owners = self._ancestors(session_id)
        turns: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = parent
        while cursor is not None:
            _require(cursor not in seen, "Selected turn chain is cyclic")
            if capacity is not None and len(seen) >= capacity.max_messages:
                raise context_error("Context exceeds its turn limit", reason="context_limit")
            seen.add(cursor)
            turn = self._record("turn", turn_id=cursor)
            turns.append(turn)
            cursor = turn["parent_turn_id"]

        messages, floors, protected, summaries = [], [], [], []
        total_chars = 0
        for turn in reversed(turns):
            node_input = self._record("node_input", input_id=turn["input_id"])
            snapshot = self._record("input_snapshot", snapshot_id=turn["snapshot_id"])
            run = self._record("run_record", run_id=turn["run_id"])
            _require(
                run["workflow_session_id"] in owners and run["node_binding_id"] == binding
                and snapshot["workflow_session_id"] == run["workflow_session_id"]
                and snapshot["node_binding_id"] == binding
                and snapshot["input_id"] == node_input["input_id"] == turn["input_id"] == run["input_id"]
                and snapshot["snapshot_id"] == run["snapshot_id"] == turn["snapshot_id"]
                and snapshot["parent_turn_id"] == turn["parent_turn_id"]
                and snapshot["projection_version"] == turn["projection_version"]
                and run["status"] == "succeeded" and run["result_turn_id"] == turn["turn_id"],
                "Archived turn has another owner or frozen input",
            )
            chain = self._record("chain_run", chain_run_id=run["chain_run_id"])
            _require(
                chain["workflow_session_id"] == run["workflow_session_id"]
                and run["run_id"] in chain["node_run_ids"],
                "Archived run has another chain owner",
            )
            historical_context, _, _, _ = self._snapshot_components(snapshot, store=self._store)
            projected = historical_context.implementation.project_turn(
                copy.deepcopy(turn), copy.deepcopy(node_input), copy.deepcopy(snapshot),
            )
            validate_message_history(projected)
            validate_turn_final(turn, snapshot)
            _require(bool(projected) and projected[1:] == turn["messages"],
                     "Context changed the accepted historical message delta")
            _require(
                all(message["source"]["kind"] in (
                    "model", "tool", "protocol_feedback", "runtime_tool_observation",
                ) for message in projected[1:]),
                "Archived delta contains a non-generated source",
            )
            source = node_input["source"]
            expected_source = (
                {"kind": "human", "visible_message_id": source["visible_message_id"]}
                if source["kind"] == "visible_message"
                else {"kind": "upstream_node", "output_id": source["output_id"]}
            )
            preparation = validate_frozen_preparation(snapshot)
            canonical = preparation["canonical_messages"] if preparation is not None else snapshot["s0"]
            _require(
                projected[0]["role"] == "user" and projected[0]["source"] == expected_source
                and projected[0]["blocks"] == [{"kind": "text", "text": dumps_pretty(node_input["payload"])}]
                and projected[0] in canonical,
                "Context changed the frozen historical input source",
            )
            if capacity is not None:
                total_chars += message_content_chars(projected)
                if len(messages) + len(projected) > capacity.max_messages or total_chars > capacity.max_total_chars:
                    raise context_error("Context exceeds its explicit capacity", reason="context_limit")
            floors.extend([[projected[0]["message_id"]], [message["message_id"] for message in projected[1:]]])
            for message in projected:
                editable = (
                    all(block["kind"] == "text" for block in message["blocks"])
                    and (
                        message["role"] == "user" and message["source"]["kind"] in ("human", "upstream_node")
                        or message["role"] == "assistant" and message["source"]["kind"] == "model"
                    )
                    and message["message_id"] != turn["final"]["message_id"]
                )
                if message["message_id"] == turn["final"]["message_id"] or protect_protocol and not editable:
                    protected.extend(
                        {"message_id": message["message_id"], "block_index": index}
                        for index, block in enumerate(message["blocks"]) if block["kind"] == "text"
                    )
            messages.extend(copy.deepcopy(projected))
            release = (
                ResultPortStore(self._store, allowed_bindings=self._bindings).get_release(run["run_id"])
                if include_ports else None
            )
            summaries.append({
                "workflow_session_id": run["workflow_session_id"], "node_binding_id": binding,
                "turn_id": turn["turn_id"], "parent_turn_id": turn["parent_turn_id"],
                "run_id": run["run_id"], "input_id": turn["input_id"],
                "snapshot_id": turn["snapshot_id"], "chain_run_id": run["chain_run_id"],
                "root_message_id": projected[0]["message_id"],
                "message_ids": [message["message_id"] for message in projected[1:]],
                "final_message_id": turn["final"]["message_id"],
                "result_ports": None if release is None else {
                    name: {field: release["ports"][name][field] for field in ("route_id", "delivery_id")}
                    for name in ("final", "context_delta")
                },
            })
        validate_message_history(messages)
        return ArchivedContext(messages, floors, protected, summaries)

    def validate_selected_refs(
        self, context: ArchivedContext, selection_refs: list[dict[str, Any]],
    ) -> None:
        """The selected head, not current mutable candidate choices, owns this path."""
        selected = {}
        for reference in selection_refs:
            group = self._record("candidate_group", candidate_group_id=reference["candidate_group_id"])
            if group["node_binding_id"] not in self._bindings:
                continue
            turn_id = reference["selected_turn_id"]
            if turn_id is None:
                continue
            _require(turn_id not in selected, "Duplicate selected turn reference")
            selected[turn_id] = group
        for turn in context.turns:
            group = selected.get(turn["turn_id"])
            _require(
                group is not None
                and group["workflow_session_id"] == turn["workflow_session_id"]
                and group["node_binding_id"] == turn["node_binding_id"]
                and group["logical_input_id"] == turn["input_id"]
                and group["frozen_snapshot_id"] == turn["snapshot_id"]
                and group["parent_turn_id"] == turn["parent_turn_id"]
                and {"run_id": turn["run_id"], "turn_id": turn["turn_id"]} in group["candidate_refs"],
                "Selected head refs differ from its archived turn path",
            )

    def validate_inherited_boundary(
        self, context: ArchivedContext, session_id: str, binding: str,
    ) -> None:
        """Inherited reads stop at the frozen fork boundary, never today's source head."""
        inherited = [turn for turn in context.turns if turn["workflow_session_id"] != session_id]
        if not inherited:
            return
        session = self._record("workflow_session", workflow_session_id=session_id)
        source = session["source"]
        _require(source["kind"] == "fork", "A new session cannot inherit an archived path")
        anchor = self._record("fork_anchor", fork_anchor_id=source["fork_anchor_id"])
        _require(
            anchor["source_workflow_session_id"] == source["source_workflow_session_id"]
            and anchor["visible_message_id"] == source["visible_message_id"],
            "Inherited context has another fork anchor owner",
        )
        checkpoint_id = anchor["checkpoint_id"]
        if anchor["role"] == "assistant" and anchor.get("candidate_floors"):
            floor = anchor["candidate_floors"][-1]
            candidates = [
                self._record("workflow_candidate", candidate_id=identity)
                for identity in floor["candidate_ids"]
            ]
            matching = [candidate for candidate in candidates
                        if candidate["chain_run_id"] == inherited[-1]["chain_run_id"]]
            _require(len(matching) == 1, "Inherited context is outside its frozen candidate floor")
            checkpoint_id = matching[0]["checkpoint_id"]
        checkpoint = self._record("workflow_checkpoint", checkpoint_id=checkpoint_id)
        _require(checkpoint["workflow_session_id"] in self._ancestors(session_id),
                 "Inherited context checkpoint has another owner")
        nodes = [node for node in checkpoint["nodes"] if node["node_binding_id"] == binding]
        _require(len(nodes) == 1, "Inherited context checkpoint has no unique node")
        allowed: set[str] = set()
        cursor = nodes[0]["selected_turn_id"]
        while cursor is not None:
            _require(cursor not in allowed, "Inherited fork path is cyclic")
            _require(len(allowed) < PromptAssemblyLimits().max_messages, "Inherited fork path exceeds capacity")
            allowed.add(cursor)
            cursor = self._record("turn", turn_id=cursor)["parent_turn_id"]
        _require(
            all(turn["turn_id"] in allowed for turn in inherited),
            "Inherited context is beyond its frozen fork checkpoint",
        )


def preview_context(
    context: ArchivedContext, scope: dict[str, Any], config: dict[str, Any], *,
    text: str | None, stage: str,
) -> dict[str, Any]:
    result = {
        "schema_version": 1, "kind": "workflow_context_preview", "scope": copy.deepcopy(scope),
        "status": "pending", "reason_code": "upstream_input_pending", "input": None, "preparation": None,
    }
    if stage == "B":
        if text is not None:
            raise context_error("B preview cannot accept fabricated upstream text")
        return result
    if type(text) is not str or not text.strip():
        raise context_error("A preview requires a nonempty input")
    node_input = {
        "schema_version": 1, "input_id": str(uuid4()), "port_id": "request",
        "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
        "source": {"kind": "visible_message", "visible_message_id": str(uuid4())},
        "payload": {"text": text},
    }
    preparation = prepare_prompt_context(
        node_input, context.messages, workflow_session_id=scope["workflow_session_id"],
        node_binding_id=scope["node_binding_id"], parent_turn_id=scope["parent_turn_id"],
        logical_floors=context.logical_floors, protected_blocks=context.protected_blocks,
        config=config, root_message_id=str(uuid4()),
    )
    result.update(
        status="ready", reason_code=None,
        input={"kind": "ephemeral", "input_id": node_input["input_id"], "source": node_input["source"]},
        preparation=preparation,
    )
    return result
