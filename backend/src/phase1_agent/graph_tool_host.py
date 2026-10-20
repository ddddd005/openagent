"""Run-scoped tool bindings: preparation, read-only description and invocation."""

from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator, ValidationError

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, validate_json_value
from .graph_execution import NodeExecutionContext
from .graph_records import graph_error


class GraphToolHost:
    """Prepare explicit tool bindings and accept each variable write separately."""

    def _prepare_tool_bindings(self, data, sid, document, plan, *, registry, definitions):
        from .tool_bindings import prepare_tool_frame
        return prepare_tool_frame(data=data, sid=sid, document=document, plan=plan,
                                  registry=registry, definitions=definitions)

    def _release_tool_bindings(self, chain_id):
        self._tool_frames.pop(chain_id, None)

    @staticmethod
    def _require_tool_owner(data, context):
        session = data.records.session(context.workflow_session_id)
        chain = data.records.chain(context.chain_run_id)
        run = data.records.node_run(context.node_run_id)
        require(session["active_chain_run_id"] == context.chain_run_id
                and chain["status"] == "running"
                and run["chain_run_id"] == context.chain_run_id
                and run["node_binding_id"] == context.node_binding_id
                and run["status"] == "running",
                "tool_invocation_scope_invalid",
                "Tool call owner is no longer this running invocation", 409)

    def _tool_bridge(self, context, operation, payload):
        if operation == "specs":
            return self._bound_tool_specs(context, payload)
        if operation == "prompt":
            return self._bound_tool_prompt(context, payload)
        if operation == "invoke":
            return self._invoke_bound_tool(context, payload)
        raise graph_error("tool_invocation_scope_invalid", "Unknown tool bridge operation", 400)

    def _bound_tool_specs(self, context, payload):
        require(type(payload) is dict and set(payload) == {"agent_node_id"}
                and type(payload["agent_node_id"]) is str,
                "tool_invocation_scope_invalid", "Tool description requires one exact Agent")
        frame = self._tool_frames.get(context.chain_run_id)
        agent_node_id = payload["agent_node_id"]
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        if context.node_binding_id == agent_node_id and agent_node_id not in frame.agent_bindings:
            return []
        require(agent_node_id in frame.agent_bindings, "tool_binding_not_found",
                "The requested Agent has no prepared tool bindings", 409)
        if context.node_binding_id != agent_node_id:
            require(frame.descriptors.get(context.node_binding_id) == agent_node_id,
                    "tool_binding_access_denied",
                    "This node is not authorized to describe the Agent's tools", 403)
        basis = self._require_run_basis(context.chain_run_id)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
        return [tool.public() for binding in frame.bindings_for(agent_node_id)
                for tool in binding.tools]

    def _bound_tool_prompt(self, context, payload):
        require(type(payload) is dict and set(payload) == {"agent_node_id"}
                and type(payload["agent_node_id"]) is str,
                "tool_invocation_scope_invalid", "Tool prompt requires one exact Agent")
        agent_node_id = payload["agent_node_id"]
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        require(context.node_binding_id in frame.anchors, "tool_binding_access_denied",
                "Only a declared tool loader may describe prepared tools", 403)
        require(frame.descriptors.get(context.node_binding_id) == agent_node_id,
                "tool_binding_access_denied",
                "This loader is not declared for that Agent", 403)
        basis = self._require_run_basis(context.chain_run_id)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
        specs = [tool.public() for binding in frame.bindings_for(agent_node_id)
                 if binding.anchor_node_id == context.node_binding_id for tool in binding.tools]
        require(bool(specs), "tool_binding_not_found",
                "This loader prepared no tools for its Agent", 409)
        return specs

    def _invoke_bound_tool(self, context, payload):
        require(type(payload) is dict and set(payload) == {
            "binding_id", "operation", "arguments", "tool_call_id", "tool_execution_id"},
            "tool_invocation_scope_invalid", "Tool invocation fields differ")
        require(type(payload["binding_id"]) is str and payload["binding_id"]
                and type(payload["operation"]) is str
                and type(payload["arguments"]) is dict
                and type(payload["tool_call_id"]) is str and payload["tool_call_id"]
                and uuid_value(payload["tool_execution_id"]),
                "tool_invocation_scope_invalid", "Tool invocation identities are invalid")
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        require(context.node_binding_id in frame.agent_bindings, "tool_binding_access_denied",
                "Only the bound Agent may invoke its business tools", 403)
        binding, spec = frame.find(context.node_binding_id, payload["binding_id"], payload["operation"])
        require(binding is not None and spec is not None, "tool_binding_not_found",
                "The exact prepared tool is unavailable", 409)
        try:
            Draft202012Validator(spec.parameters).validate(payload["arguments"])
        except ValidationError as exc:
            raise graph_error("tool_arguments_invalid", "Tool arguments differ from their schema", 400) from exc
        basis = self._require_run_basis(context.chain_run_id)
        anchor_entry = basis.registry.get(
            binding.anchor_component_id, binding.anchor_component_version)
        require(anchor_entry is not None and anchor_entry.tool_binding is not None,
                "tool_binding_not_prepared", "Prepared tool adapter is unavailable", 409)
        target_entry = basis.registry.get(spec.target_component_id, spec.target_component_version)
        require(target_entry is not None and target_entry.executor is not None,
                "tool_binding_target_unavailable", "Prepared tool target is unavailable", 409)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
            item = data.state.read_object(context.workflow_session_id, spec.object_key,
                                          node_id=context.node_binding_id)
            state = data.state.variables(context.workflow_session_id)
        config, inputs = anchor_entry.tool_binding.call(spec.adapter_spec(), payload["arguments"])
        require(type(config) is dict and type(inputs) is dict, "tool_binding_invalid",
                "Tool adapter returned invalid target arguments", 500)
        self._validate_target_inputs(basis.registry, target_entry, config, inputs)
        tool_context = NodeExecutionContext(
            definition=target_entry.definition, node_binding_id=context.node_binding_id,
            workflow_session_id=context.workflow_session_id,
            chain_run_id=context.chain_run_id, node_run_id=context.node_run_id,
            state=state, private_states={}, external_inputs={},
            object_states={spec.object_key: item}, type_registry=basis.registry.data_types,
            input_refs={},
            object_accesses={spec.object_key: "read_write" if spec.access == "write" else "read"},
            operation_scope="tool:" + payload["tool_execution_id"])
        returned = target_entry.executor(deepcopy(config), deepcopy(inputs), tool_context)
        returned = self._validate_target_outputs(basis.registry, target_entry, config, returned)
        if spec.access == "read":
            require(not tool_context.object_writes, "tool_binding_invalid",
                    "A read tool formed an object write", 500)
            result = anchor_entry.tool_binding.result(
                spec.adapter_spec(), returned, deepcopy(item))
        else:
            require(len(tool_context.object_writes) == 1, "tool_binding_invalid",
                    "A write tool must form exactly one object write", 500)
            intent = tool_context.object_writes[0]
            require(intent["object_key"] == spec.object_key
                    and intent["operation"] == "put"
                    and intent["expected_revision"] == item["revision"]
                    and intent["operation_key"].startswith(
                        "tool:" + payload["tool_execution_id"] + ":object:"),
                    "tool_binding_invalid", "Write intent differs from its prepared target", 500)
            accepted = self._accept_tool_write(
                context, spec, intent, payload["tool_call_id"], payload["tool_execution_id"])
            context.apply_accepted_object(spec.object_key, accepted)
            result = anchor_entry.tool_binding.result(
                spec.adapter_spec(), returned, deepcopy(accepted))
        validate_json_value(result)
        require(type(result) is dict, "tool_binding_invalid", "Tool result must be a JSON object", 500)
        return deepcopy(result)

    @staticmethod
    def _validate_target_inputs(registry, entry, config, inputs):
        ports = {port.port_id: port for port in entry.definition.ports(config)[0]}
        require(set(inputs) <= set(ports), "tool_binding_invalid",
                "Tool target input is not declared", 500)
        for port_id, value in inputs.items():
            registry.validate_content(value, ports[port_id].data_type,
                                      ports[port_id].data_schema_version)
        require({port.port_id for port in ports.values() if port.required} <= set(inputs),
                "tool_binding_invalid", "Tool target misses a required input", 500)

    @staticmethod
    def _validate_target_outputs(registry, entry, config, returned):
        ports = {port.port_id: port for port in entry.definition.ports(config)[1]}
        require(type(returned) is dict and set(returned) <= set(ports),
                "tool_binding_invalid", "Tool target returned an undeclared output", 500)
        require({port.port_id for port in ports.values() if port.required} <= set(returned),
                "tool_binding_invalid", "Tool target returned missing required outputs", 500)
        checked = {}
        for port_id, value in returned.items():
            checked[port_id] = registry.validate_content(
                value, ports[port_id].data_type, ports[port_id].data_schema_version)
        return checked

    def _accept_tool_write(self, context, spec, intent, tool_call_id, tool_execution_id):
        basis = self._require_run_basis(context.chain_run_id)
        request = {
            "session": context.workflow_session_id, "chain": context.chain_run_id,
            "agent_node_id": context.node_binding_id, "agent_node_run_id": context.node_run_id,
            "binding_id": spec.binding_id, "tool_call_id": tool_call_id,
            "tool_execution_id": tool_execution_id, "intent": deepcopy(intent),
        }

        def change(repo):
            data = self._data_management.execution_transaction(repo, basis=basis)
            self._require_tool_owner(data, context)
            results = data.state.apply_objects(
                context.workflow_session_id, node_id=context.node_binding_id,
                writes=[deepcopy(intent)],
                write_evidence={spec.object_key: {
                    "tool_call_id": tool_call_id, "tool_execution_id": tool_execution_id,
                    "tool_binding_id": spec.binding_id}})
            require(len(results) == 1, "tool_acceptance_mismatch",
                    "Tool acceptance did not produce one receipt", 500)
            return {"receipt": results[0], "object_key": spec.object_key,
                    "binding_id": spec.binding_id, "tool_call_id": tool_call_id,
                    "tool_execution_id": tool_execution_id}

        accepted = self._change("graph.tool.object-write", "tool:" + tool_execution_id,
                                request, change)
        receipt = accepted["receipt"]
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
            accepted = data.state.read_object(
                context.workflow_session_id, spec.object_key, node_id=context.node_binding_id)
        require(accepted["revision"] == receipt["revision"]
                and accepted["revision_id"] == receipt["revision_id"]
                and canonical_bytes(accepted["value"]) == canonical_bytes(intent["value"]),
                "tool_acceptance_mismatch",
                "Accepted tool write differs from its independent reread", 500)
        return accepted
