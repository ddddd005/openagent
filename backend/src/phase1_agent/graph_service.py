"""Model-independent workflow definitions, sessions and serial graph execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import os
import hashlib
from threading import RLock
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from .contract_json import validate_json_value
from .graph_records import graph_record, graph_error, require, uuid_value
from .graph_store import GraphRecordStore
from .program_variable_store import ProgramVariableStore
from .preparation_program import validate_program_state
from .storage import SqliteStore
from .graph_public import GraphPublic
from .graph_candidates import GraphCandidateHost
from .graph_platform import GraphPlatform
from .graph_runtime_host import GraphRuntimeHost
from .graph_resource_host import GraphResourceHost
from .graph_information import GraphInformation
from .graph_events import GraphEvents
from .graph_failed_retry import GraphFailedRetry


def _uid() -> str:
    return str(uuid4())


def _state_key(prefix, key):
    return prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _revision(value: Any, *, zero: bool = False) -> int:
    require(type(value) is int and value >= (0 if zero else 1), "invalid_request", "Invalid expected revision")
    return value


class _GraphLease:
    """Prevent startup reconciliation from taking over another live graph worker."""

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


class GraphWorkflowService(GraphFailedRetry, GraphEvents, GraphInformation, GraphRuntimeHost, GraphResourceHost, GraphPlatform, GraphPublic, GraphCandidateHost):
    def __init__(self, database_path: str | Path, *, registry=None, fault_injector=None,
                 capability_packages=(), enabled_packages=None, trusted_package_entrypoints=(),
                 public_model_factory=None):
        self.database = Path(database_path).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lease = _GraphLease(self.database)
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
                    try:
                        store._connection.execute("BEGIN IMMEDIATE")
                        TypeContractStore(store).check_registry(self.registry.data_types)
                        if enabled_packages is not None or saved.configuration_id is None:
                            selections.write_current(selected)
                        store._connection.execute("COMMIT")
                    except BaseException:
                        if store._connection.in_transaction:
                            store._connection.execute("ROLLBACK")
                        raise
            except BaseException:
                self._lease.close()
                raise
        else:
            try:
                require(not capability_packages and enabled_packages is None and not trusted_package_entrypoints,
                        "invalid_request", "Choose a custom registry or configured capability packages")
            except BaseException:
                self._lease.close()
                raise
            # Trusted callers can add declarations before launching a run.
            # Every accepted run receives its own immutable registry.
            self.registry = registry.detached()
        self._resource_frames = {}
        self._execution_registries = {}
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow-graph")
        self._futures: dict[str, Any] = {}
        self._pauses: set[str] = set()
        self._resuming: set[str] = set()
        self._runtime_hosts = {}
        self._information_routers = {}
        self._acceptance_candidates = {}
        self._failed_retry_candidates = {}
        self._service_runs = {}
        self._service_options = {}
        if public_model_factory is not None:
            from .model_host_service import MODEL_SERVICE_REF
            self._service_options[MODEL_SERVICE_REF] = {"transport_factory": public_model_factory}
        self._closed = False
        try:
            self._recover()
        except BaseException:
            self._executor.shutdown(wait=True)
            self._lease.close()
            raise

    def _store(self):
        return SqliteStore(self.database, fault_injector=self._fault_injector)

    def _check_open(self):
        require(not self._closed, "service_closed", "Graph service is closed", 409)

    def _load_current_capabilities(self, selected):
        resolved = self._package_loader.resolve(selected)
        require(all(item["package_id"] != "workflow.compat" for item in resolved.package_lock),
                "package_retired", "Compatibility packages are no longer supported", 409)
        return self._package_loader.load(selected)

    def _document_available(self, repo, document):
        if self._package_diagnostics:
            return False
        packages = {row["package_id"]: row["version"] for row in self.registry.package_lock}
        if any(packages.get(row["package_id"]) != row["version"]
               for row in document.get("package_lock", [])):
            return False
        for node in document["nodes"]:
            entry = self.registry.get(node["component_id"], node["component_version"])
            saved = repo.maybe("node_definition", component_id=node["component_id"],
                               component_version=node["component_version"])
            if saved is not None and (entry is None or not repo.equal(
                    saved["descriptor"], self._runtime_descriptor(entry))):
                return False
        return True

    def _change(self, operation, key, request, callback):
        def change(repo):
            require(not self._package_diagnostics or operation == "graph.run.recover",
                    "package_missing_dependency", "Restore the saved packages before changing workflows", 409)
            return callback(repo)

        with self._lock, closing(self._store()) as store:
            require(not self._closed, "service_closed", "Graph service is closed", 409)
            return GraphRecordStore(store).atomic(operation, key, request, change)

    def node_types(self, *, protocol_version=1):
        catalog = self.registry.catalog()
        if protocol_version == 2:
            return {"schema_version": 2, "node_types": catalog, **self.platform_capabilities()}
        require(protocol_version == 1, "invalid_request", "Unknown node catalog protocol")
        builtin = {"TEXT", "PROMPT", "JSON", "MODEL_RESOURCE"}
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
            revisions = [row for row in repo.rows("workflow_definition_revision")
                         if row["workflow_definition_id"] == identity]
            require(bool(revisions), "not_found", "Workflow definition not found", 404)
            revision = max(row["revision"] for row in revisions)
        return repo.get("workflow_definition_revision", workflow_definition_id=identity, revision=revision)["document"]

    def get_definition(self, identity, revision=None):
        with self._lock, closing(self._store()) as store:
            return self._document(GraphRecordStore(store), identity, revision)

    def _save_definition(self, repo, document, expected_revision):
        from .graph_contracts import validate_graph_document
        document = validate_graph_document(document)
        _revision(expected_revision, zero=True)
        existing = [row for row in repo.rows("workflow_definition_revision")
                    if row["workflow_definition_id"] == document["workflow_definition_id"]]
        current = max((row["revision"] for row in existing), default=0)
        require(current == expected_revision, "stale_revision", "Definition changed", 409)
        require(document["revision"] == current + 1, "invalid_request", "Definition revision must advance once")
        for node in document["nodes"]:
            entry = self.registry.get(node["component_id"], node["component_version"])
            if entry is not None:
                saved = repo.maybe("node_definition", component_id=node["component_id"],
                                   component_version=node["component_version"])
                require(saved is None or repo.equal(saved["descriptor"], self._runtime_descriptor(entry)),
                        "node_definition_changed", "Registered declaration differs from its saved version", 409,
                        node_id=node["node_binding_id"])
                repo.put("node_definition", graph_record("node_definition",
                    component_id=node["component_id"], component_version=node["component_version"],
                    descriptor=self._runtime_descriptor(entry)))
            repo.put("node_binding", graph_record("node_binding",
                workflow_definition_id=document["workflow_definition_id"],
                workflow_definition_revision=document["revision"], node_binding_id=node["node_binding_id"],
                component_id=node["component_id"], component_version=node["component_version"], config=node["config"]))
        repo.put("workflow_definition_revision", graph_record("workflow_definition_revision",
            workflow_definition_id=document["workflow_definition_id"], revision=document["revision"],
            bindings=[node["node_binding_id"] for node in document["nodes"]], edges=document["edges"], document=document))
        return document

    def save_definition(self, document, *, expected_revision, idempotency_key):
        return self._change("graph.definition.save", idempotency_key,
            {"document": document, "expected_revision": expected_revision},
            lambda repo: self._save_definition(repo, document, expected_revision))

    def _definition(self, node):
        try:
            entry = self.registry.get(node["component_id"], node["component_version"])
            return entry.definition if entry is not None else None
        except (KeyError, ValueError, AttributeError):
            return None

    def _check_implementations(self, repo, document, plan):
        for node in document["nodes"]:
            if node["node_binding_id"] not in plan.definitions:
                continue
            entry = self.registry.get(node["component_id"], node["component_version"])
            descriptor = self._runtime_descriptor(entry)
            record = repo.maybe("node_definition", component_id=node["component_id"], component_version=node["component_version"])
            require(record is None or repo.equal(record["descriptor"], descriptor),
                    "node_definition_changed", "Registered declaration differs from its saved version", 409,
                    node_id=node["node_binding_id"])
            if record is None:
                repo.put("node_definition", graph_record("node_definition", component_id=node["component_id"],
                    component_version=node["component_version"], descriptor=descriptor))

    def _defaults(self, document):
        result = {}
        for node in document["nodes"]:
            definition = self._definition(node)
            if definition is not None and definition.private_state_schema is not None:
                result[node["node_binding_id"]] = deepcopy(definition.private_state_default)
        return result

    def _private(self, repo, sid):
        return {row["node_binding_id"]: deepcopy(row["private_data"]["payload"])
                for row in repo.rows("node_session") if row["workflow_session_id"] == sid}

    def _save_private(self, repo, sid, document, private_states):
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        for node_id, value in private_states.items():
            require(node_id in nodes, "private_state_owner_mismatch", "Unknown private state owner", 409, node_id=node_id)
            node = nodes[node_id]
            definition = self._definition(node)
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
        refs = [row for row in repo.rows("workflow_ref") if row["workflow_session_id"] == sid]
        require(len(refs) == 1, "storage_contract_violation", "Session must have one Head", 500)
        return refs[0]

    def _history(self, repo, sid):
        head = self._head(repo, sid)
        commit = repo.get("workflow_commit", commit_id=head["head_commit_id"])
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        own = [row["chain_run_id"] for row in repo.rows("chain_run") if row["workflow_session_id"] == sid]
        return list(dict.fromkeys([*snapshot["history_refs"], *own]))

    def _commit(self, repo, session, *, source, history=None):
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
                            history=snapshot["history_refs"])

    def _new_session(self, repo, document, *, state=None, private=None, source=None, history=None,
                     objects=None):
        sid = _uid()
        session = graph_record("workflow_session", workflow_session_id=sid,
            workflow_definition_id=document["workflow_definition_id"], definition_revision=document["revision"],
            revision=1, source=source or {"kind": "new"}, active_chain_run_id=None)
        repo.put("workflow_session", session)
        if state is not None and (state["values"] or state.get("data")):
            ProgramVariableStore(repo.store).write_in_transaction(sid, state, expected_revision=0,
                key="graph-seed:" + sid, request={"kind": "graph-session-seed", "session_id": sid, "source": source})
        self._save_private(repo, sid, document, private if private is not None else self._defaults(document))
        mapping = {"workflow_session_id": {source["workflow_session_id"]: sid}
                   if source and source.get("workflow_session_id") else {},
                   "node_binding_id": {item["source_node_id"]: item["target_node_id"]
                                       for item in (source or {}).get("state_mappings", [])
                                       if item.get("action") == "copy" and "source_node_id" in item}}
        self._objects(repo).initialize(sid, document, inherited=objects, mapping=mapping,
                                      history=history or [],
                                      source_session_id=(source or {}).get("object_source_session_id",
                                                                          (source or {}).get("workflow_session_id")))
        self._commit(repo, session, source={"kind": "session_seed", "source": source}, history=history or [])
        return self._view(repo, sid)

    def create_session(self, workflow_definition_id, definition_revision, *, idempotency_key):
        return self._change("graph.session.create", idempotency_key,
            {"workflow_definition_id": workflow_definition_id, "definition_revision": definition_revision},
            lambda repo: self._new_session(repo, self._document(repo, workflow_definition_id, _revision(definition_revision))))

    def get_session(self, sid):
        with self._lock, closing(self._store()) as store:
            return self._view(GraphRecordStore(store), sid)

    def list_sessions(self, workflow_definition_id):
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            return [self._view(repo, row["workflow_session_id"]) for row in repo.rows("workflow_session")
                    if row["workflow_definition_id"] == workflow_definition_id]

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
                    "outputs": [row for row in repo.rows("workflow_output") if row["chain_run_id"] == chain_id],
                    "runtime_facts": self._runtime_facts(store, chain_id),
                    "state_manifests": {"start": self._manifests(repo).get("chain_start", chain_id),
                                        "end": self._manifests(repo).get("chain_end", chain_id)}}

    def _view(self, repo, sid):
        session = repo.get("workflow_session", workflow_session_id=sid)
        document = self._document(repo, session["workflow_definition_id"], session["definition_revision"])
        available = self._document_available(repo, document)
        chains = [row for row in repo.rows("chain_run") if row["workflow_session_id"] == sid]
        all_chains = {row["chain_run_id"]: row for row in repo.rows("chain_run")}
        selected = self._selected_chain(repo, sid)
        runs = [row for row in repo.rows("node_run") if selected and row["chain_run_id"] == selected["chain_run_id"]]
        if selected and (selected["workflow_session_id"] != sid
                         or (selected["workflow_definition_id"], selected["definition_revision"])
                            != (session["workflow_definition_id"], session["definition_revision"])):
            from .graph_history import history_node_mapping
            node_mapping = history_node_mapping(repo, sid, selected)
        else:
            node_mapping = {node["node_binding_id"]: node["node_binding_id"] for node in document["nodes"]}
        outputs = [row for row in repo.rows("workflow_output") if row["workflow_session_id"] == sid]
        selected_outputs = {row["output_id"]: row for row in repo.rows("workflow_output")
                            if selected and row["chain_run_id"] == selected["chain_run_id"]}
        active = next((row for row in chains if row["chain_run_id"] == session["active_chain_run_id"]), None)
        status = active["status"] if active else selected["status"] if selected else "idle"
        actions = ["pause"] if status in ("running", "prepared") else (
            ["resume", "close"] if status in ("paused", "budget_exhausted") else
            (["retry_acceptance", "close"] if active
                and active["next_node_index"] < len(active["node_run_ids"])
                and active["node_run_ids"][active["next_node_index"]] in self._acceptance_candidates.get(
                    active["chain_run_id"], {}) else ["retry_archive", "close"]) if status == "archive_failed" else
            ["close"] if active and status in ("failed", "recovery_unavailable") else [])
        retry_permission = (self._failed_retry_permission(repo, active)
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
        data = ProgramVariableStore(repo.store).current(sid)
        head = self._head(repo, sid)
        return {**session, "data_revision": data["revision"], "head_revision": head["revision"],
            "head_commit_id": head["head_commit_id"], "status": status,
            "selected_chain_run_id": selected["chain_run_id"] if selected else None,
            "can_submit": active is None and available,
            "available_actions": actions if available else [],
            "readonly": not available,
            "nodes": nodes, "chains": chains, "outputs": outputs,
            "data": data, "private_states": self._private(repo, sid), "messages": [],
            "objects": self._objects(repo).current(sid),
            "history_refs": self._history(repo, sid),
            "inherited_history": [all_chains[identity] for identity in self._history(repo, sid)
                                  if identity in all_chains and all_chains[identity]["workflow_session_id"] != sid]}

    def _stable(self, repo, sid, expected_revision, expected_data_revision=None, expected_head_revision=None):
        session = repo.get("workflow_session", workflow_session_id=sid)
        require(session["revision"] == _revision(expected_revision), "stale_revision", "Session changed", 409)
        require(session["active_chain_run_id"] is None, "active_execution_not_copyable", "Session execution is not settled", 409)
        if expected_data_revision is not None:
            require(ProgramVariableStore(repo.store).current(sid)["revision"] == _revision(expected_data_revision, zero=True),
                    "stale_data_revision", "Session data changed", 409)
        if expected_head_revision is not None:
            require(self._head(repo, sid)["revision"] == _revision(expected_head_revision), "stale_head_revision", "Session Head changed", 409)
        return session

    def _map_state(self, repo, source, target, mappings, *, private=None):
        require(type(mappings) is list, "invalid_request", "State mappings must be an array")
        source_doc = self._document(repo, source["workflow_definition_id"], source["definition_revision"])
        old_nodes = {node["node_binding_id"]: node for node in source_doc["nodes"]}
        explicit = {}
        for mapping in mappings:
            require(type(mapping) is dict and set(mapping) <= {"source_node_id", "target_node_id", "action"}
                    and {"target_node_id", "action"} <= set(mapping)
                    and mapping["action"] in ("copy", "reset"), "invalid_state_mapping", "Invalid state mapping", 409)
            require(mapping["target_node_id"] not in explicit, "invalid_state_mapping", "Repeated target mapping", 409)
            explicit[mapping["target_node_id"]] = mapping
        old_private = self._private(repo, source["workflow_session_id"]) if private is None else deepcopy(private)
        result = {}
        target_ids = {node["node_binding_id"] for node in target["nodes"]}
        require(set(explicit) <= target_ids, "invalid_state_mapping", "Mapping target is absent", 409)
        for node in target["nodes"]:
            node_id = node["node_binding_id"]
            mapping = explicit.get(node_id, {"source_node_id": node_id, "target_node_id": node_id, "action": "copy"})
            old_id = mapping.get("source_node_id", node_id)
            previous = old_nodes.get(old_id)
            require(node_id not in explicit or mapping["action"] == "reset" or "source_node_id" not in mapping or previous is not None,
                    "invalid_state_mapping", "Mapping source is absent", 409, node_id=node_id)
            definition = self._definition(node)
            old_definition = self._definition(previous) if previous else None
            if mapping["action"] == "reset" or previous is None:
                if definition is not None and definition.private_state_schema is not None:
                    result[node_id] = deepcopy(definition.private_state_default)
                continue
            compatible_type = (node["component_id"], node["component_version"]) == (
                previous["component_id"], previous["component_version"])
            if old_id not in old_private:
                require(compatible_type or old_definition is not None and old_definition.private_state_schema is None
                        and (definition is None or definition.private_state_schema is None),
                        "state_migration_required", "Type replacement needs explicit mapping", 409, node_id=node_id)
                continue
            value = old_private[old_id]
            require(definition is not None and old_definition is not None,
                    "state_migration_required", "Private state owner implementation is unavailable", 409, node_id=node_id)
            try:
                result[node_id] = self.registry.migrate_private_state(value, old_definition, definition)
            except Exception as exc:
                raise graph_error("state_migration_required", "Private state requires a declared migration or explicit reset", 409,
                                  node_id=node_id) from exc
        return result

    def _copied_data(self, repo, source, *, state=None):
        """Copy the current shared state independently of retired node declarations."""
        state = ProgramVariableStore(repo.store).current(source["workflow_session_id"]) if state is None else deepcopy(state)
        return validate_program_state(state)

    def copy_session(self, sid, *, document, expected_session_revision, expected_data_revision,
                     expected_definition_revision, expected_head_revision, idempotency_key, mappings=None):
        request = {"source": sid, "document": document, "expected_session_revision": expected_session_revision,
                   "expected_data_revision": expected_data_revision, "expected_definition_revision": expected_definition_revision,
                   "expected_head_revision": expected_head_revision, "mappings": mappings if mappings is not None else []}
        def change(repo):
            from .graph_contracts import validate_graph_document
            target_document = validate_graph_document(document)
            source = self._stable(repo, sid, expected_session_revision, expected_data_revision, expected_head_revision)
            require(source["definition_revision"] == _revision(expected_definition_revision), "stale_definition_revision", "Source definition changed", 409)
            require(target_document["workflow_definition_id"] != source["workflow_definition_id"], "invalid_request", "Copy needs a new workflow identity")
            target = self._save_definition(repo, target_document, 0)
            private = self._map_state(repo, source, target, request["mappings"])
            state = self._copied_data(repo, source)
            return self._new_session(repo, target, state=state, private=private,
                objects=self._objects(repo).current(sid),
                source={"kind": "copy_current", "workflow_session_id": sid,
                        "workflow_definition_id": source["workflow_definition_id"],
                        "definition_revision": source["definition_revision"],
                        "head_commit_id": self._head(repo, sid)["head_commit_id"], "data_revision": state["revision"],
                        "state_mappings": request["mappings"]},
                history=self._history(repo, sid))
        return self._change("graph.session.copy", idempotency_key, request, change)

    def rebind_session(self, sid, *, definition_revision, expected_revision, expected_data_revision,
                       expected_head_revision, idempotency_key, mappings=None):
        request = {"session": sid, "definition_revision": definition_revision, "expected_revision": expected_revision,
                   "expected_data_revision": expected_data_revision, "expected_head_revision": expected_head_revision,
                   "mappings": mappings if mappings is not None else []}
        def change(repo):
            session = self._stable(repo, sid, expected_revision, expected_data_revision, expected_head_revision)
            document = self._document(repo, session["workflow_definition_id"], _revision(definition_revision))
            private = self._map_state(repo, session, document, request["mappings"])
            state = self._copied_data(repo, session)
            current = ProgramVariableStore(repo.store).current(sid)
            if not repo.equal(state, current):
                ProgramVariableStore(repo.store).write_in_transaction(sid, state,
                    expected_revision=current["revision"], key=_state_key("graph-rebind:", idempotency_key), request=request)
            for node in repo.rows("node_session"):
                if node["workflow_session_id"] == sid and node["node_binding_id"] not in private:
                    repo.remove_node_state(sid, node["node_binding_id"])
            session["definition_revision"] = definition_revision
            session["revision"] += 1
            repo.put("workflow_session", session)
            self._save_private(repo, sid, document, private)
            self._objects(repo).rebind(sid, document)
            self._commit(repo, session, source={"kind": "definition_rebind", "definition_revision": definition_revision,
                                              "state_mappings": request["mappings"]})
            return self._view(repo, sid)
        return self._change("graph.session.rebind", idempotency_key, request, change)

    def update_data(self, sid, *, expected_revision, expected_data_revision, idempotency_key,
                    variables=None, shared=None):
        request = {"session": sid, "expected_revision": expected_revision, "expected_data_revision": expected_data_revision,
                   "variables": variables, "shared": shared}
        def change(repo):
            session = repo.get("workflow_session", workflow_session_id=sid)
            require(session["revision"] == _revision(expected_revision), "stale_revision", "Session changed", 409)
            state = ProgramVariableStore(repo.store).current(sid)
            require(state["revision"] == _revision(expected_data_revision, zero=True), "stale_data_revision", "Data changed", 409)
            if variables is not None:
                require(type(variables) is dict and set(variables) <= set(state["values"]), "variable_not_registered", "Variable is not registered")
                for name, value in variables.items():
                    state["values"][name] = {"type": state["values"][name]["type"], "source": "assignment", "value": value}
            if shared is not None:
                from .resource_contracts import session_data_entry, validate_data_value
                require(type(shared) is dict and set(shared) <= set(state.get("data", {})), "session_data_unregistered", "Shared data is not registered")
                for name, value in shared.items():
                    entry = state["data"][name]
                    definition = entry["definition"]
                    require(definition["writable"], "session_data_read_only", "Shared data is read-only", 409)
                    validate_data_value(definition, value)
                    state["data"][name] = session_data_entry(definition, value, assigned=True)
            validate_program_state(state)
            ProgramVariableStore(repo.store).write_in_transaction(sid, state, expected_revision=expected_data_revision,
                key=_state_key("graph-data:", idempotency_key), request=request)
            session["revision"] += 1
            repo.put("workflow_session", session)
            return self._view(repo, sid)
        return self._change("graph.session.data", idempotency_key, request, change)

    def start(self, sid, *, expected_revision, idempotency_key, inputs=None):
        request = {"session": sid, "expected_revision": expected_revision, "inputs": inputs if inputs is not None else {}}
        return self._start_run(sid, idempotency_key, request,
                               lambda repo: self._prepare_run(repo, sid, expected_revision, request["inputs"]))

    def _prepare_run(self, repo, sid, expected_revision, inputs, *, event=None):
        from .graph_contracts import GraphCompiler
        session = self._stable(repo, sid, expected_revision)
        document = self._document(repo, session["workflow_definition_id"], session["definition_revision"])
        input_error = "graph_event_payload_invalid" if event else "invalid_request"
        require(type(inputs) is dict, input_error, "External inputs must be named")
        require("_workflow_frozen_resources" not in inputs, input_error, "Reserved resource inputs cannot be supplied")
        if not event:
            validate_json_value(inputs)
        compiler = GraphCompiler(self.registry)
        plan = (compiler.compile_event(document, event["event_id"], event["schema_version"], external_inputs=inputs)
                if event else compiler.compile(document, external_inputs=inputs))
        from .type_contract_store import TypeContractStore
        TypeContractStore(repo.store).check_registry(self.registry.data_types)
        self._check_implementations(repo, document, plan)
        frozen_resources = self._preflight_capabilities(repo, sid, plan)
        chain_id = _uid()
        runtime_resources = frozen_resources.pop("current_global_resources", {})
        self._resource_frames[chain_id] = runtime_resources
        self._prepare_public_capabilities(sid, chain_id, plan, runtime_resources)
        self._execution_registries[chain_id] = self.registry.detached(frozen=True)
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        run_ids = [_uid() for _ in plan.ordered_node_ids]
        for node_id, run_id in zip(plan.ordered_node_ids, run_ids):
            repo.put("node_run", graph_record("node_run", profile="node", run_id=run_id,
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
            base_commit_id=self._head(repo, sid)["head_commit_id"])
        repo.put("chain_run", chain)
        session["revision"] += 1
        session["active_chain_run_id"] = chain_id
        repo.put("workflow_session", session)
        self._save_manifest(repo, "chain_start", chain_id, session, status="prepared")
        return self._view(repo, sid)

    def _start_run(self, sid, idempotency_key, request, prepare, *, operation="graph.run.start"):
        accepted = []

        def change(repo):
            response = prepare(repo)
            accepted.append(response["active_chain_run_id"])
            return response

        with self._lock:
            previous_frames = set(self._resource_frames)
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
                with closing(self._store()) as store:
                    chain = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
                if chain["status"] == "prepared":
                    self._futures[chain_id] = self._executor.submit(self._execute, sid, chain_id)
        return response

    def _save_effects(self, repo, sid, document, event):
        variables = ProgramVariableStore(repo.store)
        current = variables.current(sid)
        state = validate_program_state(event["state"])
        shared_effect = any(effect["kind"] in ("variable_register", "variable_assign", "shared_write")
                            for effect in event["effects"])
        if not shared_effect:
            event["state"] = current
        elif not repo.equal({key: value for key, value in current.items() if key != "revision"},
                          {key: value for key, value in state.items() if key != "revision"}):
            require(current["revision"] == state["revision"], "stale_data_revision", "Concurrent data update conflicts with node effect", 409)
            saved = variables.write_in_transaction(sid, state, expected_revision=current["revision"],
                key="graph-node-effect:" + event["node_run_id"],
                request={"run_id": event["node_run_id"], "state": state})["state"]
            event["state"] = saved
        else:
            event["state"] = current
        self._save_private(repo, sid, document, event["private_states"])
        return self._objects(repo).apply(sid, node_id=event["node_binding_id"],
                                        writes=event.get("object_writes", []))

    def _settle_node_result(self, repo, sid, chain_id, document, event, run):
        """Stage exact artifacts, writes and receipt-derived outputs in one transaction."""
        registry = self._execution_registries.get(chain_id, self.registry)
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
            require(type(prepared) is dict and set(prepared) == {"outputs", "object_writes", "finalize"}
                    and (prepared["finalize"] is None or callable(prepared["finalize"])),
                    "graph_invalid_settlement", "Settlement builder returned an invalid result")
            outputs, finalize = prepared["outputs"], prepared["finalize"]
            event["object_writes"] = prepared["object_writes"]
            run["reads"] = deepcopy(event["settlement_context"].reads)
        require(type(outputs) is dict and set(outputs) <= set(ports),
                "graph_invalid_output", "Settlement contains unknown output ports")
        run["output_refs"] = {port: refs[port] for port in (
            ports if entry.settlement_builder is not None else outputs)}
        staged_refs = deepcopy(run["output_refs"])

        def save_outputs(values):
            for port, payload in values.items():
                checked = registry.validate_content(payload, ports[port].data_type, ports[port].data_schema_version)
                repo.put("workflow_output", graph_record("workflow_output", output_id=refs[port],
                    workflow_session_id=sid, chain_run_id=chain_id, run_id=run["run_id"],
                    node_binding_id=run["node_binding_id"], port_id=port, payload=checked))

        # Only this transaction can see the provisional producer/artifacts.
        # Generic object validators resolve them through their ordinary
        # accepted-producer checks; a rollback removes all provisional rows.
        save_outputs(outputs)
        repo.put("node_run", run)
        receipts = self._save_effects(repo, sid, document, event)
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
        with self._lock, closing(self._store()) as store:
            previous_revision = GraphRecordStore(store).get("node_run", run_id=event["node_run_id"])["revision"]
        def change(repo):
            chain = repo.get("chain_run", chain_run_id=chain_id)
            session = repo.get("workflow_session", workflow_session_id=sid)
            require(session["active_chain_run_id"] == chain_id and chain["status"] == "running",
                    "execution_owner_changed", "Execution callback owner is no longer active", 409)
            run = repo.get("node_run", run_id=event["node_run_id"])
            require(run["node_binding_id"] == event["node_binding_id"] and run["chain_run_id"] == chain_id,
                    "execution_owner_changed", "Node callback owner differs", 409)
            run["status"] = event["event"]
            run["revision"] += 1
            document = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
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
                event["state"] = ProgramVariableStore(repo.store).current(sid)
            elif event["event"] == "succeeded":
                self._settle_node_result(repo, sid, chain_id, document, event, run)
            else:
                event["state"] = ProgramVariableStore(repo.store).current(sid)
            if event["event"] == "succeeded":
                chain["outputs"][run["node_binding_id"]] = deepcopy(run["output_refs"])
                chain["completed_nodes"].append(run["node_binding_id"])
                chain["next_node_index"] += 1
                chain["revision"] += 1
                repo.put("chain_run", chain)
            repo.put("node_run", run)
            session["revision"] += 1
            repo.put("workflow_session", session)
            return {"state": event["state"], "object_states": self._objects(repo).current(sid),
                    "output_refs": run["output_refs"], "outputs": event["outputs"]}
        response = self._change("graph.node." + event["event"], event["node_run_id"] + ":" + event["event"] + ":" + str(previous_revision),
            {"session": sid, "chain": chain_id,
             "event": {key: value for key, value in event.items() if key != "settlement_context"}}, change)
        event["state"] = response["state"]
        event["object_states"] = response["object_states"]
        event["output_refs"] = response["output_refs"]
        event["outputs"] = response["outputs"]
        if event["event"] == "succeeded":
            event["settlement_committed"] = True
            self._acceptance_candidates.get(chain_id, {}).pop(event["node_run_id"], None)
        try:
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

    def _completed_result_matches(self, repo, sid, chain, candidate):
        if candidate is None:
            return False
        result = candidate["result"]
        session = repo.get("workflow_session", workflow_session_id=sid)
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
                and all(repo.get("node_run", run_id=identity)["status"] == "succeeded"
                        and repo.get("node_run", run_id=identity)["output_refs"] == chain["outputs"].get(node_id)
                        for node_id, identity in zip(chain["ordered_nodes"], chain["node_run_ids"]))
                and candidate["basis"] == {
                    "state": ProgramVariableStore(repo.store).current(sid),
                    "private": self._private(repo, sid), "objects": self._objects(repo).current(sid)})

    def _execute(self, sid, chain_id):
        from .graph_contracts import GraphCompiler
        from .graph_execution import execute_graph
        run_ids = {}
        try:
            def begin(repo):
                chain = repo.get("chain_run", chain_run_id=chain_id)
                require(chain["status"] in ("prepared", "paused", "budget_exhausted", "archive_failed"), "invalid_state", "Execution cannot start", 409)
                chain["status"] = "running"
                chain["revision"] += 1
                repo.put("chain_run", chain)
                return chain
            with self._lock, closing(self._store()) as store:
                existing = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
            chain = self._change("graph.run.begin", chain_id + ":begin:" + str(existing["revision"]),
                                 {"chain": chain_id, "revision": existing["revision"]}, begin)
            with self._lock:
                self._resuming.discard(chain_id)
            with self._lock, closing(self._store()) as store:
                repo = GraphRecordStore(store)
                document = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
                state = ProgramVariableStore(store).current(sid)
                private = self._private(repo, sid)
                objects = self._objects(repo).current(sid)
                output_records = {row["output_id"]: row for row in repo.rows("workflow_output")}
                outputs = {node_id: {port: output_records[oid]["payload"] for port, oid in refs.items()}
                           for node_id, refs in chain["outputs"].items()}
                planned_runs = dict(zip(chain["ordered_nodes"], chain["node_run_ids"]))
                completed_events = [{"event": "succeeded", "node_binding_id": node_id,
                                     "node_run_id": planned_runs[node_id]}
                                    for node_id in chain["completed_nodes"]]
            registry = self._execution_registries.get(chain_id, self.registry)
            run_ids = dict(zip(chain["ordered_nodes"], chain["node_run_ids"]))
            retained = self._acceptance_candidates.setdefault(chain_id, {}).get("completed_execution")
            if retained is not None:
                with self._lock, closing(self._store()) as store:
                    repo = GraphRecordStore(store)
                    require(self._completed_result_matches(repo, sid, repo.get("chain_run", chain_run_id=chain_id), retained),
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
                    validate_output_references=self._validate_runtime_references,
                    runtime_host=self._host_for_chain(chain_id, registry),
                    acceptance_cache=self._acceptance_candidates[chain_id],
                    result_recovery=self._retained_result_recovery)
                if result.status == "succeeded":
                    with self._lock, closing(self._store()) as store:
                        repo = GraphRecordStore(store)
                        retained = {"result": deepcopy(result),
                            "definition": (chain["workflow_definition_id"], chain["definition_revision"]),
                            "basis": {"state": ProgramVariableStore(store).current(sid),
                                      "private": self._private(repo, sid), "objects": self._objects(repo).current(sid)},
                            "finish_key": chain_id + ":finish:" + str(chain["revision"])}
                        require(self._completed_result_matches(repo, sid, repo.get("chain_run", chain_run_id=chain_id), retained),
                                "storage_contract_violation", "Completed result differs from accepted execution", 500)
                        self._acceptance_candidates[chain_id]["completed_execution"] = retained
            def finish(repo):
                current = repo.get("chain_run", chain_run_id=chain_id)
                session = repo.get("workflow_session", workflow_session_id=sid)
                if result.status == "succeeded":
                    require(self._completed_result_matches(repo, sid, current, retained),
                            "graph_acceptance_basis_changed", "Completed result state basis changed", 409)
                current["status"] = result.status
                current["diagnostic"] = result.diagnostic
                if result.status == "failed":
                    try:
                        permission = self._capture_failed_retry(repo, current)
                    except Exception as error:
                        permission = {"allowed": False,
                                      "reason_code": getattr(error, "reason_code", "failed_retry_unavailable"),
                                      "node_run_id": current["node_run_ids"][current["next_node_index"]],
                                      "classification": None}
                    current["diagnostic"] = {**(current["diagnostic"] or {}), "failure_retry": permission}
                current["revision"] += 1
                repo.put("chain_run", current)
                session["revision"] += 1
                if result.status == "succeeded":
                    session["active_chain_run_id"] = None
                repo.put("workflow_session", session)
                if result.status == "succeeded" and current.get("execution_kind", "round") == "round":
                    self._commit(repo, session, source={"kind": "completed_execution", "chain_run_id": chain_id})
                self._save_manifest(repo, "chain_end", chain_id, session,
                                    status=result.status)
                return {"status": result.status}
            self._change("graph.run.finish", retained["finish_key"] if retained is not None
                         else chain_id + ":finish:" + str(chain["revision"]),
                         {"chain": chain_id, "result": result.status}, finish)
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
                    chain = repo.get("chain_run", chain_run_id=chain_id)
                    if chain["status"] in ("succeeded", "closed", "recovery_unavailable", "failed"):
                        return None
                    index = chain["next_node_index"]
                    pending = self._acceptance_candidates.get(chain_id, {})
                    result_retry = (index < len(chain["node_run_ids"])
                                    and chain["node_run_ids"][index] in pending
                                    and getattr(exc, "reason_code", None) is None)
                    completion_retry = (getattr(exc, "reason_code", None) is None
                                        and self._completed_result_matches(repo, sid, chain, pending.get("completed_execution")))
                    retryable = completion_retry or result_retry
                    chain["status"] = "archive_failed" if retryable else "recovery_unavailable"
                    acknowledged_failure = getattr(exc, "reason_code", None) == "result_committed_notification_failed"
                    ambiguous_recovery = getattr(exc, "reason_code", None) == "host_result_recovery_ambiguous"
                    chain["diagnostic"] = {
                        "reason_code": ("checkpoint_commit_pending" if completion_retry else
                                        "result_acceptance_pending" if result_retry else
                                        "result_committed_notification_failed" if acknowledged_failure else
                                        "host_result_recovery_ambiguous" if ambiguous_recovery else
                                        "execution_result_unknown"),
                        "message": ("Completed execution awaits checkpoint commit" if completion_retry else
                                    "Completed result awaits acceptance" if result_retry else
                                    "Node result is committed; local completion notification failed; business replay is unavailable"
                                    if acknowledged_failure else
                                    "Multiple declared services retain results; node-level recovery is unavailable"
                                    if ambiguous_recovery else "Execution could not preserve its completion boundary")}
                    chain["revision"] += 1
                    repo.put("chain_run", chain)
                    for identity in chain["node_run_ids"]:
                        run = repo.get("node_run", run_id=identity)
                        if run["status"] == "running":
                            run["status"] = ("archive_failed" if result_retry and identity in pending
                                             else "recovery_unavailable")
                            run["revision"] += 1
                            repo.put("node_run", run)
                    session = repo.get("workflow_session", workflow_session_id=sid)
                    session["revision"] += 1
                    repo.put("workflow_session", session)
                    self._save_manifest(repo, "chain_end", chain_id, session, status=chain["status"])
                    return {"archive_retry": retryable}
                with self._lock, closing(self._store()) as store:
                    failed_revision = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)["revision"]
                recovery = self._change("graph.run.unconfirmed", chain_id + ":unknown:" + str(failed_revision),
                                        {"chain": chain_id, "revision": failed_revision}, fail)
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

    def control(self, sid, *, action, expected_revision, idempotency_key):
        require(action in ("pause", "resume", "close", "retry_archive", "retry_acceptance", "retry_failed_node"),
                "invalid_request", "Unknown graph control")
        request = {"session": sid, "action": action, "expected_revision": expected_revision}
        accepted = []
        def change(repo):
            session = repo.get("workflow_session", workflow_session_id=sid)
            require(session["revision"] == _revision(expected_revision), "stale_revision", "Session changed", 409)
            chain_id = session["active_chain_run_id"]
            require(chain_id is not None, "invalid_state", "No active execution", 409)
            chain = repo.get("chain_run", chain_run_id=chain_id)
            if action == "pause":
                require(chain["status"] in ("prepared", "running"), "invalid_state", "Cannot pause this execution", 409)
            elif action == "retry_failed_node":
                require(chain["status"] == "failed" and chain_id in self._futures,
                        "failed_retry_unavailable", "No same-process failed invocation is retained", 409)
                self._prepare_failed_retry(repo, chain)
            elif action in ("resume", "retry_archive", "retry_acceptance"):
                allowed = ("archive_failed",) if action in ("retry_archive", "retry_acceptance") else ("paused", "budget_exhausted")
                require(chain["status"] in allowed and chain_id in self._futures,
                        "recovery_unavailable", "Execution has no same-process continuation", 409)
                pending = self._acceptance_candidates.get(chain_id, {})
                index = chain["next_node_index"]
                has_candidate = index < len(chain["node_run_ids"]) and chain["node_run_ids"][index] in pending
                require(action != "retry_acceptance" or has_candidate,
                        "recovery_unavailable", "No retained result exists for this exact invocation", 409)
                if action == "retry_acceptance":
                    basis = pending[chain["node_run_ids"][index]]["basis"]
                    require(basis["state"] == ProgramVariableStore(repo.store).current(sid)
                            and basis["private"] == self._private(repo, sid)
                            and basis["objects"] == self._objects(repo).current(sid),
                            "graph_acceptance_basis_changed", "Retained result state basis changed", 409)
                if action == "retry_archive" and "completed_execution" in pending:
                    require(self._completed_result_matches(repo, sid, chain, pending["completed_execution"]),
                            "graph_acceptance_basis_changed", "Completed result state basis changed", 409)
                require(action != "retry_archive" or not has_candidate,
                        "invalid_state", "Use the result acceptance action for this invocation", 409)
                chain["status"] = "prepared"
                chain["revision"] += 1
                repo.put("chain_run", chain)
            else:
                require(chain["status"] in ("failed", "recovery_unavailable", "archive_failed", "paused", "budget_exhausted"),
                        "invalid_state", "Only settled failures or acknowledged pauses can close", 409)
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
                    self._commit(repo, session, source={"kind": "closed_execution", "chain_run_id": chain_id})
            session["revision"] += 1
            repo.put("workflow_session", session)
            if action == "close":
                self._save_manifest(repo, "chain_end", chain_id, session, status="closed")
            accepted.append(chain_id)
            return self._view(repo, sid)
        schedule_resume = False
        with self._lock:
            if action == "retry_failed_node":
                # Resource authorization opens its own storage reader. Perform
                # it before the write transaction; the basis is checked again
                # inside that transaction and at actual model dispatch.
                with closing(self._store()) as store:
                    replay = store.read_receipt_with_digest("graph.run.control", idempotency_key)
                    if replay is None:
                        repo = GraphRecordStore(store)
                        require(not self._package_diagnostics, "package_missing_dependency",
                                "Restore the saved packages before retrying workflows", 409)
                        session = repo.get("workflow_session", workflow_session_id=sid)
                        require(session["revision"] == _revision(expected_revision),
                                "stale_revision", "Session changed", 409)
                        require(session["active_chain_run_id"] is not None,
                                "failed_retry_unavailable", "No retained failed execution", 409)
                        chain = repo.get("chain_run", chain_run_id=session["active_chain_run_id"])
                        require(chain["status"] == "failed", "failed_retry_unavailable",
                                "No retained failed invocation", 409)
                        permission = self._failed_retry_permission(repo, chain)
                        require(permission["allowed"], permission["reason_code"],
                                "This failed invocation cannot safely restart", 409)
                        candidate = self._failed_retry_candidates[chain["chain_run_id"]]
                        self._validate_failed_retry_candidate(chain["chain_run_id"], candidate, authorize=True)
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
                with closing(self._store()) as store:
                    current = GraphRecordStore(store).get("chain_run", chain_run_id=chain_id)
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
        if action == "pause" and chain_id is not None:
            self._control_hosted_invocation(chain_id, action, idempotency_key)
        if schedule_resume:
            self._control_hosted_invocation(chain_id, action, idempotency_key)
            with self._lock:
                self._futures[chain_id] = self._executor.submit(self._execute, sid, chain_id)
        return response

    def _recover(self):
        self._execution_errors = {}
        with closing(self._store()) as store:
            chains = GraphRecordStore(store).rows("chain_run")
        for previous in chains:
            if previous["status"] not in ("prepared", "running", "paused", "budget_exhausted", "archive_failed"):
                continue
            def change(repo, chain_id=previous["chain_run_id"]):
                chain = repo.get("chain_run", chain_run_id=chain_id)
                chain["status"] = "recovery_unavailable"
                chain["diagnostic"] = {"reason_code": "recovery_unavailable", "message": "Same-process execution state was lost"}
                chain["revision"] += 1
                repo.put("chain_run", chain)
                for identity in chain["node_run_ids"]:
                    run = repo.get("node_run", run_id=identity)
                    if run["status"] in ("prepared", "running", "paused", "budget_exhausted", "archive_failed"):
                        run["status"] = "recovery_unavailable"
                        run["revision"] += 1
                        repo.put("node_run", run)
                session = repo.get("workflow_session", workflow_session_id=chain["workflow_session_id"])
                session["revision"] += 1
                repo.put("workflow_session", session)
                # Startup reconciliation records the loss of the execution
                # frame; it never turns a business snapshot into a continuation.
                self._save_manifest(repo, "chain_end", chain_id, session, status="recovery_unavailable")
                return None
            self._change("graph.run.recover", previous["chain_run_id"] + ":recover",
                         {"chain": previous["chain_run_id"]}, change)

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
            self._lease.close()
