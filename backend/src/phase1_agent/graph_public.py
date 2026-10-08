"""Declared workflow consumers, independent of Agent and chat delivery."""

from contextlib import closing
from copy import deepcopy

from .contract_errors import ContractValidationError
from .graph_contracts import GraphCompiler
from .graph_history import history_node_mapping
from .graph_records import require, uuid_value
from .graph_store import GraphRecordStore


class GraphPublic:
    def _consumer_identity(self, session):
        return {"workflow_definition_id": session["workflow_definition_id"],
                "definition_revision": session["definition_revision"],
                "workflow_session_id": session["workflow_session_id"],
                "session_revision": session["revision"]}

    def _consumer_scope(self, repo, sid, identity=None, revision=None):
        require(uuid_value(sid), "not_found", "Session not found", 404)
        session = repo.get("workflow_session", workflow_session_id=sid)
        if identity is not None or revision is not None:
            require(uuid_value(identity) and type(revision) is int and revision >= 1,
                    "invalid_request", "An exact workflow definition is required")
            require(session["workflow_definition_id"] == identity
                    and session["definition_revision"] == revision,
                    "consumer_definition_mismatch", "Session belongs to another definition", 409)
        return session, self._document(repo, session["workflow_definition_id"], session["definition_revision"])

    def _consumer_ports(self, repo, node):
        record = repo.maybe("node_definition", component_id=node["component_id"],
                            component_version=node["component_version"])
        entry = self.registry.get(node["component_id"], node["component_version"])
        descriptor = record["descriptor"] if record else entry.definition.to_dict() if entry else None
        require(descriptor is not None, "graph_missing_node_type", "Node declaration is unavailable", 409)
        mode = descriptor.get("port_modes", {}).get(node["config"].get("mode"))
        require(not descriptor.get("port_modes") or mode is not None,
                "graph_invalid_config", "The node content mode is unavailable", 409)
        outputs = mode["outputs"] if mode else descriptor["outputs"]
        return {port["port_id"]: port for port in outputs}, descriptor

    def _public_declaration(self, repo, document, node_id, port_id):
        node = next((row for row in document["nodes"] if row["node_binding_id"] == node_id), None)
        require(node is not None and port_id in node.get("public_outputs", []),
                "output_not_public", "This output is not publicly declared", 403,
                node_id=node_id, port_id=port_id)
        ports, descriptor = self._consumer_ports(repo, node)
        require(port_id in ports, "output_not_public", "The declared output port is unavailable", 403,
                node_id=node_id, port_id=port_id)
        if set(descriptor["capabilities"]) & {"shared:read", "shared:write"} and "definition" in node["config"]:
            from .resource_contracts import validate_data_definition
            declaration = validate_data_definition(node["config"]["definition"])
            require(declaration["public"], "output_not_public", "Registered session data is private", 403,
                    node_id=node_id, port_id=port_id)
        return node, ports[port_id], descriptor["is_output"]

    def _public_observation(self, repo, sid, document, node_id, port_id, chain=None, run=None):
        node, port, is_output = self._public_declaration(repo, document, node_id, port_id)
        result = {"node_binding_id": node_id, "port_id": port_id, "data_type": port["data_type"],
                  "label": node["title"], "is_output": is_output, "status": "idle",
                  "availability": "unproduced", "reason_code": None, "run_id": None,
                  "chain_run_id": None, "payload": None, "source": None}
        if chain is None:
            return result
        require(chain["chain_run_id"] in self._history(repo, sid), "not_found", "Run is outside this session history", 404)
        mapping = history_node_mapping(repo, sid, chain)
        source_ids = [mapping[node_id]] if node_id in mapping else []
        if run is None:
            source_id = source_ids[0] if source_ids else None
            if source_id in chain["ordered_nodes"]:
                index = chain["ordered_nodes"].index(source_id)
                run = repo.get("node_run", run_id=chain["node_run_ids"][index])
        require(run is None or (run["chain_run_id"] == chain["chain_run_id"]
                and run["node_binding_id"] in source_ids), "not_found", "Run belongs to another node", 404)
        if run is None:
            return result
        original = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
        try:
            _, source_port, _ = self._public_declaration(repo, original, run["node_binding_id"], port_id)
            require((source_port["data_type"], source_port.get("data_schema_version", 1))
                    == (port["data_type"], port.get("data_schema_version", 1)),
                    "output_not_public", "Historical port type or schema differs", 403)
        except ContractValidationError:
            result.update(status=run["status"], availability="unavailable", reason_code="output_not_public")
            return result
        result.update(status=run["status"], run_id=run["run_id"], chain_run_id=chain["chain_run_id"],
                      source={"workflow_definition_id": chain["workflow_definition_id"],
                              "definition_revision": chain["definition_revision"],
                              "workflow_session_id": chain["workflow_session_id"],
                              "node_binding_id": run["node_binding_id"], "port_id": port_id,
                              "run_id": run["run_id"], "chain_run_id": chain["chain_run_id"]})
        output_id = run["output_refs"].get(port_id)
        if output_id is not None:
            output = repo.get("workflow_output", output_id=output_id)
            require((output["workflow_session_id"], output["chain_run_id"], output["run_id"],
                     output["node_binding_id"], output["port_id"])
                    == (chain["workflow_session_id"], chain["chain_run_id"], run["run_id"],
                        run["node_binding_id"], port_id),
                    "storage_contract_violation", "Public output reference differs from its producer", 500)
            version = port.get("data_schema_version", 1)
            if self.registry.data_types.get(port["data_type"], version, scope="content") is not None:
                payload = self.registry.validate_content(output["payload"], port["data_type"], version)
            else:
                # A disabled implementation cannot reinterpret saved payloads.
                # Historical read/export still uses the frozen producer identity.
                from .contract_json import validate_json_value
                validate_json_value(output["payload"])
                require(type(output["payload"]) is dict and output["payload"].get("schema_version") == version,
                        "storage_contract_violation", "Historical content envelope differs", 500)
                payload = deepcopy(output["payload"])
            result.update(availability="produced", payload=payload)
        return result

    def _consumer_inputs(self, document):
        try:
            plan = GraphCompiler(self.registry).compile(document)
            declarations = {}
            nodes = {node["node_binding_id"]: node for node in document["nodes"]}
            for node_id in plan.ordered_node_ids:
                node = nodes[node_id]
                entry = self.registry.get(node["component_id"], node["component_version"])
                callback = entry.external_inputs_declaration
                require(callback is not None or entry.external_inputs_validator is None,
                        "consumer_input_undeclared", "External inputs have no consumer declaration", 409, node_id=node_id)
                for value in callback(deepcopy(node["config"])) if callback else []:
                    require(type(value) is dict and set(value) == {"name", "data_type", "required"}
                            and type(value["name"]) is str and 0 < len(value["name"]) <= 128
                            and value["name"] != "_workflow_frozen_resources"
                            and value["data_type"] in ("TEXT", "PROMPT") and type(value["required"]) is bool,
                            "consumer_input_invalid", "Invalid external input declaration", 409, node_id=node_id)
                    previous = declarations.get(value["name"])
                    require(previous is None or previous["data_type"] == value["data_type"],
                            "consumer_input_conflict", "External input declarations differ", 409, node_id=node_id)
                    if previous is None:
                        declarations[value["name"]] = {**value, "node_ids": [node_id]}
                    else:
                        previous["required"] = previous["required"] or value["required"]
                        previous["node_ids"].append(node_id)
            return list(declarations.values()), []
        except Exception as error:
            return [], [{**deepcopy(value), "reason_code": value.get("reason_code", value.get("code", "graph_invalid_config"))}
                        for value in getattr(error, "diagnostics", [{"reason_code": getattr(error, "reason_code", "consumer_input_invalid"),
                                                                      "message": str(error)}])]

    def _consumer_view(self, repo, sid):
        session, document = self._consumer_scope(repo, sid)
        private_view = self._view(repo, sid)
        chain = self._selected_chain(repo, sid)
        if private_view.get("readonly"):
            inputs, input_diagnostics = [], []
        else:
            inputs, input_diagnostics = self._consumer_inputs(document)
        diagnostics = deepcopy(input_diagnostics)
        outputs = []
        for node in document["nodes"]:
            for port_id in node.get("public_outputs", []):
                try:
                    outputs.append(self._public_observation(repo, sid, document, node["node_binding_id"], port_id, chain))
                except ContractValidationError as error:
                    diagnostics.extend(deepcopy(getattr(error, "diagnostics", [])))
        history = [repo.get("chain_run", chain_run_id=identity) for identity in self._history(repo, sid)]
        nodes = [{key: deepcopy(node[key]) for key in (
            "node_binding_id", "label", "status", "run_id", "revision", "diagnostic", "budget")}
                 for node in private_view["nodes"]]
        pausing = (private_view["status"] in ("running", "prepared")
                   and session["active_chain_run_id"] in self._pauses)
        return {"schema_version": 1, "kind": "workflow.consumer", **self._consumer_identity(session),
                "status": "pausing" if pausing else private_view["status"],
                "can_submit": private_view["can_submit"] and not input_diagnostics,
                "available_actions": [] if pausing else private_view["available_actions"], "inputs": inputs, "nodes": nodes,
                "outputs": outputs, "diagnostics": diagnostics,
                "history": [{"workflow_definition_id": row["workflow_definition_id"],
                             "definition_revision": row["definition_revision"],
                             "workflow_session_id": row["workflow_session_id"],
                             "chain_run_id": row["chain_run_id"], "status": row["status"],
                             "revision": row["revision"], "inherited": row["workflow_session_id"] != sid}
                            for row in history]}

    def get_consumer(self, sid):
        with self._lock, closing(self._store()) as store:
            return self._consumer_view(GraphRecordStore(store), sid)

    def consumer_definition(self, identity):
        with self._lock, closing(self._store()) as store:
            document = self._document(GraphRecordStore(store), identity)
            return {"schema_version": 1, "kind": "workflow.consumer-definition",
                    "workflow_definition_id": identity, "definition_revision": document["revision"], "name": document["name"]}

    def consumer_frontend_extensions(self, sid, *, workflow_definition_id, definition_revision, node_id, port_id):
        """Return consumer bindings only for an exact currently public root."""
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session, document = self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)
            require(uuid_value(node_id) and type(port_id) is str and 0 < len(port_id) <= 128,
                    "invalid_request", "An exact node and port are required")
            _, port, _ = self._public_declaration(repo, document, node_id, port_id)
            target = {"scope": "content", "type_id": port["data_type"],
                      "schema_version": port.get("data_schema_version", 1)}
            extensions = [deepcopy(row) for row in self._frontend_extensions
                          if row.get("schema_version") == 2 and row["kind"] == "consumer"
                          and row["binding"] == {"surface": "consumer", "slot": "public-output", "target": target}]
            return {"schema_version": 1, "kind": "workflow.consumer-frontend-extensions",
                    **self._consumer_identity(session), "node_id": node_id, "port_id": port_id,
                    "package_lock": deepcopy(list(self.registry.package_lock)), "frontend_extensions": extensions}

    def list_consumer_sessions(self, identity):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            self._document(repo, identity)
            return [{**self._consumer_identity(row), "status": self._view(repo, row["workflow_session_id"])["status"]}
                    for row in repo.rows("workflow_session") if row["workflow_definition_id"] == identity]

    def list_consumer_candidates(self, sid, *, workflow_definition_id, definition_revision):
        """Project only usable completed rounds inside the bound workflow identity."""
        from .program_variable_store import ProgramVariableStore
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session, _ = self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)
            view = self._view(repo, sid)
            candidates = self.list_graph_candidates(sid)
            available = session["active_chain_run_id"] is None and not view["readonly"]
            pausing = (view["status"] in ("running", "prepared")
                       and session["active_chain_run_id"] in self._pauses)
            return {
                "schema_version": 1, "kind": "workflow.consumer-candidates",
                **self._consumer_identity(session),
                "data_revision": ProgramVariableStore(store).current(sid)["revision"],
                "head_revision": self._head(repo, sid)["revision"],
                "status": "pausing" if pausing else view["status"],
                "can_fork": available,
                "candidates": [{key: deepcopy(row[key]) for key in (
                    "candidate_id", "chain_run_id", "source_session_id",
                    "source_workflow_definition_id", "source_definition_revision")}
                    for row in candidates["candidates"]
                    if available and row["can_select"] and row["diagnostic"] is None
                    and row["source_workflow_definition_id"] == workflow_definition_id],
            }

    def _consumer_fork_receipt(self, result, sid, candidate_id, key):
        source = result["source"]
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            commit = repo.get("workflow_commit", commit_id=candidate_id)
            chain = repo.get("chain_run", chain_run_id=commit["source"]["chain_run_id"])
            require(source["kind"] == "fork_candidate" and source["workflow_session_id"] == sid
                    and source["candidate_commit_id"] == candidate_id
                    and commit["source"]["kind"] == "completed_execution" and chain["status"] == "succeeded"
                    and chain.get("execution_kind", "round") == "round"
                    and (result["workflow_definition_id"], result["definition_revision"])
                    == (chain["workflow_definition_id"], chain["definition_revision"]),
                    "storage_contract_violation", "Fork receipt must identify its actual completed source", 500)
            fork_source = {
                "workflow_session_id": sid, "candidate_id": candidate_id,
                "chain_run_id": chain["chain_run_id"],
                "workflow_definition_id": chain["workflow_definition_id"],
                "definition_revision": chain["definition_revision"],
            }
        return {
            "schema_version": 1, "kind": "workflow.consumer.receipt",
            "receipt": {
                **self._consumer_identity(result), "status": result["status"],
                "chain_run_id": result["active_chain_run_id"], "operation": "fork",
                "idempotency_key": key, "fork_source": fork_source,
            },
            "consumer": self.get_consumer(result["workflow_session_id"]),
        }

    def fork_consumer_candidate(self, sid, *, candidate_id, expected_revision,
                                expected_data_revision, expected_head_revision, idempotency_key):
        """The native fork owns CAS, inheritance and idempotent creation."""
        from .graph_application_identity import (
            APPLICATION_IDENTITY_OPERATION, application_command_identity, application_identity_key,
        )
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            # A replay retains the original accepted target even after the parent changes.
            prior = store.read_receipt_with_digest("graph.candidate.fork", idempotency_key)
            if prior is not None:
                request = {
                    "session_id": sid, "candidate_id": candidate_id,
                    "expected_revision": expected_revision, "expected_data_revision": expected_data_revision,
                    "expected_head_revision": expected_head_revision, "idempotency_key": idempotency_key,
                }
                identity = application_command_identity("consumer.candidate.fork", "consumer", request)
                require(prior[0] == identity["native_receipt"]["request_digest"],
                        "idempotency_conflict", "The key has a different request", 409)
                origin = store.read_receipt_with_digest(
                    APPLICATION_IDENTITY_OPERATION,
                    application_identity_key("consumer.candidate.fork", "consumer", idempotency_key))
                require(origin is not None and origin[0] == "workflow-op-v1:" + identity["request_sha256"]
                        and repo.equal(origin[1], [{"application_identity": identity}]),
                        "consumer_candidate_receipt_origin_mismatch",
                        "Consumer fork replay requires its own exact persisted request origin", 403)
            else:
                session = repo.get("workflow_session", workflow_session_id=sid)
                require(self._document_available(repo, self._document(
                    repo, session["workflow_definition_id"], session["definition_revision"])),
                    "consumer_candidate_unavailable", "The bound consumer workflow is unavailable", 409)
                _, chain, _ = self._candidate_record(repo, sid, candidate_id)
                require(chain["workflow_definition_id"] == session["workflow_definition_id"],
                        "consumer_candidate_scope_mismatch",
                        "Consumer forks remain inside their bound workflow identity", 403)
            result = self.fork_graph_candidate(
                sid, candidate_id=candidate_id, expected_revision=expected_revision,
                expected_data_revision=expected_data_revision,
                expected_head_revision=expected_head_revision, idempotency_key=idempotency_key)
            return self._consumer_fork_receipt(result, sid, candidate_id, idempotency_key)

    def public_outputs(self, sid):
        consumer = self.get_consumer(sid)
        return {"schema_version": 1, "kind": "workflow.public-outputs",
                **{key: consumer[key] for key in ("workflow_definition_id", "definition_revision", "workflow_session_id", "session_revision")},
                "outputs": consumer["outputs"]}

    def read_public_output(self, sid, *, workflow_definition_id, definition_revision, node_id, port_id, run_id=None):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            session, document = self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)
            require(uuid_value(node_id) and type(port_id) is str and 0 < len(port_id) <= 128,
                    "invalid_request", "An exact node and port are required")
            self._public_declaration(repo, document, node_id, port_id)
            run = None
            if run_id is not None:
                require(uuid_value(run_id), "invalid_request", "Invalid run identity")
                run = repo.get("node_run", run_id=run_id)
                require(run["chain_run_id"] in self._history(repo, sid), "not_found", "Run is outside this session history", 404)
                chain = repo.get("chain_run", chain_run_id=run["chain_run_id"])
            else:
                chain = self._selected_chain(repo, sid)
            result = self._public_observation(repo, sid, document, node_id, port_id, chain, run)
            require(result["availability"] != "unavailable", "output_not_public", "Historical output is not publicly declared", 403)
            return {"schema_version": 1, "kind": "workflow.public-output", **self._consumer_identity(session),
                    "node_id": node_id, "port_id": port_id, "output": result}

    def public_output_history(self, sid, *, workflow_definition_id, definition_revision, node_id, port_id):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            session, document = self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)
            require(uuid_value(node_id) and type(port_id) is str and 0 < len(port_id) <= 128,
                    "invalid_request", "An exact node and port are required")
            self._public_declaration(repo, document, node_id, port_id)
            outputs = [self._public_observation(repo, sid, document, node_id, port_id,
                       repo.get("chain_run", chain_run_id=identity)) for identity in self._history(repo, sid)]
            return {"schema_version": 1, "kind": "workflow.public-history", **self._consumer_identity(session),
                    "node_id": node_id, "port_id": port_id,
                    "outputs": [row for row in outputs if row["run_id"] is not None and row["availability"] != "unavailable"]}

    def read_public_artifact(self, sid, *, workflow_definition_id, definition_revision,
                             node_id, port_id, run_id, reference):
        """Read only a registered direct export from an exact public output.

        Both current and original declarations authorize the root. Retention
        closures, object permissions and diagnostic inputs do not grant access.
        """
        from .host_sdk import validate_reference
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact" and uuid_value(run_id)
                and uuid_value(node_id) and type(port_id) is str and 0 < len(port_id) <= 128,
                "invalid_request", "Exact public output and artifact identities are required")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session, document = self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)
            run = repo.get("node_run", run_id=run_id)
            history = self._history(repo, sid)
            require(run["chain_run_id"] in history, "output_reference_denied",
                    "Public root is outside authorized session history", 403)
            chain = repo.get("chain_run", chain_run_id=run["chain_run_id"])
            observation = self._public_observation(repo, sid, document, node_id, port_id, chain, run)
            require(observation["availability"] == "produced", "output_reference_denied",
                    "An accepted currently and originally public output is required", 403)
            root_ref = {"scope": "artifact", "output_id": run["output_refs"][port_id]}
            objects = self._objects(repo)
            root = objects._resolve_write_artifact(sid, root_ref, history=history)
            _, root_port, _ = self._public_declaration(repo, document, node_id, port_id)
            exported = self.registry.data_types.public_artifact_references(
                root_port["data_type"], root_port.get("data_schema_version", 1), root["value"])
            require(reference in exported, "output_reference_denied",
                    "Artifact is not a directly exported public reference", 403)
            target = repo.get("workflow_output", output_id=reference["output_id"])
            require(target["chain_run_id"] in history, "output_reference_denied",
                    "Referenced artifact is outside authorized session history", 403)
            accepted = objects._resolve_write_artifact(sid, reference, history=history)
            target_chain = repo.get("chain_run", chain_run_id=target["chain_run_id"])
            original = self._document(repo, target_chain["workflow_definition_id"], target_chain["definition_revision"])
            producer_node = next(row for row in original["nodes"]
                                 if row["node_binding_id"] == target["node_binding_id"])
            ports, _ = self._consumer_ports(repo, producer_node)
            require(target["port_id"] in ports, "storage_contract_violation",
                    "Referenced producer port is missing", 500)
            port = ports[target["port_id"]]
            return {"schema_version": 1, "kind": "workflow.public-artifact", **self._consumer_identity(session),
                    "node_id": node_id, "port_id": port_id, "run_id": run_id,
                    "reference": deepcopy(reference), "data_type": port["data_type"],
                    "data_schema_version": port.get("data_schema_version", 1),
                    "value": accepted["value"], "producer": accepted["producer"]}

    def _consumer_receipt(self, result, operation, key):
        return {"schema_version": 1, "kind": "workflow.consumer.receipt",
                "receipt": {**self._consumer_identity(result), "status": result["status"],
                            "chain_run_id": result["active_chain_run_id"], "operation": operation, "idempotency_key": key},
                "consumer": self.get_consumer(result["workflow_session_id"])}

    def create_consumer_session(self, **request):
        result = self.create_session(**request)
        return self._consumer_receipt(result, "create", request["idempotency_key"])

    def start_consumer(self, sid, **request):
        result = self.start(sid, **request)
        return self._consumer_receipt(result, "start", request["idempotency_key"])

    def control_consumer(self, sid, **request):
        result = self.control(sid, **request)
        return self._consumer_receipt(result, "control", request["idempotency_key"])
