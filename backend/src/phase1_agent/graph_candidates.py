"""Stable graph result selection and forks over immutable completion commits."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

from .graph_records import graph_error, require
from .graph_store import GraphRecordStore
from .program_variable_store import ProgramVariableStore


class GraphCandidateHost:
    def _selected_chain(self, repo, sid):
        session = repo.get("workflow_session", workflow_session_id=sid)
        if session["active_chain_run_id"] is not None:
            active = repo.get("chain_run", chain_run_id=session["active_chain_run_id"])
            if active.get("execution_kind", "round") == "round":
                return active
        commit_id = self._head(repo, sid)["head_commit_id"]
        seen = set()
        while commit_id is not None:
            require(commit_id not in seen, "storage_contract_violation", "Selected history is cyclic", 500)
            seen.add(commit_id)
            commit = repo.get("workflow_commit", commit_id=commit_id)
            source = commit["source"]
            chain_id = source.get("chain_run_id")
            if chain_id is not None:
                chain = repo.get("chain_run", chain_run_id=chain_id)
                require(chain_id in self._history(repo, sid), "storage_contract_violation", "Selected execution escaped frozen history", 500)
                if (chain["workflow_session_id"] == sid
                        and (chain["workflow_definition_id"], chain["definition_revision"])
                            != (session["workflow_definition_id"], session["definition_revision"])
                        and source.get("kind") != "select_execution_candidate"):
                    return None
                if chain.get("execution_kind", "round") == "round":
                    return chain
                commit_id = commit["parent_commit_id"]
                continue
            if source.get("kind") == "session_seed":
                origin = source.get("source") or {}
                commit_id = (origin.get("candidate_commit_id", origin.get("head_commit_id"))
                             if origin.get("kind") in ("copy_current", "fork_candidate") else None)
            else:
                commit_id = commit["parent_commit_id"]
        return None

    def _candidate_record(self, repo, sid, candidate_id):
        require(type(candidate_id) is str, "invalid_request", "Candidate identity is required")
        commit = repo.get("workflow_commit", commit_id=candidate_id)
        require(commit["source"].get("kind") == "completed_execution",
                "graph_candidate_invalid", "Only a completed execution is a selectable candidate", 409)
        chain = repo.get("chain_run", chain_run_id=commit["source"]["chain_run_id"])
        require(chain["chain_run_id"] in self._history(repo, sid),
                "graph_candidate_scope_mismatch", "Candidate is outside frozen session history", 403)
        require(chain["status"] == "succeeded" and chain.get("execution_kind", "round") == "round"
                and commit["workflow_session_id"] == chain["workflow_session_id"],
                "graph_candidate_invalid", "Candidate has no complete execution", 409)
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        require((snapshot["workflow_definition_id"], snapshot["definition_revision"])
                == (chain["workflow_definition_id"], chain["definition_revision"]),
                "storage_contract_violation", "Candidate definition differs from its execution", 500)
        return commit, chain, snapshot

    def _candidate_state(self, repo, session, chain, snapshot):
        from .graph_contracts import GraphCompiler

        sid = session["workflow_session_id"]
        try:
            document = self._document(repo, snapshot["workflow_definition_id"], snapshot["definition_revision"])
            plan = GraphCompiler(self.registry).compile(document)
            for node in document["nodes"]:
                node_id = node["node_binding_id"]
                if node_id not in plan.definitions and node_id not in snapshot["node_states"]:
                    continue
                entry = self.registry.get(node["component_id"], node["component_version"])
                saved = repo.maybe("node_definition", component_id=node["component_id"],
                                   component_version=node["component_version"])
                require(entry is not None and saved is not None
                        and repo.equal(saved["descriptor"], self._runtime_descriptor(entry)),
                        "node_definition_changed", "Historical node implementation differs from its saved declaration", 409,
                        node_id=node_id)
                if node_id in snapshot["node_states"]:
                    self.registry.validate_private_state(entry.definition, snapshot["node_states"][node_id])
        except Exception as exc:
            raise graph_error("historical_definition_unavailable",
                "Checkpoint workflow definition is unavailable: " + str(exc), 409,
                workflow_definition_id=snapshot["workflow_definition_id"],
                definition_revision=snapshot["definition_revision"],
                cause_reason_code=getattr(exc, "reason_code", getattr(exc, "code", "graph_invalid_definition"))) from exc
        mapping = {node["node_binding_id"]: node["node_binding_id"] for node in document["nodes"]}
        source = {"workflow_session_id": snapshot["workflow_session_id"],
                  "workflow_definition_id": snapshot["workflow_definition_id"],
                  "definition_revision": snapshot["definition_revision"]}
        private = deepcopy(snapshot["node_states"])
        require(set(private) <= set(mapping), "storage_contract_violation",
                "Checkpoint private state has no historical owner", 500)
        state = (ProgramVariableStore(repo.store).read(source["workflow_session_id"], snapshot["data_revision"])
                 if snapshot["data_revision"] else {"revision": 0, "values": {}})
        manifest = self._manifests(repo).get("state_snapshot", snapshot["state_snapshot_id"])
        object_keys = {item["object_key"] for item in document.get("object_bindings", [])}
        require(manifest is not None or not object_keys, "state_migration_required",
                "Old checkpoint has no registered-object manifest", 409)
        if manifest is not None:
            require((manifest["workflow_session_id"], manifest["workflow_definition_id"],
                     manifest["definition_revision"], manifest["legacy_data_revision"])
                    == (snapshot["workflow_session_id"], snapshot["workflow_definition_id"],
                        snapshot["definition_revision"], snapshot["data_revision"])
                    and repo.equal(manifest["legacy_node_states"], snapshot["node_states"])
                    and manifest["history_refs"] == snapshot["history_refs"],
                    "storage_contract_violation", "Checkpoint manifest state differs", 500)
            values = self._objects(repo).at_manifest(manifest["objects"])
            require(set(values) == object_keys, "state_migration_required",
                    "Candidate object declarations differ", 409)
            for binding in document.get("object_bindings", []):
                value = values[binding["object_key"]]
                require((value["type_id"], value["schema_version"])
                        == (binding["type_id"], binding["schema_version"])
                        and value["binding"] == binding,
                        "storage_contract_violation", "Checkpoint object declaration differs from its definition", 500)
                if not value["deleted"] and manifest["workflow_session_id"] != sid:
                    self.registry.data_types.remap(value["type_id"], value["schema_version"], value["value"],
                        {"workflow_session_id": {manifest["workflow_session_id"]: sid},
                         "node_binding_id": {source: target for target, source in mapping.items()}}, scope="session")
        return document, state, private

    def list_graph_candidates(self, sid):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session = repo.get("workflow_session", workflow_session_id=sid)
            visible = set(self._history(repo, sid))
            selected = self._selected_chain(repo, sid)
            candidates = []
            for commit in repo.rows("workflow_commit"):
                if (commit["source"].get("kind") != "completed_execution"
                        or commit["source"].get("chain_run_id") not in visible):
                    continue
                _, chain, snapshot = self._candidate_record(repo, sid, commit["commit_id"])
                diagnostic = None
                try:
                    self._candidate_state(repo, session, chain, snapshot)
                except Exception as exc:
                    diagnostic = {"reason_code": getattr(exc, "reason_code", "state_migration_required"),
                                  "message": str(exc)}
                candidates.append({"candidate_id": commit["commit_id"], "chain_run_id": chain["chain_run_id"],
                    "source_session_id": chain["workflow_session_id"],
                    "source_workflow_definition_id": chain["workflow_definition_id"],
                    "source_definition_revision": chain["definition_revision"],
                    "data_revision": snapshot["data_revision"],
                    "selected": selected is not None and selected["chain_run_id"] == chain["chain_run_id"],
                    "can_select": session["active_chain_run_id"] is None and diagnostic is None,
                    "diagnostic": diagnostic})
            return {"schema_version": 1, "kind": "workflow.graph-candidates", "workflow_session_id": sid,
                    "workflow_definition_id": session["workflow_definition_id"],
                    "definition_revision": session["definition_revision"], "session_revision": session["revision"],
                    "head_revision": self._head(repo, sid)["revision"], "candidates": candidates}

    def select_graph_candidate(self, sid, *, candidate_id, expected_revision, expected_data_revision,
                               expected_head_revision, idempotency_key):
        request = {"session": sid, "candidate_id": candidate_id, "expected_revision": expected_revision,
                   "expected_data_revision": expected_data_revision, "expected_head_revision": expected_head_revision}

        def change(repo):
            session = self._stable(repo, sid, expected_revision, expected_data_revision, expected_head_revision)
            commit, chain, snapshot = self._candidate_record(repo, sid, candidate_id)
            document, state, private = self._candidate_state(repo, session, chain, snapshot)
            variables = ProgramVariableStore(repo.store)
            current = variables.current(sid)
            if not repo.equal({key: value for key, value in current.items() if key != "revision"},
                              {key: value for key, value in state.items() if key != "revision"}):
                variables.write_in_transaction(sid, state, expected_revision=current["revision"],
                    key="graph-candidate:" + str(uuid4()), request=request)
            for node in repo.rows("node_session"):
                if node["workflow_session_id"] == sid and node["node_binding_id"] not in private:
                    repo.remove_node_state(sid, node["node_binding_id"])
            session["workflow_definition_id"] = document["workflow_definition_id"]
            session["definition_revision"] = document["revision"]
            self._save_private(repo, sid, document, private)
            manifest = self._manifests(repo).get("state_snapshot", snapshot["state_snapshot_id"])
            self._objects(repo).restore(sid, document, manifest["objects"] if manifest else {},
                source_session_id=snapshot["workflow_session_id"], history=snapshot["history_refs"],
                node_mapping={node["node_binding_id"]: node["node_binding_id"] for node in document["nodes"]},
                restore_bindings=True)
            session["revision"] += 1
            repo.put("workflow_session", session)
            self._commit(repo, session, source={"kind": "select_execution_candidate", "candidate_commit_id": commit["commit_id"],
                                               "chain_run_id": chain["chain_run_id"]})
            return self._view(repo, sid)

        return self._change("graph.candidate.select", idempotency_key, request, change)

    def fork_graph_candidate(self, sid, *, candidate_id, expected_revision, expected_data_revision,
                             expected_head_revision, idempotency_key):
        request = {"session": sid, "candidate_id": candidate_id, "expected_revision": expected_revision,
                   "expected_data_revision": expected_data_revision, "expected_head_revision": expected_head_revision}

        def change(repo):
            session = self._stable(repo, sid, expected_revision, expected_data_revision, expected_head_revision)
            commit, chain, snapshot = self._candidate_record(repo, sid, candidate_id)
            document, state, private = self._candidate_state(repo, session, chain, snapshot)
            source = {"kind": "fork_candidate", "workflow_session_id": sid,
                      "object_source_session_id": snapshot["workflow_session_id"],
                      "workflow_definition_id": document["workflow_definition_id"],
                      "definition_revision": document["revision"],
                      "head_commit_id": commit["commit_id"],
                      "candidate_commit_id": commit["commit_id"], "state_mappings": []}
            manifest = self._manifests(repo).get("state_snapshot", snapshot["state_snapshot_id"])
            objects = self._objects(repo).at_manifest(manifest["objects"]) if manifest else None
            return self._new_session(repo, document, state=state, private=private, source=source,
                                     objects=objects,
                                     history=deepcopy(snapshot["history_refs"]))

        return self._change("graph.candidate.fork", idempotency_key, request, change)
