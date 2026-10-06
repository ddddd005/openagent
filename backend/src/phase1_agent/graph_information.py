"""Application information discovery and exact invocation reader coordination.

Only declarations and invocation coordinates are retained here. Provider bodies
remain in their package or registered storage reader, including after restart.
"""

from contextlib import closing
from copy import deepcopy

from .contract_json import canonical_bytes, loads_strict
from .graph_records import require, uuid_value
from .graph_store import GraphRecordStore
from .host_sdk import InformationSourceReference
from .runtime_hosting import InvocationOwner


class InformationBindingStore:
    """Durable evidence of which source was actually bound to an invocation."""

    def __init__(self, store):
        self.connection = store._connection
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_information_bindings ("
            "node_run_id TEXT NOT NULL, source_id TEXT NOT NULL, exact_version TEXT NOT NULL, "
            "generation INTEGER NOT NULL, owner TEXT NOT NULL, declaration TEXT NOT NULL, "
            "PRIMARY KEY(node_run_id,source_id,exact_version,generation))"
        )

    def accept(self, owner, generation, definitions):
        InvocationOwner.from_dict(owner)
        require(type(generation) is int and 1 <= generation <= 2**53 - 1,
                "information_invalid_generation", "Generation must be a positive integer")
        require(self.connection.in_transaction, "storage_contract_violation",
                "Information binding acceptance requires a transaction", 500)
        for definition in definitions:
            reference = InformationSourceReference.from_dict(definition["source_ref"])
            coordinates = (owner["node_run_id"], reference.source_id, reference.exact_version, generation)
            encoded_owner = canonical_bytes(owner).decode("utf-8")
            encoded_definition = canonical_bytes(definition).decode("utf-8")
            prior = self.connection.execute(
                "SELECT owner,declaration FROM workflow_information_bindings "
                "WHERE node_run_id=? AND source_id=? AND exact_version=? AND generation=?", coordinates,
            ).fetchone()
            require(prior is None or (prior["owner"], prior["declaration"])
                    == (encoded_owner, encoded_definition),
                    "information_binding_conflict", "Invocation source binding was redefined", 409)
            if prior is None:
                self.connection.execute(
                    "INSERT INTO workflow_information_bindings VALUES(?,?,?,?,?,?)",
                    (*coordinates, encoded_owner, encoded_definition),
                )

    def get(self, reference, owner, generation):
        row = self.connection.execute(
            "SELECT owner,declaration FROM workflow_information_bindings "
            "WHERE node_run_id=? AND source_id=? AND exact_version=? AND generation=?",
            (owner["node_run_id"], reference.source_id, reference.exact_version, generation),
        ).fetchone()
        require(row is not None and loads_strict(row["owner"]) == owner,
                "information_unbound", "Source was not bound to this exact invocation generation", 404)
        return loads_strict(row["declaration"])

    def list(self, run_ids):
        # Query the requested runs; discovery does not scan provider bodies.
        result = []
        for run_id in run_ids:
            for row in self.connection.execute(
                "SELECT owner,generation,declaration FROM workflow_information_bindings "
                "WHERE node_run_id=? ORDER BY source_id,exact_version,generation", (run_id,),
            ):
                result.append({"owner": loads_strict(row["owner"]), "generation": row["generation"],
                               "declaration": loads_strict(row["declaration"])})
        return result


class GraphInformation:
    def _information_router_for_chain(self, chain_id, registry):
        from .runtime_information import InformationRouter
        with self._lock:
            if chain_id not in self._information_routers:
                self._information_routers[chain_id] = InformationRouter(registry.information_sources)
            return self._information_routers[chain_id]

    @staticmethod
    def _information_component_for_owner(owner, context):
        return context.definition.component_id, context.definition.component_version

    def _read_runtime_fact_page(self, request):
        """Storage provider for registered execution facts, independent of logs."""
        from .runtime_fact_store import RuntimeFactStore
        owner = request["owner"]
        with closing(self._store()) as store:
            repo = GraphRecordStore(store)
            node = self._information_original_node(repo, owner)
            registry = self._execution_registries.get(owner["chain_run_id"], self.registry)
            entry = registry.get(node["component_id"], node["component_version"])
            require(entry is not None and entry.executor_ref is not None,
                    "information_source_unavailable", "Exact executor fact source is unavailable", 409)
            return RuntimeFactStore(store).read_executor_page(
                owner, entry.executor_ref.to_dict(), request["generation"],
                limit=request["limit"], cursor=request["cursor"])

    def _accept_information_bindings(self, owner, generation, definitions):
        if not definitions:
            return
        # Runtime hooks may run while a handle is being created. This callback
        # never acquires the service lock, avoiding inversion with control reads.
        with closing(self._store()) as store:
            bindings = InformationBindingStore(store)
            try:
                store._connection.execute("BEGIN IMMEDIATE")
                bindings.accept(owner, generation, definitions)
                store._connection.execute("COMMIT")
            except BaseException:
                if store._connection.in_transaction:
                    store._connection.execute("ROLLBACK")
                raise

    def _release_information_chain(self, chain_id):
        router = self._information_routers.pop(chain_id, None)
        if router is not None:
            with closing(self._store()) as store:
                chain = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
            attempts = chain.get("node_run_attempts", [[identity] for identity in chain["node_run_ids"]])
            for node_id, run_ids in zip(chain["ordered_nodes"], attempts):
                for run_id in run_ids:
                    router.release({"workflow_session_id": chain["workflow_session_id"], "chain_run_id": chain_id,
                                    "node_binding_id": node_id, "node_run_id": run_id})

    def _information_original_node(self, repo, owner):
        """Resolve immutable original coordinates without a mutable parent head."""
        InvocationOwner.from_dict(owner)
        run = repo.get("node_run", run_id=owner["node_run_id"])
        chain = repo.get("chain_run", chain_run_id=owner["chain_run_id"])
        attempts = chain.get("node_run_attempts", [[identity] for identity in chain["node_run_ids"]])
        require(owner == {"workflow_session_id": run["workflow_session_id"],
                          "chain_run_id": run["chain_run_id"],
                          "node_binding_id": run["node_binding_id"], "node_run_id": run["run_id"]}
                and chain["workflow_session_id"] == owner["workflow_session_id"]
                and owner["node_binding_id"] in chain["ordered_nodes"]
                and owner["node_run_id"] in attempts[chain["ordered_nodes"].index(owner["node_binding_id"])],
                "information_owner_mismatch", "Information owner differs from its original invocation", 403)
        document = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
        node = next((item for item in document["nodes"]
                     if item["node_binding_id"] == owner["node_binding_id"]), None)
        require(node is not None, "information_owner_mismatch", "Original node declaration is unavailable", 409)
        return node

    def _information_owner(self, repo, sid, owner):
        """Authorize original coordinates against this branch's frozen history."""
        node = self._information_original_node(repo, owner)
        session = repo.get("workflow_session", workflow_session_id=sid)
        require(owner["chain_run_id"] in self._history(repo, sid)
                or session["active_chain_run_id"] == owner["chain_run_id"],
                "information_scope_denied", "Information is outside this session's visible history", 403)
        return node

    def list_registrations(self, *, audience="management", session_id=None, chain_id=None,
                           node_id=None, kind=None, package_id=None, channel_id=None, limit=100, cursor=None):
        from .runtime_information import registration_catalog, paginate_registrations
        require(audience in ("management", "consumer"), "invalid_request", "Unknown information audience")
        require(session_id is not None or chain_id is None and node_id is None,
                "invalid_request", "Invocation filters require a session")
        if session_id is not None:
            require(uuid_value(session_id), "invalid_request", "Invalid session identity")
        if chain_id is not None:
            require(uuid_value(chain_id), "invalid_request", "Invalid chain identity")
        if node_id is not None:
            require(uuid_value(node_id), "invalid_request", "Invalid node identity")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            entries = registration_catalog(
                self.registry, frontend_extensions=self._frontend_extensions,
                package_manifests=self._package_manifests)
            source_entries = {canonical_bytes(item["registration_ref"]): item for item in entries
                              if item["kind"] == "information_source"}
            if session_id is not None:
                session = repo.get("workflow_session", workflow_session_id=session_id)
                histories = self._history(repo, session_id)
                if session["active_chain_run_id"] is not None:
                    histories = [*histories, session["active_chain_run_id"]]
                if chain_id is not None:
                    require(chain_id in histories, "information_scope_denied",
                            "Run is outside this session's visible history", 403)
                    histories = [chain_id]
                run_ids = []
                for identity in dict.fromkeys(histories):
                    chain = repo.get("chain_run", chain_run_id=identity)
                    attempts = chain.get("node_run_attempts", [[run_id] for run_id in chain["node_run_ids"]])
                    run_ids.extend(run_id for original_node_id, node_attempts
                                   in zip(chain["ordered_nodes"], attempts) for run_id in node_attempts
                                   if node_id is None or original_node_id == node_id)
                for item in InformationBindingStore(store).list(run_ids):
                    definition = item["declaration"]
                    reference = InformationSourceReference.from_dict(definition["source_ref"])
                    registration = self.registry.information_sources.get(reference)
                    available = registration is not None and registration.definition.to_dict() == definition
                    source = source_entries.get(canonical_bytes(definition["source_ref"]), {})
                    router = self._information_routers.get(item["owner"]["chain_run_id"])
                    binding = next((value for value in router.catalog(owner=item["owner"])
                                    if value["registration_ref"] == definition["source_ref"]
                                    and value["generation"] == item["generation"]), None) if router else None
                    availability = ("source_unavailable" if not available else binding["availability"] if binding
                                    else "history" if registration.history_reader is not None else "unavailable")
                    entries.append({
                        "kind": "information_binding", "registration_ref": definition["source_ref"],
                        "owner": item["owner"], "generation": item["generation"],
                        "declaration": definition, "discover_public": definition["discover_public"],
                        "read_public": definition["read_public"],
                        "availability": availability, "package_id": source.get("package_id"),
                        "package_version": source.get("package_version"),
                    })
        filters = {key: value for key, value in (("kind", kind), ("package_id", package_id),
                                                ("channel_id", channel_id)) if value is not None}
        query_scope = {key: value for key, value in (
            ("session_id", session_id), ("chain_id", chain_id), ("node_id", node_id),
        ) if value is not None}
        return paginate_registrations(entries, limit=limit, cursor=cursor, filters=filters,
                                      audience=audience, query_scope=query_scope)

    def read_information(self, sid, *, reference, owner, generation, audience="management",
                         source_scope="live", limit=100, cursor=None):
        from .runtime_information import InformationRouter
        require(audience in ("management", "consumer"), "invalid_request", "Unknown information audience")
        require(type(generation) is int and 1 <= generation <= 2**53 - 1,
                "information_invalid_generation", "Generation must be a positive integer")
        require(source_scope in ("live", "history"), "invalid_request", "Unknown information source scope")
        reference = InformationSourceReference.from_dict(reference)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            node = self._information_owner(GraphRecordStore(store), sid, owner)
            definition = InformationBindingStore(store).get(reference, owner, generation)
            registry = self._execution_registries.get(owner["chain_run_id"], self.registry)
            registration = registry.information_sources.get(reference)
            require(registration is not None, "information_source_unavailable",
                    "Exact information source implementation is unavailable", 409)
            require(registration.definition.to_dict() == definition
                    and (definition["component_id"], definition["component_version"])
                    == (node["component_id"], node["component_version"]),
                    "information_binding_mismatch", "Source declaration differs from its accepted binding", 409)
            require(audience == "management" or definition["read_public"],
                    "information_read_denied", "Information source is not publicly readable", 403)
            router = self._information_routers.get(owner["chain_run_id"])
            if router is None:
                require(source_scope == "history", "information_live_unavailable",
                        "Live source is no longer retained for this invocation", 409)
                router = InformationRouter(registry.information_sources)
            if source_scope == "history":
                router.restore(reference, owner, generation)
        # Provider reads may block. They must never hold the workflow control lock.
        return router.read(reference, owner, generation, audience=audience,
                           source_scope=source_scope, limit=limit, cursor=cursor)

    def read_object_artifact(self, sid, *, object_key, node_id, reference, revision_id=None):
        """Resolve an exact artifact through a node's authorized object closure."""
        from .host_sdk import validate_reference
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact", "invalid_request", "An exact artifact reference is required")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            repo.get("workflow_session", workflow_session_id=sid)
            item = self._objects(repo).read(sid, object_key, node_id=node_id, revision_id=revision_id)
            roots = self.registry.data_types.references(
                item["type_id"], item["schema_version"], item["value"], scope="session")
            require(reference["output_id"] in self._artifact_closure(repo, roots),
                    "session_object_reference_denied", "Artifact is outside this authorized object revision", 403)
            result = self._objects(repo)._resolve_write_artifact(sid, reference)
            return {"schema_version": 1, "kind": "workflow.object-artifact",
                    "workflow_session_id": sid, "object_key": object_key, "revision_id": item["revision_id"],
                    "reference": deepcopy(reference), **result}
