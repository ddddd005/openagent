"""Public management facade for registered objects, resources and capabilities."""

from contextlib import closing
from copy import deepcopy

from .graph_records import require
from .graph_store import GraphRecordStore
from .session_objects import SessionObjectStore, StateManifestStore


class GraphPlatform:
    def _objects(self, repo):
        return SessionObjectStore(repo.store, self.registry.data_types)

    def _manifests(self, repo):
        return StateManifestStore(repo.store, self.registry.data_types)

    def _resource_identities(self, document):
        resources = []
        for node in document["nodes"]:
            entry = self.registry.get(node["component_id"], node["component_version"])
            if entry is None or entry.resource_dependencies_declaration is None:
                continue
            try:
                self.registry.validate_config(entry, node["config"])
                dependencies = entry.resource_dependencies_declaration(deepcopy(node["config"]))
            except Exception:
                # Saving an incomplete, dormant node remains legal. Runtime
                # preflight validates participating nodes and their identities.
                continue
            for dependency in dependencies if type(dependencies) is list else []:
                if type(dependency) is not dict:
                    continue
                if dependency.get("kind") == "global-resource":
                    from .host_sdk import ResourceIdentity
                    try:
                        identity = ResourceIdentity.from_dict(dependency["reference"]).to_dict()
                    except Exception:
                        continue
                else:
                    continue
                if identity not in resources:
                    resources.append(identity)
        return resources

    def _save_manifest(self, repo, kind, identity, session, *, history=None, status=None):
        from .program_variable_store import ProgramVariableStore
        document = self._document(repo, session["workflow_definition_id"], session["definition_revision"])
        return self._manifests(repo).save(
            kind, identity, session=session,
            data_revision=ProgramVariableStore(repo.store).current(session["workflow_session_id"])["revision"],
            node_states=self._private(repo, session["workflow_session_id"]),
            history_refs=history if history is not None else self._history(repo, session["workflow_session_id"]),
            package_lock=document.get("package_lock", self.registry.execution_package_lock),
            global_resources=self._resource_identities(document), status=status,
        )

    def platform_capabilities(self):
        return {"envelope_version": 1, "host_protocol_version": 1,
                "package_lock": deepcopy(list(self.registry.package_lock)),
                "execution_package_lock": deepcopy(list(self.registry.execution_package_lock)),
                "data_types": self.registry.data_types.catalog(),
                "executors": self.registry.executors.catalog(),
                "pause_support": self.registry.executors.pause_catalog(),
                "services": self.registry.services.catalog(),
                "package_diagnostics": deepcopy(self._package_diagnostics),
                "frontend_extensions": deepcopy(list(getattr(self, "_frontend_extensions", ())))}

    def configure_capability_packages(self, enabled_packages):
        """Project management operation; no executor replacement during active runs."""
        require(self._package_loader is not None, "package_management_unavailable",
                "This service was constructed with a custom registry", 409)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            active = [row for row in repo.rows("workflow_session")
                      if row["active_chain_run_id"] is not None]
            retained = (any(not future.done() for future in self._futures.values())
                        or self._pauses or self._resuming or self._resource_frames
                        or self._execution_registries or self._runtime_hosts or self._service_runs
                        or self._acceptance_candidates or self._failed_retry_candidates
                        or self._information_routers)
            require(not active and not retained, "package_change_during_execution",
                    "Settle active executions before changing project packages", 409)
            loaded = self._load_current_capabilities(enabled_packages)
            from .graph_package_selection import GraphPackageSelectionStore
            from .type_contract_store import TypeContractStore
            try:
                store._connection.execute("BEGIN IMMEDIATE")
                TypeContractStore(store).check_registry(loaded.registry.data_types)
                GraphPackageSelectionStore(store).write_current(enabled_packages)
                store._connection.execute("COMMIT")
            except BaseException:
                if store._connection.in_transaction:
                    store._connection.execute("ROLLBACK")
                raise
            self.registry = loaded.registry.detached()
            self._frontend_extensions = loaded.frontend_extensions
            self._package_manifests = loaded.package_manifests
            self._package_diagnostics = []
            return self.platform_capabilities()

    def get_session_objects(self, sid):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            repo.get("workflow_session", workflow_session_id=sid)
            return {"envelope_version": 1, "workflow_session_id": sid,
                    "objects": self._objects(repo).current(sid)}

    def read_session_object(self, sid, *, object_key, node_id, revision_id=None):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            repo.get("workflow_session", workflow_session_id=sid)
            return self._objects(repo).read(sid, object_key, node_id=node_id, revision_id=revision_id)

    def update_session_objects(self, sid, *, node_id, writes, expected_revision, idempotency_key):
        request = {"session": sid, "node_id": node_id, "writes": writes,
                   "expected_revision": expected_revision}

        def change(repo):
            session = self._stable(repo, sid, expected_revision)
            document = self._document(repo, session["workflow_definition_id"], session["definition_revision"])
            require(any(node["node_binding_id"] == node_id for node in document["nodes"]),
                    "session_object_access_denied", "Writer is not in this workflow", 403)
            result = self._objects(repo).apply(sid, node_id=node_id, writes=writes)
            session["revision"] += 1
            repo.put("workflow_session", session)
            # Editing working state does not manufacture a successful candidate.
            self._save_manifest(repo, "object_edit", idempotency_key + ":" + sid, session, status="state_edit")
            return {"results": result, "session": self._view(repo, sid)}

        return self._change("graph.objects.update", idempotency_key, request, change)

    def adopt_context_view(self, sid, *, node_id, view_output_id, expected_revision, idempotency_key):
        """Retry only a proven accepted view after explicitly settling a failed run."""
        from .context_package import prepare_context_adoption, validate_context_adoption_proof
        from .host_sdk import ObjectBinding
        request = {"session": sid, "node_id": node_id, "view_output_id": view_output_id,
                   "expected_revision": expected_revision}

        def change(repo):
            session = self._stable(repo, sid, expected_revision)
            document = self._document(repo, session["workflow_definition_id"], session["definition_revision"])
            writer = next((node for node in document["nodes"] if node["node_binding_id"] == node_id), None)
            require(writer is not None and (writer["component_id"], writer["component_version"])
                    in (("context.save", "1"), ("context.merge", "1"), ("context.merge", "2"),
                        ("context.merge", "4")), "context_adoption_writer_denied",
                    "Adoption requires this workflow's declared context writer", 403)
            bound = writer["component_id"] == "context.merge"
            summary_aware = bound and writer["component_version"] == "2"
            native = bound and writer["component_version"] == "4"
            key = writer["config"]["object_key"]
            objects = self._objects(repo)
            current = objects.read(sid, key, node_id=node_id)
            objects.authorize(ObjectBinding.from_dict(current["binding"]), node_id, write=True)
            output = repo.get("workflow_output", output_id=view_output_id)
            producer = repo.get("node_run", run_id=output["run_id"])
            require(output["workflow_session_id"] == sid and output["chain_run_id"] in self._history(repo, sid)
                    and producer["status"] == "succeeded"
                    and producer["output_refs"].get(output["port_id"]) == view_output_id,
                    "context_adoption_output_denied", "View is not accepted in this session history", 403)
            original = self._document_for_runtime_output(repo, output)
            source = next(node for node in original["nodes"]
                          if node["node_binding_id"] == output["node_binding_id"])
            require(source["component_version"] == ("4" if native else "2" if summary_aware else "1"), "context_adoption_source_invalid",
                    "View producer implementation version is unsupported")
            view_ref = {"scope": "artifact", "output_id": view_output_id}
            allowed = self._artifact_closure(repo, [view_ref])

            def resolve(reference):
                require(reference["scope"] == "artifact" and reference["output_id"] in allowed,
                        "context_reference_denied", "Adoption reference escapes its accepted closure", 403)
                return repo.get("workflow_output", output_id=reference["output_id"])["payload"]

            prepare = prepare_context_adoption
            if bound:
                from .context_v2 import (
                    prepare_bound_context_adoption, validate_bound_context_artifacts,
                )
                require(source["component_id"] == "context.merge"
                        and output["node_binding_id"] == node_id
                        and output["port_id"] == "output", "context_adoption_source_invalid",
                        "Bound adoption requires the same merge node's accepted context candidate")
                if native:
                    from .context_v4 import prepare_native_context_adoption, prove_native_context_view

                    def resolve_detail(reference):
                        resolve(reference)
                        return objects._resolve_write_artifact(sid, reference)

                    view = prove_native_context_view(view_ref, resolve_detail)
                    prepare = prepare_native_context_adoption
                elif summary_aware:
                    from .context_v3 import prepare_effective_view_adoption, prove_effective_view

                    def resolve_detail(reference):
                        resolve(reference)  # Restrict metadata to the same authorized artifact closure.
                        return objects._resolve_write_artifact(sid, reference)

                    view = prove_effective_view(view_ref, resolve_detail)
                    prepare = prepare_effective_view_adoption
                else:
                    view = validate_bound_context_artifacts(
                        {"view_ref": view_ref, "view": output["payload"]}, resolve)
                    prepare = prepare_bound_context_adoption
                require(view["owner"]["agent_node_id"] == writer["config"]["agent_node_id"]
                        and view["owner"]["object_key"] == key, "context_agent_binding_mismatch",
                        "Accepted candidate differs from the current writer binding")
            else:
                validate_context_adoption_proof(view_ref, output["payload"], resolve,
                                               source["component_id"], producer["config"])
            prepared = prepare(
                workflow_session_id=sid, object_key=key, object_record=current,
                view_ref=view_ref, view=output["payload"])
            writes = [] if prepared["write_intent"] is None else [prepared["write_intent"]]
            results = objects.apply(sid, node_id=node_id, writes=writes)
            session["revision"] += 1
            repo.put("workflow_session", session)
            self._save_manifest(repo, "context_adoption", idempotency_key + ":" + sid,
                                session, status="state_edit")
            return {"results": results, "session": self._view(repo, sid)}

        return self._change("graph.context.adopt", idempotency_key, request, change)

    def get_state_manifest(self, sid, *, snapshot_id=None, chain_id=None, boundary="end"):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session = repo.get("workflow_session", workflow_session_id=sid)
            require((snapshot_id is None) != (chain_id is None),
                    "invalid_request", "Choose a snapshot or a run boundary")
            if snapshot_id is not None:
                snapshot = repo.get("state_snapshot", state_snapshot_id=snapshot_id)
                history = set(self._history(repo, sid))
                visible = snapshot["workflow_session_id"] == sid or any(
                    commit["state_snapshot_id"] == snapshot_id
                    and commit["source"].get("kind") == "completed_execution"
                    and commit["source"].get("chain_run_id") in history
                    for commit in repo.rows("workflow_commit"))
                require(visible, "manifest_scope_mismatch", "Checkpoint is outside this session", 403)
                result = self._manifests(repo).get("state_snapshot", snapshot_id)
            else:
                require(chain_id in self._history(repo, sid), "manifest_scope_mismatch",
                        "Run is outside this session", 403)
                require(boundary in ("start", "end"), "invalid_request", "Unknown run boundary")
                result = self._manifests(repo).get("chain_" + boundary, chain_id)
            require(result is not None, "manifest_unavailable", "This older record has no object manifest", 404)
            return result

    def _global_store(self, store):
        from .global_resources import GlobalResourceStore
        return GlobalResourceStore(store, self.registry.data_types)

    def get_global_resource(self, identity):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._global_store(store).get(identity)

    def list_global_resources(self, *, scope=None, type_id=None):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._global_store(store).list(scope=scope, type_id=type_id)

    def save_global_resource(self, record, *, expected_sequence, idempotency_key):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._global_store(store).write(record, expected_sequence=expected_sequence,
                                                   idempotency_key=idempotency_key)

    def delete_global_resource(self, identity, *, expected_sequence, idempotency_key):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return self._global_store(store).delete(identity, expected_sequence=expected_sequence,
                                                    idempotency_key=idempotency_key)
