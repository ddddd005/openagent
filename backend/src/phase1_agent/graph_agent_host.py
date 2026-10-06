"""Optional native capabilities, frozen archives and legacy graph migration."""

from contextlib import closing, contextmanager
from copy import deepcopy
import json
from uuid import uuid4

from .contract_json import canonical_bytes, dumps_pretty
from .contracts_v2 import validate_record
from .graph_records import require, graph_error, uuid_value
from .graph_resource_host import GraphResourceHost
from .graph_store import GraphRecordStore
from .graph_prompt import archive_materials, assemble_materials
from .graph_archives import read_graph_archive, resolve_graph_archive
from .legacy_archive import read_closed_legacy_archive
from .program_variable_store import ProgramVariableStore


class GraphAgentHost:
    """A graph service acquires capabilities only for participating node types."""

    @contextmanager
    def _model_catalog(self):
        from .model_configuration_store import ModelConfigurationStore
        with closing(self._store()) as store:
            yield ModelConfigurationStore(store)

    def _check_open(self):
        require(not self._closed, "service_closed", "Graph service is closed", 409)

    def list_model_providers(self):
        from .model_configuration import DEFAULT_PROVIDER_ID, default_provider
        with self._lock, self._model_catalog() as catalog:
            self._check_open()
            if catalog.get_current("provider", DEFAULT_PROVIDER_ID) is None:
                catalog.write("provider", default_provider(), expected_revision=0,
                              idempotency_key="bootstrap-chat-provider-v1")
            return catalog.list_providers()

    def get_model_configuration(self, kind, identity, version=None):
        with self._lock, self._model_catalog() as catalog:
            self._check_open()
            record = catalog.get_current(kind, identity) if version is None else catalog.get_revision(kind, identity, version)
            require(record is not None, "not_found", "Model configuration not found", 404)
            return record

    def save_model_configuration(self, kind, **request):
        with self._lock, self._model_catalog() as catalog:
            self._check_open()
            return catalog.write(kind, **request)

    def diagnose_model_configuration(self, identity, version):
        with self._lock, self._model_catalog() as catalog:
            self._check_open()
            record = catalog.get_revision("model", identity, version)
            require(record is not None, "not_found", "Model configuration not found", 404)
            return {"config_id": identity, "revision": version, "diagnostics": catalog.diagnostics(record), "execution_supported": True}

    def _agent_runtime(self):
        if self._native_runtime is None:
            from .graph_agent_runtime import GraphAgentRuntime, create_graph_chat_adapter
            self._native_runtime = GraphAgentRuntime(adapter_factory=self._model_factory or (
                lambda binding: create_graph_chat_adapter(binding, self._model_catalog)))
        return self._native_runtime

    def _legacy_refs(self, repo, sid):
        source = repo.get("workflow_session", workflow_session_id=sid)["source"]
        return source.get("legacy_archives", [])

    def _selected_history_heads(self, repo, sid, document):
        head = self._head(repo, sid)
        commit = repo.get("workflow_commit", commit_id=head["head_commit_id"])
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        return {node["node_binding_id"]: snapshot["node_states"].get(node["node_binding_id"], {}).get("head_turn_id")
                for node in document["nodes"]
                if (node["component_id"], node["component_version"]) == ("workflow.agent", "2")}

    def _content_dependencies(self, node, *, allow_inputs=False):
        return GraphResourceHost._content_dependencies(self, node, allow_inputs=allow_inputs)

    def _preflight_capabilities(self, repo, sid, plan):
        return GraphResourceHost._preflight_capabilities(self, repo, sid, plan)

    def _preflight_legacy_model(self, repo, node):
        from .graph_agent_runtime import resolve_graph_model
        from .model_configuration_store import ModelConfigurationStore
        return resolve_graph_model(node["config"], ModelConfigurationStore(repo.store),
                                   node_id=node["node_binding_id"])

    @staticmethod
    def _preflight_legacy_content(repo, identities):
        from .workbench_resources import WorkbenchResourceStore
        return WorkbenchResourceStore(repo.store).resolve_content(identities)

    @staticmethod
    def _legacy_archive(store, turn_id):
        return read_closed_legacy_archive(store, turn_id)

    def _resolve_archive(self, repo, sid, turn_id):
        return resolve_graph_archive(repo, sid, turn_id, legacy_reader=repo.store)

    def _context_archives(self, repo, sid, node_id, *, heads=None):
        document = self._document(repo, *(repo.get("workflow_session", workflow_session_id=sid)[key]
                                       for key in ("workflow_definition_id", "definition_revision")))
        node = next((node for node in document["nodes"] if node["node_binding_id"] == node_id), None)
        require(node is not None and node["component_id"] == "workflow.agent" and node["component_version"] == "2",
                "graph_context_source_invalid", "Context source must be an Agent in this definition")
        if heads is None:
            heads = self._selected_history_heads(repo, sid, document)
        require(node_id in heads, "graph_context_source_invalid", "Context has no selected history boundary", 409,
                node_id=node_id)
        cursor = heads[node_id]
        found, seen = [], set()
        while cursor is not None:
            require(cursor not in seen and len(seen) < 2048, "graph_context_invalid", "Archive chain is cyclic or too large")
            seen.add(cursor)
            archive = self._resolve_archive(repo, sid, cursor)
            found.append(archive)
            cursor = archive["turn"]["parent_turn_id"]
        return list(reversed(found))

    def _host_call(self, context, capability, operation, payload):
        sid = context.workflow_session_id
        if capability == "model:resolve" and operation == "resolve":
            from .graph_agent_runtime import resolve_graph_model
            result = context._external_inputs.get("_workflow_frozen_resources", {}).get("models", {}).get(context.node_binding_id)
            if result is None:
                with self._model_catalog() as catalog:
                    result = resolve_graph_model(payload["config"], catalog, node_id=context.node_binding_id)
            context.reads.append({"kind": "model_provider_read", "binding": result["binding"]})
            return result
        if capability == "agent:execute" and operation == "execute":
            return self._execute_agent(context, payload)
        if capability == "resources:read" and operation == "current-global-resource":
            return GraphResourceHost._read_current_resource(self, context, payload)
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            if capability == "resources:read" and operation == "global-content":
                require(type(payload) is dict and set(payload) == {"resource_id"} and uuid_value(payload["resource_id"]),
                        "graph_resource_reference_invalid", "Resource reads require an exact global content identity", 409,
                        node_id=context.node_binding_id)
                frozen = context._external_inputs.get("_workflow_frozen_resources", {}).get("content", {}).get(context.node_binding_id)
                # Older retained runs stored one record per node. Check its
                # identity rather than resolving a new live resource on resume.
                if type(frozen) is dict and frozen.get("kind") in ("global_prompt", "role_card"):
                    frozen = {frozen["resource_id"]: frozen}
                require(type(frozen) is dict and payload["resource_id"] in frozen,
                        "graph_resource_dependency_undeclared", "Requested resource was not frozen for this node", 409,
                        node_id=context.node_binding_id)
                record = frozen[payload["resource_id"]]
                require(record["resource_id"] == payload["resource_id"], "storage_contract_violation",
                        "Frozen resource identity differs from its dependency", 500, node_id=context.node_binding_id)
                return deepcopy(record)
            if capability == "history:read" and operation == "context":
                heads = context._external_inputs.get("_workflow_frozen_resources", {}).get("history_heads")
                archives = self._context_archives(repo, sid, payload["source_node_id"], heads=heads)
                context.reads.append({"kind": "context_read", "source_node_id": payload["source_node_id"],
                                      "turn_ids": [item["turn"]["turn_id"] for item in archives]})
                return archive_materials(archives)
            if capability == "history:read" and operation == "assemble":
                inputs = payload["inputs"]
                node_run = repo.get("node_run", run_id=context.node_run_id)
                references = node_run["input_refs"].get("current_input", [])
                require(len(references) == 1, "graph_input_reference_missing", "Assembly requires one durable current input")
                result = assemble_materials(inputs.get("input", []), inputs["current_input"]["text"],
                    input_id=references[0]["output_id"], limits=payload["config"],
                    resolve_archive=lambda identity: self._resolve_archive(repo, sid, identity))
                context.reads.append({"kind": "context_assembly", "archives": result["assembly"]["context_basis"]})
                return result
        raise graph_error("graph_capability_unknown", "Host operation is not registered")

    def _save_agent_evidence(self, sid, chain_id, run_id, kind, payload):
        with self._lock, closing(self._store()) as store:
            previous = GraphRecordStore(store).get("node_run", run_id=run_id)
        def change(repo):
            run = repo.get("node_run", run_id=run_id)
            session = repo.get("workflow_session", workflow_session_id=sid)
            require(session["active_chain_run_id"] == chain_id and run["status"] == "running",
                    "execution_owner_changed", "Agent callback has no active owner", 409)
            if kind == "prepare":
                require(run.get("agent") is None or run["agent"]["snapshot"] == payload,
                        "graph_agent_snapshot_changed", "Frozen Agent snapshot changed", 409)
                if run.get("agent") is None:
                    run["agent"] = {"snapshot": payload, "facts": [], "progress": {},
                                    "limits": deepcopy(run["config"]), "accepted": None}
            elif kind == "fact":
                run["agent"]["facts"].append(payload)
            elif kind == "progress":
                run["agent"]["progress"] = payload
            elif kind == "archive":
                require(run["agent"]["facts"] == payload["facts"], "graph_agent_fact_changed", "Accepted fact history differs")
                run["agent"].update(accepted=payload, limits=payload["limits"], progress=payload["progress"])
            run["revision"] += 1
            repo.put("node_run", run)
            return None
        self._change("graph.agent." + kind, f"{run_id}:{kind}:{previous['revision']}",
                     {"run_id": run_id, "kind": kind, "payload": payload}, change)

    def _execute_agent(self, context, payload):
        from .graph_agent_runtime import GraphAgentIdentity, GraphAgentCallbacks, make_graph_agent_snapshot
        from .graph_contracts import text_value, validate_content_value
        prompt = validate_content_value(payload["prompt"], "PROMPT")
        require(prompt["stage"] == "assembled", "graph_prompt_not_ready", "Agent requires a validated prompt assembly")
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            previous = repo.get("node_run", run_id=context.node_run_id).get("agent")
            identity = GraphAgentIdentity(context.workflow_session_id, context.node_binding_id,
                context.chain_run_id, context.node_run_id, self._head(repo, context.workflow_session_id)["head_commit_id"])
            if previous is not None:
                snapshot = previous["snapshot"]
                identity = GraphAgentIdentity(**snapshot["config"]["payload"]["graph_identity"])
                require((identity.workflow_session_id, identity.node_binding_id, identity.chain_run_id, identity.node_run_id)
                        == (context.workflow_session_id, context.node_binding_id, context.chain_run_id, context.node_run_id),
                        "execution_owner_changed", "Retained native identity differs from current graph scope", 409)
                require(snapshot["config"]["payload"]["graph_preparation"] == prompt["assembly"],
                        "graph_agent_snapshot_changed", "Resumed preparation differs from the frozen snapshot")
            else:
                # Revalidate source archives at the dispatch boundary as well as assembly.
                evidence = prompt["assembly"]
                rebuilt = assemble_materials([prompt], evidence["current_root"]["blocks"][0]["text"],
                    input_id=evidence["current_root"]["source"]["output_id"],
                    limits=evidence["prompt"]["manifest"]["limits"],
                    current_root=evidence["current_root"], prompt_message_ids=evidence["prompt"]["manifest"]["prompt_message_ids"],
                    resolve_archive=lambda turn_id: self._resolve_archive(repo, context.workflow_session_id, turn_id))
                require(rebuilt == prompt, "graph_prompt_not_ready", "Assembly differs from its graph materials and closed archives")
                source = evidence["current_root"]["source"]
                require(source["kind"] == "upstream_node", "graph_prompt_not_ready", "Current root has no graph output source")
                origin = repo.get("workflow_output", output_id=source["output_id"])
                require(origin["chain_run_id"] == context.chain_run_id and origin["payload"].get("kind") == "workflow.text"
                        and origin["payload"]["text"] == evidence["current_root"]["blocks"][0]["text"],
                        "graph_prompt_not_ready", "Current root differs from its durable text source")
                snapshot = make_graph_agent_snapshot(identity=identity,
                    s0=prompt["assembly"]["prompt"]["messages"],
                    parent_turn_id=context.private_read()["head_turn_id"],
                    model_binding=payload["model"]["binding"], config=payload["config"],
                    input_id=str(uuid4()), evidence=prompt["assembly"])
        callbacks = GraphAgentCallbacks(
            prepare=lambda value: self._save_agent_evidence(context.workflow_session_id, context.chain_run_id, context.node_run_id, "prepare", value),
            fact=lambda value: self._save_agent_evidence(context.workflow_session_id, context.chain_run_id, context.node_run_id, "fact", value),
            progress=lambda value: self._save_agent_evidence(context.workflow_session_id, context.chain_run_id, context.node_run_id, "progress", value),
            archive=lambda value: self._save_agent_evidence(context.workflow_session_id, context.chain_run_id, context.node_run_id, "archive", value),
            should_pause=lambda: context.chain_run_id in self._pauses or self._closed,
        )
        runtime = self._agent_runtime()
        if runtime.has_accepted(context.node_run_id):
            accepted = runtime.retry_archive(context.node_run_id, callbacks=callbacks).to_dict()
        else:
            accepted = runtime.execute(identity=identity, snapshot=snapshot, model_binding=payload["model"]["binding"],
                                       limits=previous["limits"] if previous else payload["config"], callbacks=callbacks).to_dict()
        turn = accepted["turn"]
        context.private_write({"head_turn_id": turn["turn_id"]})
        context.reads.append({"kind": "agent_snapshot", "snapshot_id": snapshot["snapshot_id"]})
        return {"output": text_value(turn["final"]["value"]["text"]),
                "output_json": text_value(json.dumps(turn["final"]["value"], ensure_ascii=False)),
                "context_delta": archive_materials([{"turn": turn, "root": [prompt["assembly"]["current_root"]]}])}

    def migrate_legacy(self, *, document, source_session_id, expected_source_revision, mappings, idempotency_key):
        from .graph_contracts import uuid4_string
        require(type(mappings) is list and all(type(item) is dict and set(item) == {"source_node_id", "target_node_id"}
                for item in mappings), "invalid_state_mapping", "Migration mapping fields differ")
        for mapping in mappings:
            uuid4_string(mapping["source_node_id"])
            uuid4_string(mapping["target_node_id"])
        request = {"document": document, "source_session_id": source_session_id,
                   "expected_source_revision": expected_source_revision, "mappings": mappings}
        def change(repo):
            from .graph_contracts import validate_graph_document
            validate_graph_document(document)
            source, private, state, refs = {"kind": "legacy_migration", "legacy_archives": []}, self._defaults(document), None, []
            if source_session_id is not None:
                from .contract_graph import validate_bundle
                validate_bundle(repo.store.read_bundle())
                legacy = validate_record("workflow_session", repo.store.get_record("workflow_session", {"workflow_session_id": source_session_id}))
                require(legacy["revision"] == expected_source_revision, "stale_revision", "Legacy session changed", 409)
                unsettled = [row for row in repo.store.list_records("chain_run")
                             if row["workflow_session_id"] == source_session_id and row["status"] not in ("succeeded", "closed")]
                require(not unsettled, "source_session_busy", "Unsettled legacy runtime cannot migrate", 409)
                old_head = next(row for row in repo.store.list_records("workflow_ref") if row["workflow_session_id"] == source_session_id)
                commit = repo.store.get_record("workflow_commit", {"commit_id": old_head["head_commit_id"]})
                frozen = repo.store.get_record("state_snapshot", {"state_snapshot_id": commit["state_snapshot_id"]})
                require(type(mappings) is list and len({m["target_node_id"] for m in mappings}) == len(mappings),
                        "invalid_state_mapping", "Migration mappings repeat targets")
                for mapping in mappings:
                    require(set(mapping) == {"source_node_id", "target_node_id"}, "invalid_state_mapping", "Migration mapping fields differ")
                    target = next((node for node in document["nodes"] if node["node_binding_id"] == mapping["target_node_id"]), None)
                    require(target is not None and target["component_id"] == "workflow.agent" and target["component_version"] == "2",
                            "invalid_state_mapping", "Migration target must be an ordinary Agent")
                    selected = next((node for node in frozen["node_states"] if node["node_binding_id"] == mapping["source_node_id"]), None)
                    require(selected is not None, "invalid_state_mapping", "Legacy source node is absent")
                    from .workflow import AGENT_COMPONENT, KERNEL_COMPONENT, VERSION
                    binding = repo.store.get_record("node_binding", {
                        "workflow_definition_id": legacy["workflow_definition_id"],
                        "workflow_definition_revision": legacy["definition_revision"],
                        "node_binding_id": mapping["source_node_id"],
                    })
                    require(binding is not None and binding["component_id"] == AGENT_COMPONENT,
                            "invalid_state_mapping", "Legacy source must be an Agent binding")
                    kernel = binding["config"]["payload"]["resolved"]["kernel"]
                    require((kernel["component_id"], kernel["component_version"]) == (KERNEL_COMPONENT, VERSION),
                            "graph_legacy_kernel_unsupported", "Legacy kernel needs an explicit migration adapter", 409,
                            node_id=mapping["source_node_id"])
                    head = selected["selected_result"]
                    require(head is None or head["kind"] == "agent_turn", "invalid_state_mapping", "Legacy source is not Agent history")
                    cursor = head["turn_id"] if head else None
                    turns = []
                    while cursor is not None:
                        require(cursor not in turns and len(turns) < 2048, "graph_context_invalid", "Legacy history is cyclic")
                        archive = self._legacy_archive(repo.store, cursor)
                        if not turns:
                            kernel = archive["snapshot"]["config"]["payload"]["resolved"]["kernel"]
                            require((kernel["component_id"], kernel["component_version"]) == (KERNEL_COMPONENT, VERSION),
                                    "graph_legacy_kernel_unsupported", "Selected legacy kernel needs an explicit migration adapter", 409,
                                    node_id=mapping["source_node_id"])
                        require(archive["snapshot"]["node_binding_id"] == mapping["source_node_id"],
                                "invalid_state_mapping", "Legacy history changes binding")
                        turns.append(cursor)
                        cursor = archive["turn"]["parent_turn_id"]
                    private[mapping["target_node_id"]] = {"head_turn_id": head["turn_id"] if head else None}
                    refs.append({**mapping, "turn_ids": turns})
                state = ProgramVariableStore(repo.store).current(source_session_id)
                source.update(source_session_id=source_session_id, source_revision=expected_source_revision,
                              head_commit_id=old_head["head_commit_id"], legacy_archives=refs)
            else:
                require(expected_source_revision is None and not mappings, "invalid_state_mapping", "No source means no inherited node state")
            saved = self._save_definition(repo, document, 0)
            session = self._new_session(repo, saved, state=state, private=private, source=source)
            return {"document": saved, "session": session, "provenance": source}
        return self._change("graph.legacy.migrate", idempotency_key, request, change)

    def get_agent_archive(self, sid, turn_id):
        with self._lock:
            return read_graph_archive(self.database, sid, turn_id)
