"""Workflow-owned chat operations over registered data and success checkpoints."""

from copy import deepcopy
from uuid import uuid4

from .chat_contracts import bind_chat_candidate_checkpoint
from .checkpoint_collection import (
    checkpoint_chat_bindings, checkpoint_context_bindings, checkpoint_variable_bindings,
    checkpoint_preloaded_bindings,
    collect_checkpoint_state_data,
)
from .checkpoint_loading import validate_checkpoint_configuration_target
from .checkpoint_repository import CheckpointRepository
from .graph_records import require
from .runtime_checkpoints import remap_checkpoint_state_chat, remap_checkpoint_storage_chat
from .workflow_chat_operations import (
    begin_chat_round, plan_chat_continuation, plan_chat_floor_fork,
    plan_chat_reroll, select_chat_candidate,
)
from .workflow_chat_runtime import copy_chat_round_inputs


def _uid():
    return str(uuid4())


class WorkflowChatService:
    def _chat_fixed_document(self, source, session):
        if hasattr(source, "records"):
            return source.configuration.fixed_document(
                session["workflow_definition_id"], session["definition_revision"])
        return self._document(
            source, session["workflow_definition_id"], session["definition_revision"])

    def _chat_points(self, source, session):
        if hasattr(source, "records"):
            return source.settlement
        return CheckpointRepository(
            source.store._connection, workflow_definition_id=session["workflow_definition_id"],
            workflow_session_id=session["workflow_session_id"],
            fixed_definition_revision=session["definition_revision"])

    def _chat_bindings(self, document, *, registry=None):
        registry = self.registry if registry is None else registry
        return checkpoint_chat_bindings(
            document, types=registry.data_types, registry=registry)

    def _chat_item(self, source, session, object_key, *, expected_object_revision=None, registry=None):
        registry = (source.registry if registry is None and hasattr(source, "records")
                    else self.registry if registry is None else registry)
        document = self._chat_fixed_document(source, session)
        bindings = self._chat_bindings(document, registry=registry)
        require(type(object_key) is str and object_key in bindings,
                "chat_item_unbound", "Chat operations require an exact registered chat item", 409)
        if hasattr(source, "records"):
            return source.chat.item(
                session, object_key, expected_object_revision=expected_object_revision)
        record = self._objects(source, registry=registry)._checkpoint_current_value(
            session["workflow_session_id"], object_key, bindings[object_key])
        require(record is not None and not record["deleted"],
                "chat_item_unavailable", "The registered chat item has no current value", 409)
        if expected_object_revision is not None:
            require(type(expected_object_revision) is int
                    and record["revision"] == expected_object_revision,
                    "stale_object_revision", "The chat item changed", 409)
        return document, record

    def _chat_operation_basis(self, source, sid, request, *, registry=None):
        for name, minimum in (("expected_session_revision", 1), ("expected_data_revision", 0),
                              ("expected_head_revision", 1), ("expected_object_revision", 1)):
            require(type(request[name]) is int and minimum <= request[name] <= 2**53 - 1,
                    "invalid_request", "Chat operations require exact revision guards")
        session = self._stable(source, sid, request["expected_session_revision"],
                               request["expected_data_revision"], request["expected_head_revision"])
        document, record = self._chat_item(
            source, session, request["object_key"],
            expected_object_revision=request["expected_object_revision"],
            registry=registry)
        return session, document, record

    def _write_chat_values(self, source, session, document, values, *, registry=None):
        if hasattr(source, "records"):
            source.chat.write_values(session, document, values)
            return
        registry = self.registry if registry is None else registry
        current = self._objects(source, registry=registry).current(session["workflow_session_id"])
        cas = {key: {field: current[key][field] for field in ("revision", "revision_id")} for key in values}
        self._objects(source, registry=registry)._write_chat_operation_values(
            session["workflow_session_id"], document, values, cas, registry=registry)

    def _chat_compute_values(self, document, state, *, registry=None):
        registry = self.registry if registry is None else registry
        contexts = checkpoint_context_bindings(
            document, types=registry.data_types, registry=registry)
        variables = checkpoint_variable_bindings(
            document, types=registry.data_types, registry=registry)
        preloads = checkpoint_preloaded_bindings(
            document, types=registry.data_types, registry=registry)
        require(set(state["agent_contexts"]) <= contexts.keys()
                and set(state["variables"]) == variables.keys()
                and set(state.get("preloaded_items", {})) == preloads.keys(),
                "chat_computation_binding_mismatch", "Chat computation must match admitted fixed items", 409)
        empty_context = {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0}
        values = {key: deepcopy(state["agent_contexts"].get(key, {"value": empty_context})["value"])
                  for key in contexts}
        for key, binding in variables.items():
            entry = state["variables"][key]
            require((entry["type_id"], entry["schema_version"]) == (binding.type_id, binding.schema_version),
                    "chat_computation_binding_mismatch", "Chat variable has another exact type", 409)
            values[key] = deepcopy(entry["value"])
        for key, binding in preloads.items():
            entry = state["preloaded_items"][key]
            require((entry["type_id"], entry["schema_version"]) == (binding.type_id, binding.schema_version),
                    "chat_computation_binding_mismatch", "Chat preload has another exact type", 409)
            values[key] = registry.data_types.validate(
                binding.type_id, binding.schema_version, entry["value"], scope="session")
        return values

    def _restore_chat_compute(self, source, session, document, state, *, registry=None):
        registry = (source.registry if registry is None and hasattr(source, "records")
                    else self.registry if registry is None else registry)
        if "configuration" in state:
            validate_checkpoint_configuration_target(state["configuration"], document, registry=registry)
        self._write_chat_values(
            source, session, document, self._chat_compute_values(document, state, registry=registry),
            registry=registry)

    @staticmethod
    def _chat_input_overrides(document, overrides):
        from .configuration_mutations import (
            configuration_mutation_rule, validate_editable_input_configuration,
        )
        require(type(overrides) is dict, "invalid_request", "Run input overrides must be named")
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        for node_id, text in overrides.items():
            target = nodes.get(node_id)
            require(target is not None
                    and (target["component_id"], target["component_version"]) == ("tools.editable-input", "1"),
                    "chat_input_override_denied", "Only an exact editable input's text can be projected", 409)
            rule = configuration_mutation_rule(target["component_id"], target["component_version"])
            rule.validate_changes({"text": text}, node_id=node_id)
            validate_editable_input_configuration({"text": text}, node_id=node_id)
            target["config"] = {"text": text}

    def _prepare_chat_run(self, data, session, document, plan, chain_id, inputs, *,
                          chat_request=None):
        """Freeze item-specific identities before nodes run, independently of browser launch."""
        registry = data.registry
        fixed = data.chat.fixed_document(session)
        bindings = data.chat.bindings(fixed)
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        participating = {
            key: binding for key, binding in bindings.items()
            if binding.registrar_node_id in plan.ordered_node_ids
            and (nodes[binding.registrar_node_id]["component_id"],
                 nodes[binding.registrar_node_id]["component_version"]) == ("frontend.node", "2")
        }
        if chat_request is not None:
            require(chat_request["object_key"] in participating, "chat_item_not_executed",
                    "The requested chat provider is not in this workflow invocation", 409)
        if not participating:
            return
        current = data.state.objects(session["workflow_session_id"])
        initial = collect_checkpoint_state_data(data, session["workflow_session_id"], fixed)
        frozen_overrides = {
            node_id: node["config"]["text"] for node_id, node in nodes.items()
            if node_id in plan.ordered_node_ids
            and (node["component_id"], node["component_version"]) == ("tools.editable-input", "1")
        }
        invocations, values = {}, {}
        for key, binding in participating.items():
            node = nodes[binding.registrar_node_id]
            value = current[key]["value"]
            targeted = chat_request is not None and chat_request["object_key"] == key
            if targeted and chat_request["mode"] == "reroll":
                metadata = plan_chat_reroll(value, chat_request["round_id"])
            else:
                continuation = plan_chat_continuation(value)
                latest = data.chat.latest_checkpoint(session) if continuation is None else None
                base = (continuation["result_checkpoint_id"] if continuation is not None
                        else latest["checkpoint_id"] if latest is not None else None)
                input_node = nodes.get(node["config"]["input_node_id"])
                submitted = (input_node["config"]["text"] if input_node is not None else None)
                metadata = {
                    "round_id": _uid(), "base_checkpoint_id": base, "submitted_input": submitted,
                }
                values[key] = begin_chat_round(value, **metadata)
                data.chat.save_round_inputs(
                    session["workflow_session_id"], key, metadata["round_id"],
                    inputs=inputs, input_overrides=frozen_overrides,
                    initial_compute_state=(
                        {section: initial[section] for section in
                         ("agent_contexts", "variables", "preloaded_items") if section in initial}
                        if base is None else None))
            invocations[key] = {**metadata, "candidate_id": _uid()}
        require(len({metadata["base_checkpoint_id"] for metadata in invocations.values()}) <= 1,
                "chat_computation_basis_conflict",
                "Participating chat items require different computation checkpoints", 409)
        if values:
            data.chat.write_values(session, fixed, values)
        data.chat.save_invocations(session["workflow_session_id"], chain_id, invocations)

    def _chat_execution_contexts(self, data, sid, chain_id, document):
        nodes = {binding.registrar_node_id: key
                 for key, binding in data.chat.bindings(document).items()}
        invocations = data.chat.invocations(sid, chain_id)
        return {node_id: invocations[key] for node_id, key in nodes.items() if key in invocations}

    def _bind_chat_success(self, data, session, chain_id, checkpoint_id):
        values = {}
        document = data.chat.fixed_document(session)
        for key, metadata in data.chat.invocations(
                session["workflow_session_id"], chain_id).items():
            _, record = data.chat.item(session, key)
            values[key] = bind_chat_candidate_checkpoint(
                record["value"], round_id=metadata["round_id"], candidate_id=metadata["candidate_id"],
                checkpoint_id=checkpoint_id)
        if values:
            data.chat.write_values(session, document, values)

    def _refresh_chat_snapshots(self, sid):
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            session = data.records.session(sid)
            document = self._chat_fixed_document(data, session)
            for node in document["nodes"]:
                if (node["component_id"], node["component_version"]) == ("frontend.node", "2"):
                    self._read_frontend_snapshot(
                        data, session["workflow_definition_id"],
                        session["definition_revision"], node["node_binding_id"], sid)

    def chat_reroll(self, sid, *, object_key, round_id, expected_session_revision,
                    expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key):
        request = {
            "session": sid, "object_key": object_key, "round_id": round_id,
            "expected_session_revision": expected_session_revision, "expected_data_revision": expected_data_revision,
            "expected_head_revision": expected_head_revision, "expected_object_revision": expected_object_revision,
        }

        def prepare(data, basis):
            session, document, record = self._chat_operation_basis(
                data, sid, request, registry=basis.registry)
            metadata = plan_chat_reroll(record["value"], round_id)
            frozen = data.chat.round_inputs(sid, object_key, round_id)
            require(frozen is not None, "chat_round_inputs_unavailable",
                    "The original round's accepted inputs are unavailable", 409)
            base = metadata["base_checkpoint_id"]
            state = (data.chat.materialize_checkpoint(session, base) if base is not None
                     else frozen["initial_compute_state"])
            require(state is not None, "chat_round_basis_unavailable",
                    "The initial round computation basis is unavailable", 409)
            self._restore_chat_compute(data, session, document, state, registry=basis.registry)
            return self._prepare_run(
                data, sid, expected_session_revision, frozen["inputs"],
                input_overrides=frozen["input_overrides"],
                chat_request={"mode": "reroll", "object_key": object_key, "round_id": round_id},
                basis=basis)

        return self._start_run(sid, idempotency_key, request, prepare, operation="graph.chat.reroll")

    def chat_select(self, sid, *, object_key, round_id, candidate_id, expected_session_revision,
                    expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key):
        request = {
            "session": sid, "object_key": object_key, "round_id": round_id, "candidate_id": candidate_id,
            "expected_session_revision": expected_session_revision, "expected_data_revision": expected_data_revision,
            "expected_head_revision": expected_head_revision, "expected_object_revision": expected_object_revision,
        }
        accepted = []

        def change(repo):
            session, document, record = self._chat_operation_basis(repo, sid, request)
            value = select_chat_candidate(record["value"], round_id=round_id, candidate_id=candidate_id)
            point_id = plan_chat_continuation(value)["result_checkpoint_id"]
            state = self._chat_points(repo, session).materialize(point_id)
            self._restore_chat_compute(repo, session, document, state)
            self._write_chat_values(repo, session, document, {object_key: value})
            session["revision"] += 1
            repo.put("workflow_session", session)
            self._commit(repo, session, source={
                "kind": "chat_candidate_selection", "object_key": object_key,
                "round_id": round_id, "candidate_id": candidate_id,
            })
            accepted.append(True)
            return self._view(
                self._data_management.execution_read(repo, registry=self.registry), sid)

        response = self._change("graph.chat.select", idempotency_key, request, change)
        if accepted:
            self._refresh_chat_snapshots(sid)
            self._frontend_node_state_changed(sid)
        return response

    def _fork_checkpoint_state(self, repo, source, checkpoint_id, *, logical_state=None):
        """Copy an exact prefix, then initialize a fresh target with its own checkpoint identities."""
        source_points = self._chat_points(repo, source)
        prefix = source_points.prefix(checkpoint_id)
        require(bool(prefix) and prefix[-1]["checkpoint_id"] == checkpoint_id,
                "runtime_checkpoint_lineage_invalid", "Selected checkpoint is not the prefix end", 409)
        state = (source_points.materialize(checkpoint_id) if logical_state is None else deepcopy(logical_state))
        document = self._chat_fixed_document(repo, source)
        validate_checkpoint_configuration_target(state["configuration"], document, registry=self.registry)
        from .session_preloads import session_preload_bindings
        preload_bindings = session_preload_bindings(document, self.registry.data_types)
        historical_values = {}
        for key, binding in preload_bindings.items():
            section = ("variables" if (binding.type_id, binding.schema_version) ==
                       ("workflow.variable", 1) else "preloaded_items")
            entry = state.get(section, {}).get(key)
            require(type(entry) is dict
                    and (entry.get("type_id"), entry.get("schema_version")) ==
                    (binding.type_id, binding.schema_version),
                    "checkpoint_target_preload_binding_mismatch",
                    "Fork requires each exact historical preload value", 409, object_key=key)
            historical_values[key] = self.registry.data_types.validate(
                binding.type_id, binding.schema_version, entry["value"], scope="session")
        target = self._initialize_session(
            repo, document, preloaded_values=historical_values if preload_bindings else None)
        point_map = {point["checkpoint_id"]: _uid() for point in prefix}
        target_points = self._chat_points(repo, target)
        for point in prefix:
            storage = remap_checkpoint_storage_chat(point["state_storage"], point_map)
            parent = point["parent_checkpoint_id"]
            if parent is not None:
                require(parent in point_map and storage["mode"] == "diff"
                        and storage["base_checkpoint_id"] in point_map,
                        "runtime_checkpoint_lineage_invalid", "Checkpoint prefix omits a parent or base", 409)
                storage["base_checkpoint_id"] = point_map[storage["base_checkpoint_id"]]
            target_points.insert({
                "schema_version": point["schema_version"], "checkpoint_id": point_map[point["checkpoint_id"]],
                "workflow_definition_id": source["workflow_definition_id"],
                "workflow_session_id": target["workflow_session_id"],
                "fixed_definition_revision": source["definition_revision"],
                "parent_checkpoint_id": point_map[parent] if parent is not None else None,
                "state_storage": storage,
            })
        state = remap_checkpoint_state_chat(state, point_map)
        current = self._objects(repo).current(target["workflow_session_id"])
        keys = set().union(*(state.get(section, {}) for section in
                             ("agent_contexts", "variables", "chat_contexts", "preloaded_items")))
        cas = {key: {field: current[key][field] for field in ("revision", "revision_id")} for key in keys}
        from .checkpoint_loading import load_checkpoint_state_in_transaction
        load_checkpoint_state_in_transaction(
            repo, target, state, cas, types=self.registry.data_types, registry=self.registry)
        copy_chat_round_inputs(repo, source["workflow_session_id"], target["workflow_session_id"], state["chat_contexts"])
        self._commit(repo, target, source={"kind": "session_seed", "source": None}, history=[])
        head = self._head(repo, target["workflow_session_id"])
        return {
            "target_session_id": target["workflow_session_id"], "target_checkpoint_id": point_map[checkpoint_id],
            "target_commit_id": head["head_commit_id"], "target_workflow_ref_id": head["workflow_ref_id"],
        }

    def chat_fork_floor(self, sid, *, object_key, round_id, expected_session_revision,
                        expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key):
        request = {
            "session": sid, "object_key": object_key, "round_id": round_id,
            "expected_session_revision": expected_session_revision, "expected_data_revision": expected_data_revision,
            "expected_head_revision": expected_head_revision, "expected_object_revision": expected_object_revision,
        }

        def change(repo):
            session, document, record = self._chat_operation_basis(repo, sid, request)
            planned = plan_chat_floor_fork(record["value"], round_id)
            points = self._chat_points(repo, session)
            state = points.materialize(planned["copy_through_checkpoint_id"])
            selected = points.materialize(planned["selected_checkpoint_id"])
            state["agent_contexts"], state["variables"] = selected["agent_contexts"], selected["variables"]
            if "preloaded_items" in selected:
                state["preloaded_items"] = deepcopy(selected["preloaded_items"])
            state["chat_contexts"][object_key]["value"] = planned["chat_context"]
            return self._fork_checkpoint_state(
                repo, session, planned["copy_through_checkpoint_id"], logical_state=state)

        return self._change("graph.chat.fork-floor", idempotency_key, request, change)
