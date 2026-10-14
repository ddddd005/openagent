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
            from .context_contract import EFFECTIVE_CONTEXT_TYPE
            from .host_sdk import WriteIntent

            objects = self._objects(repo)
            heads = objects.current(sid)
            if type(writes) is list and len(writes) <= 256:
                intents = [WriteIntent.from_dict(item) for item in writes]
                if len({intent.object_key for intent in intents}) == len(intents):
                    require(not any(
                        (head := heads.get(intent.object_key)) is not None
                        and (head["type_id"], head["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 4)
                        for intent in intents
                    ), "context_managed_write_denied",
                        "Native context changes require accepted context merge settlement", 403)
            result = objects.apply(sid, node_id=node_id, writes=writes)
            session["revision"] += 1
            repo.put("workflow_session", session)
            # Editing working state does not manufacture a successful candidate.
            self._save_manifest(repo, "object_edit", idempotency_key + ":" + sid, session, status="state_edit")
            return {"results": result, "session": self._view(repo, sid)}

        return self._change("graph.objects.update", idempotency_key, request, change)

    def adopt_context_view(self, sid, *, node_id, view_output_id, expected_revision, idempotency_key):
        """Reject the retired out-of-workflow context write path."""
        require(False, "context_adoption_retired",
                "Context updates must be accepted by an in-workflow write node", 410)

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
        from .content_configuration import global_resource_service
        from .host_sdk import ResourceIdentity
        identity = ResourceIdentity.from_dict(identity).to_dict()
        with self._lock:
            self._check_open()
            return global_resource_service(self, identity["type_id"]).get_global_resource(identity)

    def list_global_resources(self, *, scope=None, type_id=None):
        from .content_configuration import list_managed_global_resources
        with self._lock:
            self._check_open()
            return list_managed_global_resources(self, scope=scope, type_id=type_id)

    def save_global_resource(self, record, *, expected_sequence, idempotency_key):
        from .content_configuration import global_resource_service
        require(type(record) is dict, "global_resource_invalid", "Global resource envelope is invalid")
        with self._lock:
            self._check_open()
            return global_resource_service(self, record.get("type_id")).save_global_resource(
                record, expected_sequence=expected_sequence, idempotency_key=idempotency_key)

    def delete_global_resource(self, identity, *, expected_sequence, idempotency_key):
        from .content_configuration import global_resource_service
        from .host_sdk import ResourceIdentity
        identity = ResourceIdentity.from_dict(identity).to_dict()
        with self._lock:
            self._check_open()
            return global_resource_service(self, identity["type_id"]).delete_global_resource(
                identity, expected_sequence=expected_sequence, idempotency_key=idempotency_key)
