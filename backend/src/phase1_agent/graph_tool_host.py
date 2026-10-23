"""Run-scoped tool bindings: preparation, read-only description and invocation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .content_contracts import CONTENT_WIRE_BYTES
from .contract_errors import ContractValidationError, require, uuid_value
from .contract_json import canonical_bytes, validate_json_value
from .graph_execution import NodeExecutionContext
from .graph_records import graph_error
from .tool_binding_contract import (
    PreparedPureToolSpec, PreparedWorkspaceToolBinding, PreparedWorkspaceToolSpec,
    PureToolBindingAdapter, WorkspaceToolBindingAdapter,
)


@dataclass(frozen=True)
class _WorkspaceInstance:
    """Host-private record for one open workspace; the handle stays opaque.

    ``data_basis`` freezes the CAS basis (revision/revision_id/type) read by the
    host when opening a data-capable workspace; the exported values never live
    here beyond the open handoff.
    """

    binding_id: str
    agent_node_id: str
    anchor_component_id: str
    anchor_component_version: str
    handle: Any
    data_basis: dict[str, dict] = field(default_factory=dict)


class GraphToolHost:
    """Prepare explicit tool bindings and accept each variable write separately."""

    def _prepare_tool_bindings(self, data, sid, document, plan, *, registry, definitions):
        from .tool_bindings import prepare_tool_frame
        return prepare_tool_frame(data=data, sid=sid, document=document, plan=plan,
                                  registry=registry, definitions=definitions)

    def _tool_workspace_map(self):
        """Lazy view of the per-chain opaque workspace handles.

        The workflow service initializes the real table beside ``_tool_frames``;
        a bare host used by focused tests still receives an empty table on
        first access.  Handles never leave this in-memory map.
        """
        workspaces = getattr(self, "_tool_workspaces", None)
        if workspaces is None:
            workspaces = {}
            self._tool_workspaces = workspaces
        return workspaces

    def _release_tool_bindings(self, chain_id):
        self._tool_frames.pop(chain_id, None)
        instances = self._tool_workspace_map().pop(chain_id, None)
        if not instances:
            return
        # A residual instance never reached its normal finish, so cleanup only
        # releases it (no save, no export) and is never repeated: an abort
        # failure cannot change the already settled business result.
        basis = getattr(self, "_execution_registries", {}).get(chain_id)
        for instance in instances.values():
            try:
                adapter = self._frozen_workspace_adapter(
                    basis, instance.anchor_component_id, instance.anchor_component_version)
                adapter.abort(instance.handle)
            except Exception:
                pass

    @staticmethod
    def _frozen_workspace_adapter(basis, component_id, component_version):
        """Resolve the adapter only from the frozen run basis, never the live registry."""
        require(basis is not None, "run_basis_unavailable",
                "This invocation has no retained run basis", 409)
        entry = basis.registry.get(component_id, component_version)
        require(entry is not None
                and isinstance(entry.tool_binding, WorkspaceToolBindingAdapter),
                "tool_binding_not_prepared", "Prepared workspace adapter is unavailable", 409)
        return entry.tool_binding

    @staticmethod
    def _workspace_agent(payload):
        require(type(payload) is dict and set(payload) == {"agent_node_id"}
                and type(payload["agent_node_id"]) is str and payload["agent_node_id"],
                "tool_invocation_scope_invalid",
                "A workspace operation requires one exact Agent")
        return payload["agent_node_id"]

    @staticmethod
    def _workspace_binding(frame, agent_node_id):
        bindings = [binding for binding in frame.bindings_for(agent_node_id)
                    if isinstance(binding, PreparedWorkspaceToolBinding)]
        require(len(bindings) <= 1, "workspace_binding_conflict",
                "An Agent may prepare only one workspace tool binding", 500)
        return bindings[0] if bindings else None

    @staticmethod
    def _workspace_invocation(context, binding_id):
        return {"workflow_session_id": context.workflow_session_id,
                "chain_run_id": context.chain_run_id,
                "node_binding_id": context.node_binding_id,
                "node_run_id": context.node_run_id, "binding_id": binding_id}

    @staticmethod
    def _workspace_open_input(binding, data):
        value = {"binding_id": binding.binding_id, "agent_node_id": binding.agent_node_id,
                 "config": deepcopy(binding.anchor_config),
                 "tools": [{"name": tool.name, "config": deepcopy(tool.config)}
                           for tool in binding.tools]}
        if data is not None:
            value["data"] = data
        return value

    def _read_workspace_data(self, context, basis, binding):
        """Read every declared object beside the owner check, then freeze its basis."""
        values = {}
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
            for key in binding.data_objects:
                try:
                    item = data.state.read_object(
                        context.workflow_session_id, key, node_id=context.node_binding_id)
                except Exception as exc:
                    raise graph_error(
                        "workspace_open_failed",
                        "Workspace data object could not be read: " + key, 500,
                        object_key=key) from exc
                values[key] = {"value": deepcopy(item["value"]), "revision": item["revision"],
                               "revision_id": item["revision_id"], "type_id": item["type_id"],
                               "schema_version": item["schema_version"]}
        return values

    def _require_workspace_owner(self, context, basis):
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)

    @staticmethod
    def _checked_workspace_export(exported):
        if exported is None:
            return None
        require(type(exported) is dict and set(exported) == {"text"}
                and type(exported["text"]) is str and exported["text"],
                "workspace_export_invalid",
                "Workspace finish must return null or one nonempty text", 500)
        require(len(exported["text"].encode("utf-8")) <= CONTENT_WIRE_BYTES,
                "workspace_export_invalid",
                "Workspace export exceeds the content transport budget", 500)
        return {"text": exported["text"]}

    @staticmethod
    def _checked_workspace_text(text):
        require(type(text) is str and text, "workspace_export_invalid",
                "Workspace finish must return null or one nonempty text", 500)
        require(len(text.encode("utf-8")) <= CONTENT_WIRE_BYTES,
                "workspace_export_invalid",
                "Workspace export exceeds the content transport budget", 500)
        return text

    def _checked_workspace_saves(self, context, saves, *, binding, instance, basis):
        """Validate the declared save intents before one atomic transaction."""
        checked, seen = [], set()
        for save in saves:
            require(type(save) is dict
                    and set(save) == {"object_key", "value", "expected_revision"},
                    "workspace_save_invalid", "Workspace save fields differ", 500)
            key = save["object_key"]
            require(type(key) is str and key, "workspace_save_invalid",
                    "Workspace save requires one exact object key", 500)
            require(key not in seen, "workspace_save_invalid",
                    "Workspace save repeats one object", 500)
            seen.add(key)
            require(key in binding.writable, "workspace_save_invalid",
                    "Workspace save is outside the Agent's writable declarations", 500)
            frozen = instance.data_basis.get(key)
            require(frozen is not None and type(save["expected_revision"]) is int
                    and save["expected_revision"] == frozen["revision"],
                    "workspace_save_invalid",
                    "Workspace save revision differs from its open basis", 500)
            value = save["value"]
            definition = basis.registry.data_types.get(
                frozen["type_id"], frozen["schema_version"], scope="session")
            require(definition is not None, "workspace_save_invalid",
                    "Workspace save object type is unavailable", 500)
            try:
                validate_json_value(value)
                basis.registry.data_types.validate(
                    frozen["type_id"], frozen["schema_version"], deepcopy(value),
                    scope="session")
            except ContractValidationError as exc:
                raise graph_error(
                    "workspace_save_invalid",
                    "Workspace save value differs from its object declaration", 500) from exc
            checked.append({
                "object_key": key, "expected_revision": save["expected_revision"],
                "operation_key": ("workspace:" + context.node_run_id + ":object:"
                                  + sha256(key.encode("utf-8")).hexdigest()),
                "operation": "put", "value": deepcopy(value)})
        return checked

    def _save_workspace_objects(self, context, binding, basis, writes):
        """Commit every finish save in one transaction, then reread it independently."""
        sid = context.workflow_session_id
        evidence = {write["object_key"]: {
            "tool_binding_id": binding.binding_id,
            "agent_node_id": context.node_binding_id,
            "chain_run_id": context.chain_run_id,
            "node_run_id": context.node_run_id,
            "stage": "workspace-finish",
        } for write in writes}
        request = {
            "session": sid, "chain": context.chain_run_id,
            "agent_node_id": context.node_binding_id,
            "agent_node_run_id": context.node_run_id, "binding_id": binding.binding_id,
            "stage": "workspace-finish",
            "saves": [{"object_key": write["object_key"],
                       "expected_revision": write["expected_revision"],
                       "value": deepcopy(write["value"])} for write in writes],
        }

        def change(repo):
            data = self._data_management.execution_transaction(repo, basis=basis)
            self._require_tool_owner(data, context)
            results = data.state.apply_objects(
                sid, node_id=context.node_binding_id, writes=deepcopy(writes),
                write_evidence=deepcopy(evidence))
            require(len(results) == len(writes), "tool_acceptance_mismatch",
                    "Workspace save did not produce one receipt per object", 500)
            return {"receipts": results, "binding_id": binding.binding_id}

        accepted = self._change("graph.tool.object-write",
                                "workspace:" + context.node_run_id + ":finish",
                                request, change)
        receipts = accepted["receipts"]
        require(type(accepted) is dict and type(receipts) is list
                and len(receipts) == len(writes),
                "tool_acceptance_mismatch",
                "Workspace save receipts differ from the accepted write set", 500)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
            for write, receipt in zip(writes, receipts):
                current = data.state.read_object(
                    sid, write["object_key"], node_id=context.node_binding_id)
                require(receipt["object_key"] == write["object_key"]
                        and current["revision"] == receipt["revision"]
                        and current["revision_id"] == receipt["revision_id"]
                        and canonical_bytes(current["value"])
                        == canonical_bytes(write["value"]),
                        "workspace_save_mismatch",
                        "Workspace save differs from its independent reread", 500)

    def _finish_data_workspace(self, context, binding, instance, basis, exported):
        """Accept the data-capable finish shape: null, text and/or saves."""
        if exported is None:
            return None
        require(type(exported) is dict
                and set(exported) in ({"text"}, {"saves"}, {"text", "saves"}),
                "workspace_export_invalid",
                "Workspace finish must return null, one text and/or saves", 500)
        text = self._checked_workspace_text(exported["text"]) if "text" in exported else None
        saves = exported.get("saves", [])
        require(type(saves) is list, "workspace_save_invalid",
                "Workspace saves must be a list", 500)
        writes = self._checked_workspace_saves(
            context, saves, binding=binding, instance=instance, basis=basis)
        if writes:
            self._save_workspace_objects(context, binding, basis, writes)
        return None if text is None else {"text": text}

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
        if operation == "workspace-specs":
            return self._bound_workspace_specs(context, payload)
        if operation == "workspace-open":
            return self._open_bound_workspace(context, payload)
        if operation == "workspace-finish":
            return self._finish_bound_workspace(context, payload)
        if operation == "workspace-close":
            return self._close_bound_workspace(context, payload)
        if operation == "workspace-abort":
            return self._abort_bound_workspace(context, payload)
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

    def _bound_workspace_specs(self, context, payload):
        agent_node_id = self._workspace_agent(payload)
        require(context.node_binding_id == agent_node_id, "tool_binding_access_denied",
                "Only the bound Agent may read its workspace declarations", 403)
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        binding = self._workspace_binding(frame, agent_node_id)
        if binding is None:
            return []
        basis = self._require_run_basis(context.chain_run_id)
        self._require_workspace_owner(context, basis)
        return [{"binding_id": binding.binding_id,
                 "tools": [tool.public() for tool in binding.tools]}]

    def _open_bound_workspace(self, context, payload):
        agent_node_id = self._workspace_agent(payload)
        require(context.node_binding_id == agent_node_id, "tool_binding_access_denied",
                "Only the bound Agent may open its workspace", 403)
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        binding = self._workspace_binding(frame, agent_node_id)
        require(binding is not None, "tool_binding_not_found",
                "The Agent has no prepared workspace binding", 409)
        basis = self._require_run_basis(context.chain_run_id)
        self._require_workspace_owner(context, basis)
        instances = self._tool_workspace_map().setdefault(context.chain_run_id, {})
        if binding.binding_id in instances:
            return {"opened": [binding.binding_id]}
        adapter = self._frozen_workspace_adapter(
            basis, binding.anchor_component_id, binding.anchor_component_version)
        # The data handoff exists only for adapters that declared the capability;
        # every other adapter keeps its exact previous open payload.
        data = (self._read_workspace_data(context, basis, binding)
                if adapter.data_config_field is not None else None)
        try:
            handle = adapter.open(self._workspace_open_input(binding, data),
                                  self._workspace_invocation(context, binding.binding_id))
        except Exception as exc:
            raise graph_error("workspace_open_failed",
                              "Workspace could not be opened", 500) from exc
        instances[binding.binding_id] = _WorkspaceInstance(
            binding_id=binding.binding_id, agent_node_id=agent_node_id,
            anchor_component_id=binding.anchor_component_id,
            anchor_component_version=binding.anchor_component_version, handle=handle,
            data_basis={key: {name: item[name]
                              for name in ("revision", "revision_id", "type_id", "schema_version")}
                        for key, item in (data or {}).items()})
        return {"opened": [binding.binding_id]}

    def _finish_bound_workspace(self, context, payload):
        agent_node_id = self._workspace_agent(payload)
        require(context.node_binding_id == agent_node_id, "tool_binding_access_denied",
                "Only the bound Agent may finish its workspace", 403)
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        binding = self._workspace_binding(frame, agent_node_id)
        require(binding is not None, "tool_binding_not_found",
                "The Agent has no prepared workspace binding", 409)
        instance = self._tool_workspace_map().get(
            context.chain_run_id, {}).get(binding.binding_id)
        require(instance is not None, "workspace_not_open",
                "The Agent workspace is not open", 409)
        basis = self._require_run_basis(context.chain_run_id)
        self._require_workspace_owner(context, basis)
        adapter = self._frozen_workspace_adapter(
            basis, instance.anchor_component_id, instance.anchor_component_version)
        try:
            exported = adapter.finish(
                instance.handle, self._workspace_invocation(context, binding.binding_id))
        except Exception as exc:
            raise graph_error("workspace_finalize_failed",
                              "Workspace finalization failed", 500) from exc
        if adapter.data_config_field is None:
            return self._checked_workspace_export(exported)
        return self._finish_data_workspace(context, binding, instance, basis, exported)

    def _close_bound_workspace(self, context, payload):
        agent_node_id = self._workspace_agent(payload)
        require(context.node_binding_id == agent_node_id, "tool_binding_access_denied",
                "Only the bound Agent may close its workspace", 403)
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        binding = self._workspace_binding(frame, agent_node_id)
        require(binding is not None, "tool_binding_not_found",
                "The Agent has no prepared workspace binding", 409)
        instances = self._tool_workspace_map().get(context.chain_run_id, {})
        instance = instances.get(binding.binding_id)
        require(instance is not None, "workspace_not_open",
                "The Agent workspace is not open", 409)
        basis = self._require_run_basis(context.chain_run_id)
        self._require_workspace_owner(context, basis)
        adapter = self._frozen_workspace_adapter(
            basis, instance.anchor_component_id, instance.anchor_component_version)
        # A failed close keeps the handle for the later abort-only disposal; it
        # is never retried automatically and is never reported as closed.
        try:
            adapter.close(instance.handle)
        except Exception as exc:
            raise graph_error("workspace_close_failed",
                              "Workspace could not be closed", 500) from exc
        instances.pop(binding.binding_id, None)
        return {}

    def _abort_bound_workspace(self, context, payload):
        agent_node_id = self._workspace_agent(payload)
        require(context.node_binding_id == agent_node_id, "tool_binding_access_denied",
                "Only the bound Agent may abort its workspace", 403)
        instances = self._tool_workspace_map().get(context.chain_run_id, {})
        # Cleanup may run after the owner record stopped being verifiable, so
        # this path deliberately skips the running-owner check.  It only
        # releases; a failed abort is reported instead of being reported as
        # success and is not retried.
        records = [instance for instance in instances.values()
                   if instance.agent_node_id == agent_node_id]
        require(len(records) <= 1, "workspace_binding_conflict",
                "An Agent may prepare only one workspace tool binding", 500)
        if not records:
            return {}
        instance = records[0]
        instances.pop(instance.binding_id, None)
        basis = self._require_run_basis(context.chain_run_id)
        adapter = self._frozen_workspace_adapter(
            basis, instance.anchor_component_id, instance.anchor_component_version)
        try:
            adapter.abort(instance.handle)
        except Exception as exc:
            raise graph_error("workspace_abort_failed",
                              "Workspace abort failed", 500) from exc
        return {}

    def _invoke_bound_tool(self, context, payload):
        fields = {"binding_id", "operation", "arguments", "tool_call_id", "tool_execution_id"}
        require(type(payload) is dict and set(payload) in (fields, fields | {"tool_name"}),
                "tool_invocation_scope_invalid", "Tool invocation fields differ")
        require(type(payload["binding_id"]) is str and payload["binding_id"]
                and type(payload["operation"]) is str
                and type(payload["arguments"]) is dict
                and type(payload["tool_call_id"]) is str and payload["tool_call_id"]
                and uuid_value(payload["tool_execution_id"]),
                "tool_invocation_scope_invalid", "Tool invocation identities are invalid")
        tool_name = payload.get("tool_name")
        require(tool_name is None or type(tool_name) is str and tool_name,
                "tool_invocation_scope_invalid", "Tool invocation name is invalid")
        frame = self._tool_frames.get(context.chain_run_id)
        require(frame is not None, "tool_binding_not_prepared",
                "This invocation has no prepared tool bindings", 409)
        require(context.node_binding_id in frame.agent_bindings, "tool_binding_access_denied",
                "Only the bound Agent may invoke its business tools", 403)
        binding, spec = frame.find(
            context.node_binding_id, payload["binding_id"], payload["operation"])
        if isinstance(binding, PreparedWorkspaceToolBinding):
            # One workspace binding serves several tools under the same
            # binding_id; only the exact model-facing name selects the tool.
            # A missing or unknown name is rejected instead of guessing one.
            spec = next((tool for tool in binding.tools if tool.name == tool_name), None)
        require(binding is not None and spec is not None, "tool_binding_not_found",
                "The exact prepared tool is unavailable", 409)
        try:
            if isinstance(spec, (PreparedPureToolSpec, PreparedWorkspaceToolSpec)):
                validate_json_value(payload["arguments"])
            Draft202012Validator(spec.parameters).validate(payload["arguments"])
        except (ValidationError, ContractValidationError) as exc:
            raise graph_error("tool_arguments_invalid", "Tool arguments differ from their schema", 400) from exc
        basis = self._require_run_basis(context.chain_run_id)
        anchor_entry = basis.registry.get(
            binding.anchor_component_id, binding.anchor_component_version)
        require(anchor_entry is not None and anchor_entry.tool_binding is not None,
                "tool_binding_not_prepared", "Prepared tool adapter is unavailable", 409)
        if isinstance(spec, PreparedWorkspaceToolSpec):
            result = self._invoke_workspace_tool(
                context, payload, spec, anchor_entry.tool_binding, basis)
        else:
            target_entry = basis.registry.get(spec.target_component_id, spec.target_component_version)
            require(target_entry is not None and target_entry.executor is not None,
                    "tool_binding_target_unavailable", "Prepared tool target is unavailable", 409)
            if isinstance(spec, PreparedPureToolSpec):
                result = self._invoke_pure_tool(
                    context, payload, spec, anchor_entry.tool_binding, target_entry, basis)
            else:
                result = self._invoke_variable_tool(
                    context, payload, spec, anchor_entry.tool_binding, target_entry, basis)
        validate_json_value(result)
        require(type(result) is dict, "tool_binding_invalid", "Tool result must be a JSON object", 500)
        return deepcopy(result)

    def _invoke_workspace_tool(self, context, payload, spec, adapter, basis):
        require(isinstance(adapter, WorkspaceToolBindingAdapter), "tool_binding_not_prepared",
                "Prepared workspace adapter differs from its frozen binding", 409)
        instance = self._tool_workspace_map().get(
            context.chain_run_id, {}).get(spec.binding_id)
        require(instance is not None, "workspace_not_open",
                "The Agent workspace is not open", 409)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
        invocation = self._workspace_invocation(context, spec.binding_id)
        invocation.update({"tool_call_id": payload["tool_call_id"],
                           "tool_execution_id": payload["tool_execution_id"]})
        tool = {"name": spec.name, "config": deepcopy(spec.config),
                "binding_id": spec.binding_id, "agent_node_id": spec.agent_node_id}
        # Adapter failures stay raw: the kernel keeps its existing
        # ToolExecutionError / tool_contract_error classification and never
        # retries a workspace call.
        return adapter.invoke(instance.handle, tool, deepcopy(payload["arguments"]),
                              invocation)

    def _invoke_pure_tool(self, context, payload, spec, adapter, target_entry, basis):
        require(isinstance(adapter, PureToolBindingAdapter), "tool_binding_not_prepared",
                "Prepared computation adapter differs from its frozen binding", 409)
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
        config, inputs = adapter.call(spec.adapter_spec(), deepcopy(payload["arguments"]))
        require(type(config) is dict and type(inputs) is dict, "tool_binding_invalid",
                "Tool adapter returned invalid target arguments", 500)
        validate_json_value(config)
        validate_json_value(inputs)
        basis.registry.validate_config(target_entry, config)
        self._validate_target_inputs(basis.registry, target_entry, config, inputs)
        tool_context = NodeExecutionContext(
            definition=target_entry.definition, node_binding_id=context.node_binding_id,
            workflow_session_id=context.workflow_session_id,
            chain_run_id=context.chain_run_id, node_run_id=context.node_run_id,
            state={}, private_states={}, external_inputs={}, object_states={},
            type_registry=basis.registry.data_types, input_refs={}, object_accesses={},
            operation_scope="tool:" + payload["tool_execution_id"])
        returned = target_entry.executor(deepcopy(config), deepcopy(inputs), tool_context)
        returned = self._validate_target_outputs(basis.registry, target_entry, config, returned)
        require(not tool_context.effects and not tool_context.object_writes,
                "tool_binding_invalid", "A pure tool formed a business effect", 500)
        return adapter.result(spec.adapter_spec(), deepcopy(returned))

    def _invoke_variable_tool(self, context, payload, spec, adapter, target_entry, basis):
        with self._lock, self._data_management.read_execution(registry=basis.registry) as data:
            self._require_tool_owner(data, context)
            item = data.state.read_object(context.workflow_session_id, spec.object_key,
                                          node_id=context.node_binding_id)
            state = data.state.variables(context.workflow_session_id)
        config, inputs = adapter.call(spec.adapter_spec(), deepcopy(payload["arguments"]))
        require(type(config) is dict and type(inputs) is dict, "tool_binding_invalid",
                "Tool adapter returned invalid target arguments", 500)
        basis.registry.validate_config(target_entry, config)
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
            result = adapter.result(
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
            result = adapter.result(
                spec.adapter_spec(), returned, deepcopy(accepted))
        return result

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
