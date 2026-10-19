"""Model-independent workflow definitions, sessions and serial graph execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import os
from threading import RLock
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from .contract_json import canonical_bytes, validate_json_value
from .configuration_data import WorkflowConfigurationData, WorkflowDataManagement
from .configuration_service import WorkflowConfigurationService
from .graph_records import graph_record, graph_error, require, uuid_value
from .graph_store import GraphRecordStore
from .run_basis import RunBasis
from .program_variable_store import ProgramVariableStore
from .preparation_program import validate_program_state
from .storage import SqliteStore
from .graph_public import GraphPublic
from .graph_history import GraphHistory
from .graph_platform import GraphPlatform
from .graph_runtime_host import GraphRuntimeHost
from .frontend_node_snapshots import GraphFrontendNodes
from .graph_resource_host import GraphResourceHost
from .graph_information import GraphInformation
from .graph_events import GraphEvents
from .graph_failed_retry import GraphFailedRetry
from .graph_editing_host import GraphEditingHost
from .graph_provider_host import ProviderConfigurationService
from .content_configuration import ContentConfigurationService
from .workflow_lifecycle import WorkflowLifecycle
from .workflow_configuration import WorkflowConfiguration
from .workflow_chat_service import WorkflowChatService


def _uid() -> str:
    return str(uuid4())


def _revision(value: Any, *, zero: bool = False) -> int:
    require(type(value) is int and value >= (0 if zero else 1), "invalid_request", "Invalid expected revision")
    return value


class _GraphLease:
    """Ensure one graph service owns execution for this database."""

    def __init__(self, database: Path):
        self.file = open(str(database) + ".graph.lock", "a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise graph_error("service_already_owned", "Graph database already has a live owner", 409) from None

    def close(self):
        self.file.close()


class GraphWorkflowService(WorkflowChatService, WorkflowConfiguration, WorkflowLifecycle, GraphEditingHost,
                           GraphFailedRetry, GraphEvents, GraphInformation, GraphRuntimeHost,
                           GraphFrontendNodes, GraphResourceHost, GraphPlatform, GraphPublic, GraphHistory):
    def __init__(self, database_path: str | Path, *, registry=None, fault_injector=None,
                 capability_packages=(), enabled_packages=None, trusted_package_entrypoints=(),
                 public_model_factory=None, credential_protector=None, provider_discovery_client_factory=None):
        self._initialize_service(
            database_path, registry=registry, fault_injector=fault_injector,
            capability_packages=capability_packages, enabled_packages=enabled_packages,
            trusted_package_entrypoints=trusted_package_entrypoints,
            public_model_factory=public_model_factory, credential_protector=credential_protector,
            provider_discovery_client_factory=provider_discovery_client_factory, execution_owner=True)

    def _initialize_service(self, database_path: str | Path, *, registry=None, fault_injector=None,
                            capability_packages=(), enabled_packages=None, trusted_package_entrypoints=(),
                            public_model_factory=None, credential_protector=None,
                            provider_discovery_client_factory=None, execution_owner):
        self.database = Path(database_path).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._execution_owner = execution_owner
        self._lease = _GraphLease(self.database) if execution_owner else None
        self._fault_injector = fault_injector
        self._package_loader = None
        self._frontend_extensions = ()
        self._package_manifests = ()
        self._package_diagnostics = []
        if registry is None:
            from .capability_packages import CapabilityPackageLoader, discover_trusted_packages
            from .graph_package_selection import GraphPackageSelectionStore
            from .builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
            try:
                explicit_packages = [*capability_packages, *discover_trusted_packages(trusted_package_entrypoints)]
                explicit_identities = {(package.manifest.package_id, package.manifest.version)
                                       for package in explicit_packages}
                packages = [*(package for package in builtin_capability_packages(runtime_fact_reader=self._read_runtime_fact_page)
                              if (package.manifest.package_id, package.manifest.version) not in explicit_identities),
                            *explicit_packages]
                self._package_loader = CapabilityPackageLoader(packages)
                with closing(self._store()) as store:
                    selections = GraphPackageSelectionStore(store)
                    saved = (selections.read_effective(DEFAULT_PACKAGES)
                             if enabled_packages is None else None)
                    selected = (deepcopy(enabled_packages)
                                if enabled_packages is not None else saved.enabled_packages)
                    try:
                        loaded = self._load_current_capabilities(selected)
                    except Exception as exc:
                        if enabled_packages is not None or saved.configuration_id is None \
                                or getattr(exc, "reason_code", None) != "package_missing_dependency":
                            raise
                        self._package_diagnostics = [{"reason_code": exc.reason_code,
                                                      "message": str(exc), "enabled_packages": deepcopy(selected)}]
                        loaded = self._load_current_capabilities({})
                    self.registry, self._frontend_extensions = loaded.registry.detached(), loaded.frontend_extensions
                    self._package_manifests = loaded.package_manifests
                    from .type_contract_store import TypeContractStore
                    contracts = TypeContractStore(store)
                    if execution_owner:
                        try:
                            store._connection.execute("BEGIN IMMEDIATE")
                            contracts.check_registry(self.registry.data_types)
                            if enabled_packages is not None or saved.configuration_id is None:
                                selections.write_current(selected)
                            store._connection.execute("COMMIT")
                        except BaseException:
                            if store._connection.in_transaction:
                                store._connection.execute("ROLLBACK")
                            raise
                    else:
                        for definition in self.registry.data_types.definitions():
                            contracts.check(self.registry.data_types, definition.type_id,
                                            definition.schema_version, definition.scope,
                                            allow_legacy=True)
            except BaseException:
                if self._lease is not None:
                    self._lease.close()
                raise
        else:
            try:
                require(not capability_packages and enabled_packages is None and not trusted_package_entrypoints,
                        "invalid_request", "Choose a custom registry or configured capability packages")
            except BaseException:
                if self._lease is not None:
                    self._lease.close()
                raise
            # Trusted callers can add declarations before launching a run.
            # Every accepted run receives its own immutable registry.
            self.registry = registry.detached()
        self._resource_frames = {}
        # chain id -> RunBasis.  The historical attribute name is kept because
        # it is the existing per-chain execution map; values are now the whole
        # bound load result, not a bare registry.
        self._execution_registries = {}
        self._lock = RLock()
        # Configuration and definition evidence are management-scope data
        # domains.  They share the data-management entry but keep separate
        # domain ports and repositories.
        self._data_management = WorkflowDataManagement(self._store)
        self._configuration_data = WorkflowConfigurationData(self._data_management)
        from .definition_data import WorkflowDefinitionData
        self._definition_data = WorkflowDefinitionData(self._data_management)
        self.configuration_service = WorkflowConfigurationService(self)
        self._executor = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow-graph")
                          if execution_owner else None)
        self._futures: dict[str, Any] = {}
        self._pauses: set[str] = set()
        self._resuming: set[str] = set()
        self._management_sessions: set[str] = set()
        self._management_workflows: set[str] = set()
        self._runtime_hosts = {}
        self._information_routers = {}
        self._acceptance_candidates = {}
        self._failed_retry_candidates = {}
        self._service_runs = {}
        self._service_options = {}
        self._frontend_node_snapshots = {}
        self._frontend_node_connections = None
        self._execution_errors = {}
        self._closed = False
        if execution_owner:
            try:
                self.provider_configuration = ProviderConfigurationService(
                    self.database,
                    credential_protector=credential_protector,
                    discovery_client_factory=provider_discovery_client_factory, store_factory=self._store)
                self.content_configuration = ContentConfigurationService(
                    self.database, store_factory=self._store,
                    type_registry_factory=lambda: self.registry.data_types)
                from .model_host_service import MODEL_SERVICE_REF
                self._service_options[MODEL_SERVICE_REF] = {
                    "credential_broker_factory": self.provider_configuration.new_credential_broker}
                if public_model_factory is not None:
                    self._service_options[MODEL_SERVICE_REF]["transport_factory"] = public_model_factory
            except BaseException:
                if self._executor is not None:
                    self._executor.shutdown(wait=True)
                if self._lease is not None:
                    self._lease.close()
                raise

    def _store(self):
        return SqliteStore(self.database, fault_injector=self._fault_injector)

    def _check_open(self):
        require(not self._closed, "service_closed", "Graph service is closed", 409)

    def _require_execution_owner(self):
        require(self._execution_owner, "execution_authority_required",
                "This management service does not own workflow execution", 409)

    def _load_current_capabilities(self, selected):
        resolved = self._package_loader.resolve(selected)
        require(all(item["package_id"] != "workflow.compat" for item in resolved.package_lock),
                "package_retired", "Compatibility packages are no longer supported", 409)
        return self._package_loader.load(selected)

    @property
    def definition_query(self):
        """Exact read-only definition query over the active trusted registry."""
        from .workflow_definitions import WorkflowDefinitionCatalog
        return WorkflowDefinitionCatalog(self.registry, getattr(self, "_package_manifests", ()))

    def _capture_run_basis(self):
        """Capture one published load result for a single invocation.

        Registry, package manifests and definition query all come from the
        same publication point.  This is intentionally transient; execution
        paths must use this value rather than re-reading the service tables.
        """
        from .workflow_definitions import WorkflowDefinitionCatalog
        with self._lock:
            registry = self.registry.detached(frozen=True)
            manifests = deepcopy(getattr(self, "_package_manifests", ()))
        return RunBasis(registry, WorkflowDefinitionCatalog(registry, manifests))

    def _require_run_basis(self, chain_id):
        basis = self._execution_registries.get(chain_id)
        require(isinstance(basis, RunBasis), "run_basis_unavailable",
                "This invocation has no retained run basis", 409)
        return basis

    def definition_evidence(self, reference):
        """Read one exact data-type evidence record through the definition data port."""
        from .definition_data import require_evidence
        from .workflow_definitions import definition_reference

        reference = definition_reference("data_type", reference)
        with self._definition_data.read() as data:
            return require_evidence(data.type_evidence(
                reference["scope"], reference["type_id"], reference["schema_version"]), reference)

    def definition_references(self, identity, revision):
        """Resolve definitions used by one already-persisted fixed workflow revision."""
        require(type(revision) is int and revision >= 1, "invalid_request",
                "Definition references require one exact fixed revision")
        with self._data_management.read_configuration() as data:
            document = data.fixed_document(identity, revision)
        return self.definition_query.workflow_references(document)

    def _document_available(self, document):
        if self._package_diagnostics:
            return False
        packages = {row["package_id"]: row["version"] for row in self.registry.package_lock}
        if any(packages.get(row["package_id"]) != row["version"]
               for row in document.get("package_lock", [])):
            return False
        return all(self.registry.get_definition(node["component_id"], node["component_version"]) is not None
                   for node in document["nodes"])

    def _change(self, operation, key, request, callback):
        with self._lock, closing(self._store()) as store:
            require(not self._closed, "service_closed", "Graph service is closed", 409)
            return GraphRecordStore(store, definition_registry=self.registry).atomic(operation, key, request, callback)

    def _configuration_change(self, operation, key, request, callback):
        """Run a configuration command in the original transaction.

        The callback receives a limited configuration data session bound to the
        same ``GraphRecordStore``; the repo, connection and transaction holder
        stay inside the existing ``_change``/``atomic`` envelope.
        """
        return self._change(
            operation, key, request,
            lambda repo: callback(self._configuration_data.transaction(repo)))

    def node_types(self, *, protocol_version=1):
        catalog = self.definition_query.node_catalog()
        if protocol_version == 2:
            return {"schema_version": 2, "node_types": catalog, **self.platform_capabilities()}
        require(protocol_version == 1, "invalid_request", "Unknown node catalog protocol")
        builtin = {"TEXT", "PROMPT", "JSON"}
        legacy = []
        for node in catalog:
            ports = [*node["inputs"], *node["outputs"]]
            for mode in node.get("port_modes", {}).values():
                ports.extend([*mode["inputs"], *mode["outputs"]])
            if all(port["data_type"] in builtin and port.get("data_schema_version", 1) == 1 for port in ports) \
                    and not set(node["capabilities"]) & {"objects:read", "objects:write"}:
                legacy.append(node)
        return {"schema_version": 1, "node_types": legacy}

    def _document(self, repo, identity, revision=None):
        if revision is None:
            revisions = repo.rows("workflow_definition_revision", workflow_definition_id=identity)
            require(bool(revisions), "not_found", "Workflow definition not found", 404)
            revision = max(row["revision"] for row in revisions)
        return repo.get("workflow_definition_revision", workflow_definition_id=identity, revision=revision)["document"]

    def get_definition(self, identity, revision=None):
        return self.configuration_service.read_configuration(identity, revision)

    def list_definitions(self, *, include_archived=False):
        return self.configuration_service.list_configuration_definitions(
            include_archived=include_archived)

    def _configuration_document(self, repo, identity, *, registry=None, definitions=None):
        """Compatibility adapter for out-of-batch consumers holding a repo."""
        return self._configuration_view(
            self._configuration_data.transaction(repo), identity,
            registry=registry, definitions=definitions)

    def _save_definition(self, repo, document, expected_revision):
        """Compatibility adapter for legacy definition writes inside a repo transaction."""
        return self._configuration_data.transaction(repo).save_fixed_revision(
            document, expected_revision)

    def update_definition(self, document, *, workflow_definition_id, expected_edit_sequence,
                          idempotency_key, confirm_derivation=None):
        return self.configuration_service.update_definition(
            document, workflow_definition_id=workflow_definition_id,
            expected_edit_sequence=expected_edit_sequence, idempotency_key=idempotency_key,
            confirm_derivation=confirm_derivation)

    def patch_node_configuration(self, *, workflow_definition_id, node_binding_id, component_id,
                                 component_version, changes, expected_edit_sequence, idempotency_key,
                                 confirm_derivation=None):
        return self.configuration_service.patch_node_configuration(
            workflow_definition_id=workflow_definition_id, node_binding_id=node_binding_id,
            component_id=component_id, component_version=component_version, changes=changes,
            expected_edit_sequence=expected_edit_sequence, idempotency_key=idempotency_key,
            confirm_derivation=confirm_derivation)

    def _definition(self, node, *, registry=None):
        registry = self.registry if registry is None else registry
        try:
            entry = registry.get(node["component_id"], node["component_version"])
            return entry.definition if entry is not None else None
        except (KeyError, ValueError, AttributeError):
            return None

    def _check_implementations(self, document, plan=None, *, registry):
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        for identity in (plan.ordered_node_ids if plan is not None else ()):
            node = nodes[identity]
            entry = registry.get(node["component_id"], node["component_version"])
            require(entry is not None and (entry.executor is not None or entry.executor_ref is not None
                    and registry.executors.get(entry.executor_ref) is not None),
                    "graph_missing_node_type", "Node implementation is missing or unavailable", 409,
                    node_id=identity)

    def _defaults(self, document):
        result = {}
        for node in document["nodes"]:
            definition = self._definition(node)
            if definition is not None and definition.private_state_schema is not None:
                result[node["node_binding_id"]] = deepcopy(definition.private_state_default)
        return result

    def _private(self, repo, sid):
        return {row["node_binding_id"]: deepcopy(row["private_data"]["payload"])
                for row in repo.rows("node_session", workflow_session_id=sid)}

    def _save_private(self, target, sid, document, private_states, *, registry=None):
        if hasattr(target, "records"):
            data = target
            registry = data.registry if registry is None else registry
            for node_id, value in private_states.items():
                require(node_id in {node["node_binding_id"] for node in document["nodes"]},
                        "private_state_owner_mismatch", "Unknown private state owner", 409, node_id=node_id)
                node = next(node for node in document["nodes"] if node["node_binding_id"] == node_id)
                definition = self._definition(node, registry=registry)
                require(definition is not None and definition.private_state_schema is not None,
                        "private_state_not_declared", "Private state is not declared", 409, node_id=node_id)
                require(next(Draft202012Validator(definition.private_state_schema).iter_errors(value), None) is None,
                        "private_state_schema_mismatch", "Private state does not match schema", 409, node_id=node_id)
                existing = data.records.private_state(sid, node_id)
                payload = {"owner_component_id": node["component_id"], "schema_version": 1, "payload": value}
                if existing is not None and canonical_bytes(existing["private_data"]) == canonical_bytes(payload):
                    continue
                data.records.put_private_state(graph_record(
                    "node_session", workflow_session_id=sid, node_binding_id=node_id,
                    data_version=existing["data_version"] + 1 if existing else 1,
                    private_data=payload))
            return
        repo = target
        registry = self.registry if registry is None else registry
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        for node_id, value in private_states.items():
            require(node_id in nodes, "private_state_owner_mismatch", "Unknown private state owner", 409, node_id=node_id)
            node = nodes[node_id]
            definition = self._definition(node, registry=registry)
            require(definition is not None and definition.private_state_schema is not None,
                    "private_state_not_declared", "Private state is not declared", 409, node_id=node_id)
            require(next(Draft202012Validator(definition.private_state_schema).iter_errors(value), None) is None,
                    "private_state_schema_mismatch", "Private state does not match schema", 409, node_id=node_id)
            existing = repo.maybe("node_session", workflow_session_id=sid, node_binding_id=node_id)
            payload = {"owner_component_id": node["component_id"], "schema_version": 1, "payload": value}
            if existing is not None and repo.equal(existing["private_data"], payload):
                continue
            repo.put("node_session", graph_record("node_session", workflow_session_id=sid,
                node_binding_id=node_id, data_version=existing["data_version"] + 1 if existing else 1,
                private_data=payload))

    def _head(self, repo, sid):
        refs = repo.rows("workflow_ref", workflow_session_id=sid)
        require(len(refs) == 1, "storage_contract_violation", "Session must have one Head", 500)
        return refs[0]

    def _history(self, repo, sid):
        head = self._head(repo, sid)
        commit = repo.get("workflow_commit", commit_id=head["head_commit_id"])
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        own = [row["chain_run_id"] for row in repo.rows("chain_run", workflow_session_id=sid)]
        return list(dict.fromkeys([*snapshot["history_refs"], *own]))

    def _commit(self, target, session, *, source, history=None, registry=None):
        if hasattr(target, "settlement"):
            snapshot, _ = target.settlement.commit(
                session, source=source, history=history)
            self._save_manifest(
                target, "state_snapshot", snapshot["state_snapshot_id"], session,
                history=snapshot["history_refs"])
            return
        repo = target
        sid = session["workflow_session_id"]
        old = next((row for row in repo.rows("workflow_ref") if row["workflow_session_id"] == sid), None)
        state = ProgramVariableStore(repo.store).current(sid)
        snapshot = graph_record("state_snapshot", state_snapshot_id=_uid(), workflow_session_id=sid,
            workflow_definition_id=session["workflow_definition_id"], definition_revision=session["definition_revision"],
            node_states=self._private(repo, sid), data_revision=state["revision"],
            history_refs=history if history is not None else self._history(repo, sid) if old else [])
        commit = graph_record("workflow_commit", commit_id=_uid(), workflow_session_id=sid,
            state_snapshot_id=snapshot["state_snapshot_id"], parent_commit_id=old["head_commit_id"] if old else None,
            source=source)
        repo.put("state_snapshot", snapshot)
        repo.put("workflow_commit", commit)
        repo.put("workflow_ref", graph_record("workflow_ref", workflow_ref_id=old["workflow_ref_id"] if old else _uid(),
            workflow_session_id=sid, head_commit_id=commit["commit_id"], revision=old["revision"] + 1 if old else 1))
        variables = ProgramVariableStore(repo.store)
        variables.bind("state_snapshot", snapshot["state_snapshot_id"], sid, state["revision"])
        variables.bind("workflow_commit", commit["commit_id"], sid, state["revision"])
        self._save_manifest(repo, "state_snapshot", snapshot["state_snapshot_id"], session,
                            history=snapshot["history_refs"], registry=registry)

    def _initialize_session(self, repo, document):
        self._require_definition_active(repo, document["workflow_definition_id"])
        require(not self._package_diagnostics, "package_missing_dependency",
                "Restore the saved packages before initializing session data", 409)
        sid = _uid()
        session = graph_record("workflow_session", workflow_session_id=sid,
            workflow_definition_id=document["workflow_definition_id"], definition_revision=document["revision"],
            revision=1, source={"kind": "new"}, active_chain_run_id=None)
        repo.put("workflow_session", session)
        self._save_private(repo, sid, document, self._defaults(document))
        self._objects(repo).initialize(sid, document)
        return session

    def _new_session(self, repo, document):
        session = self._initialize_session(repo, document)
        self._commit(repo, session, source={"kind": "session_seed", "source": None}, history=[])
        return self._view(
            self._data_management.execution_read(repo, registry=self.registry),
            session["workflow_session_id"])

    def create_session(self, workflow_definition_id, definition_revision, *, idempotency_key):
        def change(repo):
            revision = _revision(definition_revision)
            current = repo.current_configuration(workflow_definition_id)
            require(current is None or current["fixed_definition_revision"] == revision,
                    "stale_revision", "A new session must use the current fixed definition", 409)
            return self._new_session(repo, self._document(repo, workflow_definition_id, revision))

        return self._change("graph.session.create", idempotency_key,
            {"workflow_definition_id": workflow_definition_id, "definition_revision": definition_revision},
            change)

    def fork_checkpoint(self, sid, *, checkpoint_id, expected_session_revision, expected_data_revision,
                        expected_head_revision, idempotency_key):
        request = {
            "session": sid,
            "checkpoint_id": checkpoint_id,
            "expected_session_revision": expected_session_revision,
            "expected_data_revision": expected_data_revision,
            "expected_head_revision": expected_head_revision,
        }

        def change(repo):
            _revision(expected_data_revision, zero=True)
            _revision(expected_head_revision)
            source = self._stable(
                repo, sid, expected_session_revision, expected_data_revision, expected_head_revision)
            return self._fork_checkpoint_state(repo, source, checkpoint_id)

        return self._change("graph.checkpoint.fork", idempotency_key, request, change)

    def get_session(self, sid):
        with self._lock, closing(self._store()) as store:
            data = self._data_management.execution_read(
                GraphRecordStore(store), registry=self.registry)
            return self._view(data, sid)

    def list_sessions(self, workflow_definition_id):
        with self._lock, closing(self._store()) as store:
            data = self._data_management.execution_read(
                GraphRecordStore(store), registry=self.registry)
            return [self._view(data, row["workflow_session_id"])
                    for row in data.records.sessions(workflow_definition_id)]

    def get_run(self, sid, chain_id):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            repo.get("workflow_session", workflow_session_id=sid)
            require(chain_id in self._history(repo, sid), "not_found", "Execution is not in this session history", 404)
            chain = repo.get("chain_run", chain_run_id=chain_id)
            return {"chain": chain,
                    "node_runs": [repo.get("node_run", run_id=identity)
                                  for items in chain.get("node_run_attempts", [[identity] for identity in chain["node_run_ids"]])
                                  for identity in items],
                    "outputs": repo.rows("workflow_output", chain_run_id=chain_id),
                    "runtime_facts": self._runtime_facts(store, chain_id),
                    "state_manifests": {"start": self._manifests(repo).get("chain_start", chain_id),
                                        "end": self._manifests(repo).get("chain_end", chain_id)}}

    def list_checkpoints(self, session_id):
        with self._lock, closing(self._store()) as store:
            connection = store._connection
            connection.execute("BEGIN")
            try:
                repo = GraphRecordStore(store)
                session = repo.get("workflow_session", workflow_session_id=session_id)
                from .checkpoint_repository import CheckpointRepository
                checkpoints = CheckpointRepository(
                    connection,
                    workflow_definition_id=session["workflow_definition_id"],
                    workflow_session_id=session_id,
                    fixed_definition_revision=session["definition_revision"],
                )
                latest = checkpoints.latest()
                prefix = [] if latest is None else checkpoints.prefix(latest["checkpoint_id"])
                result = [{"checkpoint_id": point["checkpoint_id"],
                           "parent_checkpoint_id": point["parent_checkpoint_id"]}
                          for point in prefix]
                connection.execute("COMMIT")
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def read_checkpoint(self, session_id, *, checkpoint_id):
        with self._lock, closing(self._store()) as store:
            connection = store._connection
            connection.execute("BEGIN")
            try:
                repo = GraphRecordStore(store)
                session = repo.get("workflow_session", workflow_session_id=session_id)
                from .checkpoint_repository import CheckpointRepository
                checkpoint = CheckpointRepository(
                    connection,
                    workflow_definition_id=session["workflow_definition_id"],
                    workflow_session_id=session_id,
                    fixed_definition_revision=session["definition_revision"],
                ).get(checkpoint_id)
                require(checkpoint is not None, "not_found", "Checkpoint not found", 404)
                result = {field: deepcopy(checkpoint[field]) for field in (
                    "schema_version", "checkpoint_id", "workflow_definition_id",
                    "workflow_session_id", "fixed_definition_revision", "parent_checkpoint_id")}
                connection.execute("COMMIT")
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _view(self, data, sid):
        session = data.records.session(sid)
        document = data.configuration.fixed_document(
            session["workflow_definition_id"], session["definition_revision"])
        available = self._document_available(document)
        chains = data.records.chains(sid)
        history_refs = data.records.history(sid)
        all_chains = {identity: data.records.chain(identity)
                      for identity in history_refs}
        selected = self._selected_chain(data.records, sid)
        runs = data.records.node_runs(selected["chain_run_id"]) if selected else []
        if selected and (selected["workflow_session_id"] != sid
                         or (selected["workflow_definition_id"], selected["definition_revision"])
                            != (session["workflow_definition_id"], session["definition_revision"])):
            node_mapping = data.records.history_node_mapping(sid, selected)
        else:
            node_mapping = {node["node_binding_id"]: node["node_binding_id"] for node in document["nodes"]}
        outputs = data.records.outputs(sid)
        selected_outputs = {row["output_id"]: row
                            for row in data.records.outputs_for_chain(selected["chain_run_id"])} if selected else {}
        active = next((row for row in chains if row["chain_run_id"] == session["active_chain_run_id"]), None)
        status = active["status"] if active else selected["status"] if selected else "idle"
        actions = ["pause"] if status in ("running", "prepared") else (
            ["resume", "close"] if status == "paused" else
            (["retry_acceptance", "close"] if active
                and active["next_node_index"] < len(active["node_run_ids"])
                and active["node_run_ids"][active["next_node_index"]] in self._acceptance_candidates.get(
                    active["chain_run_id"], {}) else ["retry_archive", "close"]) if status == "archive_failed" else
            ["close"] if active and status in ("failed", "recovery_unavailable", "budget_exhausted") else [])
        retry_permission = (self._failed_retry_permission(data, active)
                            if available and active and status == "failed" else None)
        if retry_permission and retry_permission["allowed"]:
            actions.insert(0, "retry_failed_node")
        nodes = []
        for node in document["nodes"]:
            own = [run for run in runs if run["node_binding_id"] == node_mapping.get(node["node_binding_id"])]
            original_node_id = node_mapping.get(node["node_binding_id"])
            planned_id = (selected["node_run_ids"][selected["ordered_nodes"].index(original_node_id)]
                          if selected and original_node_id in selected["ordered_nodes"] else None)
            run = next((item for item in own if item["run_id"] == planned_id), own[-1] if own else None)
            refs = run["output_refs"] if run else {}
            nodes.append({"node_binding_id": node["node_binding_id"], "label": node["title"],
                "status": run["status"] if run else "idle", "run_id": run["run_id"] if run else None,
                "revision": run["revision"] if run else None, "diagnostic": run["diagnostic"] if run else None,
                "source_workflow_session_id": run["workflow_session_id"] if run else None,
                "budget": None,
                "outputs": {port: selected_outputs[oid]["payload"]
                            for port, oid in refs.items()}})
            if retry_permission and run and run["run_id"] == retry_permission["node_run_id"]:
                nodes[-1]["diagnostic"] = {**(nodes[-1]["diagnostic"] or {}), "failure_retry": retry_permission}
        data_state = data.state.variables(sid)
        head = data.records.head(sid)
        archived = (data.records.archived("session", sid)
                    or data.records.archived("definition", session["workflow_definition_id"]))
        available_actions = actions if available or active else []
        return {**session, **data.records.session_metadata(sid),
            "data_revision": data_state["revision"], "head_revision": head["revision"],
            "head_commit_id": head["head_commit_id"], "status": status,
            "selected_chain_run_id": selected["chain_run_id"] if selected else None,
            "can_submit": active is None and available and not archived,
            "available_actions": available_actions if not archived else [],
            "readonly": not available or archived, "archived": archived,
            "nodes": nodes, "chains": chains, "outputs": outputs,
            "data": data_state, "private_states": data.records.private_states(sid), "messages": [],
            "objects": data.state.objects(sid),
            "history_refs": history_refs,
            "inherited_history": [all_chains[identity] for identity in history_refs
                                  if identity in all_chains and all_chains[identity]["workflow_session_id"] != sid]}

    def _stable(self, source, sid, expected_revision, expected_data_revision=None,
                expected_head_revision=None):
        if hasattr(source, "records"):
            data = source
            session = data.records.session(sid)
            self._require_session_active_in(data.configuration, session)
            require(session["revision"] == _revision(expected_revision),
                    "stale_revision", "Session changed", 409)
            require(session["active_chain_run_id"] is None,
                    "active_execution_not_copyable", "Session execution is not settled", 409)
            if expected_data_revision is not None:
                require(data.state.variables(sid)["revision"]
                        == _revision(expected_data_revision, zero=True),
                        "stale_data_revision", "Session data changed", 409)
            if expected_head_revision is not None:
                require(data.records.head(sid)["revision"]
                        == _revision(expected_head_revision),
                        "stale_head_revision", "Session Head changed", 409)
            return session
        session = source.get("workflow_session", workflow_session_id=sid)
        self._require_session_active(source, session)
        require(session["revision"] == _revision(expected_revision), "stale_revision", "Session changed", 409)
        require(session["active_chain_run_id"] is None, "active_execution_not_copyable", "Session execution is not settled", 409)
        if expected_data_revision is not None:
            require(ProgramVariableStore(source.store).current(sid)["revision"] == _revision(expected_data_revision, zero=True),
                    "stale_data_revision", "Session data changed", 409)
        if expected_head_revision is not None:
            require(self._head(source, sid)["revision"] == _revision(expected_head_revision), "stale_head_revision", "Session Head changed", 409)
        return session

    def start(self, sid, *, expected_revision, idempotency_key, inputs=None):
        self._require_execution_owner()
        request = {"session": sid, "expected_revision": expected_revision, "inputs": inputs if inputs is not None else {}}
        return self._start_run(sid, idempotency_key, request,
                               lambda data, basis: self._prepare_run(
                                   data, sid, expected_revision, request["inputs"], basis=basis))

    def _prepare_run(self, data, sid, expected_revision, inputs, *, event=None,
                     input_overrides=None, chat_request=None, basis):
        registry = data.registry
        definitions = basis.definitions
        session = self._stable(data, sid, expected_revision)
        require(not self._package_diagnostics, "package_missing_dependency",
                "Restore the saved packages before running workflows", 409)
        document = data.configuration.fixed_document(
            session["workflow_definition_id"], session["definition_revision"])
        current_configuration = data.configuration.current_configuration(
            session["workflow_definition_id"])
        if (current_configuration is not None
                and current_configuration["fixed_definition_revision"] == session["definition_revision"]):
            document = self._configuration_view(
                data.configuration, session["workflow_definition_id"],
                registry=registry, definitions=definitions)
        self._chat_input_overrides(document, {} if input_overrides is None else input_overrides)
        input_error = "graph_event_payload_invalid" if event else "invalid_request"
        require(type(inputs) is dict, input_error, "External inputs must be named")
        require(not {"_workflow_frozen_resources", "_workflow_chat"} & set(inputs),
                input_error, "Reserved runtime inputs cannot be supplied")
        if not event:
            validate_json_value(inputs)
        from .graph_contracts import GraphCompiler
        compiler = GraphCompiler(registry)
        plan = (compiler.compile_event(document, event["event_id"], event["schema_version"], external_inputs=inputs)
                if event else compiler.compile(document, external_inputs=inputs))
        data.type_evidence.check_registry(registry.data_types)
        self._check_implementations(document, plan, registry=registry)
        frozen_resources = self._preflight_capabilities(data.records, sid, plan, registry=registry)
        chain_id = _uid()
        runtime_resources = frozen_resources.pop("current_global_resources", {})
        from .content_configuration import CONTENT_RESOURCE_TYPES
        self._resource_frames[chain_id] = {
            node_id: [record for record in records if record["type_id"] not in CONTENT_RESOURCE_TYPES]
            for node_id, records in runtime_resources.items()}
        self._prepare_public_capabilities(sid, chain_id, plan, runtime_resources, registry=registry)
        self._execution_registries[chain_id] = basis
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        if not event:
            self._prepare_chat_run(
                data, session, document, plan, chain_id, inputs, chat_request=chat_request)
        run_ids = [_uid() for _ in plan.ordered_node_ids]
        for node_id, run_id in zip(plan.ordered_node_ids, run_ids):
            data.records.put_node_run(graph_record("node_run", profile="node", run_id=run_id,
                workflow_session_id=sid, node_binding_id=node_id, chain_run_id=chain_id,
                status="prepared", revision=1, input_refs={}, input_values={}, output_refs={}, reads=[], effects=[],
                config=nodes[node_id]["config"], diagnostic=None,
                input_storage=plan.definitions[node_id].input_storage))
        chain = graph_record("chain_run", chain_run_id=chain_id, workflow_session_id=sid,
            workflow_definition_id=session["workflow_definition_id"], definition_revision=session["definition_revision"],
            status="prepared", revision=1, targets=list(plan.target_node_ids), ordered_nodes=list(plan.ordered_node_ids),
            node_run_ids=run_ids, completed_nodes=[], next_node_index=0,
            inputs={**inputs, "_workflow_frozen_resources": frozen_resources}, outputs={}, diagnostic=None,
            execution_kind="event" if event else "round", event=event,
            base_commit_id=data.records.head(sid)["head_commit_id"])
        data.records.put_chain(chain)
        session["revision"] += 1
        session["active_chain_run_id"] = chain_id
        data.records.put_session(session)
        self._save_manifest(data, "chain_start", chain_id, session, status="prepared")
        return self._view(data, sid)

    def _start_run(self, sid, idempotency_key, request, prepare, *, operation="graph.run.start"):
        self._require_execution_owner()
        accepted = []

        with self._lock:
            basis = self._capture_run_basis()
            previous_frames = set(self._resource_frames)

            def change(repo):
                data = self._data_management.execution_transaction(repo, basis=basis)
                response = prepare(data, basis)
                accepted.append(response["active_chain_run_id"])
                return response

            try:
                response = self._change(operation, idempotency_key, request, change)
            except BaseException:
                for unaccepted in set(self._resource_frames) - previous_frames:
                    self._release_public_capabilities(sid, unaccepted)
                    self._resource_frames.pop(unaccepted, None)
                    self._execution_registries.pop(unaccepted, None)
                raise
        # A persisted receipt replay never creates another execution frame.
        if not accepted:
            return response
        chain_id = accepted[0]
        with self._lock:
            if chain_id not in self._futures:
                basis = self._require_run_basis(chain_id)
                with closing(self._store()) as store:
                    data = self._data_management.execution_read(
                        GraphRecordStore(store), registry=basis.registry)
                    chain = data.records.chain(chain_id)
                if chain["status"] == "prepared":
                    self._futures[chain_id] = self._executor.submit(self._execute, sid, chain_id)
        return response

    def _save_effects(self, data, sid, document, event):
        current = data.state.variables(sid)
        state = validate_program_state(event["state"])
        shared_effect = any(effect["kind"] in ("variable_register", "variable_assign", "shared_write")
                            for effect in event["effects"])
        if not shared_effect:
            event["state"] = current
        elif not canonical_bytes({key: value for key, value in current.items() if key != "revision"}) \
                == canonical_bytes({key: value for key, value in state.items() if key != "revision"}):
            require(current["revision"] == state["revision"], "stale_data_revision", "Concurrent data update conflicts with node effect", 409)
            saved = data.state.write_variables(
                sid, state, expected_revision=current["revision"],
                key="graph-node-effect:" + event["node_run_id"],
                request={"run_id": event["node_run_id"], "state": state})["state"]
            event["state"] = saved
        else:
            event["state"] = current
        self._save_private(data, sid, document, event["private_states"])
        return data.state.apply_objects(
            sid, node_id=event["node_binding_id"],
            writes=event.get("object_writes", []),
            write_evidence=event.get("write_evidence"))

    def _settle_node_result(self, data, sid, chain_id, document, event, run):
        """Stage exact artifacts, writes and receipt-derived outputs in one transaction."""
        registry = data.registry
        node = next(item for item in document["nodes"] if item["node_binding_id"] == run["node_binding_id"])
        entry = registry.get(node["component_id"], node["component_version"])
        ports = {port.port_id: port for port in entry.definition.ports(node["config"])[1]}
        outputs = deepcopy(event["outputs"])
        validated_reference_ids = {
            ref["output_id"] for port, payload in outputs.items()
            for ref in registry.data_types.references(
                ports[port].data_type, ports[port].data_schema_version, payload, scope="content")
            if ref["scope"] == "artifact"}
        refs = event["candidate_output_refs"]
        finalize = None
        if entry.settlement_builder is not None:
            prepared = entry.settlement_builder(event["settlement_context"], deepcopy(outputs), deepcopy(refs))
            require(type(prepared) is dict and {"outputs", "object_writes", "finalize"} <= set(prepared)
                    <= {"outputs", "object_writes", "finalize", "write_evidence"}
                    and (prepared["finalize"] is None or callable(prepared["finalize"])),
                    "graph_invalid_settlement", "Settlement builder returned an invalid result")
            outputs, finalize = prepared["outputs"], prepared["finalize"]
            event["object_writes"] = prepared["object_writes"]
            event["write_evidence"] = prepared.get("write_evidence")
            run["reads"] = deepcopy(event["settlement_context"].reads)
        require(type(outputs) is dict and set(outputs) <= set(ports),
                "graph_invalid_output", "Settlement contains unknown output ports")
        run["output_refs"] = {port: refs[port] for port in (
            ports if entry.settlement_builder is not None else outputs)}
        staged_refs = deepcopy(run["output_refs"])

        def save_outputs(values):
            for port, payload in values.items():
                checked = registry.validate_content(payload, ports[port].data_type, ports[port].data_schema_version)
                data.records.put_output(graph_record(
                    "workflow_output", output_id=refs[port], workflow_session_id=sid,
                    chain_run_id=chain_id, run_id=run["run_id"],
                    node_binding_id=run["node_binding_id"], port_id=port, payload=checked))

        # Only this transaction can see the provisional producer/artifacts.
        # Generic object validators resolve them through their ordinary
        # accepted-producer checks; a rollback removes all provisional rows.
        save_outputs(outputs)
        data.records.put_node_run(run)
        receipts = self._save_effects(data, sid, document, event)
        if finalize is not None:
            outputs = finalize(deepcopy(outputs), deepcopy(refs), deepcopy(receipts))
        require(type(outputs) is dict and set(outputs) <= set(ports)
                and {port for port, definition in ports.items() if definition.required} <= set(outputs),
                "graph_invalid_output", "Settled result has missing or unknown output ports")
        allowed = set(event.get("allowed_output_ids", [])) | set(refs.values()) | validated_reference_ids
        for port, payload in outputs.items():
            checked, references = registry.data_types.validate_and_references(
                ports[port].data_type, ports[port].data_schema_version, payload, scope="content")
            require(all(ref["scope"] != "artifact" or ref["output_id"] in allowed for ref in references),
                    "graph_artifact_reference_denied", "Settled output contains an unauthorized artifact reference")
        save_outputs(outputs)
        run["output_refs"] = {port: refs[port] for port in outputs}
        if run["output_refs"] != staged_refs:
            run["revision"] += 1
        event["outputs"] = deepcopy(outputs)

    def _node_event(self, sid, chain_id, event):
        basis = self._require_run_basis(chain_id)
        with self._lock, closing(self._store()) as store:
            data = self._data_management.execution_read(
                GraphRecordStore(store), registry=basis.registry)
            previous_revision = data.records.node_run(event["node_run_id"])["revision"]
        def change(data):
            chain = data.records.chain(chain_id)
            session = data.records.session(sid)
            require(session["active_chain_run_id"] == chain_id and chain["status"] == "running",
                    "execution_owner_changed", "Execution callback owner is no longer active", 409)
            run = data.records.node_run(event["node_run_id"])
            require(run["node_binding_id"] == event["node_binding_id"] and run["chain_run_id"] == chain_id,
                    "execution_owner_changed", "Node callback owner differs", 409)
            run["status"] = event["event"]
            run["revision"] += 1
            document = data.configuration.fixed_document(
                chain["workflow_definition_id"], chain["definition_revision"])
            run["input_values"] = ({} if run.get("input_storage") == "references"
                                   else deepcopy(event["inputs"]))
            run["input_refs"] = {}
            if run["schema_version"] >= 4:
                run["control_refs"] = []
                for edge in document.get("control_edges", []):
                    if edge["target_node_id"] == run["node_binding_id"]:
                        source_id = edge["source_node_id"]
                        require(source_id in chain["completed_nodes"], "execution_dependency_unaccepted",
                                "Control predecessor has not completed", 409)
                        source_run_id = chain["node_run_ids"][chain["ordered_nodes"].index(source_id)]
                        run["control_refs"].append({"edge_id": edge["edge_id"], "run_id": source_run_id})
            run["diagnostic"] = deepcopy(event["diagnostic"])
            unresolved = []
            for edge in document["edges"]:
                if edge["target_node_id"] == run["node_binding_id"]:
                    oid = chain["outputs"].get(edge["source_node_id"], {}).get(edge["source_port_id"])
                    if oid is None and event["event"] == "failed":
                        diagnostic = run["diagnostic"] or {}
                        require(diagnostic.get("code") == "graph_linked_output_missing"
                                and diagnostic.get("node_id") == run["node_binding_id"]
                                and edge["source_node_id"] in chain["completed_nodes"],
                                "storage_contract_violation", "Input source was not durably released", 500)
                        unresolved.append({
                            "edge_id": edge["edge_id"], "source_node_id": edge["source_node_id"],
                            "source_port_id": edge["source_port_id"], "target_port_id": edge["target_port_id"],
                            "order": edge["order"]})
                        continue
                    require(oid is not None, "storage_contract_violation", "Input source was not durably released", 500)
                    run["input_refs"].setdefault(edge["target_port_id"], []).append({
                        "edge_id": edge["edge_id"], "output_id": oid, "order": edge["order"]})
            if unresolved:
                require(any(item["edge_id"] == run["diagnostic"].get("edge_id")
                            and item["target_port_id"] == run["diagnostic"].get("port_id")
                            for item in unresolved), "storage_contract_violation",
                        "Missing input diagnostic differs from unresolved sources", 500)
                run["diagnostic"]["unresolved_inputs"] = sorted(
                    unresolved, key=lambda item: (item["target_port_id"], item["order"], item["edge_id"]))
            for refs in run["input_refs"].values():
                refs.sort(key=lambda item: (item["order"], item["edge_id"]))
            run["reads"] = deepcopy(event["reads"])
            run["effects"] = deepcopy(event["effects"])
            if event["event"] == "running":
                event["state"] = data.state.variables(sid)
            elif event["event"] == "succeeded":
                self._settle_node_result(data, sid, chain_id, document, event, run)
            else:
                event["state"] = data.state.variables(sid)
            if event["event"] == "succeeded":
                chain["outputs"][run["node_binding_id"]] = deepcopy(run["output_refs"])
                chain["completed_nodes"].append(run["node_binding_id"])
                chain["next_node_index"] += 1
                chain["revision"] += 1
                data.records.put_chain(chain)
            data.records.put_node_run(run)
            session["revision"] += 1
            data.records.put_session(session)
            return {"state": event["state"], "object_states": data.state.objects(sid),
                    "output_refs": run["output_refs"], "outputs": event["outputs"]}
        response = self._change("graph.node." + event["event"], event["node_run_id"] + ":" + event["event"] + ":" + str(previous_revision),
            {"session": sid, "chain": chain_id,
             "event": {key: value for key, value in event.items() if key != "settlement_context"}},
            lambda repo: change(self._data_management.execution_transaction(
                repo, basis=self._require_run_basis(chain_id))))
        event["state"] = response["state"]
        event["object_states"] = response["object_states"]
        event["output_refs"] = response["output_refs"]
        event["outputs"] = response["outputs"]
        if event["event"] == "succeeded":
            event["settlement_committed"] = True
            self._acceptance_candidates.get(chain_id, {}).pop(event["node_run_id"], None)
        try:
            self._accept_frontend_node_output(sid, chain_id, event)
            self._accept_public_outputs(sid, chain_id, event)
        except Exception:
            raise graph_error("result_committed_notification_failed",
                              "Node result committed but its capability notification failed", 500) from None

    def _retained_result_recovery(self, context):
        """Ask registered capability owners for completed-result-only recovery."""
        from .runtime_hosting import InvocationOwner
        chain_id = context.chain_run_id
        host = self._runtime_hosts.get(chain_id)
        owner = InvocationOwner(context.workflow_session_id, chain_id,
                                context.node_binding_id, context.node_run_id)
        if host is not None and host.has_pending_acceptance(owner):
            return lambda: host.retry_acceptance(owner).outputs
        service_run = self._service_runs.get(chain_id)
        permitted = ({reference for reference, _ in service_run.routes.get(
            context.node_binding_id, {}).values()} if service_run is not None else set())
        candidates = []
        for reference in permitted:
            service = service_run.instances[reference]
            pending = getattr(service, "has_pending_acceptance", None)
            recover = getattr(service, "recover_acceptance", None)
            if callable(pending) and callable(recover) and pending(context):
                candidates.append(service)
        require(len(candidates) <= 1, "host_result_recovery_ambiguous",
                "Multiple declared services retain results; node-level recovery is unavailable", 409)
        return (lambda: candidates[0].recover_acceptance(context)) if candidates else None

    def _completed_result_matches(self, target, sid, chain, candidate, *, registry):
        if candidate is None:
            return False
        data = target if hasattr(target, "records") else self._data_management.execution_read(
            target, registry=registry)
        result = candidate["result"]
        session = data.records.session(sid)
        return (result.status == "succeeded" and result.diagnostic is None
                and candidate["definition"] == (chain["workflow_definition_id"], chain["definition_revision"])
                and candidate["definition"] == (session["workflow_definition_id"], session["definition_revision"])
                and session["active_chain_run_id"] == chain["chain_run_id"]
                and result.next_node_index == chain["next_node_index"] == len(chain["ordered_nodes"])
                and chain["completed_nodes"] == chain["ordered_nodes"]
                and result.artifact_refs == chain["outputs"]
                and [run["node_binding_id"] for run in result.node_runs] == chain["ordered_nodes"]
                and [run["node_run_id"] for run in result.node_runs] == chain["node_run_ids"]
                and all(run["event"] == "succeeded" for run in result.node_runs)
                and all(data.records.node_run(identity)["status"] == "succeeded"
                        and data.records.node_run(identity)["output_refs"] == chain["outputs"].get(node_id)
                        for node_id, identity in zip(chain["ordered_nodes"], chain["node_run_ids"]))
                and candidate["basis"] == {
                    "state": data.state.variables(sid),
                    "private": data.records.private_states(sid),
                    "objects": data.state.objects(sid)})

    def _append_success_checkpoint(self, data, session, *, checkpoint_id=None):
        from .checkpoint_collection import collect_checkpoint_state_data
        from .runtime_checkpoints import CHECKPOINT_SCHEMA_VERSION, make_diff

        workflow_id = session["workflow_definition_id"]
        session_id = session["workflow_session_id"]
        definition_revision = session["definition_revision"]
        document = data.configuration.fixed_document(workflow_id, definition_revision)
        state = collect_checkpoint_state_data(data, session_id, document)
        previous = data.settlement.latest_checkpoint(session)
        if previous is None:
            parent_id = None
            state_storage = {"mode": "full", "state": state}
        else:
            prefix = data.settlement.checkpoint_prefix(session, previous["checkpoint_id"])
            parent_state = data.settlement.materialize_checkpoint(session, previous["checkpoint_id"])
            parent_id = previous["checkpoint_id"]
            state_storage = {
                "mode": "diff",
                "base_checkpoint_id": prefix[0]["checkpoint_id"],
                **make_diff(parent_state, state),
            }
        data.settlement.insert_checkpoint(session, {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id or _uid(),
            "workflow_definition_id": workflow_id,
            "workflow_session_id": session_id,
            "fixed_definition_revision": definition_revision,
            "parent_checkpoint_id": parent_id,
            "state_storage": state_storage,
        })
        data.settlement.inject_checkpoint_write()

    def _execute(self, sid, chain_id):
        from .graph_contracts import GraphCompiler
        from .graph_execution import execute_graph
        run_ids = {}
        registry = None
        try:
            def begin(data):
                chain = data.records.chain(chain_id)
                require(chain["status"] in ("prepared", "paused", "archive_failed"), "invalid_state", "Execution cannot start", 409)
                chain["status"] = "running"
                chain["revision"] += 1
                data.records.put_chain(chain)
                return chain
            with self._lock:
                basis = self._require_run_basis(chain_id)
                with closing(self._store()) as store:
                    data = self._data_management.execution_read(
                        GraphRecordStore(store), registry=basis.registry)
                    existing = data.records.chain(chain_id)
            chain = self._change(
                "graph.run.begin", chain_id + ":begin:" + str(existing["revision"]),
                {"chain": chain_id, "revision": existing["revision"]},
                lambda repo: begin(self._data_management.execution_transaction(
                    repo, basis=basis)))
            registry = basis.registry
            with self._lock:
                self._resuming.discard(chain_id)
            with self._lock, closing(self._store()) as store:
                store._connection.execute("BEGIN")
                data = self._data_management.execution_read(
                    GraphRecordStore(store), registry=basis.registry)
                document = data.configuration.fixed_document(
                    chain["workflow_definition_id"], chain["definition_revision"])
                planned_runs = dict(zip(chain["ordered_nodes"], chain["node_run_ids"]))
                for node in document["nodes"]:
                    run_id = planned_runs.get(node["node_binding_id"])
                    if run_id is not None:
                        node["config"] = data.records.node_run(run_id)["config"]
                state = data.state.variables(sid)
                private = data.records.private_states(sid)
                objects = data.state.objects(sid)
                output_records = {row["output_id"]: row for row in data.records.all_outputs()}
                outputs = {node_id: {port: output_records[oid]["payload"] for port, oid in refs.items()}
                           for node_id, refs in chain["outputs"].items()}
                completed_events = [{"event": "succeeded", "node_binding_id": node_id,
                                     "node_run_id": planned_runs[node_id]}
                                    for node_id in chain["completed_nodes"]]
                chat_contexts = self._chat_execution_contexts(
                    data, sid, chain_id, document)
            run_ids = dict(zip(chain["ordered_nodes"], chain["node_run_ids"]))
            retained = self._acceptance_candidates.setdefault(chain_id, {}).get("completed_execution")
            if retained is not None:
                with self._lock, closing(self._store()) as store:
                    data = self._data_management.execution_read(
                        GraphRecordStore(store), registry=registry)
                    require(self._completed_result_matches(
                                data, sid, data.records.chain(chain_id), retained,
                                registry=registry),
                            "graph_acceptance_basis_changed", "Completed result state basis changed", 409)
                result = retained["result"]
            else:
                compiler = GraphCompiler(registry)
                event = chain.get("event")
                plan = (compiler.compile_event(document, event["event_id"], event["schema_version"])
                        if chain.get("execution_kind", "round") == "event" else compiler.compile(document))
                result = execute_graph(plan, registry, workflow_session_id=sid, chain_run_id=chain_id,
                    node_run_ids=run_ids, state=state, private_states=private, external_inputs=chain["inputs"],
                    object_states=objects,
                    artifact_refs=chain["outputs"],
                    outputs=outputs, node_runs=completed_events, start_index=chain["next_node_index"],
                    should_pause=lambda: chain_id in self._pauses or self._closed,
                    on_node=lambda event: self._node_event(sid, chain_id, event), host=self._host_call,
                    validate_output_references=lambda context, references:
                        self._validate_runtime_references(context, references, registry=registry),
                    runtime_host=self._host_for_chain(chain_id, registry),
                    acceptance_cache=self._acceptance_candidates[chain_id],
                    result_recovery=self._retained_result_recovery,
                    chat_append_contexts=chat_contexts)
                if result.status == "succeeded":
                    with self._lock, closing(self._store()) as store:
                        data = self._data_management.execution_read(
                            GraphRecordStore(store), registry=registry)
                        retained = {"result": deepcopy(result),
                            "definition": (chain["workflow_definition_id"], chain["definition_revision"]),
                            "basis": {"state": data.state.variables(sid),
                                      "private": data.records.private_states(sid),
                                      "objects": data.state.objects(sid)},
                            "finish_key": chain_id + ":finish:" + str(chain["revision"])}
                        require(self._completed_result_matches(
                                    data, sid, data.records.chain(chain_id), retained,
                                    registry=registry),
                                "storage_contract_violation", "Completed result differs from accepted execution", 500)
                        self._acceptance_candidates[chain_id]["completed_execution"] = retained
            def finish(data):
                current = data.records.chain(chain_id)
                session = data.records.session(sid)
                if result.status == "succeeded":
                    require(self._completed_result_matches(data, sid, current, retained, registry=registry),
                            "graph_acceptance_basis_changed", "Completed result state basis changed", 409)
                current["status"] = result.status
                current["diagnostic"] = result.diagnostic
                if result.status == "failed":
                    try:
                        permission = self._capture_failed_retry(data, current)
                    except Exception as error:
                        permission = {"allowed": False,
                                      "reason_code": getattr(error, "reason_code", "failed_retry_unavailable"),
                                      "node_run_id": current["node_run_ids"][current["next_node_index"]],
                                      "classification": None}
                    current["diagnostic"] = {**(current["diagnostic"] or {}), "failure_retry": permission}
                current["revision"] += 1
                data.records.put_chain(current)
                session["revision"] += 1
                if result.status == "succeeded":
                    session["active_chain_run_id"] = None
                data.records.put_session(session)
                if result.status == "succeeded" and current.get("execution_kind", "round") == "round":
                    checkpoint_id = _uid()
                    self._bind_chat_success(data, session, chain_id, checkpoint_id)
                    self._commit(data, session, source={"kind": "completed_execution", "chain_run_id": chain_id})
                    self._append_success_checkpoint(data, session, checkpoint_id=checkpoint_id)
                self._save_manifest(data, "chain_end", chain_id, session,
                                    status=result.status)
                return {"status": result.status}
            self._change("graph.run.finish", retained["finish_key"] if retained is not None
                         else chain_id + ":finish:" + str(chain["revision"]),
                         {"chain": chain_id, "result": result.status},
                         lambda repo: finish(self._data_management.execution_transaction(
                             repo, basis=self._require_run_basis(chain_id))))
            if result.status == "succeeded":
                self._refresh_chat_snapshots(sid)
            self._frontend_node_state_changed(sid)
            if result.status == "succeeded" or result.status == "failed" and chain_id not in self._failed_retry_candidates:
                with self._lock:
                    self._release_public_capabilities(sid, chain_id)
                    self._resource_frames.pop(chain_id, None)
                    self._execution_registries.pop(chain_id, None)
                    self._acceptance_candidates.pop(chain_id, None)
                    self._failed_retry_candidates.pop(chain_id, None)
        except BaseException as exc:
            # Only an exact retained result can retry.
            # Unknown execution and rejected business results cannot be replayed.
            archive_retry = False
            try:
                def fail(repo):
                    basis = self._execution_registries.get(chain_id)
                    data = (self._data_management.execution_transaction(repo, basis=basis)
                            if basis is not None else None)
                    chain = (data.records.chain(chain_id) if data is not None
                             else repo.get("chain_run", chain_run_id=chain_id))
                    if chain["status"] in ("succeeded", "closed", "recovery_unavailable", "failed"):
                        return None
                    index = chain["next_node_index"]
                    pending = self._acceptance_candidates.get(chain_id, {})
                    result_retry = (index < len(chain["node_run_ids"])
                                    and chain["node_run_ids"][index] in pending
                                    and getattr(exc, "reason_code", None) is None)
                    run_registry = basis.registry if basis is not None else registry
                    completion_retry = (run_registry is not None
                                        and getattr(exc, "reason_code", None) is None
                                        and self._completed_result_matches(
                                            data if data is not None else repo, sid, chain,
                                            pending.get("completed_execution"),
                                            registry=run_registry))
                    retryable = completion_retry or result_retry
                    chain["status"] = "archive_failed" if retryable else "recovery_unavailable"
                    acknowledged_failure = getattr(exc, "reason_code", None) == "result_committed_notification_failed"
                    ambiguous_recovery = getattr(exc, "reason_code", None) == "host_result_recovery_ambiguous"
                    missing_basis = getattr(exc, "reason_code", None) == "run_basis_unavailable"
                    chain["diagnostic"] = {
                        "reason_code": ("run_basis_unavailable" if missing_basis else
                                        "checkpoint_commit_pending" if completion_retry else
                                        "result_acceptance_pending" if result_retry else
                                        "result_committed_notification_failed" if acknowledged_failure else
                                        "host_result_recovery_ambiguous" if ambiguous_recovery else
                                        "execution_result_unknown"),
                        "message": ("This invocation has no retained run basis" if missing_basis else
                                    "Completed execution awaits checkpoint commit" if completion_retry else
                                    "Completed result awaits acceptance" if result_retry else
                                    "Node result is committed; local completion notification failed; business replay is unavailable"
                                    if acknowledged_failure else
                                    "Multiple declared services retain results; node-level recovery is unavailable"
                                    if ambiguous_recovery else "Execution could not preserve its completion boundary")}
                    chain["revision"] += 1
                    if data is not None:
                        data.records.put_chain(chain)
                    else:
                        repo.put("chain_run", chain)
                    for identity in chain["node_run_ids"]:
                        run = (data.records.node_run(identity) if data is not None
                               else repo.get("node_run", run_id=identity))
                        if run["status"] == "running":
                            run["status"] = ("archive_failed" if result_retry and identity in pending
                                             else "recovery_unavailable")
                            run["revision"] += 1
                            if data is not None:
                                data.records.put_node_run(run)
                            else:
                                repo.put("node_run", run)
                    session = (data.records.session(sid) if data is not None
                               else repo.get("workflow_session", workflow_session_id=sid))
                    session["revision"] += 1
                    if data is not None:
                        data.records.put_session(session)
                    else:
                        repo.put("workflow_session", session)
                    if data is not None:
                        self._save_manifest(data, "chain_end", chain_id, session,
                                            status=chain["status"])
                    else:
                        self._save_manifest(repo, "chain_end", chain_id, session,
                                            status=chain["status"], registry=run_registry,
                                            fallback_manifest=("chain_start", chain_id))
                    return {"archive_retry": retryable}
                with self._lock, self._data_management.read_execution(
                        registry=registry if registry is not None else self.registry) as data:
                    failed_revision = data.records.chain(chain_id)["revision"]
                recovery = self._change("graph.run.unconfirmed", chain_id + ":unknown:" + str(failed_revision),
                                        {"chain": chain_id, "revision": failed_revision}, fail)
                self._frontend_node_state_changed(sid)
                archive_retry = bool(recovery and recovery["archive_retry"])
            except BaseException:
                pass
            with self._lock:
                if not archive_retry:
                    self._execution_errors[chain_id] = exc
                    self._release_public_capabilities(sid, chain_id)
                    self._resource_frames.pop(chain_id, None)
                    self._execution_registries.pop(chain_id, None)
                    self._acceptance_candidates.pop(chain_id, None)
                    self._failed_retry_candidates.pop(chain_id, None)

    def _control_change_data(self, data, sid, action, expected_revision):
        session = data.records.session(sid)
        self._require_session_active_in(data.records, session)
        require(session["revision"] == _revision(expected_revision),
                "stale_revision", "Session changed", 409)
        if action in ("resume", "retry_archive", "retry_acceptance", "retry_failed_node"):
            require(not self._package_diagnostics, "package_missing_dependency",
                    "Restore the saved packages before continuing execution", 409)
        chain_id = session["active_chain_run_id"]
        require(chain_id is not None, "invalid_state", "No active execution", 409)
        chain = data.records.chain(chain_id)
        if action == "pause":
            require(chain["status"] in ("prepared", "running"),
                    "invalid_state", "Cannot pause this execution", 409)
        elif action == "retry_failed_node":
            require(chain["status"] == "failed" and chain_id in self._futures,
                    "failed_retry_unavailable",
                    "No same-process failed invocation is retained", 409)
            self._prepare_failed_retry(data, chain)
        elif action in ("resume", "retry_archive", "retry_acceptance"):
            allowed = (("archive_failed",) if action in ("retry_archive", "retry_acceptance")
                       else ("paused",))
            require(chain["status"] in allowed and chain_id in self._futures,
                    "recovery_unavailable",
                    "Execution has no same-process continuation", 409)
            pending = self._acceptance_candidates.get(chain_id, {})
            index = chain["next_node_index"]
            has_candidate = (index < len(chain["node_run_ids"])
                             and chain["node_run_ids"][index] in pending)
            require(action != "retry_acceptance" or has_candidate,
                    "recovery_unavailable",
                    "No retained result exists for this exact invocation", 409)
            if action == "retry_acceptance":
                basis = pending[chain["node_run_ids"][index]]["basis"]
                require(basis["state"] == data.state.variables(sid)
                        and basis["private"] == data.records.private_states(sid)
                        and basis["objects"] == data.state.objects(sid),
                        "graph_acceptance_basis_changed",
                        "Retained result state basis changed", 409)
            if action == "retry_archive" and "completed_execution" in pending:
                require(self._completed_result_matches(
                            data, sid, chain, pending["completed_execution"],
                            registry=data.registry),
                        "graph_acceptance_basis_changed",
                        "Completed result state basis changed", 409)
            require(action != "retry_archive" or not has_candidate,
                    "invalid_state",
                    "Use the result acceptance action for this invocation", 409)
            chain["status"] = "prepared"
            chain["revision"] += 1
            data.records.put_chain(chain)
        else:
            require(chain["status"] in ("failed", "recovery_unavailable", "archive_failed",
                                        "paused", "budget_exhausted"),
                    "invalid_state",
                    "Only settled failures or acknowledged pauses can close", 409)
            chain["status"] = "closed"
            chain["revision"] += 1
            data.records.put_chain(chain)
            for identity in chain["node_run_ids"]:
                run = data.records.node_run(identity)
                if run["status"] not in ("succeeded", "closed"):
                    run["status"] = "closed"
                    run["revision"] += 1
                    data.records.put_node_run(run)
            session["active_chain_run_id"] = None
            if chain.get("execution_kind", "round") == "round":
                self._commit(data, session,
                             source={"kind": "closed_execution", "chain_run_id": chain_id})
        session["revision"] += 1
        data.records.put_session(session)
        if action == "close":
            self._save_manifest(data, "chain_end", chain_id, session, status="closed")
        return self._view(data, sid)

    def _control_change_current(self, repo, sid, action, expected_revision):
        """Cross-process control keeps the original action-specific diagnostics.

        A released binding has no writable execution data session.  This branch
        is therefore limited to commands that do not execute a node: pause and
        close may update the persisted command state, while resume/retry keep
        their original same-process checks and fail before any node runs.
        """
        session = repo.get("workflow_session", workflow_session_id=sid)
        self._require_session_active(repo, session)
        require(session["revision"] == _revision(expected_revision),
                "stale_revision", "Session changed", 409)
        if action in ("resume", "retry_archive", "retry_acceptance", "retry_failed_node"):
            require(not self._package_diagnostics, "package_missing_dependency",
                    "Restore the saved packages before continuing execution", 409)
        chain_id = session["active_chain_run_id"]
        require(chain_id is not None, "invalid_state", "No active execution", 409)
        chain = repo.get("chain_run", chain_run_id=chain_id)
        if action == "pause":
            require(chain["status"] in ("prepared", "running"),
                    "invalid_state", "Cannot pause this execution", 409)
        elif action == "retry_failed_node":
            require(chain["status"] == "failed" and chain_id in self._futures,
                    "failed_retry_unavailable",
                    "No same-process failed invocation is retained", 409)
            require(False, "failed_retry_unavailable",
                    "The failed invocation has no retained run basis", 409)
        elif action in ("resume", "retry_archive", "retry_acceptance"):
            allowed = (("archive_failed",) if action in ("retry_archive", "retry_acceptance")
                       else ("paused",))
            require(chain["status"] in allowed and chain_id in self._futures,
                    "recovery_unavailable",
                    "Execution has no same-process continuation", 409)
            require(chain_id in self._execution_registries,
                    "recovery_unavailable",
                    "Execution has no retained run basis", 409)
            pending = self._acceptance_candidates.get(chain_id, {})
            run_registry = self._require_run_basis(chain_id).registry
            index = chain["next_node_index"]
            has_candidate = (index < len(chain["node_run_ids"])
                             and chain["node_run_ids"][index] in pending)
            require(action != "retry_acceptance" or has_candidate,
                    "recovery_unavailable",
                    "No retained result exists for this exact invocation", 409)
            if action == "retry_acceptance":
                basis = pending[chain["node_run_ids"][index]]["basis"]
                require(basis["state"] == ProgramVariableStore(repo.store).current(sid)
                        and basis["private"] == self._private(repo, sid)
                        and basis["objects"] == self._objects(
                            repo, registry=run_registry).current(sid),
                        "graph_acceptance_basis_changed",
                        "Retained result state basis changed", 409)
            if action == "retry_archive" and "completed_execution" in pending:
                require(self._completed_result_matches(
                            repo, sid, chain, pending["completed_execution"],
                            registry=run_registry),
                        "graph_acceptance_basis_changed",
                        "Completed result state basis changed", 409)
            require(action != "retry_archive" or not has_candidate,
                    "invalid_state",
                    "Use the result acceptance action for this invocation", 409)
            chain["status"] = "prepared"
            chain["revision"] += 1
            repo.put("chain_run", chain)
        else:
            require(chain["status"] in ("failed", "recovery_unavailable", "archive_failed",
                                        "paused", "budget_exhausted"),
                    "invalid_state",
                    "Only settled failures or acknowledged pauses can close", 409)
            chain["status"] = "closed"
            chain["revision"] += 1
            repo.put("chain_run", chain)
            for identity in chain["node_run_ids"]:
                run = repo.get("node_run", run_id=identity)
                if run["status"] not in ("succeeded", "closed"):
                    run["status"] = "closed"
                    run["revision"] += 1
                    repo.put("node_run", run)
            session["active_chain_run_id"] = None
            if chain.get("execution_kind", "round") == "round":
                self._commit(repo, session,
                             source={"kind": "closed_execution", "chain_run_id": chain_id})
        session["revision"] += 1
        repo.put("workflow_session", session)
        if action == "close":
            self._save_manifest(repo, "chain_end", chain_id, session, status="closed")
        return self._view(
            self._data_management.execution_read(repo, registry=self.registry), sid)

    def control(self, sid, *, action, expected_revision, idempotency_key):
        self._require_execution_owner()
        require(action in ("pause", "resume", "close", "retry_archive", "retry_acceptance", "retry_failed_node"),
                "invalid_request", "Unknown graph control")
        request = {"session": sid, "action": action, "expected_revision": expected_revision}
        accepted = []
        schedule_resume = False
        with self._lock:
            # A persisted receipt is authoritative even after the original
            # execution was settled, so check it before requiring a live basis.
            with closing(self._store()) as store:
                replay = store.read_receipt_with_digest("graph.run.control", idempotency_key)
            if replay is not None:
                def replay_change(repo):
                    require(False, "storage_contract_violation",
                            "A persisted control receipt was not replayed", 500)
                return self._change("graph.run.control", idempotency_key, request, replay_change)

            with self._data_management.read_execution(registry=self.registry) as probe:
                probe_session = probe.records.session(sid)
                active_chain_id = probe_session["active_chain_run_id"]
            basis = (self._execution_registries.get(active_chain_id)
                     if active_chain_id is not None else None)

            if action == "retry_failed_node":
                # Resource authorization opens its own storage reader. Perform
                # it before the write transaction; the basis is checked again
                # inside that transaction and at actual model dispatch.
                registry = basis.registry if basis is not None else self.registry
                with self._data_management.read_execution(registry=registry) as probe:
                    require(not self._package_diagnostics, "package_missing_dependency",
                            "Restore the saved packages before retrying workflows", 409)
                    session = probe.records.session(sid)
                    require(session["revision"] == _revision(expected_revision),
                            "stale_revision", "Session changed", 409)
                    require(session["active_chain_run_id"] is not None,
                            "failed_retry_unavailable", "No retained failed execution", 409)
                    chain = probe.records.chain(session["active_chain_run_id"])
                    require(chain["status"] == "failed", "failed_retry_unavailable",
                            "No retained failed invocation", 409)
                    permission = self._failed_retry_permission(probe, chain)
                    require(permission["allowed"], permission["reason_code"],
                            "This failed invocation cannot safely restart", 409)
                    candidate = self._failed_retry_candidates[chain["chain_run_id"]]
                    self._validate_failed_retry_candidate(
                        chain["chain_run_id"], candidate, authorize=True)

            def change(repo):
                if basis is not None:
                    data = self._data_management.execution_transaction(repo, basis=basis)
                    require(data.records.session(sid)["active_chain_run_id"] == active_chain_id,
                            "invalid_state", "Active execution changed before control", 409)
                    response = self._control_change_data(
                        data, sid, action, expected_revision)
                else:
                    require(repo.get(
                        "workflow_session", workflow_session_id=sid
                    )["active_chain_run_id"] == active_chain_id,
                        "invalid_state", "Active execution changed before control", 409)
                    response = self._control_change_current(
                        repo, sid, action, expected_revision)
                accepted.append(active_chain_id)
                return response

            response = self._change("graph.run.control", idempotency_key, request, change)
            # Receipt replay observes the original command. Reapplying its
            # in-memory effects could pause a resumed run or dispose its frame.
            if not accepted:
                return response
            chain_id = response["active_chain_run_id"]
            controlled_chain_id = next((chain["chain_run_id"] for chain in response["chains"]
                if action == "close" and chain["status"] == "closed"
                and chain["chain_run_id"] in self._execution_registries), chain_id)
            if action == "pause":
                self._pauses.add(chain_id)
            elif action in ("resume", "retry_archive", "retry_acceptance", "retry_failed_node"):
                if action == "retry_failed_node":
                    self._failed_retry_candidates.pop(chain_id, None)
                self._pauses.discard(chain_id)
                registry = basis.registry if basis is not None else self.registry
                with self._data_management.read_execution(registry=registry) as data:
                    current = data.records.chain(chain_id)
                if current["status"] == "prepared" and chain_id not in self._resuming:
                    self._resuming.add(chain_id)
                    schedule_resume = True
            if action == "close":
                self._pauses.discard(controlled_chain_id)
                self._resuming.discard(controlled_chain_id)
                self._release_public_capabilities(sid, controlled_chain_id)
                self._resource_frames.pop(controlled_chain_id, None)
                self._execution_registries.pop(controlled_chain_id, None)
                self._acceptance_candidates.pop(controlled_chain_id, None)
                self._failed_retry_candidates.pop(controlled_chain_id, None)
        self._frontend_node_state_changed(sid)
        if action == "pause" and chain_id is not None:
            self._control_hosted_invocation(chain_id, action, idempotency_key)
        if schedule_resume:
            self._control_hosted_invocation(chain_id, action, idempotency_key)
            with self._lock:
                self._futures[chain_id] = self._executor.submit(self._execute, sid, chain_id)
        return response

    def wait(self, chain_id, timeout=30):
        future = self._futures.get(chain_id)
        if future is not None:
            future.result(timeout=timeout)
        if chain_id in self._execution_errors:
            raise self._execution_errors[chain_id]

    def close(self):
        with self._lock:
            self._pauses.update(self._futures)
            chains = list(self._futures)
        for chain_id in chains:
            self._control_hosted_invocation(chain_id, "pause", "service-shutdown:" + chain_id)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        with self._lock:
            self._closed = True
            for chain_id in set(self._resource_frames) | set(self._runtime_hosts) | set(self._service_runs):
                self._release_public_capabilities(None, chain_id)
            self._resource_frames.clear()
            self._execution_registries.clear()
            self._acceptance_candidates.clear()
            self._failed_retry_candidates.clear()
            self._service_runs.clear()
            self._frontend_node_snapshots.clear()
            if self._frontend_node_connections is not None:
                self._frontend_node_connections.close()
            if self._lease is not None:
                self._lease.close()


class GraphManagementService(GraphWorkflowService):
    """Management operations over the shared graph domain without execution ownership."""

    def __init__(self, database_path: str | Path):
        self._initialize_service(database_path, execution_owner=False)
        self._provider_configuration = None
        self._content_configuration = None

    @property
    def provider_configuration(self):
        with self._lock:
            if self._provider_configuration is None:
                self._provider_configuration = ProviderConfigurationService(
                    self.database, store_factory=self._store)
            return self._provider_configuration

    @property
    def content_configuration(self):
        with self._lock:
            if self._content_configuration is None:
                self._content_configuration = ContentConfigurationService(
                    self.database, store_factory=self._store,
                    type_registry_factory=lambda: self.registry.data_types)
            return self._content_configuration

    def _stop_owned_runs(self, sessions, key):
        require(all(session["active_chain_run_id"] is None for session in sessions),
                "execution_stop_unconfirmed",
                "An active execution has no existing coordinator to acknowledge stopping", 409)
