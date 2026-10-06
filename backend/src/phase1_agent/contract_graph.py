"""Closed-bundle reference checks for offline contract fixtures.

This is not a database, command executor, or a replacement for transactional
checks when records are changed concurrently. External schema/component
implementations and in-memory request records are not resolved here.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict
from .contracts_v2 import CONTRACT_SCHEMAS, validate_record


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _validate_message_sequence(
    messages: list[dict[str, Any]], expected_pending: tuple[str, ...],
) -> None:
    pending: dict[str, str] = {}
    seen_messages: set[str] = set()
    seen_calls: set[str] = set()
    seen_executions: set[str] = set()
    for raw in messages:
        message = validate_record("agent_message", raw)
        identity = message["message_id"]
        _require(identity not in seen_messages, "Duplicate message identity")
        seen_messages.add(identity)
        if pending:
            _require(message["role"] == "tool", "Tool results must settle before another model message")
        for block in message["blocks"]:
            kind = block["kind"]
            if kind == "tool_call":
                call_id = block["tool_call_id"]
                _require(call_id not in seen_calls, "Duplicate tool call identity")
                seen_calls.add(call_id)
                pending[call_id] = block["tool_name"]
            elif kind == "tool_result":
                call_id = block["tool_call_id"]
                _require(call_id in pending, "Tool result has no pending call")
                _require(call_id == next(iter(pending)), "Tool results must retain call order")
                _require(block["status"] in ("success", "error"), "Unknown outcomes are not settled model results")
                if message["schema_version"] == 2:
                    _require(pending[call_id] != "final_answer",
                             "Final control call cannot receive a synthetic tool observation")
                execution_id = block["tool_execution_id"]
                _require(message["source"]["tool_execution_id"] == execution_id,
                         "Tool source and result execution differ")
                if execution_id is not None:
                    _require(execution_id not in seen_executions, "Duplicate tool execution identity")
                    seen_executions.add(execution_id)
                del pending[call_id]
    if expected_pending:
        _require(tuple(pending) == expected_pending,
                 "Pending call identities differ from the message sequence")
    else:
        _require(not pending, "Unsettled tools cannot enter a closed model history")


def validate_message_history(messages: list[dict[str, Any]]) -> None:
    """Check a closed model-visible sequence, never a pending execution queue."""
    _validate_message_sequence(messages, ())


def validate_pending_message_history(
    messages: list[dict[str, Any]], pending_call_ids: tuple[str, ...] | list[str],
) -> None:
    """Check a checkpoint prefix without inventing results for pending calls."""
    _require(type(pending_call_ids) in (tuple, list) and bool(pending_call_ids),
             "Pending call identities must be a nonempty ordered sequence")
    _require(all(type(identity) is str and bool(identity) for identity in pending_call_ids),
             "Pending call identities must be nonempty strings")
    _require(len(set(pending_call_ids)) == len(pending_call_ids),
             "Pending call identities cannot contain duplicates")
    _validate_message_sequence(messages, tuple(pending_call_ids))


def validate_turn_final(turn: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Validate final provenance and schema equally at import and archive time."""
    turn = validate_record("turn", turn)
    snapshot = validate_record("input_snapshot", snapshot)
    validate_message_history(turn["messages"])
    final_message = next((message for message in turn["messages"]
                          if message["message_id"] == turn["final"]["message_id"]), None)
    _require(final_message is not None and final_message["role"] == "assistant"
             and final_message["source"]["kind"] == "model",
             "Final must refer to a model-produced assistant message")
    last_assistant = next(message for message in reversed(turn["messages"]) if message["role"] == "assistant")
    _require(last_assistant["message_id"] == final_message["message_id"], "Final is not the last assistant response")
    final_calls = [block for block in final_message["blocks"]
                   if block["kind"] == "tool_call" and block["tool_name"] == "final_answer"]
    if "final_answer" in {tool["name"] for tool in snapshot["tool_definitions"]}:
        _require(len(final_calls) == 1, "Agent final_answer mode requires one final control call")
    if final_calls:
        _require(sum(item["kind"] == "tool_call" for item in final_message["blocks"]) == 1,
                 "Final cannot share a tool batch")
        call = final_calls[0]
        result = next(item for message in turn["messages"] for item in message["blocks"]
                      if item["kind"] == "tool_result" and item["tool_call_id"] == call["tool_call_id"])
        _require(result["status"] == "success", "Final control result must be successful")
        answer = call["parsed_arguments"].get("answer")
        _require(canonical_bytes(result["content"]) == canonical_bytes(answer),
                 "Final control result changed the answer")
        try:
            projection = loads_strict(result["model_visible_text"])
        except ContractValidationError:
            raise ContractValidationError("Final control projection must encode its answer") from None
        _require(canonical_bytes(projection) == canonical_bytes(answer),
                 "Final control projection changed the answer")
        values = [answer]
    else:
        values = []
        for block in final_message["blocks"]:
            if block["kind"] == "text":
                try:
                    values.append(loads_strict(block["text"]))
                except ContractValidationError:
                    pass
    _require(any(canonical_bytes(value) == canonical_bytes(turn["final"]["value"]) for value in values),
             "Final value does not match its source message")
    try:
        Draft202012Validator.check_schema(snapshot["output_schema"])
        Draft202012Validator(snapshot["output_schema"]).validate(turn["final"]["value"])
    except (SchemaError, ValidationError, Unresolvable, RecursionError):
        raise ContractValidationError("Final does not satisfy the frozen output schema") from None


class ContractGraph:
    """Detached, structurally checked records with explicit reference resolution."""

    def __init__(self, bundle: dict[str, list[dict[str, Any]]]):
        _require(type(bundle) is dict, "A contract bundle must be an object")
        self.records: dict[str, list[dict[str, Any]]] = {}
        for kind, values in bundle.items():
            _require(kind in CONTRACT_SCHEMAS and type(values) is list, "Unknown bundle collection")
            self.records[kind] = [validate_record(kind, value) for value in values]
        self._indexes: dict[tuple[str, tuple[str, ...]], dict[tuple[Any, ...], dict]] = {}

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return self.records.get(kind, [])

    def index(self, kind: str, *fields: str) -> dict[tuple[Any, ...], dict]:
        cache_key = kind, fields
        if cache_key not in self._indexes:
            result: dict[tuple[Any, ...], dict] = {}
            for row in self.rows(kind):
                identity = tuple(row[field] for field in fields)
                _require(identity not in result, f"Duplicate {kind} identity")
                result[identity] = row
            self._indexes[cache_key] = result
        return self._indexes[cache_key]

    def get(self, kind: str, **identity: Any) -> dict[str, Any]:
        result = self.index(kind, *identity).get(tuple(identity.values()))
        _require(result is not None, f"Missing {kind} reference")
        return result

    def session(self, identity: str) -> dict[str, Any]:
        return self.get("workflow_session", workflow_session_id=identity)

    def binding(self, session_id: str, binding_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        return self.get(
            "node_binding", workflow_definition_id=session["workflow_definition_id"],
            workflow_definition_revision=session["definition_revision"], node_binding_id=binding_id,
        )

    def turn_owner(self, turn_id: str) -> dict[str, Any]:
        turn = self.get("turn", turn_id=turn_id)
        return self.get("run_record", run_id=turn["run_id"])

    def execution(self, run_id: str) -> dict[str, Any]:
        agent = self.index("run_record", "run_id").get((run_id,))
        node = self.index("node_run", "run_id").get((run_id,))
        _require((agent is None) != (node is None), "Run must resolve to exactly one execution profile")
        return agent if agent is not None else node

    @staticmethod
    def _same_frozen_basis(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for key in (
            "parent_turn_id", "component_id", "component_version", "config", "s0",
            "tool_definitions", "model_parameters", "output_schema", "projection_version",
        ):
            if canonical_bytes(left[key]) != canonical_bytes(right[key]):
                return False
        return True

    def _ancestor_sessions(self, session_id: str) -> set[str]:
        found: set[str] = set()
        while True:
            _require(session_id not in found, "Workflow session ancestry is cyclic")
            found.add(session_id)
            source = self.session(session_id)["source"]
            if source["kind"] == "new":
                return found
            session_id = source["source_workflow_session_id"]

    def _check_parent(self, turn_id: str | None, session_id: str, binding_id: str) -> None:
        if turn_id is None:
            return
        owner = self.turn_owner(turn_id)
        _require(owner["workflow_session_id"] in self._ancestor_sessions(session_id),
                 "Turn belongs to an unrelated session")
        _require(owner["node_binding_id"] == binding_id, "Turn belongs to a different binding")
        if owner["workflow_session_id"] != session_id:
            source = self.session(session_id)["source"]
            ref = self.get("visible_message_ref", workflow_session_id=source["source_workflow_session_id"],
                           visible_message_id=source["visible_message_id"])
            boundary = ref["boundary"]
            checkpoint_id = boundary.get("before_checkpoint_id", boundary.get("after_checkpoint_id"))
            checkpoint = self.get("workflow_checkpoint", checkpoint_id=checkpoint_id)
            selected = next((node["selected_turn_id"] for node in checkpoint["nodes"]
                             if node["node_binding_id"] == binding_id), None)
            allowed: set[str] = set()
            heads = [selected]
            if source["kind"] == "fork":
                anchor = self.get("fork_anchor", fork_anchor_id=source["fork_anchor_id"])
                if anchor["role"] == "assistant" and anchor.get("candidate_floors"):
                    for candidate_id in anchor["candidate_floors"][-1]["candidate_ids"]:
                        candidate = self.get("workflow_candidate", candidate_id=candidate_id)
                        candidate_checkpoint = self.get(
                            "workflow_checkpoint", checkpoint_id=candidate["checkpoint_id"],
                        )
                        heads.append(next(
                            (node["selected_turn_id"] for node in candidate_checkpoint["nodes"]
                             if node["node_binding_id"] == binding_id), None,
                        ))
            for head in heads:
                path: set[str] = set()
                cursor = head
                while cursor is not None:
                    _require(cursor not in path, "Turn parent chain is cyclic")
                    path.add(cursor)
                    cursor = self.get("turn", turn_id=cursor)["parent_turn_id"]
                allowed.update(path)
            _require(turn_id in allowed, "Inherited turn is beyond the branch checkpoint")

    def _frozen_inherited_candidate(
        self, session_id: str, candidate_id: str,
    ) -> dict[str, Any] | None:
        session = self.session(session_id)
        source = session["source"]
        if source["kind"] != "fork":
            return None
        anchor = self.get("fork_anchor", fork_anchor_id=source["fork_anchor_id"])
        if (anchor["role"] != "assistant" or not anchor.get("candidate_floors")
                or candidate_id not in anchor["candidate_floors"][-1]["candidate_ids"]):
            return None
        candidate = self.get("workflow_candidate", candidate_id=candidate_id)
        _require(candidate["workflow_session_id"] in self._ancestor_sessions(session_id)
                 and candidate["workflow_session_id"] != session_id,
                 "Inherited candidate belongs to another branch")
        return candidate

    def validate(self) -> None:
        identities = {
            "node_definition": ("component_id", "component_version"),
            "workflow_definition_revision": ("workflow_definition_id", "revision"),
            "node_binding": ("workflow_definition_id", "workflow_definition_revision", "node_binding_id"),
            "workflow_session": ("workflow_session_id",),
            "node_session": ("workflow_session_id", "node_binding_id"),
            "node_input": ("input_id",), "input_snapshot": ("snapshot_id",),
            "run_record": ("run_id",), "turn": ("turn_id",),
            "node_run": ("run_id",), "fork_anchor": ("fork_anchor_id",),
            "candidate_group": ("candidate_group_id",), "candidate_selection": ("candidate_group_id",),
            "agent_message": ("message_id",), "workflow_checkpoint": ("checkpoint_id",),
            "chain_run": ("chain_run_id",), "chain_input_origin": ("chain_run_id",),
            "execution_closeout": ("closeout_id",),
            "execution_continuation": ("chain_run_id",),
            "workflow_output": ("output_id",),
            "output_delivery": ("delivery_id",),
            "visible_message": ("visible_message_id",),
            "visible_message_ref": ("workflow_session_id", "visible_message_id"),
            "control_command": ("command_id",), "run_event": ("event_id",),
            "state_snapshot": ("state_snapshot_id",),
            "workflow_commit": ("commit_id",),
            "workflow_ref": ("workflow_ref_id",),
            "session_selection": ("session_selection_id",),
            "workflow_candidate": ("candidate_id",),
            "workflow_operation": ("operation_id",),
        }
        for kind, fields in identities.items():
            self.index(kind, *fields)
        self._definitions()
        self._runs_and_turns()
        self._generic_runs()
        self._execution_recovery()
        self._candidates()
        self._outputs_and_checkpoints()
        self._visible_messages()
        self._forks_and_controls()
        self._input_ownership()
        self._state_versions()

    def _chain_input_origins(self) -> None:
        for origin in self.rows("chain_input_origin"):
            chain_id = origin["chain_run_id"]
            chain = self.get("chain_run", chain_run_id=chain_id)
            source = self.get("chain_run", chain_run_id=origin["source_chain_run_id"])
            session_id = origin["workflow_session_id"]
            inherited = source["workflow_session_id"] != session_id
            _require(chain_id != source["chain_run_id"]
                     and chain["workflow_session_id"] == session_id
                     and (not inherited or source["workflow_session_id"]
                          in self._ancestor_sessions(session_id))
                     and chain["input_id"] == origin["input_id"],
                     "Reroll origin changed chain ownership or input")
            _require(chain["input_id"] != source["input_id"],
                     "Reroll must create a new input identity")
            _require(chain["schema_version"] == 2
                     and self.get("workflow_operation", operation_id=chain["operation_id"])["kind"]
                     in ("reroll", "continue_workflow"),
                     "Chain input origin requires a versioned reroll or continuation")
            if source["schema_version"] == 2 and not inherited:
                _require(chain["base_commit_id"] == source["base_commit_id"]
                         and chain["base_ref_revision"] == source["base_ref_revision"],
                         "Reroll changed its frozen chain base")
            message = self.get("visible_message", visible_message_id=origin["visible_message_id"])
            ref = self.get("visible_message_ref", workflow_session_id=session_id,
                           visible_message_id=origin["visible_message_id"])
            _require(message["role"] == ref["role"] == "user",
                     "Reroll origin must be a user boundary")
            source_origin = self.index("chain_input_origin", "chain_run_id").get(
                (source["chain_run_id"],)
            )
            if inherited:
                session = self.session(session_id)
                _require(session["source"]["kind"] == "fork",
                         "Inherited reroll requires a fork")
                anchor = self.get(
                    "fork_anchor", fork_anchor_id=session["source"]["fork_anchor_id"],
                )
                frozen = next(
                    (row for row in self.rows("workflow_candidate")
                     if row["chain_run_id"] == source["chain_run_id"]
                     and anchor.get("candidate_floors")
                     and row["candidate_id"] in anchor["candidate_floors"][-1]["candidate_ids"]),
                    None,
                )
                base = self.get("workflow_commit", commit_id=chain["base_commit_id"])
                base_state = self.get(
                    "state_snapshot", state_snapshot_id=base["state_snapshot_id"],
                )
                visible = base_state["visible_message_refs"]
                _require(
                    anchor["role"] == "assistant" and frozen is not None
                    and visible[-2]["visible_message_id"] == origin["visible_message_id"]
                    and visible[-1]["boundary"] == {
                        "chain_run_id": source["chain_run_id"],
                        "output_id": frozen["output_id"],
                        "after_checkpoint_id": frozen["checkpoint_id"],
                    },
                    "Inherited reroll is not based on a frozen selected reply",
                )
            else:
                _require(
                    (source_origin is None
                     and ref["boundary"]["chain_run_id"] == source["chain_run_id"]
                     and ref["boundary"]["input_id"] == source["input_id"])
                    or (source_origin is not None
                        and source_origin["visible_message_id"] == origin["visible_message_id"]),
                    "Reroll source changed its user boundary",
                )
            current_input = self.get("node_input", input_id=origin["input_id"])
            source_input = self.get("node_input", input_id=source["input_id"])
            _require(current_input["source"] == {
                "kind": "visible_message", "visible_message_id": origin["visible_message_id"],
            } and all(canonical_bytes(current_input[field]) == canonical_bytes(source_input[field])
                      for field in ("source", "port_id", "payload_schema_ref", "payload")),
                     "Reroll changed its frozen user input")
            seen = {chain_id}
            cursor = source["chain_run_id"]
            while cursor in self.index("chain_input_origin", "chain_run_id"):
                _require(cursor not in seen, "Reroll origin chain is cyclic")
                seen.add(cursor)
                cursor = self.get("chain_input_origin", chain_run_id=cursor)["source_chain_run_id"]
            _require(cursor not in seen, "Reroll origin chain is cyclic")

    def _state_versions(self) -> None:
        def related(owner: str, session_id: str) -> bool:
            return owner in self._ancestor_sessions(session_id)

        for operation in self.rows("workflow_operation"):
            scope, target = operation["scope"], operation["target"]
            if scope["kind"] == "workflow_session":
                self.session(scope["id"])
            elif scope["kind"] == "session_selection":
                self.get("session_selection", session_selection_id=scope["id"])
            kind = target["kind"]
            if kind == "workflow_session":
                self.session(target["id"])
            elif kind == "run_record":
                run = self.get("run_record", run_id=target["id"])
                _require(run["workflow_session_id"] == scope["id"], "Operation run belongs elsewhere")
            elif kind == "chain_run":
                chain = self.get("chain_run", chain_run_id=target["id"])
                _require(
                    chain["workflow_session_id"] == scope["id"]
                    or (
                        operation["kind"] == "reroll"
                        and chain["workflow_session_id"] in self._ancestor_sessions(scope["id"])
                        and any(
                            origin["workflow_session_id"] == scope["id"]
                            and origin["source_chain_run_id"] == target["id"]
                            and self.get("chain_run", chain_run_id=origin["chain_run_id"])["operation_id"]
                            == operation["operation_id"]
                            for origin in self.rows("chain_input_origin")
                        )
                    ),
                    "Operation chain belongs elsewhere",
                )
            elif kind == "node_input":
                self.get("node_input", input_id=target["id"])
                _require(any(
                    ref["workflow_session_id"] == scope["id"]
                    and ref["role"] == "user"
                    and ref["boundary"]["input_id"] == target["id"]
                    for ref in self.rows("visible_message_ref")
                ), "Operation input belongs to another session")
            elif kind == "visible_message":
                self.get("visible_message_ref", workflow_session_id=scope["id"],
                         visible_message_id=target["id"])
            elif kind == "candidate":
                candidate = self.get("workflow_candidate", candidate_id=target["id"])
                _require(candidate["workflow_session_id"] == scope["id"]
                         or self._frozen_inherited_candidate(scope["id"], target["id"]) is not None,
                         "Operation candidate belongs elsewhere")
            if operation["kind"] == "create_and_switch_branch":
                child_id = operation["payload"].get("child_workflow_session_id")
                _require(type(child_id) is str, "Branch switch has no child session")
                child = self.session(child_id)
                _require(
                    child["source"]["kind"] == "fork"
                    and child["source"]["source_workflow_session_id"] == scope["id"]
                    and child["source"]["visible_message_id"] == target["id"],
                    "Branch switch target differs from its child",
                )
            for expectation in operation["expected_revisions"]:
                kind = expectation["kind"]
                if kind == "workflow_session":
                    self.session(expectation["id"])
                elif kind == "run_record":
                    run = self.get("run_record", run_id=expectation["id"])
                    _require(run["workflow_session_id"] == scope["id"],
                             "Operation run revision belongs elsewhere")
                elif kind == "workflow_ref":
                    ref = self.get("workflow_ref", workflow_ref_id=expectation["id"])
                    _require(ref["workflow_session_id"] == scope["id"],
                             "Operation head revision belongs elsewhere")
                elif kind == "session_selection":
                    selection = self.get(
                        "session_selection", session_selection_id=expectation["id"],
                    )
                    _require(
                        (operation["kind"] == "switch_session"
                         and selection["session_selection_id"] == scope["id"])
                        or operation["kind"] == "create_and_switch_branch",
                        "Operation selection revision belongs elsewhere",
                    )

        self._chain_input_origins()
        selections = self.rows("session_selection")
        _require(len(selections) <= 1, "Multiple active session selections")
        for selection in selections:
            self.session(selection["active_workflow_session_id"])
        for snapshot in self.rows("state_snapshot"):
            session_id = snapshot["workflow_session_id"]
            session = self.session(session_id)
            _require((snapshot["workflow_definition_id"], snapshot["definition_revision"]) ==
                     (session["workflow_definition_id"], session["definition_revision"]),
                     "State snapshot definition differs from its session")
            bindings: set[str] = set()
            for node in snapshot["node_states"]:
                binding_id = node["node_binding_id"]
                _require(binding_id not in bindings, "Duplicate state snapshot node")
                bindings.add(binding_id)
                binding = self.binding(session_id, binding_id)
                _require(node["private_data"]["owner_component_id"] == binding["component_id"],
                         "State snapshot private data owner differs")
                result = node["selected_result"]
                if result is None:
                    continue
                if result["kind"] == "agent_turn":
                    owner = self.turn_owner(result["turn_id"])
                else:
                    output = self.get("workflow_output", output_id=result["output_id"])
                    owner = self.execution(output["source"]["run_id"])
                _require(owner["node_binding_id"] == binding_id
                         and related(owner["workflow_session_id"], session_id)
                         and owner["status"] == "succeeded",
                         "State snapshot selected result belongs elsewhere")
            definition = self.get(
                "workflow_definition_revision",
                workflow_definition_id=session["workflow_definition_id"],
                revision=session["definition_revision"],
            )
            _require(bindings == set(definition["bindings"]),
                     "State snapshot must include every workflow binding")
            selected_groups: set[str] = set()
            for selection in snapshot["selection_refs"]:
                group_id = selection["candidate_group_id"]
                _require(group_id not in selected_groups, "Duplicate state snapshot selection")
                selected_groups.add(group_id)
                group = self.get("candidate_group", candidate_group_id=group_id)
                _require(related(group["workflow_session_id"], session_id)
                         and (selection["selected_turn_id"] is None or
                              selection["selected_turn_id"] in {
                                  ref["turn_id"] for ref in group["candidate_refs"]
                              }), "State snapshot selection is not an owned candidate")
            visible_ids: set[str] = set()
            sequences: set[int] = set()
            for ref in snapshot["visible_message_refs"]:
                message_id = ref["visible_message_id"]
                _require(message_id not in visible_ids and ref["sequence"] not in sequences,
                         "Duplicate state snapshot visible boundary")
                visible_ids.add(message_id)
                sequences.add(ref["sequence"])
                message = self.get("visible_message", visible_message_id=message_id)
                _require(message["role"] == ref["role"]
                         and related(message["origin_workflow_session_id"], session_id),
                         "State snapshot visible message belongs elsewhere")
                boundary = ref["boundary"]
                if ref["role"] == "user":
                    node_input = self.get("node_input", input_id=boundary["input_id"])
                    _require(node_input["source"] ==
                             {"kind": "visible_message", "visible_message_id": message_id},
                             "State snapshot user input differs")
                    _require((boundary["chain_run_id"] is None) ==
                             (boundary["input_status"] == "pending"),
                             "State snapshot input boundary has inconsistent execution")
                    checkpoint = self.get("workflow_checkpoint",
                                          checkpoint_id=boundary["before_checkpoint_id"])
                    if boundary["chain_run_id"] is not None:
                        chain = self.get("chain_run", chain_run_id=boundary["chain_run_id"])
                        _require(related(chain["workflow_session_id"], session_id),
                                 "State snapshot chain belongs elsewhere")
                        if boundary["input_status"] == "completed":
                            _require(self._has_completed_descendant(chain["chain_run_id"]),
                                     "State snapshot completed input has unfinished chain")
                else:
                    chain = self.get("chain_run", chain_run_id=boundary["chain_run_id"])
                    output = self.get("workflow_output", output_id=boundary["output_id"])
                    _require(output["chain_run_id"] == chain["chain_run_id"]
                             and related(chain["workflow_session_id"], session_id),
                             "State snapshot output belongs elsewhere")
                    if boundary["after_checkpoint_id"] is None:
                        continue
                    checkpoint = self.get("workflow_checkpoint",
                                          checkpoint_id=boundary["after_checkpoint_id"])
                _require(related(checkpoint["workflow_session_id"], session_id),
                         "State snapshot checkpoint belongs elsewhere")
            pending_id = snapshot["pending_input_id"]
            if pending_id is not None:
                self.get("node_input", input_id=pending_id)
                _require(any(
                    ref["role"] == "user" and ref["boundary"]["input_id"] == pending_id
                    and ref["boundary"]["input_status"] == "pending"
                    for ref in snapshot["visible_message_refs"]
                ), "State snapshot pending input has no pending boundary")

        for commit in self.rows("workflow_commit"):
            session_id = commit["workflow_session_id"]
            self.session(session_id)
            snapshot = self.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
            _require(snapshot["workflow_session_id"] == session_id,
                     "Commit snapshot belongs to another session")
            operation = self.get("workflow_operation", operation_id=commit["operation_id"])
            source = commit["source"]
            if source["kind"] == "session_seed":
                _require(source["workflow_session_id"] == session_id,
                         "Commit seed belongs to another session")
                _require(operation["scope"]["kind"] == "workflow_session"
                         and related(operation["scope"]["id"], session_id),
                         "Commit seed operation belongs elsewhere")
                session_source = self.session(session_id)["source"]
                _require(
                    (session_source["kind"] == "new" and commit["parent_commit_id"] is None)
                    or (session_source["kind"] == "fork" and commit["parent_commit_id"] is not None),
                    "Commit seed has an invalid parent",
                )
            else:
                _require(operation["scope"] == {"kind": "workflow_session", "id": session_id},
                         "Commit operation belongs to another session")
                _require(commit["parent_commit_id"] is not None,
                         "Non-seed commit requires a parent")
                if source["kind"] == "chain_run":
                    chain = self.get("chain_run", chain_run_id=source["chain_run_id"])
                    _require(chain["workflow_session_id"] == session_id
                             and chain["status"] == "succeeded",
                             "Commit chain is not completed in its session")
                elif source["kind"] == "candidate":
                    candidate = self.get("workflow_candidate", candidate_id=source["candidate_id"])
                    _require(candidate["workflow_session_id"] == session_id
                             and candidate["result_commit_id"] == commit["commit_id"],
                             "Commit candidate differs from its result")
                else:
                    _require(source["operation_id"] == commit["operation_id"],
                             "Commit control source differs from its operation")
                    inherited_id = operation["payload"].get("inherited_candidate_id")
                    if inherited_id is not None:
                        _require(operation["kind"] == "select_candidate"
                                 and operation["target"] == {
                                     "kind": "candidate", "id": inherited_id,
                                 }, "Inherited selection operation differs from its target")
                        candidate = self._frozen_inherited_candidate(session_id, inherited_id)
                        _require(candidate is not None,
                                 "Control commit selected a non-frozen inherited candidate")
                        previous = self.get(
                            "workflow_commit", commit_id=commit["parent_commit_id"],
                        )
                        previous_state = self.get(
                            "state_snapshot", state_snapshot_id=previous["state_snapshot_id"],
                        )
                        target_commit = self.get(
                            "workflow_commit", commit_id=candidate["result_commit_id"],
                        )
                        target_state = self.get(
                            "state_snapshot", state_snapshot_id=target_commit["state_snapshot_id"],
                        )
                        before, after = (previous_state["visible_message_refs"],
                                         snapshot["visible_message_refs"])
                        _require(
                            len(before) == len(after) and len(after) >= 2
                            and before[:-1] == after[:-1]
                            and after[-1]["role"] == "assistant"
                            and after[-1]["boundary"] == {
                                "chain_run_id": candidate["chain_run_id"],
                                "output_id": candidate["output_id"],
                                "after_checkpoint_id": candidate["checkpoint_id"],
                            }
                            and snapshot["selection_refs"] == target_state["selection_refs"],
                            "Inherited selection changed the child prefix or candidate state",
                        )
                        target_nodes = {
                            node["node_binding_id"]: node
                            for node in target_state["node_states"]
                        }
                        _require(
                            {node["node_binding_id"] for node in snapshot["node_states"]}
                            == set(target_nodes)
                            and all(
                                node["private_data"] == target_nodes[node["node_binding_id"]]["private_data"]
                                and node["selected_result"] == target_nodes[node["node_binding_id"]]["selected_result"]
                                for node in snapshot["node_states"]
                            ), "Inherited selection changed its frozen node results",
                        )
            seen: set[str] = {commit["commit_id"]}
            parent_id = commit["parent_commit_id"]
            descendant = commit
            while parent_id is not None:
                _require(parent_id not in seen, "Workflow commit parent chain is cyclic")
                seen.add(parent_id)
                parent = self.get("workflow_commit", commit_id=parent_id)
                _require(parent["workflow_session_id"] == descendant["workflow_session_id"]
                         or (descendant["source"]["kind"] == "session_seed"
                             and related(parent["workflow_session_id"],
                                         descendant["workflow_session_id"])),
                         "Workflow commit parent belongs to an unrelated branch")
                descendant = parent
                parent_id = parent["parent_commit_id"]

        seen_refs: set[str] = set()
        for ref in self.rows("workflow_ref"):
            session_id = ref["workflow_session_id"]
            self.session(session_id)
            _require(session_id not in seen_refs, "Workflow session has multiple heads")
            seen_refs.add(session_id)
            if ref["head_commit_id"] is not None:
                commit = self.get("workflow_commit", commit_id=ref["head_commit_id"])
                _require(commit["workflow_session_id"] == session_id,
                         "Workflow head points to another session")

        seen_chains: set[str] = set()
        for candidate in self.rows("workflow_candidate"):
            session_id = candidate["workflow_session_id"]
            chain_id = candidate["chain_run_id"]
            _require(chain_id not in seen_chains, "Chain has multiple workflow candidates")
            seen_chains.add(chain_id)
            chain = self.get("chain_run", chain_run_id=chain_id)
            _require(chain["workflow_session_id"] == session_id
                     and chain["status"] == "succeeded"
                     and chain["output_id"] == candidate["output_id"],
                     "Workflow candidate requires a completed chain and output")
            checkpoint = self.get("workflow_checkpoint", checkpoint_id=candidate["checkpoint_id"])
            _require(checkpoint["workflow_session_id"] == session_id
                     and checkpoint["kind"] == "completed_output"
                     and checkpoint["output_id"] == candidate["output_id"],
                     "Workflow candidate checkpoint differs from output")
            base = self.get("workflow_commit", commit_id=candidate["base_commit_id"])
            result = self.get("workflow_commit", commit_id=candidate["result_commit_id"])
            _require(base["workflow_session_id"] == session_id
                     and result["workflow_session_id"] == session_id
                     and result["parent_commit_id"] == base["commit_id"]
                     and result["source"] == {"kind": "candidate",
                                              "candidate_id": candidate["candidate_id"]},
                     "Workflow candidate result is not based on its commit")
            if chain["schema_version"] == 2:
                _require(chain["base_commit_id"] == base["commit_id"]
                         and chain["operation_id"] == result["operation_id"],
                         "Workflow candidate changed its chain base or operation")
            snapshot = self.get("state_snapshot", state_snapshot_id=result["state_snapshot_id"])
            _require(snapshot["selection_refs"] == checkpoint["selection_refs"],
                     "Workflow candidate selection differs from its checkpoint")
            visible = sorted(snapshot["visible_message_refs"], key=lambda row: row["sequence"])
            origin = self.index("chain_input_origin", "chain_run_id").get((chain_id,))
            user_id = origin["visible_message_id"] if origin is not None else None
            if user_id is None:
                users = [row for row in visible if row["role"] == "user"
                         and row["boundary"]["chain_run_id"] == chain_id]
                _require(len(users) == 1, "Workflow candidate has no unique user floor")
                user_id = users[0]["visible_message_id"]
            floor = next((index for index, row in enumerate(visible)
                          if row["role"] == "user"
                          and row["visible_message_id"] == user_id), None)
            _require(floor is not None, "Workflow candidate lost its user floor")
            next_user = next((index for index in range(floor + 1, len(visible))
                              if visible[index]["role"] == "user"), len(visible))
            replies = [row for row in visible[floor + 1:next_user]
                       if row["role"] == "assistant"]
            _require(len(replies) == 1
                     and replies[0]["boundary"] == {
                         "chain_run_id": chain_id,
                         "output_id": candidate["output_id"],
                         "after_checkpoint_id": candidate["checkpoint_id"],
                     },
                     "Workflow candidate must project one reply on its user floor")
            checkpoint_nodes = {node["node_binding_id"]: node for node in checkpoint["nodes"]}
            for node in snapshot["node_states"]:
                frozen = checkpoint_nodes[node["node_binding_id"]]
                selected = node["selected_result"]
                selected_turn_id = selected["turn_id"] if selected is not None and selected["kind"] == "agent_turn" else None
                _require(node["data_version"] == frozen["data_version"]
                         and node["private_data"] == frozen["private_data"]
                         and selected_turn_id == frozen["selected_turn_id"],
                         "Workflow candidate state differs from its checkpoint")

    def _definitions(self) -> None:
        for definition in self.rows("node_definition"):
            ports = [(port["direction"], port["port_id"]) for port in definition["ports"]]
            _require(len(ports) == len(set(ports)), "Duplicate component port")
        for binding in self.rows("node_binding"):
            self.get("node_definition", component_id=binding["component_id"],
                     component_version=binding["component_version"])
            definition = self.get("workflow_definition_revision", workflow_definition_id=binding["workflow_definition_id"],
                                  revision=binding["workflow_definition_revision"])
            _require(binding["node_binding_id"] in definition["bindings"], "Binding not declared by workflow")
            _require(binding["config"]["owner_component_id"] == binding["component_id"], "Configuration owner mismatch")
        for definition in self.rows("workflow_definition_revision"):
            bindings = {}
            for binding_id in definition["bindings"]:
                bindings[binding_id] = self.get(
                    "node_binding", workflow_definition_id=definition["workflow_definition_id"],
                    workflow_definition_revision=definition["revision"], node_binding_id=binding_id,
                )
            for edge in definition["edges"]:
                ports = []
                for side, direction in (("from", "output"), ("to", "input")):
                    binding = bindings.get(edge[f"{side}_node_binding_id"])
                    _require(binding is not None, "Edge leaves its workflow definition")
                    component = self.get("node_definition", component_id=binding["component_id"],
                                         component_version=binding["component_version"])
                    port = next((port for port in component["ports"] if port["direction"] == direction
                                 and port["port_id"] == edge[f"{side}_port_id"]), None)
                    _require(port is not None, "Edge refers to an unknown port")
                    ports.append(port)
                _require(ports[0]["schema_ref"] == ports[1]["schema_ref"], "Edge schemas require an explicit conversion")
        for session in self.rows("workflow_session"):
            self.get("workflow_definition_revision", workflow_definition_id=session["workflow_definition_id"],
                     revision=session["definition_revision"])
            self._ancestor_sessions(session["workflow_session_id"])
            if session["source"]["kind"] == "fork":
                source = session["source"]
                seed = self.get("fork_anchor", fork_anchor_id=source["fork_anchor_id"])
                _require(seed["source_workflow_session_id"] == source["source_workflow_session_id"]
                         and seed["visible_message_id"] == source["visible_message_id"], "Fork source differs from its seed")
                parent = self.session(source["source_workflow_session_id"])
                _require(session["workflow_definition_id"] == parent["workflow_definition_id"]
                         and session["definition_revision"] == parent["definition_revision"], "Fork changed workflow definition")
                anchor = self.get("visible_message_ref", workflow_session_id=parent["workflow_session_id"],
                                  visible_message_id=source["visible_message_id"])
                boundary = anchor["boundary"]
                _require(boundary.get("before_checkpoint_id", boundary.get("after_checkpoint_id")) is not None,
                         "Fork source has no stable checkpoint")
        for state in self.rows("node_session"):
            binding = self.binding(state["workflow_session_id"], state["node_binding_id"])
            _require(state["private_data"]["owner_component_id"] == binding["component_id"], "Node data owner mismatch")

    def _runs_and_turns(self) -> None:
        message_owners: dict[str, str] = {}
        call_owners: dict[str, str] = {}
        execution_owners: dict[str, str] = {}
        for snapshot in self.rows("input_snapshot"):
            binding = self.binding(snapshot["workflow_session_id"], snapshot["node_binding_id"])
            _require((snapshot["component_id"], snapshot["component_version"]) ==
                     (binding["component_id"], binding["component_version"]), "Frozen component differs from binding")
            _require(snapshot["config"]["owner_component_id"] == snapshot["component_id"], "Frozen config owner mismatch")
            self.get("node_input", input_id=snapshot["input_id"])
            self._check_parent(snapshot["parent_turn_id"], snapshot["workflow_session_id"], snapshot["node_binding_id"])
            validate_message_history(snapshot["s0"])
        for run in self.rows("run_record"):
            self.execution(run["run_id"])
            binding = self.binding(run["workflow_session_id"], run["node_binding_id"])
            component = self.get("node_definition", component_id=binding["component_id"],
                                 component_version=binding["component_version"])
            _require(component["kind"] == "agent", "Agent run requires an Agent binding")
            snapshot = self.get("input_snapshot", snapshot_id=run["snapshot_id"])
            for key in ("workflow_session_id", "node_binding_id", "input_id"):
                _require(run[key] == snapshot[key], f"Run and snapshot {key} differ")
            chain = self.get("chain_run", chain_run_id=run["chain_run_id"])
            _require(chain["workflow_session_id"] == run["workflow_session_id"] and run["run_id"] in chain["node_run_ids"],
                     "Run is not owned by its chain")
            if run["status"] == "succeeded":
                turn = self.get("turn", turn_id=run["result_turn_id"])
                _require(turn["run_id"] == run["run_id"], "Run closure points to another run result")
                _require(run["superseded_by_run_id"] is None, "Successful run cannot be superseded")
            else:
                _require(run["result_turn_id"] is None, "Unsuccessful run cannot own a successful turn")
            if run["status"] == "superseded":
                replacement = self.get("run_record", run_id=run["superseded_by_run_id"])
                _require(replacement["source_run_id"] == run["run_id"], "Replacement has a different origin")
            else:
                _require(run["superseded_by_run_id"] is None, "Unexpected replacement closure")
            if run["source_run_id"] is not None:
                source = self.get("run_record", run_id=run["source_run_id"])
                _require(source["run_id"] != run["run_id"], "Reroll cannot refer to itself")
                _require(source["workflow_session_id"] == run["workflow_session_id"]
                         and source["node_binding_id"] == run["node_binding_id"], "Reroll changed its ownership")
                _require(source["input_id"] != run["input_id"], "Reroll must create a new input identity")
                original = self.get("input_snapshot", snapshot_id=source["snapshot_id"])
                for key in ("parent_turn_id", "component_id", "component_version", "config", "tool_definitions",
                            "model_parameters", "output_schema", "projection_version"):
                    _require(snapshot[key] == original[key], f"Reroll changed frozen {key}")
                _require(canonical_bytes(snapshot["s0"]) == canonical_bytes(original["s0"]),
                         "Reroll changed frozen s0")
                new_input = self.get("node_input", input_id=run["input_id"])
                old_input = self.get("node_input", input_id=source["input_id"])
                for key in ("payload", "payload_schema_ref", "port_id", "source"):
                    _require(canonical_bytes(new_input[key]) == canonical_bytes(old_input[key]), "Reroll changed input content")
                seen_runs = {run["run_id"]}
                cursor = run["source_run_id"]
                while cursor is not None:
                    _require(cursor not in seen_runs, "Reroll origin chain is cyclic")
                    seen_runs.add(cursor)
                    cursor = self.get("run_record", run_id=cursor)["source_run_id"]
        for turn in self.rows("turn"):
            run = self.get("run_record", run_id=turn["run_id"])
            _require(run["status"] == "succeeded" and run["result_turn_id"] == turn["turn_id"], "Turn has no successful run closure")
            snapshot = self.get("input_snapshot", snapshot_id=turn["snapshot_id"])
            for key in ("snapshot_id", "input_id"):
                _require(turn[key] == run[key], "Turn and generating run differ")
            _require(turn["parent_turn_id"] == snapshot["parent_turn_id"], "Turn parent differs from frozen start")
            _require(turn["projection_version"] == snapshot["projection_version"], "Turn projection version changed")
            validate_message_history(turn["messages"])
            for message in turn["messages"]:
                _require(message["message_id"] not in message_owners, "New turns cannot reuse message identities")
                message_owners[message["message_id"]] = turn["turn_id"]
                for block in message["blocks"]:
                    if block["kind"] == "tool_call":
                        identity = block["tool_call_id"]
                        _require(identity not in call_owners, "New turns cannot reuse tool call identities")
                        call_owners[identity] = turn["turn_id"]
                    elif block["kind"] == "tool_result":
                        identity = block["tool_execution_id"]
                        if identity is not None:
                            _require(identity not in execution_owners, "New turns cannot reuse execution identities")
                            execution_owners[identity] = turn["turn_id"]
            validate_turn_final(turn, snapshot)
            seen = {turn["turn_id"]}
            parent = turn["parent_turn_id"]
            while parent is not None:
                _require(parent not in seen, "Turn parent chain is cyclic")
                seen.add(parent)
                parent = self.get("turn", turn_id=parent)["parent_turn_id"]

    def _generic_runs(self) -> None:
        for run in self.rows("node_run"):
            self.execution(run["run_id"])
            binding = self.binding(run["workflow_session_id"], run["node_binding_id"])
            component = self.get("node_definition", component_id=binding["component_id"],
                                 component_version=binding["component_version"])
            _require(component["kind"] != "agent", "Agent binding requires the Agent run profile")
            self.get("node_input", input_id=run["input_id"])
            _require(run["input_snapshot_id"] is None, "Generic private checkpoints are not implemented")
            chain = self.get("chain_run", chain_run_id=run["chain_run_id"])
            _require(chain["workflow_session_id"] == run["workflow_session_id"]
                     and run["run_id"] in chain["node_run_ids"], "Generic run is not owned by its chain")
            if run["status"] == "succeeded":
                output = self.get("workflow_output", output_id=run["result_output_id"])
                _require(output["source"]["run_id"] == run["run_id"], "Generic result belongs to another run")
            else:
                _require(run["result_output_id"] is None, "Unsuccessful node cannot have a successful output")
            if run["status"] == "superseded":
                replacement = self.execution(run["superseded_by_run_id"])
                _require(replacement["source_run_id"] == run["run_id"], "Generic replacement source differs")
            else:
                _require(run["superseded_by_run_id"] is None, "Unexpected generic replacement closure")
            if run["source_run_id"] is not None:
                source = self.execution(run["source_run_id"])
                _require(source["profile"] == "node" and source["run_id"] != run["run_id"]
                         and source["workflow_session_id"] == run["workflow_session_id"]
                         and source["node_binding_id"] == run["node_binding_id"], "Generic reroll changed ownership")
                _require(source["input_id"] != run["input_id"], "Generic reroll requires a new input identity")
                original = self.get("node_input", input_id=source["input_id"])
                rerolled = self.get("node_input", input_id=run["input_id"])
                for field in ("payload", "payload_schema_ref", "port_id", "source"):
                    _require(canonical_bytes(original[field]) == canonical_bytes(rerolled[field]),
                             "Generic reroll changed input content")
                seen = {run["run_id"]}
                cursor = run["source_run_id"]
                while cursor is not None:
                    _require(cursor not in seen, "Generic reroll origin chain is cyclic")
                    seen.add(cursor)
                    cursor = self.execution(cursor)["source_run_id"]

    def _has_completed_descendant(self, chain_id: str) -> bool:
        """A closed root floor can become complete through a later execution."""
        root = self.get("chain_run", chain_run_id=chain_id)
        if root["status"] == "succeeded":
            return True
        if root["status"] not in ("superseded", "closed"):
            return False
        seen = {chain_id}
        pending = [chain_id]
        while pending:
            source_id = pending.pop()
            for origin in self.rows("chain_input_origin"):
                if (origin["source_chain_run_id"] != source_id
                        or origin["workflow_session_id"] != root["workflow_session_id"]
                        or origin["chain_run_id"] in seen):
                    continue
                child_id = origin["chain_run_id"]
                seen.add(child_id)
                child = self.get("chain_run", chain_run_id=child_id)
                if child["status"] == "succeeded":
                    return True
                pending.append(child_id)
        return False

    def _execution_recovery(self) -> None:
        by_chain: dict[str, dict[str, Any]] = {}
        closed_runs: set[str] = set()
        for closeout in self.rows("execution_closeout"):
            chain_id = closeout["chain_run_id"]
            _require(chain_id not in by_chain, "Chain has multiple execution closeouts")
            by_chain[chain_id] = closeout
            chain = self.get("chain_run", chain_run_id=chain_id)
            _require(chain["status"] == "closed"
                     and chain["workflow_session_id"] == closeout["workflow_session_id"]
                     and chain["output_id"] is None,
                     "Execution closeout requires its closed unfinished chain")
            expected = [
                run_id for run_id in chain["node_run_ids"]
                if self.execution(run_id)["status"] != "succeeded"
            ]
            _require(closeout["run_ids"] == expected
                     and all(self.execution(run_id)["profile"] == "agent"
                             and self.execution(run_id)["status"] == "closed"
                             for run_id in expected),
                     "Execution closeout must retain every unsuccessful Agent run")
            _require([ref["run_id"] for ref in closeout["fact_refs"]] == expected,
                     "Execution closeout fact references differ from its runs")
            closed_runs.update(expected)
            controls = [
                row for row in self.rows("workflow_operation")
                if row["kind"] in ("close_execution", "reroll", "continue_workflow")
                and row["target"] == {"kind": "chain_run", "id": chain_id}
                and row["scope"] == {
                    "kind": "workflow_session", "id": chain["workflow_session_id"],
                }
                and row["payload"].get("closeout_id") == closeout["closeout_id"]
            ]
            _require(bool(controls), "Execution closeout has no explicit workflow operation")
            if closeout["reason"] == "paused_reroll":
                continuation = self.index("execution_continuation", "chain_run_id").get((chain_id,))
                effective = [
                    *(continuation["reused_run_ids"] if continuation is not None else []),
                    *chain["node_run_ids"],
                ]
                _require(any(row["kind"] == "reroll" for row in controls) and (
                    len(effective) == 2
                    and self.execution(effective[0])["status"] == "succeeded"
                    or len(effective) == 1 and continuation is not None
                    and not continuation["reused_run_ids"]
                ),
                         "Paused reroll closeout must preserve the old successful A")
        _require(
            {row["chain_run_id"] for row in self.rows("chain_run") if row["status"] == "closed"}
            == set(by_chain), "Closed chain has no unique explicit closeout",
        )
        _require(
            {row["run_id"] for row in self.rows("run_record") if row["status"] == "closed"}
            == closed_runs, "Closed Agent run has no execution closeout",
        )
        for continuation in self.rows("execution_continuation"):
            chain = self.get("chain_run", chain_run_id=continuation["chain_run_id"])
            source = self.get("chain_run", chain_run_id=continuation["source_chain_run_id"])
            closeout = self.get("execution_closeout", closeout_id=continuation["closeout_id"])
            session_id = continuation["workflow_session_id"]
            _require(source["status"] == "closed"
                     and chain["workflow_session_id"] == source["workflow_session_id"] == session_id
                     and source["chain_run_id"] != chain["chain_run_id"]
                     and closeout["chain_run_id"] == source["chain_run_id"],
                     "Continuation requires a closed source in the same session")
            _require(closeout["diagnostic"]["category"] != "contract",
                     "Program contract failure cannot become model continuation")
            _require(chain["schema_version"] == 2 and bool(chain["node_run_ids"]),
                     "Continuation requires a versioned prepared execution")
            operation = self.get("workflow_operation", operation_id=chain["operation_id"])
            _require(operation["kind"] == "continue_workflow"
                     and operation["target"] == {"kind": "chain_run", "id": source["chain_run_id"]},
                     "Continuation operation targets a different source")
            origin = self.get("chain_input_origin", chain_run_id=chain["chain_run_id"])
            _require(origin["source_chain_run_id"] == source["chain_run_id"],
                     "Continuation changed its user floor lineage")
            old = self.get("run_record", run_id=continuation["source_run_id"])
            new = self.get("run_record", run_id=chain["node_run_ids"][0])
            _require(old["run_id"] in closeout["run_ids"] and old["status"] == "closed"
                     and old["node_binding_id"] == new["node_binding_id"]
                     and new["source_run_id"] is None,
                     "Continuation cannot pretend to resume or replace a different node")
            evidence = continuation["evidence_message"]
            _require(evidence["source"] == {
                "kind": "runtime_execution_observation", "closeout_id": closeout["closeout_id"],
                "source_run_id": old["run_id"],
            }, "Continuation evidence has different execution provenance")
            _require(all(block["text"].strip() for block in evidence["blocks"]),
                     "Continuation evidence must explain its interruption")
            frozen = self.get("input_snapshot", snapshot_id=old["snapshot_id"])
            snapshot = self.get("input_snapshot", snapshot_id=new["snapshot_id"])
            _require(snapshot["s0"] == [*frozen["s0"], evidence],
                     "Continuation must append evidence to the trusted frozen start")
            _require(all(canonical_bytes(snapshot[field]) == canonical_bytes(frozen[field])
                         for field in (
                             "parent_turn_id", "component_id", "component_version", "config",
                             "tool_definitions", "model_parameters", "output_schema", "projection_version",
                         )), "Continuation changed its frozen node contract")
            new_input = self.get("node_input", input_id=new["input_id"])
            old_input = self.get("node_input", input_id=old["input_id"])
            _require(new["input_id"] != old["input_id"]
                     and all(canonical_bytes(new_input[field]) == canonical_bytes(old_input[field])
                             for field in ("payload", "payload_schema_ref", "port_id", "source")),
                     "Continuation changed its trusted node input")
            ancestors = {source["chain_run_id"]}
            cursor = source["chain_run_id"]
            while (cursor,) in self.index("chain_input_origin", "chain_run_id"):
                cursor = self.get("chain_input_origin", chain_run_id=cursor)["source_chain_run_id"]
                _require(cursor not in ancestors, "Continuation lineage is cyclic")
                ancestors.add(cursor)
            session = self.session(session_id)
            definition = self.get(
                "workflow_definition_revision", workflow_definition_id=session["workflow_definition_id"],
                revision=session["definition_revision"],
            )
            reused = [self.get("run_record", run_id=run_id)
                      for run_id in continuation["reused_run_ids"]]
            _require(all(run["workflow_session_id"] == session_id
                         and run["chain_run_id"] in ancestors
                         and run["status"] == "succeeded"
                         and run["node_binding_id"] == definition["bindings"][0]
                         for run in reused), "Continuation can reuse only its successful A lineage")
            if new["node_binding_id"] == definition["bindings"][0]:
                _require(not reused and new["input_id"] == chain["input_id"],
                         "A continuation cannot reuse or impersonate an old A")
            else:
                _require(new["node_binding_id"] == definition["bindings"][1] and len(reused) == 1,
                         "B continuation requires one real successful A")
                outputs = [row for row in self.rows("workflow_output")
                           if row["source"]["run_id"] == reused[0]["run_id"]]
                _require(len(outputs) == 1 and new_input["source"] == {
                    "kind": "upstream_output", "output_id": outputs[0]["output_id"],
                }, "B continuation input must reference its reused A output")
        evidence_by_id = {
            row["evidence_message"]["message_id"]: row["evidence_message"]
            for row in self.rows("execution_continuation")
        }
        _require(len(evidence_by_id) == len(self.rows("execution_continuation")),
                 "Continuation evidence message identity was reused")
        histories = [
            *(row["s0"] for row in self.rows("input_snapshot")),
            *(row["messages"] for row in self.rows("turn")),
            self.rows("agent_message"),
        ]
        for messages in histories:
            for message in messages:
                if message["source"]["kind"] == "runtime_execution_observation":
                    _require(evidence_by_id.get(message["message_id"]) == message,
                             "Execution observation has no immutable continuation provenance")

    def _candidates(self) -> None:
        memberships: set[str] = set()
        for group in self.rows("candidate_group"):
            snapshot = self.get("input_snapshot", snapshot_id=group["frozen_snapshot_id"])
            for key in ("workflow_session_id", "node_binding_id", "parent_turn_id"):
                _require(group[key] == snapshot[key], "Candidate group differs from its frozen basis")
            _require(group["logical_input_id"] == snapshot["input_id"], "Logical input differs from group basis")
            members = set()
            for ref in group["candidate_refs"]:
                turn = self.get("turn", turn_id=ref["turn_id"])
                run = self.get("run_record", run_id=ref["run_id"])
                member_snapshot = self.get("input_snapshot", snapshot_id=turn["snapshot_id"])
                _require(turn["run_id"] == run["run_id"], "Candidate run and turn disagree")
                _require(run["status"] == "succeeded", "Candidate member must be a successful run")
                _require(turn["parent_turn_id"] == group["parent_turn_id"], "Candidate has another parent")
                _require(run["workflow_session_id"] == group["workflow_session_id"]
                         and run["node_binding_id"] == group["node_binding_id"], "Candidate has another owner")
                frozen_snapshot = self.get("input_snapshot", snapshot_id=group["frozen_snapshot_id"])
                _require(self._same_frozen_basis(member_snapshot, frozen_snapshot),
                         "Candidate member changed its frozen basis")
                member_input = self.get("node_input", input_id=member_snapshot["input_id"])
                frozen_input = self.get("node_input", input_id=frozen_snapshot["input_id"])
                for field in ("payload", "payload_schema_ref", "port_id", "source"):
                    _require(canonical_bytes(member_input[field]) == canonical_bytes(frozen_input[field]),
                             "Candidate member changed input content")
                _require(turn["turn_id"] not in members, "Duplicate candidate member")
                _require(turn["turn_id"] not in memberships, "Turn belongs to multiple candidate groups")
                members.add(turn["turn_id"])
                memberships.add(turn["turn_id"])
        for selection in self.rows("candidate_selection"):
            group = self.get("candidate_group", candidate_group_id=selection["candidate_group_id"])
            _require(selection["selected_turn_id"] is None or selection["selected_turn_id"] in
                     {ref["turn_id"] for ref in group["candidate_refs"]}, "Selection is not a candidate member")
        _require(memberships == {turn["turn_id"] for turn in self.rows("turn")},
                 "Successful turn is missing from candidate groups")

    def _outputs_and_checkpoints(self) -> None:
        for chain in self.rows("chain_run"):
            self.session(chain["workflow_session_id"])
            self.get("node_input", input_id=chain["input_id"])
            if chain["status"] == "superseded":
                successors = [
                    origin for origin in self.rows("chain_input_origin")
                    if origin["source_chain_run_id"] == chain["chain_run_id"]
                    and origin["workflow_session_id"] == chain["workflow_session_id"]
                ]
                _require(len(chain["node_run_ids"]) == 1 and len(successors) == 1,
                         "Superseded chain needs one prepared replacement")
                source_run = self.get("run_record", run_id=chain["node_run_ids"][0])
                successor = self.get("chain_run", chain_run_id=successors[0]["chain_run_id"])
                replacement = self.get("run_record", run_id=successor["node_run_ids"][0])
                _require(source_run["status"] == "superseded"
                         and source_run["superseded_by_run_id"] == replacement["run_id"]
                         and replacement["source_run_id"] == source_run["run_id"]
                         and self.get("workflow_operation", operation_id=successor["operation_id"])[
                             "target"
                         ] == {"kind": "chain_run", "id": chain["chain_run_id"]},
                         "Superseded chain replacement differs from its origin")
            if chain["schema_version"] == 2:
                base = self.get("workflow_commit", commit_id=chain["base_commit_id"])
                operation = self.get("workflow_operation", operation_id=chain["operation_id"])
                _require(base["workflow_session_id"] == chain["workflow_session_id"]
                         and operation["scope"] == {
                             "kind": "workflow_session", "id": chain["workflow_session_id"],
                         } and operation["kind"] in (
                             "submit", "continue_pending_input", "reroll", "continue_workflow",
                         )
                         and operation["payload"].get("chain_run_id") == chain["chain_run_id"]
                         and operation["payload"].get("base_commit_id") == chain["base_commit_id"]
                         and operation["payload"].get("base_ref_revision") == chain["base_ref_revision"],
                         "Versioned chain changed its frozen base or operation")
                if operation["kind"] in ("reroll", "continue_workflow"):
                    self.get("chain_input_origin", chain_run_id=chain["chain_run_id"])
                if operation["kind"] == "continue_workflow":
                    self.get("execution_continuation", chain_run_id=chain["chain_run_id"])
            for run_id in chain["node_run_ids"]:
                run = self.execution(run_id)
                _require(run["chain_run_id"] == chain["chain_run_id"], "Chain contains another chain's run")
            if chain["output_id"] is not None:
                output = self.get("workflow_output", output_id=chain["output_id"])
                _require(output["chain_run_id"] == chain["chain_run_id"], "Chain output belongs elsewhere")
            if chain["status"] == "succeeded":
                _require(chain["output_id"] is not None, "Completed chain requires its output")
                _require(all(self.execution(identity)["status"] in ("succeeded", "superseded")
                             for identity in chain["node_run_ids"]), "Completed chain contains unfinished runs")
        for output in self.rows("workflow_output"):
            chain = self.get("chain_run", chain_run_id=output["chain_run_id"])
            _require(chain["workflow_session_id"] == output["workflow_session_id"], "Output has another session")
            source = output["source"]
            run = self.execution(source["run_id"])
            _require(run["chain_run_id"] == chain["chain_run_id"] and run["status"] == "succeeded",
                     "Output requires a successful source in its chain")
            _require(source["node_binding_id"] == run["node_binding_id"], "Output source binding differs")
            if run["profile"] == "agent":
                _require(source["turn_id"] == run["result_turn_id"], "Output source does not match its Agent run")
                turn = self.get("turn", turn_id=source["turn_id"])
                _require(canonical_bytes(output["payload"]) == canonical_bytes(turn["final"]["value"]),
                         "Direct Agent output changed final content")
            else:
                _require(source["turn_id"] is None and output["output_id"] == run["result_output_id"],
                         "Generic output must not require an Agent turn")
        for delivery in self.rows("output_delivery"):
            output = self.get("workflow_output", output_id=delivery["output_id"])
            if delivery["target"]["kind"] == "ui":
                _require(delivery["target"]["workflow_session_id"] == output["workflow_session_id"], "UI delivery session differs")
        for checkpoint in self.rows("workflow_checkpoint"):
            session = self.session(checkpoint["workflow_session_id"])
            _require(checkpoint["workflow_definition_id"] == session["workflow_definition_id"]
                     and checkpoint["definition_revision"] == session["definition_revision"], "Checkpoint definition changed")
            bindings = set()
            for node in checkpoint["nodes"]:
                _require(node["node_binding_id"] not in bindings, "Duplicate node in checkpoint")
                bindings.add(node["node_binding_id"])
                binding = self.binding(checkpoint["workflow_session_id"], node["node_binding_id"])
                _require(node["private_data"]["owner_component_id"] == binding["component_id"], "Checkpoint data owner mismatch")
                self._check_parent(node["selected_turn_id"], checkpoint["workflow_session_id"], node["node_binding_id"])
            for selection in checkpoint["selection_refs"]:
                group = self.get("candidate_group", candidate_group_id=selection["candidate_group_id"])
                _require(group["workflow_session_id"] in self._ancestor_sessions(session["workflow_session_id"]),
                         "Checkpoint selection belongs to another session")
                _require(selection["selected_turn_id"] is None or selection["selected_turn_id"] in
                         {ref["turn_id"] for ref in group["candidate_refs"]}, "Checkpoint selection is not a candidate")
            if checkpoint["kind"] == "completed_output":
                selection_by_group = {
                    selection["candidate_group_id"]: selection["selected_turn_id"]
                    for selection in checkpoint["selection_refs"]
                }
                _require(len(selection_by_group) == len(checkpoint["selection_refs"]),
                         "Duplicate checkpoint selection group")
                paths: dict[str, set[str]] = {}
                for node in checkpoint["nodes"]:
                    selected_turn = node["selected_turn_id"]
                    path: set[str] = set()
                    paths[node["node_binding_id"]] = path
                    while selected_turn is not None:
                        _require(selected_turn not in path, "Checkpoint turn path is cyclic")
                        path.add(selected_turn)
                        groups = [group for group in self.rows("candidate_group")
                                  if group["node_binding_id"] == node["node_binding_id"]
                                  and selected_turn in {ref["turn_id"] for ref in group["candidate_refs"]}]
                        _require(len(groups) == 1, "Checkpoint node selection has no unique candidate group")
                        _require(selection_by_group.get(groups[0]["candidate_group_id"]) == selected_turn,
                                 "Checkpoint node selection disagrees with selection refs")
                        selected_turn = self.get("turn", turn_id=selected_turn)["parent_turn_id"]
                for group_id, selected_turn in selection_by_group.items():
                    if selected_turn is None:
                        continue
                    group = self.get("candidate_group", candidate_group_id=group_id)
                    _require(selected_turn in paths.get(group["node_binding_id"], set()),
                             "Checkpoint selection ref is outside its selected node path")
            if checkpoint["kind"] == "before_input":
                self.get("node_input", input_id=checkpoint["input_id"])
            elif checkpoint["kind"] == "completed_output":
                output = self.get("workflow_output", output_id=checkpoint["output_id"])
                chain = self.get("chain_run", chain_run_id=output["chain_run_id"])
                _require(chain["workflow_session_id"] == checkpoint["workflow_session_id"]
                         and chain["status"] == "succeeded", "Completion checkpoint requires a completed workflow")
                source = output["source"]
                _require(source["node_binding_id"] in bindings, "Checkpoint is missing its output node")
                if source["turn_id"] is not None:
                    _require(source["turn_id"] in paths[source["node_binding_id"]],
                             "Checkpoint output is outside its selected node path")

    def _visible_messages(self) -> None:
        assistant_sources: set[tuple[str, str]] = set()
        for message in self.rows("visible_message"):
            self.session(message["origin_workflow_session_id"])
            source = message["source"]
            original = self.get("node_input", input_id=source["input_id"]) if message["role"] == "user" else \
                self.get("workflow_output", output_id=source["output_id"])
            _require(canonical_bytes(message["payload"]) == canonical_bytes(original["payload"])
                     and message["payload_schema_ref"] == original["payload_schema_ref"], "Visible message changed source content")
            if message["role"] == "assistant":
                _require(original["workflow_session_id"] == message["origin_workflow_session_id"], "Visible output origin differs")
                source_key = (message["origin_workflow_session_id"], source["output_id"])
                _require(source_key not in assistant_sources, "One workflow output cannot publish two assistant messages in a session")
                assistant_sources.add(source_key)
        sequences: set[tuple[str, int]] = set()
        for ref in self.rows("visible_message_ref"):
            session_id = ref["workflow_session_id"]
            sequence = session_id, ref["sequence"]
            _require(sequence not in sequences, "Duplicate visible sequence in session")
            sequences.add(sequence)
            message = self.get("visible_message", visible_message_id=ref["visible_message_id"])
            _require(ref["role"] == message["role"], "Visible message role changed")
            _require(message["origin_workflow_session_id"] in self._ancestor_sessions(session_id),
                     "Visible message belongs to an unrelated session")
            inherited = message["origin_workflow_session_id"] != session_id
            replayed_user = False
            if inherited:
                source = self.session(session_id)["source"]
                parent_ref = self.index(
                    "visible_message_ref", "workflow_session_id", "visible_message_id",
                ).get((source["source_workflow_session_id"], ref["visible_message_id"]))
                anchor_ref = self.get("visible_message_ref", workflow_session_id=source["source_workflow_session_id"],
                                      visible_message_id=source["visible_message_id"])
                alternate = None
                if ref["role"] == "assistant":
                    alternate = next(
                        (row for row in self.rows("workflow_candidate")
                         if row["output_id"] == ref["boundary"]["output_id"]
                         and self._frozen_inherited_candidate(session_id, row["candidate_id"]) is not None),
                        None,
                    )
                    if alternate is not None:
                        target_commit = self.get(
                            "workflow_commit", commit_id=alternate["result_commit_id"],
                        )
                        target_state = self.get(
                            "state_snapshot", state_snapshot_id=target_commit["state_snapshot_id"],
                        )
                        frozen_reply = target_state["visible_message_refs"][-1]
                        anchor = self.get("fork_anchor", fork_anchor_id=source["fork_anchor_id"])
                        user_id = anchor["candidate_floors"][-1]["user_visible_message_id"]
                        floor_user = next(
                            (row for row in self.rows("visible_message_ref")
                             if row["workflow_session_id"] == session_id
                             and row["role"] == "user"
                             and row["visible_message_id"] == user_id),
                            None,
                        )
                        _require(floor_user is not None,
                                 "Inherited reply has no frozen user floor")
                        _require(
                            frozen_reply["visible_message_id"] == ref["visible_message_id"]
                            and frozen_reply["boundary"] == ref["boundary"]
                            and ref["sequence"] > floor_user["sequence"],
                            "Inherited reply is not a frozen candidate at its floor",
                        )
                _require((parent_ref is not None
                          and parent_ref["sequence"] <= anchor_ref["sequence"])
                         or alternate is not None,
                         "Inherited message is beyond fork boundary")
                replayed_user = ref["role"] == "user" and ref["visible_message_id"] == anchor_ref["visible_message_id"]
                if replayed_user:
                    _require(parent_ref is not None, "Fork user has no parent boundary")
                    _require(ref["boundary"]["before_checkpoint_id"] == parent_ref["boundary"]["before_checkpoint_id"],
                             "Fork user must retain the exact pre-input checkpoint")
                    _require(ref["boundary"]["input_id"] != parent_ref["boundary"]["input_id"],
                             "Fork user needs an independent input identity")
                elif alternate is None:
                    _require(parent_ref is not None, "Inherited message has no parent boundary")
                    _require(ref["boundary"] == parent_ref["boundary"], "Inherited history cannot retarget execution")
            boundary = ref["boundary"]
            if ref["role"] == "user":
                node_input = self.get("node_input", input_id=boundary["input_id"])
                _require(node_input["source"] == {"kind": "visible_message", "visible_message_id": message["visible_message_id"]},
                         "User boundary input has another source")
                _require(canonical_bytes(node_input["payload"]) == canonical_bytes(message["payload"]), "User boundary content changed")
                checkpoint = self.get("workflow_checkpoint", checkpoint_id=boundary["before_checkpoint_id"])
                _require(checkpoint["kind"] in ("initial", "before_input"), "User anchor must precede processing")
                if checkpoint["kind"] == "before_input" and not inherited:
                    _require(checkpoint["input_id"] == boundary["input_id"], "User checkpoint belongs to a different input")
                if boundary["input_status"] == "pending":
                    _require(boundary["chain_run_id"] is None, "Pending input cannot inherit executed chain")
                else:
                    chain = self.get("chain_run", chain_run_id=boundary["chain_run_id"])
                    expected_sessions = self._ancestor_sessions(session_id) if inherited and not replayed_user else {session_id}
                    _require(chain["workflow_session_id"] in expected_sessions and chain["input_id"] == boundary["input_id"],
                             "User boundary chain differs")
                    if boundary["input_status"] == "completed":
                        _require(self._has_completed_descendant(chain["chain_run_id"]),
                                 "Completed input requires a successful chain")
            else:
                output = self.get("workflow_output", output_id=boundary["output_id"])
                _require(message["source"]["output_id"] == output["output_id"]
                         and output["chain_run_id"] == boundary["chain_run_id"], "Assistant boundary source differs")
                if boundary["after_checkpoint_id"] is None:
                    continue
                checkpoint = self.get("workflow_checkpoint", checkpoint_id=boundary["after_checkpoint_id"])
                _require(checkpoint["kind"] == "completed_output" and checkpoint["output_id"] == output["output_id"],
                         "Assistant anchor is not its completed workflow boundary")
            _require(checkpoint["workflow_session_id"] in self._ancestor_sessions(session_id), "Visible checkpoint belongs elsewhere")
        for item in self.rows("node_input"):
            if item["source"]["kind"] == "visible_message":
                message = self.get("visible_message", visible_message_id=item["source"]["visible_message_id"])
                _require(message["role"] == "user", "Node user input must refer to a UI user")
            elif item["source"]["kind"] == "upstream_output":
                output = self.get("workflow_output", output_id=item["source"]["output_id"])
                _require(canonical_bytes(item["payload"]) == canonical_bytes(output["payload"])
                         and item["payload_schema_ref"] == output["payload_schema_ref"],
                         "Direct upstream input changed output content")

    def _candidate_root_chain(self, candidate: dict[str, Any]) -> str:
        chain_id = candidate["chain_run_id"]
        origins = self.index("chain_input_origin", "chain_run_id")
        while (chain_id,) in origins:
            chain_id = origins[(chain_id,)]["source_chain_run_id"]
        return chain_id

    def _fork_candidate_floors(
        self, anchor: dict[str, Any], children: list[dict[str, Any]],
    ) -> None:
        source_id = anchor["source_workflow_session_id"]
        source_anchor = self.get(
            "visible_message_ref", workflow_session_id=source_id,
            visible_message_id=anchor["visible_message_id"],
        )
        source_ancestors = self._ancestor_sessions(source_id)
        candidate_order = self.rows("workflow_candidate")
        by_id = {row["candidate_id"]: row for row in candidate_order}
        seen_floors: set[str] = set()
        seen_candidates: set[str] = set()
        sequences: list[int] = []
        floors = anchor["candidate_floors"]
        for floor in floors:
            user_id = floor["user_visible_message_id"]
            _require(user_id not in seen_floors, "Duplicate fork candidate floor")
            seen_floors.add(user_id)
            user = self.get(
                "visible_message_ref", workflow_session_id=source_id,
                visible_message_id=user_id,
            )
            _require(user["role"] == "user"
                     and user["boundary"]["input_status"] == "completed"
                     and user["sequence"] < source_anchor["sequence"],
                     "Fork candidate floor is beyond its stable user boundary")
            sequences.append(user["sequence"])
            root_id = user["boundary"]["chain_run_id"]
            _require(root_id is not None, "Fork candidate floor has no original chain")
            source_session = self.session(source_id)
            inherited_ids: list[str] = []
            if source_session["source"]["kind"] == "fork":
                source_fork = self.get(
                    "fork_anchor", fork_anchor_id=source_session["source"]["fork_anchor_id"],
                )
                inherited_ids = next(
                    (row["candidate_ids"] for row in source_fork.get("candidate_floors", [])
                     if row["user_visible_message_id"] == user_id),
                    [],
                )
            if inherited_ids:
                members = [
                    *inherited_ids,
                    *(row["candidate_id"] for row in candidate_order
                      if row["workflow_session_id"] == source_id
                      and self._candidate_root_chain(row) == root_id),
                ]
            else:
                members = [
                    row["candidate_id"] for row in candidate_order
                    if self._candidate_root_chain(row) == root_id
                    and row["workflow_session_id"] in source_ancestors
                ]
            ids = floor["candidate_ids"]
            _require(ids and ids == members[:len(ids)],
                     "Fork candidate order is not an archived floor prefix")
            _require(floor["selected_candidate_id"] in ids,
                     "Fork selected candidate is not in its frozen floor")
            for candidate_id in ids:
                _require(candidate_id not in seen_candidates,
                         "Fork candidate is listed in multiple floors")
                seen_candidates.add(candidate_id)
                candidate = by_id[candidate_id]
                _require(candidate["workflow_session_id"] in source_ancestors
                         and self._candidate_root_chain(candidate) == root_id,
                         "Fork candidate belongs to another floor or session")
        _require(sequences == sorted(sequences),
                 "Fork candidate floors are not in visible order")

        for child in children:
            seeds = [
                row for row in self.rows("workflow_commit")
                if row["workflow_session_id"] == child["workflow_session_id"]
                and row["source"] == {
                    "kind": "session_seed",
                    "workflow_session_id": child["workflow_session_id"],
                }
            ]
            _require(len(seeds) == 1, "Fork candidate manifest has no child seed")
            snapshot = self.get(
                "state_snapshot", state_snapshot_id=seeds[0]["state_snapshot_id"],
            )
            refs = snapshot["visible_message_refs"]
            pairs = [
                (row, refs[index + 1])
                for index, row in enumerate(refs[:-1])
                if row["role"] == "user" and refs[index + 1]["role"] == "assistant"
            ]
            _require([row["visible_message_id"] for row, _ in pairs]
                     == [floor["user_visible_message_id"] for floor in floors],
                     "Fork candidate manifest differs from its child visible prefix")
            for floor, (_, assistant) in zip(floors, pairs):
                selected = by_id[floor["selected_candidate_id"]]
                _require(assistant["boundary"]["chain_run_id"] == selected["chain_run_id"]
                         and assistant["boundary"]["output_id"] == selected["output_id"]
                         and assistant["boundary"]["after_checkpoint_id"] == selected["checkpoint_id"],
                         "Fork selected candidate differs from its child formal reply")
            if anchor["role"] == "assistant":
                selected = by_id[floors[-1]["selected_candidate_id"]]
                _require(seeds[0]["parent_commit_id"] == selected["result_commit_id"],
                         "Fork seed parent differs from its selected candidate")
                target_checkpoint = self.get(
                    "workflow_checkpoint", checkpoint_id=selected["checkpoint_id"],
                )
                _require(snapshot["selection_refs"] == target_checkpoint["selection_refs"]
                         and all(
                             node["private_data"] == next(
                                 target["private_data"]
                                 for target in target_checkpoint["nodes"]
                                 if target["node_binding_id"] == node["node_binding_id"]
                             )
                             for node in snapshot["node_states"]
                         ), "Fork seed nodes differ from its selected candidate")

    def _forks_and_controls(self) -> None:
        pending_seeds: set[str] = set()
        for anchor in self.rows("fork_anchor"):
            session_id = anchor["source_workflow_session_id"]
            ref = self.get("visible_message_ref", workflow_session_id=session_id,
                           visible_message_id=anchor["visible_message_id"])
            _require(ref["role"] == anchor["role"], "Fork role differs from visible message")
            boundary = ref["boundary"]
            expected = boundary.get("before_checkpoint_id", boundary.get("after_checkpoint_id"))
            _require(expected is not None and anchor["checkpoint_id"] == expected, "Fork checkpoint differs from visible boundary")
            children = [child for child in self.rows("workflow_session")
                        if child["source"]["kind"] == "fork"
                        and child["source"]["fork_anchor_id"] == anchor["fork_anchor_id"]]
            if anchor["role"] == "user":
                pending = anchor["pending_input"]
                _require(pending["input_id"] not in pending_seeds, "Fork seeds require independent input identities")
                pending_seeds.add(pending["input_id"])
                _require(pending["input_id"] != boundary["input_id"], "Fork must allocate a new pending input identity")
                _require(pending["source_visible_message_id"] == ref["visible_message_id"], "Pending fork input source differs")
                original = self.get("node_input", input_id=boundary["input_id"])
                for field in ("payload", "payload_schema_ref", "port_id"):
                    _require(canonical_bytes(pending[field]) == canonical_bytes(original[field]), "Pending fork input changed content")
                _require(len(children) <= 1, "A branch seed cannot own multiple mutable sessions")
                for child in children:
                    child_ref = self.get("visible_message_ref", workflow_session_id=child["workflow_session_id"],
                                         visible_message_id=anchor["visible_message_id"])
                    _require(child_ref["boundary"]["input_id"] == pending["input_id"],
                             "Fork seed and child pending input differ")
            else:
                _require(anchor["chain_run_id"] == boundary["chain_run_id"] and anchor["output_id"] == boundary["output_id"],
                         "Assistant fork source differs")
            if "candidate_floors" in anchor:
                self._fork_candidate_floors(anchor, children)
        for command in self.rows("control_command"):
            target = command["target"]
            if target["kind"] == "run":
                run = self.execution(target["run_id"])
                _require(run["workflow_session_id"] == target["workflow_session_id"], "Command targets another session")
            else:
                self.get("visible_message_ref", workflow_session_id=target["source_workflow_session_id"],
                         visible_message_id=target["visible_message_id"])
        for event in self.rows("run_event"):
            run = self.execution(event["run_id"])
            _require(event["workflow_session_id"] == run["workflow_session_id"]
                     and event["node_binding_id"] == run["node_binding_id"], "Event ownership differs from run")

    def _input_ownership(self) -> None:
        owners: dict[str, str] = {}

        def bind(input_id: str, session_id: str) -> None:
            owner = owners.setdefault(input_id, session_id)
            _require(owner == session_id, "Input identity belongs to another workflow session")

        for message in self.rows("visible_message"):
            if message["role"] == "user":
                bind(message["source"]["input_id"], message["origin_workflow_session_id"])
        for kind in ("input_snapshot", "run_record", "node_run", "chain_run"):
            for row in self.rows(kind):
                bind(row["input_id"], row["workflow_session_id"])
        for ref in self.rows("visible_message_ref"):
            if ref["role"] != "user":
                continue
            session = self.session(ref["workflow_session_id"])
            source = session["source"]
            if source["kind"] == "fork":
                parent = self.index("visible_message_ref", "workflow_session_id", "visible_message_id").get(
                    (source["source_workflow_session_id"], ref["visible_message_id"])
                )
                if parent is not None and parent["boundary"]["input_id"] == ref["boundary"]["input_id"]:
                    continue  # Shared history retains its original execution owner.
            bind(ref["boundary"]["input_id"], ref["workflow_session_id"])
        for anchor in self.rows("fork_anchor"):
            if anchor["role"] != "user":
                continue
            children = [child for child in self.rows("workflow_session")
                        if child["source"]["kind"] == "fork"
                        and child["source"]["fork_anchor_id"] == anchor["fork_anchor_id"]]
            input_id = anchor["pending_input"]["input_id"]
            if children:
                bind(input_id, children[0]["workflow_session_id"])
            else:
                _require(input_id not in owners, "Proposed fork input identity is already owned")


def validate_bundle(bundle: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    from .graph_records import is_graph_record, validate_graph_bundle
    legacy = {kind: [row for row in rows if not is_graph_record(kind, row)] for kind, rows in bundle.items()}
    generic = {kind: [row for row in rows if is_graph_record(kind, row)] for kind, rows in bundle.items()}
    if any(generic.values()):
        validate_graph_bundle(generic)
    graph = ContractGraph(legacy)
    graph.validate()
    return {kind: [validate_record(kind, row) for row in rows] for kind, rows in bundle.items()}


def validate_fork_request(bundle: dict[str, list[dict[str, Any]]], anchor: dict[str, Any]) -> None:
    """Check a proposed fork against current state, not historical branch facts.

    The storage owner must repeat this check under the same session lock or
    revision transaction as start/commit. Validation alone grants no lock.
    """
    graph = ContractGraph(bundle)
    proposal = validate_record("fork_anchor", anchor)
    existing = next((row for row in graph.rows("fork_anchor") if row["fork_anchor_id"] == proposal["fork_anchor_id"]), None)
    if existing is None:
        graph.records["fork_anchor"] = [*graph.rows("fork_anchor"), proposal]
    else:
        _require(canonical_bytes(existing) == canonical_bytes(proposal), "Fork identity has conflicting content")
    graph.validate()
    session_id = proposal["source_workflow_session_id"]
    for kind in ("run_record", "node_run", "chain_run"):
        for row in graph.rows(kind):
            if row["workflow_session_id"] == session_id:
                _require(row["status"] in ("succeeded", "superseded", "closed"), "Unfinished execution blocks session fork")
    for delivery in graph.rows("output_delivery"):
        output = graph.get("workflow_output", output_id=delivery["output_id"])
        if output["workflow_session_id"] == session_id:
            _require(delivery["status"] in ("succeeded", "failed"), "Unsettled delivery blocks session fork")
