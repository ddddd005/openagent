"""Serial graph execution with scoped state access and durable node boundaries."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from .contract_json import canonical_bytes, content_digest, validate_json_value
from .graph_contracts import (
    GraphDiagnosticError, GraphPlan, NodeDefinition, NodeRegistry, require,
    validate_content_value, uuid4_string,
)
from .preparation_program import (
    _replace as replace_program_macros,
    merge_program_definitions, validate_program_state, validate_typed_value,
    validate_variable_name, variable_text,
)
from .workbench_resources import (
    session_data_entry, validate_data_definition, validate_data_value,
)


class NodeExecutionContext:
    """A trusted node receives its own state and explicitly declared capabilities."""

    def __init__(self, *, definition: NodeDefinition, node_binding_id: str,
                 workflow_session_id: str, chain_run_id: str, node_run_id: str,
                 state: dict, private_states: dict, external_inputs: dict, host=None,
                 object_states: dict | None = None, type_registry=None, input_refs=None, object_accesses=None):
        self.definition = definition
        self.node_binding_id, self.workflow_session_id = node_binding_id, workflow_session_id
        self.chain_run_id, self.node_run_id = chain_run_id, node_run_id
        self._state, self._private_states = state, private_states
        self._external_inputs = external_inputs
        self._host = host
        self._object_states = object_states if object_states is not None else {}
        self._type_registry = type_registry
        self._input_refs = copy.deepcopy(input_refs or {})
        self._object_accesses = object_accesses
        self.object_writes: list[dict] = []
        self.effects: list[dict] = []
        self.reads: list[dict] = []
        self._last_variables_read: tuple[int, int] | None = None

    def validate_content(self, value: Any, type_id: str, schema_version: int = 1) -> dict:
        require(self._type_registry is not None, "host_unknown_type", "Content types are unavailable")
        return validate_content_value(value, type_id, schema_version, registry=self._type_registry)

    def transform_content(self, value: Any, type_id: str, schema_version: int,
                          operation: Callable, *, scope: str = "body") -> dict:
        require(self._type_registry is not None, "host_unknown_type", "Content types are unavailable")
        return self._type_registry.transform_content(type_id, schema_version, value, operation, scope=scope)

    def input_artifact_refs(self, port_id: str) -> list[dict]:
        return copy.deepcopy(self._input_refs.get(port_id, []))

    def _allow(self, capability: str) -> None:
        require(capability in self.definition.capabilities, "graph_capability_denied",
                "Node did not declare this state capability", node_id=self.node_binding_id)

    def host_call(self, capability: str, operation: str, payload: dict) -> Any:
        self._allow(capability)
        require(callable(self._host), "graph_host_unavailable", "Node requires a workflow host",
                node_id=self.node_binding_id)
        return self._host(self, capability, operation, copy.deepcopy(payload))

    def external_input(self, name: str) -> Any:
        self._allow("external:read")
        require(name in self._external_inputs, "graph_external_input_missing",
                "Required external input is missing", node_id=self.node_binding_id, port_id=name)
        return copy.deepcopy(self._external_inputs[name])

    def _object(self, key: str, *, write=False) -> dict:
        from .host_sdk import ObjectBinding
        from .session_objects import SessionObjectStore
        self._allow("objects:write" if write else "objects:read")
        if self._object_accesses is not None:
            access = self._object_accesses.get(key)
            require(access is not None and access in (("write", "read_write") if write else ("read", "read_write")),
                    "graph_object_access_undeclared", "Object access is outside the node declaration",
                    node_id=self.node_binding_id)
        require(key in self._object_states, "session_object_unbound", "Object is not bound",
                node_id=self.node_binding_id)
        item = self._object_states[key]
        SessionObjectStore.authorize(ObjectBinding.from_dict(item["binding"]), self.node_binding_id, write=write)
        return item

    def object_read(self, key: str) -> dict:
        item = self._object(key)
        require(not item["deleted"], "session_object_deleted", "Object has been deleted",
                node_id=self.node_binding_id)
        require(self._type_registry is not None, "host_unknown_type", "Object types are unavailable")
        self._type_registry.validate(item["type_id"], item["schema_version"], item["value"], scope="session")
        self.reads.append({"kind": "object_read", "object_key": key, "revision": item["revision"],
                           "revision_id": item["revision_id"]})
        return copy.deepcopy(item)

    def object_write(self, key: str, value: Any, *, expected_revision: int, operation_key: str | None = None):
        self._object_write(key, value, expected_revision=expected_revision, operation_key=operation_key)

    def object_delete(self, key: str, *, expected_revision: int, operation_key: str | None = None):
        self._object_write(key, None, expected_revision=expected_revision, operation_key=operation_key, delete=True)

    def _object_write(self, key, value, *, expected_revision, operation_key, delete=False):
        from .host_sdk import WriteIntent
        item = self._object(key, write=True)
        require(type(expected_revision) is int and expected_revision == item["revision"],
                "stale_object_revision", "Object changed since it was read", node_id=self.node_binding_id)
        require(not any(write["object_key"] == key for write in self.object_writes),
                "session_object_duplicate_write", "Settle one write per object at a node boundary")
        if not delete:
            require(self._type_registry is not None, "host_unknown_type", "Object types are unavailable")
            value = self._type_registry.validate(item["type_id"], item["schema_version"], value, scope="session")
        default_key = self.node_run_id + ":object:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        intent = WriteIntent(key, expected_revision, operation_key or default_key,
                             value, "delete" if delete else "put").to_dict()
        self.object_writes.append(intent)
        self.effects.append({"kind": "object_delete" if delete else "object_write", "object_key": key,
                             "expected_revision": expected_revision, "operation_key": intent["operation_key"]})

    def private_read(self) -> Any:
        self._allow("private:read")
        value = self._private_states.get(self.node_binding_id, self.definition.private_state_default)
        return NodeRegistry.validate_private_state(self.definition, value)

    def private_write(self, value: Any) -> None:
        self._allow("private:write")
        value = NodeRegistry.validate_private_state(self.definition, value)
        self._private_states[self.node_binding_id] = value
        self.effects.append({"kind": "private_write", "node_binding_id": self.node_binding_id})

    def register_variable(self, name: str, value_type: str, *, initial: Any = None,
                          has_initial: bool = False) -> dict:
        self._allow("variables:write")
        validate_variable_name(name)
        definition = {"name": name, "type": value_type}
        if has_initial:
            validate_typed_value(value_type, initial)
            definition["default"] = initial
        current = merge_program_definitions(self._state, [definition])
        self._state.clear()
        self._state.update(current)
        self.effects.append({"kind": "variable_register", "name": name})
        return copy.deepcopy(current["values"][name])

    def assign_variable(self, name: str, value: Any, *, operation: str = "set") -> dict:
        self._allow("variables:write")
        validate_variable_name(name)
        require(name in self._state["values"], "variable_not_registered",
                "Variable assignment target is not registered", node_id=self.node_binding_id)
        entry = self._state["values"][name]
        if operation != "set":
            require(operation in ("add", "subtract") and entry["type"] in ("integer", "number")
                    and "value" in entry, "variable_type_mismatch",
                    "Numeric update needs an assigned numeric variable", node_id=self.node_binding_id)
            validate_typed_value(entry["type"], value)
            value = entry["value"] + value if operation == "add" else entry["value"] - value
        validate_typed_value(entry["type"], value)
        current = copy.deepcopy(self._state)
        current["values"][name] = {"type": entry["type"], "source": "assignment", "value": value}
        current = validate_program_state(current)
        self._state.clear()
        self._state.update(current)
        self.effects.append({"kind": "variable_assign", "name": name, "operation": operation})
        return copy.deepcopy(current["values"][name])

    def variable_value_text(self, name: str) -> str:
        self._allow("variables:read")
        value = variable_text(self._state, name, self.node_binding_id)
        self.reads.append({"kind": "variable_read", "name": name, "revision": self._state["revision"]})
        return value

    def replace_variables(self, text: str) -> str:
        self._allow("variables:read")
        basis = len(self.effects), self._state["revision"]
        if self._last_variables_read != basis:
            self.reads.append({"kind": "variables_read", "revision": self._state["revision"],
                               "values_digest": content_digest(self._state["values"])})
            self._last_variables_read = basis
        return replace_program_macros(text, self._state, {
            "workflow_session_id": self.workflow_session_id, "node_binding_id": self.node_binding_id,
        }, self.node_binding_id)

    def read_shared(self, definition: dict) -> Any:
        self._allow("shared:read")
        definition = validate_data_definition(definition)
        entry = self._state.get("data", {}).get(definition["key"], session_data_entry(definition))
        require(canonical_bytes(entry["definition"]) == canonical_bytes(definition),
                "session_data_definition_conflict", "Session data definition changed",
                node_id=self.node_binding_id)
        require("value" in entry, "session_data_unassigned", "Session data has not been assigned",
                node_id=self.node_binding_id)
        self.reads.append({"kind": "shared_read", "key": definition["key"],
                           "definition_id": definition["definition_id"], "revision": self._state["revision"]})
        return copy.deepcopy(entry["value"])

    def write_shared(self, definition: dict, value: Any) -> None:
        self._allow("shared:write")
        definition = validate_data_definition(definition)
        require(definition["writable"], "session_data_read_only", "Session data is read-only",
                node_id=self.node_binding_id)
        existing = self._state.get("data", {}).get(definition["key"])
        require(existing is None or canonical_bytes(existing["definition"]) == canonical_bytes(definition),
                "session_data_definition_conflict", "Session data definition changed",
                node_id=self.node_binding_id)
        validate_data_value(definition, value)
        current = copy.deepcopy(self._state)
        current.setdefault("data", {})[definition["key"]] = session_data_entry(definition, value, assigned=True)
        current = validate_program_state(current)
        self._state.clear()
        self._state.update(current)
        self.effects.append({"kind": "shared_write", "key": definition["key"],
                             "definition_id": definition["definition_id"]})


@dataclass
class GraphExecutionResult:
    status: str
    outputs: dict[str, dict[str, dict]]
    state: dict
    private_states: dict
    node_runs: list[dict]
    diagnostic: dict | None
    next_node_index: int
    artifact_refs: dict


def default_private_states(document: dict, registry: NodeRegistry) -> dict:
    """Only pure declaration defaults are consulted; no executor is constructed."""
    result = {}
    for node in document["nodes"]:
        entry = registry.get(node["component_id"], node["component_version"])
        if entry is not None:
            result[node["node_binding_id"]] = copy.deepcopy(entry.definition.private_state_default)
    return result


def copy_private_state(value: Any, source: NodeDefinition, target: NodeDefinition) -> Any:
    require((source.component_id, source.component_version) == (target.component_id, target.component_version)
            and canonical_bytes(source.private_state_schema) == canonical_bytes(target.private_state_schema),
            "graph_private_state_incompatible", "Private state has no declared compatible migration")
    NodeRegistry.validate_private_state(source, value)
    return NodeRegistry.validate_private_state(target, value)


def execute_graph(plan: GraphPlan, registry: NodeRegistry, *, workflow_session_id: str,
                  chain_run_id: str, node_run_ids: dict[str, str], state: dict,
                  private_states: dict | None = None, external_inputs: dict | None = None,
                  on_node: Callable[[dict], None] | None = None,
                  should_pause: Callable[[], bool] | None = None,
                  start_index: int = 0, outputs: dict | None = None,
                  node_runs: list[dict] | None = None, host=None,
                  object_states: dict | None = None, artifact_refs: dict | None = None,
                  validate_output_references: Callable | None = None,
                  runtime_host=None, acceptance_cache=None,
                  result_recovery: Callable | None = None) -> GraphExecutionResult:
    """Resume only from a persisted completed boundary; never retry unknown effects."""
    uuid4_string(workflow_session_id)
    uuid4_string(chain_run_id)
    require(type(start_index) is int and 0 <= start_index <= len(plan.ordered_node_ids),
            "graph_invalid_resume", "Execution boundary is invalid")
    current = validate_program_state(state)
    private = copy.deepcopy(private_states or {})
    produced, runs = copy.deepcopy(outputs or {}), copy.deepcopy(node_runs or [])
    external = copy.deepcopy(external_inputs or {})
    objects = copy.deepcopy(object_states or {})
    accepted_refs = copy.deepcopy(artifact_refs or {})
    pending_results = acceptance_cache if acceptance_cache is not None else {}
    validate_json_value([private, external, produced, runs])
    nodes = {node["node_binding_id"]: node for node in plan.document["nodes"]}
    edges_by_id = {edge["edge_id"]: edge for edge in plan.document["edges"]}
    ancestors: dict[str, set[str]] = {}
    for identity in plan.ordered_node_ids:
        predecessors = {edge["source_node_id"] for edges in plan.input_edges[identity].values() for edge in edges}
        predecessors.update(edge["source_node_id"] for edge in plan.document.get("control_edges", [])
                            if edge["target_node_id"] == identity)
        ancestors[identity] = set(predecessors)
        for predecessor in predecessors:
            ancestors[identity].update(ancestors[predecessor])
    require(type(node_run_ids) is dict and set(plan.ordered_node_ids) <= set(node_run_ids)
            and len({node_run_ids[identity] for identity in plan.ordered_node_ids}) == len(plan.ordered_node_ids),
            "graph_node_run_missing", "Each participating node needs a distinct assigned run identity")
    for identity in plan.ordered_node_ids:
        uuid4_string(node_run_ids[identity])
    completed = set(plan.ordered_node_ids[:start_index])
    require(set(produced) == completed
            and len(runs) == start_index
            and [run.get("node_binding_id") for run in runs] == list(plan.ordered_node_ids[:start_index])
            and all(run.get("event") == "succeeded" and run.get("node_run_id") == node_run_ids[run["node_binding_id"]]
                    for run in runs),
            "graph_invalid_resume", "Saved execution boundary does not match completed results")
    for identity in completed:
        node = nodes[identity]
        ports = {port.port_id: port for port in plan.definitions[identity].ports(node["config"])[1]}
        require(type(produced[identity]) is dict and set(produced[identity]) <= set(ports)
                and {port.port_id for port in ports.values() if port.required} <= set(produced[identity]),
                "graph_invalid_resume", "Saved output ports differ")
        for port_id, value in produced[identity].items():
            registry.validate_content(value, ports[port_id].data_type, ports[port_id].data_schema_version)

    def result(status: str, index: int, diagnostic: dict | None = None) -> GraphExecutionResult:
        return GraphExecutionResult(status, produced, current, private, runs, diagnostic, index, accepted_refs)

    def emit(event: dict) -> None:
        if on_node is not None:
            payload = {**copy.deepcopy({key: value for key, value in event.items()
                                       if key != "settlement_context"}),
                       "state": copy.deepcopy(current), "private_states": copy.deepcopy(private)}
            if "settlement_context" in event:
                payload["settlement_context"] = event["settlement_context"]
            try:
                on_node(payload)
            except Exception:
                if payload.get("settlement_committed"):
                    raise GraphDiagnosticError(
                        "result_committed_notification_failed",
                        "Node result committed but its local completion acknowledgment failed",
                        node_id=event["node_binding_id"]) from None
                raise
            # A persistence adapter may advance the shared-state revision after
            # saving a node. Its CAS basis must reach the next scoped context.
            updated = validate_program_state(payload["state"])
            current.clear()
            current.update(updated)
            if "object_states" in payload:
                objects.clear()
                objects.update(copy.deepcopy(payload["object_states"]))
            if "output_refs" in payload:
                event["output_refs"] = copy.deepcopy(payload["output_refs"])
            event["outputs"] = copy.deepcopy(payload["outputs"])

    for index in range(start_index, len(plan.ordered_node_ids)):
        if should_pause is not None and should_pause():
            return result("paused", index)
        identity = plan.ordered_node_ids[index]
        node, definition = nodes[identity], plan.definitions[identity]
        entry = registry.get(node["component_id"], node["component_version"])
        require(entry is not None and entry.definition == definition
                and (entry.executor is not None or entry.executor_ref is not None),
                "graph_implementation_changed", "Node implementation changed after planning", node_id=identity)
        require(identity in node_run_ids, "graph_node_run_missing", "Node run identity was not assigned", node_id=identity)
        run_id = uuid4_string(node_run_ids[identity])
        inputs: dict = {}
        input_refs: dict = {}
        before_state, before_private = copy.deepcopy(current), copy.deepcopy(private)
        context = None
        event = {"event": "running", "node_binding_id": identity, "node_run_id": run_id,
                 "inputs": inputs, "outputs": {}, "effects": [], "reads": [], "diagnostic": None,
                 "object_writes": []}
        in_callback = False
        try:
            for port in definition.ports(node["config"])[0]:
                values = []
                for edge in plan.input_edges[identity].get(port.port_id, []):
                    source_outputs = produced.get(edge["source_node_id"], {})
                    require(edge["source_port_id"] in source_outputs,
                            "graph_linked_output_missing", "Producer did not return the required output",
                            node_id=identity, port_id=port.port_id, edge_id=edge["edge_id"])
                    values.append(registry.validate_content(source_outputs[edge["source_port_id"]],
                                                           port.data_type, port.data_schema_version))
                    output_id = accepted_refs.get(edge["source_node_id"], {}).get(edge["source_port_id"])
                    if output_id is not None:
                        input_refs.setdefault(port.port_id, []).append({
                            "edge_id": edge["edge_id"], "output_id": output_id, "order": edge["order"]})
                if port.multiple:
                    inputs[port.port_id] = values
                elif values:
                    inputs[port.port_id] = values[0]
            in_callback = True
            emit(event)
            in_callback = False
            context = NodeExecutionContext(
                definition=definition, node_binding_id=identity, workflow_session_id=workflow_session_id,
                chain_run_id=chain_run_id, node_run_id=run_id, state=current,
                private_states=private, external_inputs=external, host=host,
                object_states=objects, type_registry=registry.data_types,
                input_refs=input_refs,
                object_accesses={
                    key: access["access"] for access in definition.object_accesses
                    for key in (node["config"][access["config_field"]] if access["multiple"]
                                else [node["config"][access["config_field"]]])
                } if definition.object_accesses or definition.input_storage == "references" else None,
            )
            pending = pending_results.get(run_id)
            if pending is not None:
                require(pending["basis"] == {
                    "config": node["config"], "inputs": inputs, "input_refs": input_refs,
                    "state": before_state, "private": before_private, "objects": objects},
                    "graph_acceptance_basis_changed", "Retained result basis changed", node_id=identity)
                context = pending["context"]
                returned = (pending["recover"]() if pending["recover"] is not None
                            else copy.deepcopy(pending["outputs"]))
                current.clear()
                current.update(copy.deepcopy(context._state))
                private.clear()
                private.update(copy.deepcopy(context._private_states))
            elif entry.executor_ref is None:
                if runtime_host is not None:
                    runtime_host.bind_node_information(context)
                returned = entry.executor(copy.deepcopy(node["config"]), copy.deepcopy(inputs), context)
            else:
                from .runtime_control import NodeExecutionSuspended
                from .runtime_hosting import InvocationOwner
                require(runtime_host is not None, "runtime_host_unavailable",
                        "Hosted executor requires the public runtime host", node_id=identity)
                owner = InvocationOwner(workflow_session_id, chain_run_id, identity, run_id)
                if runtime_host.contains(owner):
                    context = runtime_host.context(owner)
                else:
                    # Retain unaccepted writes at a safe point; the accepted
                    # graph state advances only after successful settlement.
                    context._state = copy.deepcopy(current)
                    context._private_states = copy.deepcopy(private)
                    runtime_host.start(owner, entry.executor_ref, node["config"], inputs, context)
                if should_pause is not None and should_pause():
                    runtime_host.request_pause(owner)
                outcome = (runtime_host.retry_acceptance(owner)
                           if runtime_host.has_pending_acceptance(owner) else runtime_host.drive(owner))
                if outcome.status == "paused":
                    raise NodeExecutionSuspended()
                require(outcome.status == "succeeded", "runtime_invocation_unavailable",
                        "Hosted invocation did not produce a successful result", node_id=identity)
                returned = outcome.outputs
                current.clear()
                current.update(copy.deepcopy(context._state))
                private.clear()
                private.update(copy.deepcopy(context._private_states))
            output_ports = {port.port_id: port for port in definition.ports(node["config"])[1]}
            require(type(returned) is dict and set(returned) <= set(output_ports)
                    and (entry.settlement_builder is not None or {
                        port.port_id for port in output_ports.values() if port.required} <= set(returned)),
                    "graph_invalid_output", "Executor returned missing or unknown named outputs", node_id=identity)
            validated = {}
            allowed_artifacts = {output_id for ancestor in ancestors[identity]
                                 for output_id in accepted_refs.get(ancestor, {}).values()}
            for port_id, value in returned.items():
                port = output_ports[port_id]
                checked, references = registry.data_types.validate_and_references(
                    port.data_type, port.data_schema_version, value, scope="content")
                extra_references = [ref for ref in references
                                    if ref["scope"] == "artifact" and ref["output_id"] not in allowed_artifacts]
                require(not extra_references or validate_output_references is not None
                        and validate_output_references(context, extra_references),
                        "graph_artifact_reference_denied",
                        "Output references must name accepted artifacts from this invocation's dependencies",
                        node_id=identity, port_id=port_id)
                for binding in registry.data_types.content_artifact_bindings(
                        port.data_type, port.data_schema_version, checked):
                    edge = edges_by_id.get(binding["edge_id"])
                    require(edge is not None and edge["target_node_id"] in ancestors[identity] | {identity}
                            and edge["source_node_id"] in ancestors[identity]
                            and edge["order"] == binding["order"]
                            and accepted_refs.get(edge["source_node_id"], {}).get(edge["source_port_id"])
                                == binding["output_id"],
                            "graph_artifact_binding_mismatch",
                            "Content provenance differs from the graph's accepted input binding",
                            node_id=identity, port_id=port_id)
                validated[port_id] = checked
            validate_program_state(current)
            if identity in private:
                NodeRegistry.validate_private_state(definition, private[identity])
        except Exception as exc:
            if in_callback:
                raise
            from .runtime_control import NodeExecutionSuspended
            if isinstance(exc, NodeExecutionSuspended):
                diagnostic = {"code": exc.reason_code, "message": str(exc), "node_id": identity,
                              "port_id": None, "edge_id": None, "dependent_outputs": list(plan.target_node_ids)}
                event.update(event=exc.status, effects=context.effects if context else [],
                             reads=context.reads if context else [], diagnostic=diagnostic,
                              object_writes=[])
                emit(event)
                return result(exc.status, index, diagnostic)
            code = getattr(exc, "reason_code", getattr(exc, "code", "graph_node_failed"))
            recovery = result_recovery(context) if context is not None and result_recovery is not None else None
            if recovery is not None and run_id not in pending_results:
                context._state = copy.deepcopy(context._state)
                context._private_states = copy.deepcopy(context._private_states)
                pending_results[run_id] = {
                    "basis": copy.deepcopy({"config": node["config"], "inputs": inputs,
                        "input_refs": input_refs, "state": before_state, "private": before_private,
                        "objects": objects}), "context": context, "outputs": None, "recover": recovery}
            retryable = recovery is not None
            if not retryable:
                pending_results.pop(run_id, None)
            # A rejected node publishes neither state edits nor object writes.
            current.clear()
            current.update(before_state)
            private.clear()
            private.update(before_private)
            diagnostic = {"code": code, "message": str(exc), "node_id": identity,
                          "port_id": None, "edge_id": None, "dependent_outputs": list(plan.target_node_ids)}
            if isinstance(exc, GraphDiagnosticError):
                diagnostic.update(exc.diagnostics[0])
                diagnostic["node_id"] = diagnostic["node_id"] or identity
            event.update(event="archive_failed" if retryable else "failed", effects=context.effects if context else [],
                         reads=context.reads if context else [], diagnostic=diagnostic,
                         object_writes=[])
            runs.append(copy.deepcopy(event))
            emit(event)
            return result("archive_failed" if retryable else "failed", index, diagnostic)
        context._state = copy.deepcopy(current)
        context._private_states = copy.deepcopy(private)
        pending_results[run_id] = {
            "basis": copy.deepcopy({"config": node["config"], "inputs": inputs,
                "input_refs": input_refs, "state": before_state, "private": before_private,
                "objects": objects}), "context": context, "outputs": copy.deepcopy(validated), "recover": None}
        retained_refs = pending.get("candidate_output_refs") if pending is not None else None
        event["candidate_output_refs"] = retained_refs or {port: str(uuid4()) for port in output_ports}
        pending_results[run_id]["candidate_output_refs"] = copy.deepcopy(event["candidate_output_refs"])
        event["allowed_output_ids"] = sorted(allowed_artifacts)
        if entry.settlement_builder is not None:
            require(on_node is not None, "graph_settlement_unavailable", "Node requires an atomic settlement host")
            event["settlement_context"] = context
        event.update(event="succeeded", outputs=validated, effects=context.effects, reads=context.reads,
                     object_writes=context.object_writes)
        emit(event)
        pending_results.pop(run_id, None)
        event.pop("settlement_context", None)
        if "output_refs" in event:
            accepted_refs[identity] = copy.deepcopy(event["output_refs"])
        else:
            accepted_refs[identity] = {port: str(uuid4()) for port in validated}
        produced[identity] = copy.deepcopy(event["outputs"])
        runs.append(copy.deepcopy(event))
    return result("succeeded", len(plan.ordered_node_ids))
