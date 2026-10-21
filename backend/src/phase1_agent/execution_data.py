"""Execution-domain data interface bound to the caller's existing transaction.

The execution functional layer sees only ``RunData`` and its named domain
sessions.  The shared ``SqliteStore``, ``GraphRecordStore`` and the transaction
holder stay inside this module.  Nothing here decides graph topology, node
participation, checkpoint admission or settlement order; those remain in the
execution, chat and checkpoint functional layers.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass

from .checkpoint_repository import CheckpointRepository
from .configuration_data import ConfigurationDataSession, ConfigurationRepository
from .contract_errors import require
from .graph_records import graph_record
from .graph_store import GraphRecordStore
from .program_variable_store import ProgramVariableStore
from .runtime_fact_store import RuntimeFactStore
from .session_objects import SessionObjectStore, StateManifestStore
from .type_contract_store import TypeContractStore


class ExecutionRecords:
    """Named graph-record reads and writes for one invocation."""

    def __init__(self, records: GraphRecordStore, *, registry, writable: bool):
        self._records = records
        self._registry = registry
        self._writable = writable

    @property
    def registry(self):
        return self._registry

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution read session is not writable", 500)

    def session(self, sid: str) -> dict:
        return self._records.get("workflow_session", workflow_session_id=sid)

    def sessions(self, workflow_definition_id: str) -> list[dict]:
        return self._records.rows(
            "workflow_session", workflow_definition_id=workflow_definition_id)

    def chain(self, chain_id: str) -> dict:
        return self._records.get("chain_run", chain_run_id=chain_id)

    def node_run(self, run_id: str) -> dict:
        return self._records.get("node_run", run_id=run_id)

    def node_runs(self, chain_id: str) -> list[dict]:
        return self._records.rows("node_run", chain_run_id=chain_id)

    def chains(self, sid: str) -> list[dict]:
        return self._records.rows("chain_run", workflow_session_id=sid)

    def outputs(self, sid: str) -> list[dict]:
        return self._records.rows("workflow_output", workflow_session_id=sid)

    def outputs_for_chain(self, chain_id: str) -> list[dict]:
        return self._records.rows("workflow_output", chain_run_id=chain_id)

    def all_outputs(self) -> list[dict]:
        return self._records.rows("workflow_output")

    def output(self, output_id: str) -> dict:
        return self._records.get("workflow_output", output_id=output_id)

    def head(self, sid: str) -> dict:
        refs = self._records.rows("workflow_ref", workflow_session_id=sid)
        require(len(refs) == 1, "storage_contract_violation",
                "Session must have one Head", 500)
        return refs[0]

    def commit(self, commit_id: str) -> dict:
        return self._records.get("workflow_commit", commit_id=commit_id)

    def snapshot(self, snapshot_id: str) -> dict:
        return self._records.get("state_snapshot", state_snapshot_id=snapshot_id)

    def history(self, sid: str) -> list[str]:
        head = self.head(sid)
        commit = self.commit(head["head_commit_id"])
        snapshot = self.snapshot(commit["state_snapshot_id"])
        own = [row["chain_run_id"] for row in self.chains(sid)]
        return list(dict.fromkeys([*snapshot["history_refs"], *own]))

    def private_states(self, sid: str) -> dict:
        return {row["node_binding_id"]: deepcopy(row["private_data"]["payload"])
                for row in self._records.rows("node_session", workflow_session_id=sid)}

    def private_state(self, sid: str, node_id: str) -> dict | None:
        return self._records.maybe("node_session", workflow_session_id=sid,
                                   node_binding_id=node_id)

    def session_metadata(self, sid: str) -> dict:
        row = self._records.store._connection.execute(
            "SELECT name,metadata_sequence FROM workflow_session_metadata "
            "WHERE workflow_session_id=?", (sid,)).fetchone()
        return ({"name": row["name"], "metadata_sequence": row["metadata_sequence"]}
                if row else {"name": "", "metadata_sequence": 0})

    def archived(self, kind: str, identity: str) -> bool:
        row = self._records.store._connection.execute(
            "SELECT archived FROM workflow_management WHERE object_kind=? AND object_id=?",
            (kind, identity)).fetchone()
        return bool(row and row["archived"])

    def read_global_resources(self, identities: list[dict]) -> list[dict]:
        from .global_resources import GlobalResourceStore
        return GlobalResourceStore(
            self._records.store, self._registry.data_types).read_many(identities)

    def runtime_facts(self, chain_id: str, *, node_run_id: str | None = None) -> list[dict]:
        return RuntimeFactStore(self._records.store).list(
            chain_id, node_run_id=node_run_id)

    def executor_facts(self, owner: dict, reference: dict, fact_ids: list[str]) -> list[dict]:
        return RuntimeFactStore(self._records.store).executor_facts(
            owner, reference, fact_ids)

    def history_node_mapping(self, sid: str, chain: dict, *, compatible_types=True):
        from .graph_history import history_node_mapping
        return history_node_mapping(self._records, sid, chain, compatible_types=compatible_types)

    def put_session(self, value: dict) -> None:
        self._write()
        self._records.put("workflow_session", value)

    def put_chain(self, value: dict) -> None:
        self._write()
        self._records.put("chain_run", value)

    def put_node_run(self, value: dict) -> None:
        self._write()
        self._records.put("node_run", value)

    def put_output(self, value: dict) -> None:
        self._write()
        self._records.put("workflow_output", value)

    def put_private_state(self, value: dict) -> None:
        self._write()
        self._records.put("node_session", value)

    def put_snapshot(self, value: dict) -> None:
        self._write()
        self._records.put("state_snapshot", value)

    def put_commit(self, value: dict) -> None:
        self._write()
        self._records.put("workflow_commit", value)

    def put_ref(self, value: dict) -> None:
        self._write()
        self._records.put("workflow_ref", value)


class ExecutionFacts:
    """One immutable runtime fact accepted in its own narrow transaction.

    This session intentionally does not join ``RunData``: facts are accepted
    before or during a node call and must not wait for node acceptance.  The
    data-management entry owns the store, transaction and commit boundary.
    """

    def __init__(self, store):
        self._store = store

    def accept(self, owner: dict, stream_id: str, fact: dict) -> dict:
        records = GraphRecordStore(self._store)
        session = records.get(
            "workflow_session", workflow_session_id=owner["workflow_session_id"])
        chain = records.get("chain_run", chain_run_id=owner["chain_run_id"])
        run = records.get("node_run", run_id=owner["node_run_id"])
        require(session["active_chain_run_id"] == owner["chain_run_id"]
                and chain["status"] == "running"
                and run["chain_run_id"] == owner["chain_run_id"]
                and run["node_binding_id"] == owner["node_binding_id"]
                and run["status"] == "running",
                "execution_owner_changed",
                "Runtime fact owner is no longer running", 409)
        return RuntimeFactStore(self._store).accept(
            owner=owner, stream_id=stream_id, fact=fact)


class ExecutionState:
    """Program variables, objects and checkpoint object reads."""

    def __init__(self, records: ExecutionRecords, *, type_evidence, writable: bool):
        self._records = records
        self._store = records._records.store
        registry = records.registry
        self._types = registry.data_types if registry is not None else None
        self._type_evidence = type_evidence
        self._writable = writable

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution read session is not writable", 500)

    def variables(self, sid: str) -> dict:
        return ProgramVariableStore(self._store).current(sid)

    def session(self, sid: str) -> dict:
        return self._records.session(sid)

    def write_variables(self, sid: str, state: dict, *, expected_revision: int,
                        key: str, request: dict) -> dict:
        self._write()
        return ProgramVariableStore(self._store).write_in_transaction(
            sid, state, expected_revision=expected_revision, key=key, request=request)

    def bind_variables(self, owner_kind: str, owner_id: str, sid: str, revision: int) -> None:
        self._write()
        ProgramVariableStore(self._store).bind(owner_kind, owner_id, sid, revision)

    def objects(self, sid: str) -> dict:
        return SessionObjectStore(
            self._store, self._types, type_evidence=self._type_evidence).current(sid)

    def read_object(self, sid: str, object_key: str, *, node_id: str,
                    revision_id: str | None = None) -> dict:
        """Read one object through its original node and revision authorization."""
        return SessionObjectStore(
            self._store, self._types, type_evidence=self._type_evidence).read(
                sid, object_key, node_id=node_id, revision_id=revision_id)

    def apply_objects(self, sid: str, *, node_id: str, writes: list[dict],
                      write_evidence=None) -> list[dict]:
        self._write()
        return SessionObjectStore(
            self._store, self._types, type_evidence=self._type_evidence).apply(
                sid, node_id=node_id, writes=writes, write_evidence=write_evidence)

    def checkpoint_value(self, sid: str, key: str, binding):
        return SessionObjectStore(
            self._store, self._types, type_evidence=self._type_evidence
        )._checkpoint_current_value(sid, key, binding)

    def write_chat_values(self, sid: str, document: dict, values: dict) -> None:
        self._write()
        objects = SessionObjectStore(
            self._store, self._types, type_evidence=self._type_evidence)
        current = objects.current(sid)
        cas = {key: {field: current[key][field] for field in ("revision", "revision_id")}
               for key in values}
        objects._write_chat_operation_values(
            sid, document, values, cas, registry=self._records.registry)


class ExecutionTypeEvidence:
    """The only writable type-contract adapter in this transaction."""

    def __init__(self, store, *, writable: bool):
        self._store = store
        self._writable = writable

    def check_registry(self, types) -> list[dict]:
        require(self._writable, "storage_contract_violation",
                "This execution read session cannot register type evidence", 500)
        return TypeContractStore(self._store).check_registry(types)

    def check(self, types, type_id, schema_version, scope, **options):
        if self._writable:
            return TypeContractStore(self._store).check(
                types, type_id, schema_version, scope, **options)
        definition = types.get(type_id, schema_version, scope=scope)
        require(definition is not None or options.get("allow_missing", False),
                "host_unknown_type", "Required data type version is unavailable")
        if definition is None:
            return None
        existing = TypeContractStore(self._store).get(type_id, schema_version, scope)
        if existing is None:
            if options.get("allow_legacy", False):
                return None
            require(False, "type_contract_evidence_missing",
                    "Type has no accepted contract evidence; register it in the owning save transaction")
        from .contract_json import canonical_bytes
        require(canonical_bytes(existing["declaration"])
                == canonical_bytes(definition.to_dict()),
                "type_contract_redefined",
                "Registered schema version differs from its persistent contract; use an explicit new version")
        return existing


class ExecutionSettlement:
    """Commit, manifest and success-checkpoint persistence for one transaction."""

    def __init__(self, records: ExecutionRecords, state: ExecutionState, *, writable: bool):
        self._records = records
        self._state = state
        self._store = records._records.store
        self._writable = writable

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution read session is not writable", 500)

    def _manifest_types(self):
        # The manifest projection never consults the type catalog; a released
        # basis has no retained registry to offer.
        registry = self._records.registry
        return registry.data_types if registry is not None else None

    def manifest(self, kind: str, identity: str) -> dict | None:
        return StateManifestStore(
            self._store, self._manifest_types()).get(kind, identity)

    def save_manifest(self, kind: str, identity: str, session: dict, *, history: list[str],
                      status: str | None, package_lock: list[dict],
                      global_resources: list[dict]) -> dict:
        self._write()
        return StateManifestStore(
            self._store, self._manifest_types()).save(
                kind, identity, session=session,
                data_revision=self._state.variables(session["workflow_session_id"])["revision"],
                node_states=self._records.private_states(session["workflow_session_id"]),
                history_refs=history, package_lock=package_lock,
                global_resources=global_resources, status=status)

    def commit(self, session: dict, *, source: dict, history: list[str] | None = None):
        self._write()
        sid = session["workflow_session_id"]
        old = next((row for row in self._records._records.rows("workflow_ref")
                    if row["workflow_session_id"] == sid), None)
        state = self._state.variables(sid)
        from uuid import uuid4

        snapshot = graph_record(
            "state_snapshot", state_snapshot_id=str(uuid4()), workflow_session_id=sid,
            workflow_definition_id=session["workflow_definition_id"],
            definition_revision=session["definition_revision"],
            node_states=self._records.private_states(sid), data_revision=state["revision"],
            history_refs=(history if history is not None
                          else self._records.history(sid) if old else []))
        commit = graph_record(
            "workflow_commit", commit_id=str(uuid4()), workflow_session_id=sid,
            state_snapshot_id=snapshot["state_snapshot_id"],
            parent_commit_id=old["head_commit_id"] if old else None, source=source)
        self._records.put_snapshot(snapshot)
        self._records.put_commit(commit)
        self._records.put_ref(graph_record(
            "workflow_ref", workflow_ref_id=(old["workflow_ref_id"] if old else str(uuid4())),
            workflow_session_id=sid, head_commit_id=commit["commit_id"],
            revision=old["revision"] + 1 if old else 1))
        self._state.bind_variables("state_snapshot", snapshot["state_snapshot_id"], sid,
                                   state["revision"])
        self._state.bind_variables("workflow_commit", commit["commit_id"], sid,
                                   state["revision"])
        return snapshot, commit

    def _checkpoints(self, session: dict) -> CheckpointRepository:
        return CheckpointRepository(
            self._store._connection,
            workflow_definition_id=session["workflow_definition_id"],
            workflow_session_id=session["workflow_session_id"],
            fixed_definition_revision=session["definition_revision"])

    def latest_checkpoint(self, session: dict) -> dict | None:
        return self._checkpoints(session).latest()

    def checkpoint_prefix(self, session: dict, checkpoint_id: str) -> list[dict]:
        return self._checkpoints(session).prefix(checkpoint_id)

    def materialize_checkpoint(self, session: dict, checkpoint_id: str) -> dict:
        return self._checkpoints(session).materialize(checkpoint_id)

    def insert_checkpoint(self, session: dict, value: dict) -> None:
        self._write()
        self._checkpoints(session).insert(value)

    def inject_checkpoint_write(self) -> None:
        self._write()
        self._store._inject("after_checkpoint_write")


class ConfigurationReadSession:
    """Read-only projection of fixed documents and current coverage."""

    def __init__(self, data: ConfigurationDataSession):
        self._data = data

    def current_configuration(self, identity: str) -> dict | None:
        return self._data.current_configuration(identity)

    def fixed_document(self, identity: str, revision: int | None = None) -> dict:
        return self._data.fixed_document(identity, revision)

    def configuration_view(self, identity: str) -> tuple[dict | None, dict | None]:
        return self._data.configuration_view(identity)

    def definition_revisions(self, identity: str) -> list[dict]:
        return self._data.definition_revisions(identity)

    def definition_sessions(self, identity: str) -> list[dict]:
        return self._data.definition_sessions(identity)

    def session(self, session_id: str) -> dict:
        return self._data.session(session_id)

    def archived(self, kind: str, identity: str) -> bool:
        return self._data.archived(kind, identity)


class ExecutionChatData:
    """Chat bindings and frozen inputs used by the selected execution path."""

    def __init__(self, records: ExecutionRecords, state: ExecutionState,
                 configuration: ConfigurationReadSession, settlement: ExecutionSettlement,
                 *, writable: bool):
        self._records = records
        self._state = state
        self._configuration = configuration
        self._settlement = settlement
        self._writable = writable

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution read session is not writable", 500)

    def fixed_document(self, session: dict) -> dict:
        return self._configuration.fixed_document(
            session["workflow_definition_id"], session["definition_revision"])

    def bindings(self, document: dict) -> dict:
        from .checkpoint_collection import checkpoint_chat_bindings
        return checkpoint_chat_bindings(
            document, types=self._records.registry.data_types,
            registry=self._records.registry)

    def invocations(self, sid: str, chain_id: str) -> dict:
        from .workflow_chat_runtime import read_chat_invocations
        return read_chat_invocations(self._records._records, sid, chain_id)

    def save_invocations(self, sid: str, chain_id: str, invocations: dict) -> None:
        self._write()
        from .workflow_chat_runtime import save_chat_invocations
        save_chat_invocations(self._records._records, sid, chain_id, invocations)

    def save_round_inputs(self, sid: str, object_key: str, round_id: str, **values) -> None:
        self._write()
        from .workflow_chat_runtime import save_chat_round_inputs
        save_chat_round_inputs(self._records._records, sid, object_key, round_id, **values)

    def round_inputs(self, sid: str, object_key: str, round_id: str) -> dict | None:
        from .workflow_chat_runtime import read_chat_round_inputs
        return read_chat_round_inputs(
            self._records._records, sid, object_key, round_id)

    def latest_checkpoint(self, session: dict) -> dict | None:
        return self._settlement.latest_checkpoint(session)

    def materialize_checkpoint(self, session: dict, checkpoint_id: str) -> dict:
        return self._settlement.materialize_checkpoint(session, checkpoint_id)

    def item(self, session: dict, object_key: str, *, expected_object_revision=None):
        document = self.fixed_document(session)
        bindings = self.bindings(document)
        require(type(object_key) is str and object_key in bindings,
                "chat_item_unbound", "Chat operations require an exact registered chat item", 409)
        record = self._state.checkpoint_value(
            session["workflow_session_id"], object_key, bindings[object_key])
        require(record is not None and not record["deleted"],
                "chat_item_unavailable", "The registered chat item has no current value", 409)
        if expected_object_revision is not None:
            require(type(expected_object_revision) is int
                    and record["revision"] == expected_object_revision,
                    "stale_object_revision", "The chat item changed", 409)
        return document, record

    def write_values(self, session: dict, document: dict, values: dict) -> None:
        self._write()
        self._state.write_chat_values(session["workflow_session_id"], document, values)


@dataclass(frozen=True)
class RunData:
    """The six limited execution-domain sessions; no repo or connection."""

    records: ExecutionRecords
    state: ExecutionState
    settlement: ExecutionSettlement
    type_evidence: ExecutionTypeEvidence
    configuration: ConfigurationReadSession
    chat: ExecutionChatData

    @property
    def registry(self):
        return self.records.registry


def build_run_data(records: GraphRecordStore, *, basis=None, registry=None,
                   writable: bool) -> RunData:
    """Assemble the limited execution bundle behind the data-management entry."""
    from .run_basis import RunBasis

    if basis is not None:
        require(isinstance(basis, RunBasis), "run_basis_unavailable",
                "This invocation has no retained run basis", 409)
        registry = basis.registry
    require(registry is not None, "run_basis_unavailable",
            "Execution data requires one definition source", 409)
    record_port = ExecutionRecords(records, registry=registry, writable=writable)
    evidence = ExecutionTypeEvidence(records.store, writable=writable)
    state = ExecutionState(record_port, type_evidence=evidence, writable=writable)
    settlement = ExecutionSettlement(record_port, state, writable=writable)
    configuration = ConfigurationReadSession(
        ConfigurationDataSession(ConfigurationRepository(records)))
    chat = ExecutionChatData(record_port, state, configuration, settlement,
                             writable=writable)
    return RunData(record_port, state, settlement, evidence, configuration, chat)


class ExecutionBoundaryRecords:
    """Exact invocation-state reads and end-boundary state writes.

    The session exists only for a released invocation whose ``RunBasis`` is no
    longer retained.  It deliberately exposes no registry, generic record
    access, object/variable writes or continuation: callers receive the exact
    session/chain/run identities they persisted and may write back only those
    records' status fields.
    """

    def __init__(self, records: ExecutionRecords, *, writable: bool):
        self._records = records
        self._writable = writable

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution boundary read session is not writable", 500)

    def session(self, sid: str) -> dict:
        return self._records.session(sid)

    def chain(self, chain_id: str) -> dict:
        return self._records.chain(chain_id)

    def node_run(self, run_id: str) -> dict:
        return self._records.node_run(run_id)

    def history(self, sid: str) -> list[str]:
        return self._records.history(sid)

    def put_session(self, value: dict) -> None:
        self._write()
        self._records.put_session(value)

    def put_chain(self, value: dict) -> None:
        self._write()
        self._records.put_chain(value)

    def put_node_run(self, value: dict) -> None:
        self._write()
        self._records.put_node_run(value)


class ExecutionBoundarySettlement:
    """Boundary manifest and commit persistence with resolved evidence only."""

    def __init__(self, settlement: ExecutionSettlement, *, writable: bool):
        self._settlement = settlement
        self._writable = writable

    def _write(self):
        require(self._writable, "storage_contract_violation",
                "This execution boundary read session is not writable", 500)

    def manifest(self, kind: str, identity: str) -> dict | None:
        return self._settlement.manifest(kind, identity)

    def save_manifest(self, kind: str, identity: str, session: dict, *, history: list[str],
                      status: str | None, package_lock: list[dict],
                      global_resources: list[dict]) -> dict:
        self._write()
        return self._settlement.save_manifest(
            kind, identity, session, history=history, status=status,
            package_lock=package_lock, global_resources=global_resources)

    def commit(self, session: dict, *, source: dict, history: list[str] | None = None):
        self._write()
        return self._settlement.commit(session, source=source, history=history)


@dataclass(frozen=True)
class RunBoundary:
    """Released-basis boundary bundle; no executor, types, resources or replay."""

    records: ExecutionBoundaryRecords
    settlement: ExecutionBoundarySettlement
    configuration: ConfigurationReadSession


def build_run_boundary(records: GraphRecordStore, *, writable: bool = True) -> RunBoundary:
    """Assemble the released-basis bundle behind the data-management entry.

    The normal ``build_run_data`` path keeps requiring a real ``RunBasis``; this
    bundle only serves the explicit unconfirmed-close boundary.  Manifests are
    persisted from resolved evidence supplied by the caller, so the projection
    never consults a current service registry.
    """
    record_port = ExecutionRecords(records, registry=None, writable=writable)
    evidence = ExecutionTypeEvidence(records.store, writable=False)
    state = ExecutionState(record_port, type_evidence=evidence, writable=writable)
    settlement = ExecutionSettlement(record_port, state, writable=writable)
    configuration = ConfigurationReadSession(
        ConfigurationDataSession(ConfigurationRepository(records)))
    return RunBoundary(ExecutionBoundaryRecords(record_port, writable=writable),
                       ExecutionBoundarySettlement(settlement, writable=writable),
                       configuration)
