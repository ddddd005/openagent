"""Internal historical roots and original facts on one caller-owned snapshot.

This module initializes no tables and exports no consumer/API capability.
Only registered references and narrowly proven context inputs extend a root.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3

from jsonschema import Draft202012Validator, ValidationError

from .contract_json import canonical_bytes, content_digest, loads_strict
from .graph_contracts import validate_graph_document
from .graph_records import (
    _validate_consumed_configuration, graph_error, require, uuid_value, validate_graph_record,
)
from .host_sdk import ObjectBinding, validate_reference
from .model_request_fact_reader import ModelRequestFactReader
from .storage import _record_id


@contextmanager
def readonly_history_connection(database_path):
    connection = sqlite3.connect(
        Path(database_path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


class HistoryArtifactAuthority:
    """Trusted operation-local authority, never reconstructed from caller JSON."""

    def __init__(self, connection, types, *, workflow_definition_id, workflow_session_id,
                 fixed_definition_revision, registry=None,
                 context_sources=(("agents.execute", "4"), ("agents.execute", "8")),
                 model_sources=(("models.source", "5"), ("models.source", "6")),
                 project_messages=None, resolve_transport=None):
        require(connection.in_transaction, "history_snapshot_required",
                "Historical reads require one caller-owned transaction", 409)
        require(uuid_value(workflow_definition_id) and uuid_value(workflow_session_id)
                and type(fixed_definition_revision) is int
                and 1 <= fixed_definition_revision <= 2**53 - 1,
                "history_owner_invalid", "Historical owner coordinates are invalid")
        self.connection, self.types, self.registry = connection, types, registry
        self.workflow_definition_id = workflow_definition_id
        self.workflow_session_id = workflow_session_id
        self.fixed_definition_revision = fixed_definition_revision
        self._context_sources = self._components(context_sources)
        self._model_sources = self._components(model_sources)
        self._project_messages, self._resolve_transport = project_messages, resolve_transport
        self._allowed, self._details, self._session_refs = set(), {}, set()
        self._object_details = {}
        self._object_roots = []
        self._checkpoint_id, self._checkpoint_state = None, None
        self._session = self._record("workflow_session", workflow_session_id=workflow_session_id)
        require((self._session["workflow_definition_id"], self._session["definition_revision"])
                == (workflow_definition_id, fixed_definition_revision),
                "history_owner_denied", "Target session belongs to another fixed workflow", 403)
        definition = self._record(
            "workflow_definition_revision", workflow_definition_id=workflow_definition_id,
            revision=fixed_definition_revision)
        self._document = definition["document"]
        self._bindings = {
            item["object_key"]: ObjectBinding.from_dict(item)
            for item in self._document.get("object_bindings", [])
        }

    @staticmethod
    def _components(values):
        require(type(values) is tuple and values
                and all(type(value) is tuple and len(value) == 2
                        and all(type(part) is str and part for part in value) for value in values),
                "history_host_invalid", "Historical sources require exact trusted declarations")
        return frozenset(values)

    def _transaction(self):
        require(self.connection.in_transaction, "history_snapshot_required",
                "Historical authority cannot outlive its transaction", 409)

    @contextmanager
    def _root_boundary(self):
        previous = (self._allowed.copy(), self._details.copy(), self._session_refs.copy(),
                    self._object_details.copy())
        try:
            yield
        except BaseException:
            self._allowed, self._details, self._session_refs, self._object_details = previous
            raise

    def _verify_roots(self):
        session = self._record("workflow_session", workflow_session_id=self.workflow_session_id)
        require((session["workflow_definition_id"], session["definition_revision"])
                == (self.workflow_definition_id, self.fixed_definition_revision),
                "history_owner_denied", "Historical target no longer owns this fixed workflow", 403)
        document = self._record(
            "workflow_definition_revision", workflow_definition_id=self.workflow_definition_id,
            revision=self.fixed_definition_revision)["document"]
        require(self._fixed_document(document) == self._fixed_document(self._document),
                "history_root_changed", "The formal fixed declaration changed", 409)
        if self._checkpoint_id is not None:
            from .checkpoint_repository import CheckpointRepository
            repository = CheckpointRepository(
                self.connection, workflow_definition_id=self.workflow_definition_id,
                workflow_session_id=self.workflow_session_id,
                fixed_definition_revision=self.fixed_definition_revision)
            require(canonical_bytes(repository.materialize(self._checkpoint_id))
                    == canonical_bytes(self._checkpoint_state),
                    "history_root_changed", "The formal checkpoint root changed", 409)
            for key in self._checkpoint_state["objects"]:
                row = self.connection.execute(
                    "SELECT binding FROM session_object_bindings WHERE session_id=? AND object_key=?",
                    (self.workflow_session_id, key)).fetchone()
                require(row is not None and loads_strict(row["binding"]) == self._bindings[key].to_dict(),
                        "history_object_denied", "The formal target object binding changed", 403)
        object_roots = []
        for run_id, key, revision_id, original, node_id, chain_id in self._object_roots:
            run, binding, body, _ = self._read_object_root(run_id, key, revision_id)
            require(run["node_binding_id"] == node_id and run["chain_run_id"] == chain_id
                    and run["workflow_session_id"] == self.workflow_session_id, "history_root_changed",
                    "The actual object read root is no longer present", 403)
            require(canonical_bytes(body) == canonical_bytes(original),
                    "history_root_changed", "The original object read body changed", 409)
            object_roots.append((binding, body, node_id))
        previous_details, previous_objects = self._details, self._object_details
        with self._root_boundary():
            self._allowed, self._details, self._session_refs, self._object_details = set(), {}, set(), {}
            if self._checkpoint_state is not None:
                for key, entry in self._checkpoint_state["objects"].items():
                    if not entry["deleted"]:
                        self._grant_value(entry["type_id"], entry["schema_version"], entry["value"],
                                          "session", binding=self._bindings[key])
            for binding, body, node_id in object_roots:
                self._grant_value(binding.type_id, binding.schema_version, body, "session",
                                  binding=binding, node_id=node_id)
            require(all(canonical_bytes(value) == canonical_bytes(self._details[key])
                        for key, value in previous_details.items() if key in self._details)
                    and all(canonical_bytes(value) == canonical_bytes(self._object_details[key])
                            for key, value in previous_objects.items() if key in self._object_details),
                    "history_root_changed", "Original reference-closure evidence changed", 409)

    def _record(self, kind, *, optional=False, **identity):
        self._transaction()
        key = _record_id(kind, identity)
        row = self.connection.execute(
            "SELECT record_id,payload FROM records WHERE record_type=? AND record_id=?",
            (kind, key)).fetchone()
        if row is None:
            require(optional, "history_record_missing", "Historical graph record is missing", 404)
            return None
        record = validate_graph_record(kind, loads_strict(row["payload"]))
        require(_record_id(kind, record) == row["record_id"] == key,
                "history_storage_invalid", "Historical record differs from its SQL identity", 500)
        return record

    def _type(self, type_id, version, scope):
        definition = self.types.get(type_id, version, scope=scope)
        require(definition is not None, "history_type_unavailable",
                "Exact historical data type is unavailable", 409)
        row = self.connection.execute(
            "SELECT digest,declaration FROM registered_type_contracts "
            "WHERE scope=? AND type_id=? AND schema_version=?", (scope, type_id, version)).fetchone()
        require(row is not None, "history_type_evidence_missing",
                "Historical type has no accepted declaration", 409)
        declaration = loads_strict(row["declaration"])
        require(content_digest(declaration) == row["digest"]
                and canonical_bytes(declaration) == canonical_bytes(definition.to_dict()),
                "history_type_evidence_invalid", "Historical type declaration differs", 500)
        return definition

    def _node_descriptor(self, component_id, component_version):
        record = self._record(
            "node_definition", component_id=component_id,
            component_version=component_version, optional=True)
        entry = self.registry.get(component_id, component_version) if self.registry is not None else None
        require(entry is not None if self.registry is not None else record is not None,
                "history_node_unavailable", "Exact historical node declaration is unavailable", 409)

        if record is not None:
            descriptor = deepcopy(record["descriptor"])
        else:
            descriptor = entry.definition.to_dict()
            if entry.executor_ref is not None:
                descriptor["executor_ref"] = entry.executor_ref.to_dict()
        require((descriptor.get("component_id"), descriptor.get("component_version"))
                == (component_id, component_version),
                "history_storage_invalid", "Frozen descriptor has another component", 500)

        if record is not None and entry is not None:
            current = entry.definition.to_dict()
            if entry.executor_ref is not None:
                current["executor_ref"] = entry.executor_ref.to_dict()
            left, right = deepcopy(descriptor), deepcopy(current)
            for field in ("executable", "checkpoint_save", "execution_class"):
                left.pop(field, None)
                right.pop(field, None)
            require(canonical_bytes(left) == canonical_bytes(right),
                    "history_node_redefined", "Historical node contract was redefined", 409)
        return descriptor

    def _node(self, chain, run):
        definition = self._record(
            "workflow_definition_revision", workflow_definition_id=chain["workflow_definition_id"],
            revision=chain["definition_revision"])
        node = next((item for item in definition["document"]["nodes"]
                     if item["node_binding_id"] == run["node_binding_id"]), None)
        require(node is not None, "history_storage_invalid",
                "Original producer has no frozen declaration", 500)
        binding = self._record(
            "node_binding", workflow_definition_id=chain["workflow_definition_id"],
            workflow_definition_revision=chain["definition_revision"],
            node_binding_id=run["node_binding_id"])
        require(all(binding[field] == node[field] for field in (
            "node_binding_id", "component_id", "component_version", "config")),
            "history_storage_invalid", "Frozen node binding differs from its document", 500)
        descriptor = self._node_descriptor(node["component_id"], node["component_version"])
        _validate_consumed_configuration(node, binding["config"], run["config"], descriptor)
        try:
            Draft202012Validator(descriptor["config_schema"]).validate(run["config"])
        except ValidationError as error:
            raise graph_error("history_storage_invalid",
                              "Historical consumed configuration violates its declaration", 500) from error
        return node, descriptor, definition["document"]

    @staticmethod
    def _ports(descriptor, config, direction):
        if descriptor.get("port_modes"):
            mode = descriptor["port_modes"].get(config.get("mode"))
            require(mode is not None, "history_storage_invalid",
                    "Historical port mode is unavailable", 500)
            return mode[direction]
        return descriptor[direction]

    def _membership(self, chain_id):
        from .checkpoint_repository import CheckpointRepository
        repository = CheckpointRepository(
            self.connection, workflow_definition_id=self.workflow_definition_id,
            workflow_session_id=self.workflow_session_id,
            fixed_definition_revision=self.fixed_definition_revision)
        require(repository.get_membership(chain_id) is not None, "history_membership_denied",
                "Execution is not in this target session's formal history", 403)

    def _validate_inputs(self, chain, run, descriptor, document):
        edges = {edge["edge_id"]: edge for edge in document["edges"]}
        incoming = {identity for identity, edge in edges.items()
                    if edge["target_node_id"] == run["node_binding_id"]}
        bindings = [ref for refs in run["input_refs"].values() for ref in refs]
        require(len({ref["edge_id"] for ref in bindings}) == len(bindings)
                and {ref["edge_id"] for ref in bindings} == incoming,
                "history_storage_invalid", "Original invocation input bindings are incomplete", 500)
        ports = {port["port_id"]: port for port in self._ports(descriptor, run["config"], "inputs")}
        nodes = chain["ordered_nodes"]
        require(all(not port["required"] or run["input_refs"].get(identity)
                    for identity, port in ports.items()),
                "history_storage_invalid", "Accepted producer lacks a required actual input", 500)
        for port_id, references in run["input_refs"].items():
            port = ports.get(port_id)
            require(port is not None and (port["multiple"] or len(references) <= 1)
                    and len({reference["order"] for reference in references}) == len(references)
                    and references == sorted(references, key=lambda reference: (
                        reference["order"], reference["edge_id"])),
                    "history_storage_invalid", "Original input port or cardinality differs", 500)
            for reference in references:
                edge = edges[reference["edge_id"]]
                output = self._record("workflow_output", output_id=reference["output_id"])
                source = self._record("node_run", run_id=output["run_id"])
                require(edge["target_port_id"] == port_id
                        and edge["source_node_id"] == output["node_binding_id"]
                        and edge["source_port_id"] == output["port_id"]
                        and edge["order"] == reference["order"]
                        and output["chain_run_id"] == source["chain_run_id"] == chain["chain_run_id"]
                        and output["workflow_session_id"] == source["workflow_session_id"]
                            == chain["workflow_session_id"]
                        and source["node_binding_id"] == output["node_binding_id"]
                        and source["status"] == "succeeded"
                        and source["output_refs"].get(output["port_id"]) == output["output_id"]
                        and source["node_binding_id"] in nodes
                        and nodes.index(source["node_binding_id"]) < nodes.index(run["node_binding_id"])
                        and chain["node_run_ids"][nodes.index(source["node_binding_id"])] == source["run_id"]
                        and chain["outputs"].get(source["node_binding_id"]) == source["output_refs"],
                        "history_storage_invalid", "Original input escaped its actual accepted upstream", 500)
                _, source_descriptor, _ = self._node(chain, source)
                source_port = next((item for item in self._ports(source_descriptor, source["config"], "outputs")
                                    if item["port_id"] == output["port_id"]), None)
                require(source_port is not None and (
                    source_port["data_type"], source_port.get("data_schema_version", 1))
                        == (port["data_type"], port.get("data_schema_version", 1)),
                    "history_storage_invalid", "Original graph input has another exact data type", 500)
        controls = {edge["edge_id"]: edge for edge in document.get("control_edges", [])
                    if edge["target_node_id"] == run["node_binding_id"]}
        require(len({ref["edge_id"] for ref in run["control_refs"]}) == len(run["control_refs"])
                and {ref["edge_id"] for ref in run["control_refs"]} == set(controls),
                "history_storage_invalid", "Original control dependencies are incomplete", 500)
        for reference in run["control_refs"]:
            predecessor = self._record("node_run", run_id=reference["run_id"])
            require(predecessor["workflow_session_id"] == chain["workflow_session_id"]
                    and predecessor["chain_run_id"] == chain["chain_run_id"]
                    and predecessor["status"] == "succeeded"
                    and predecessor["node_binding_id"] == controls[reference["edge_id"]]["source_node_id"]
                    and predecessor["node_binding_id"] in nodes
                    and nodes.index(predecessor["node_binding_id"]) < nodes.index(run["node_binding_id"])
                    and chain["node_run_ids"][nodes.index(predecessor["node_binding_id"])] == predecessor["run_id"],
                    "history_storage_invalid", "Original control predecessor was not actually accepted", 500)

    def _detail(self, output_id):
        output = self._record("workflow_output", output_id=output_id)
        chain = self._record("chain_run", chain_run_id=output["chain_run_id"])
        require((chain["workflow_definition_id"], chain["definition_revision"])
                == (self.workflow_definition_id, self.fixed_definition_revision),
                "history_artifact_denied", "Artifact belongs to another fixed workflow", 403)
        self._membership(chain["chain_run_id"])
        source_session = self._record(
            "workflow_session", workflow_session_id=chain["workflow_session_id"], optional=True)
        require(source_session is None or (
            source_session["workflow_definition_id"], source_session["definition_revision"])
                == (chain["workflow_definition_id"], chain["definition_revision"]),
                "history_storage_invalid", "Original source session has another fixed workflow", 500)
        run = self._record("node_run", run_id=output["run_id"])
        require(run["status"] == "succeeded"
                and run["workflow_session_id"] == output["workflow_session_id"] == chain["workflow_session_id"]
                and run["chain_run_id"] == chain["chain_run_id"]
                and run["node_binding_id"] == output["node_binding_id"]
                and run["output_refs"].get(output["port_id"]) == output_id,
                "history_storage_invalid", "Artifact lacks its exact accepted producer", 500)
        nodes = chain["ordered_nodes"]
        require(run["node_binding_id"] in nodes, "history_storage_invalid",
                "Producer is absent from its original plan", 500)
        index = nodes.index(run["node_binding_id"])
        require(chain["node_run_ids"][index] == run["run_id"]
                and index < chain["next_node_index"]
                and chain["outputs"].get(run["node_binding_id"]) == run["output_refs"],
                "history_storage_invalid", "Accepted output differs from original chain progress", 500)
        node, descriptor, document = self._node(chain, run)
        self._validate_inputs(chain, run, descriptor, document)
        port = next((item for item in self._ports(descriptor, run["config"], "outputs")
                     if item["port_id"] == output["port_id"]), None)
        require(port is not None, "history_storage_invalid",
                "Artifact has no exact frozen output port", 500)
        version = port.get("data_schema_version", 1)
        self._type(port["data_type"], version, "content")
        value = self.types.validate(port["data_type"], version, output["payload"], scope="content")
        return {
            "value": value, "output_id": output_id,
            "producer": {"workflow_session_id": run["workflow_session_id"],
                         "chain_run_id": run["chain_run_id"], "node_binding_id": run["node_binding_id"],
                         "node_run_id": run["run_id"]},
            "component_id": node["component_id"], "component_version": node["component_version"],
            "config": deepcopy(run["config"]), "input_refs": deepcopy(run["input_refs"]),
            "reads": deepcopy(run["reads"]), "workflow_definition_id": chain["workflow_definition_id"],
            "fixed_definition_revision": chain["definition_revision"],
            "data_type": port["data_type"], "data_schema_version": version,
            "_descriptor": descriptor, "_document": document,
        }

    def _object(self, key, revision_id=None, *, node_id=None):
        binding = self._bindings.get(key)
        require(binding is not None, "history_object_denied",
                "Object has no target fixed binding", 403)
        row = self.connection.execute(
            "SELECT binding,revision,revision_id FROM session_object_bindings "
            "WHERE session_id=? AND object_key=?", (self.workflow_session_id, key)).fetchone()
        require(row is not None and loads_strict(row["binding"]) == binding.to_dict(),
                "history_object_denied", "Object binding differs from the fixed target", 403)
        if node_id is not None:
            from .session_objects import SessionObjectStore
            SessionObjectStore.authorize(binding, node_id)
        identity = row["revision_id"] if revision_id is None else revision_id
        require(uuid_value(identity) and self.connection.execute(
            "SELECT 1 FROM session_object_revision_membership "
            "WHERE session_id=? AND object_key=? AND revision_id=?",
            (self.workflow_session_id, key, identity)).fetchone() is not None,
            "history_object_revision_denied", "Object revision is outside target membership", 403)
        value = self.connection.execute(
            "SELECT type_id,schema_version,deleted,value FROM session_object_values WHERE revision_id=?",
            (identity,)).fetchone()
        require(value is not None and (value["type_id"], value["schema_version"])
                == (binding.type_id, binding.schema_version) and value["deleted"] in (0, 1),
                "history_storage_invalid", "Object revision has another registered type", 500)
        require(not value["deleted"], "history_object_deleted", "Object revision is deleted", 410)
        self._type(binding.type_id, binding.schema_version, "session")
        body = self.types.validate(
            binding.type_id, binding.schema_version, loads_strict(value["value"]), scope="session")
        return binding, body

    def _grant_value(self, type_id, version, value, scope, *, binding=None, node_id=None):
        self._type(type_id, version, scope)
        pending = [(ref, binding) for ref in self.types.references(type_id, version, value, scope=scope)]
        traversed_artifacts, traversed_objects = set(), set()
        while pending:
            reference, source_binding = pending.pop()
            reference = validate_reference(reference)
            if reference["scope"] == "artifact":
                identity = reference["output_id"]
                context_key = (identity, None if source_binding is None else source_binding.object_key, node_id)
                if context_key in traversed_artifacts:
                    continue
                traversed_artifacts.add(context_key)
                require(identity in self._allowed or len(self._allowed) < 8192, "history_reference_limit",
                        "Historical artifact closure exceeds its limit", 409)
                detail = self._detail(identity)
                self._allowed.add(identity)
                self._details[identity] = detail
                pending.extend((ref, source_binding) for ref in self.types.references(
                    detail["data_type"], detail["data_schema_version"], detail["value"], scope="content"))
            elif reference["scope"] == "session":
                require(reference["workflow_session_id"] == self.workflow_session_id,
                        "history_object_denied", "Session reference escapes the target owner", 403)
                key, revision = reference["object_key"], reference["revision_id"]
                target, body = self._object(key, revision, node_id=node_id)
                require(source_binding is None or set(source_binding.readers) <= set(target.readers),
                        "history_object_denied", "Reference readers exceed target object permissions", 403)
                coordinate = (key, revision)
                self._object_details[coordinate] = deepcopy(body)
                context_key = (key, revision, node_id)
                if context_key not in traversed_objects:
                    traversed_objects.add(context_key)
                    require(coordinate in self._session_refs or len(self._session_refs) < 4096, "history_reference_limit",
                            "Historical object closure exceeds its limit", 409)
                    self._session_refs.add(coordinate)
                    pending.extend((ref, target) for ref in self.types.references(
                        target.type_id, target.schema_version, body, scope="session"))
            # Global identities are provenance only, never current-read grants.

    def add_checkpoint_root(self, checkpoint_id):
        self._transaction()
        require(self._checkpoint_id is None or self._checkpoint_id == checkpoint_id,
                "history_root_conflict", "One restore authority has one selected checkpoint", 409)
        from .checkpoint_repository import CheckpointRepository
        repository = CheckpointRepository(
            self.connection, workflow_definition_id=self.workflow_definition_id,
            workflow_session_id=self.workflow_session_id,
            fixed_definition_revision=self.fixed_definition_revision)
        state = repository.materialize(checkpoint_id)
        with self._root_boundary():
            for key, entry in state["objects"].items():
                binding = self._bindings.get(key)
                row = self.connection.execute(
                    "SELECT binding FROM session_object_bindings WHERE session_id=? AND object_key=?",
                    (self.workflow_session_id, key)).fetchone()
                require(binding is not None and row is not None
                        and loads_strict(row["binding"]) == binding.to_dict()
                        and (entry["type_id"], entry["schema_version"])
                            == (binding.type_id, binding.schema_version),
                        "history_object_denied", "Checkpoint object differs from the fixed binding", 403)
                self._type(binding.type_id, binding.schema_version, "session")
                if not entry["deleted"]:
                    self.types.validate(binding.type_id, binding.schema_version, entry["value"], scope="session")
                    self._grant_value(binding.type_id, binding.schema_version, entry["value"], "session",
                                      binding=binding)
            for identity, entry in state["node_states"].items():
                node = next((item for item in self._document["nodes"]
                             if item["node_binding_id"] == identity), None)
                require(node is not None and entry["owner_component_id"] == node["component_id"],
                        "history_node_state_denied", "Checkpoint private state has another owner", 403)
                descriptor = self._node_descriptor(node["component_id"], node["component_version"])
                try:
                    Draft202012Validator(descriptor["private_state_schema"]).validate(entry["payload"])
                except ValidationError as error:
                    raise graph_error("history_node_state_invalid",
                                      "Historical private state violates its declaration", 500) from error
        self._checkpoint_id, self._checkpoint_state = checkpoint_id, deepcopy(state)
        return deepcopy(state)

    def _read_object_root(self, node_run_id, object_key, revision_id):
        run = self._record("node_run", run_id=node_run_id)
        chain = self._record("chain_run", chain_run_id=run["chain_run_id"])
        require(run["workflow_session_id"] == chain["workflow_session_id"] == self.workflow_session_id
                and (chain["workflow_definition_id"], chain["definition_revision"])
                    == (self.workflow_definition_id, self.fixed_definition_revision),
                "history_object_denied", "Object reader belongs to another target invocation", 403)
        require(run["node_binding_id"] in chain["ordered_nodes"]
                and node_run_id in chain["node_run_attempts"][chain["ordered_nodes"].index(run["node_binding_id"])],
                "history_object_read_denied", "Actual reader is absent from its original execution plan", 403)
        reads = [item for item in run["reads"] if type(item) is dict
                 and item.get("kind") == "object_read" and item.get("object_key") == object_key
                 and (revision_id is None or item.get("revision_id") == revision_id)]
        require(reads and all(set(item) == {"kind", "object_key", "revision", "revision_id"}
                             and type(item["revision"]) is int and 1 <= item["revision"] <= 2**53 - 1
                             and uuid_value(item["revision_id"]) for item in reads),
                "history_object_read_denied", "Root requires an actual persisted object read", 403)
        require(len({item["revision_id"] for item in reads}) == 1, "history_object_read_denied",
                "An ambiguous read requires its exact revision selector", 403)
        _, descriptor, _ = self._node(chain, run)
        accesses = [item for item in descriptor.get("object_accesses", [])
                    if item["access"] in ("read", "read_write")
                    and object_key in (run["config"].get(item["config_field"], [])
                                       if item["multiple"] else [run["config"].get(item["config_field"])])]
        require(accesses and "objects:read" in descriptor.get("capabilities", []),
                "history_object_read_denied", "Object read lacks its exact frozen node permission", 403)
        binding, value = self._object(
            object_key, reads[0]["revision_id"], node_id=run["node_binding_id"])
        require(all((item["type_id"], item["schema_version"])
                    == (binding.type_id, binding.schema_version) for item in accesses),
                "history_object_read_denied", "Frozen object access has another type", 403)
        return run, binding, value, reads[0]["revision_id"]

    def add_object_read_root(self, *, node_run_id, object_key, revision_id=None):
        self._transaction()
        run, binding, value, actual_revision = self._read_object_root(node_run_id, object_key, revision_id)
        with self._root_boundary():
            self._grant_value(binding.type_id, binding.schema_version, value, "session",
                              binding=binding, node_id=run["node_binding_id"])
        self._object_roots.append((node_run_id, object_key, actual_revision, deepcopy(value),
                                   run["node_binding_id"], run["chain_run_id"]))
        return deepcopy(value)

    @property
    def checkpoint_state(self):
        self._transaction()
        require(self._checkpoint_state is not None, "history_root_required",
                "Restore requires a formal selected checkpoint", 403)
        return deepcopy(self._checkpoint_state)

    def validate_object_restore(self, sid, document, logical_objects, object_keys):
        self._transaction()
        self._verify_roots()
        state = self.checkpoint_state
        document = validate_graph_document(document)
        require(sid == self.workflow_session_id
                and (document["workflow_definition_id"], document["revision"])
                    == (self.workflow_definition_id, self.fixed_definition_revision)
                and self._fixed_document(document) == self._fixed_document(self._document)
                and type(object_keys) in (list, tuple, set) and len(set(object_keys)) == len(object_keys)
                and set(object_keys) <= set(self._bindings) and type(logical_objects) is dict
                and set(logical_objects) <= set(object_keys),
                "history_restore_denied", "Logical restore exceeds its formal fixed target", 403)
        for key in object_keys:
            binding = self._bindings[key]
            self._type(binding.type_id, binding.schema_version, "session")
            require(canonical_bytes(logical_objects.get(key)) == canonical_bytes(state["objects"].get(key)),
                    "history_restore_denied", "Logical restore differs from the selected checkpoint", 403)
            entry = logical_objects.get(key)
            if ((binding.type_id, binding.schema_version) == ("workflow.effective-context", 4)
                    and entry is not None and not entry["deleted"]):
                from .historical_context_reader import validate_historical_context_object
                validate_historical_context_object(self, entry["value"], object_key=key)

    def _fixed_document(self, document):
        from .workflow_configuration import fixed_configuration_projection, validate_node_configuration
        result = deepcopy(document)
        result.pop("name", None)
        result.pop("edit_sequence", None)
        for node in result["nodes"]:
            node.pop("title", None)
            node.pop("position", None)
            descriptor = self._node_descriptor(node["component_id"], node["component_version"])
            policy = node.get("configuration_policy")
            declaration = None if policy is None else validate_node_configuration(node, descriptor)
            node["config"] = fixed_configuration_projection(node["config"], declaration, policy)
        return canonical_bytes(result)

    def resolve_detail(self, reference):
        self._transaction()
        self._verify_roots()
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact" and reference["output_id"] in self._allowed,
                "history_artifact_denied", "Artifact is outside this formal root closure", 403)
        detail = self._detail(reference["output_id"])
        require(canonical_bytes(detail) == canonical_bytes(self._details[reference["output_id"]]),
                "history_artifact_changed", "Original artifact evidence changed", 409)
        return deepcopy({key: value for key, value in detail.items() if not key.startswith("_")})

    resolve_artifact = resolve_detail

    def _read_original_object_basis(self, reference, *, object_key, revision_id, head_revision,
                                    write=False):
        """Read only the immutable revision actually read by this accepted producer."""
        from .session_objects import SessionObjectStore

        detail = self.resolve_detail(reference)
        require(type(object_key) is str and uuid_value(revision_id)
                and type(head_revision) is int and 1 <= head_revision <= 2**53 - 1
                and type(write) is bool, "history_object_read_denied",
                "Original object basis requires exact persisted coordinates", 403)
        reads = [item for item in detail["reads"] if type(item) is dict
                 and item.get("kind") == "object_read" and item.get("object_key") == object_key]
        require(reads and all(set(item) == {"kind", "object_key", "revision", "revision_id"}
                             and type(item["revision"]) is int and 1 <= item["revision"] <= 2**53 - 1
                             and uuid_value(item["revision_id"]) for item in reads)
                and any(item["revision_id"] == revision_id and item["revision"] == head_revision
                        for item in reads)
                and all(item["revision"] == head_revision for item in reads
                        if item["revision_id"] == revision_id),
                "history_object_read_denied", "Basis differs from the original actual object read", 403)
        frozen = self._details[detail["output_id"]]
        bindings = [item for item in frozen["_document"].get("object_bindings", [])
                    if item["object_key"] == object_key]
        require(len(bindings) == 1, "history_object_denied",
                "Original object has no exact frozen binding", 403)
        binding = ObjectBinding.from_dict(bindings[0])
        node_id = detail["producer"]["node_binding_id"]
        SessionObjectStore.authorize(binding, node_id)
        if write:
            SessionObjectStore.authorize(binding, node_id, write=True)
        descriptor = frozen["_descriptor"]
        declarations = descriptor.get("object_accesses", [])
        require(type(declarations) is list and all(
            type(item) is dict and set(item) == {
                "config_field", "multiple", "access", "type_id", "schema_version"}
            and type(item["multiple"]) is bool for item in declarations),
            "history_object_read_denied", "Original object access declaration is invalid", 403)
        accesses = [item for item in declarations
                    if item["access"] in (("read_write",) if write else ("read", "read_write"))
                    and object_key in (detail["config"].get(item["config_field"], [])
                                       if item["multiple"] else [detail["config"].get(item["config_field"])])]
        require(accesses and "objects:read" in descriptor.get("capabilities", [])
                and (not write or "objects:write" in descriptor.get("capabilities", []))
                and all((item["type_id"], item["schema_version"])
                        == (binding.type_id, binding.schema_version) for item in accesses),
                "history_object_read_denied", "Original reader lacks its frozen object permission", 403)
        # Source membership is retained evidence, not a lookup of its current head.
        require(self.connection.execute(
            "SELECT 1 FROM session_object_revision_membership "
            "WHERE session_id=? AND object_key=? AND revision_id=?",
            (detail["producer"]["workflow_session_id"], object_key, revision_id)).fetchone() is not None,
            "history_object_revision_denied", "Original read revision membership is unavailable", 403)
        row = self.connection.execute(
            "SELECT type_id,schema_version,deleted,value FROM session_object_values WHERE revision_id=?",
            (revision_id,)).fetchone()
        require(row is not None and (row["type_id"], row["schema_version"])
                == (binding.type_id, binding.schema_version) and row["deleted"] in (0, 1),
                "history_storage_invalid", "Original read revision has another registered type", 500)
        require(not row["deleted"], "history_object_deleted", "Original read revision is deleted", 410)
        self._type(binding.type_id, binding.schema_version, "session")
        value = self.types.validate(
            binding.type_id, binding.schema_version, loads_strict(row["value"]), scope="session")
        return {"revision_id": revision_id, "revision": head_revision, "binding": binding.to_dict(),
                "type_id": binding.type_id, "schema_version": binding.schema_version,
                "deleted": False, "value": deepcopy(value)}


def read_context_update_facts(connection, authority, update_ref):
    """Prove the exact schema-2 update before returning its internal fact sets."""
    from .context_update_v2 import validate_context_update_v2, validate_execution_update_v2
    from .model_execution_contracts import AGENT_EXECUTOR_REF_V2
    from .model_request_contracts import MODEL_REQUEST_SERVICE_REF, validate_model_binding

    require(isinstance(authority, HistoryArtifactAuthority)
            and connection is authority.connection and connection.in_transaction,
            "history_snapshot_required", "Fact proof must share the authority snapshot", 409)
    detail = authority.resolve_detail(update_ref)
    require((detail["component_id"], detail["component_version"]) in authority._context_sources
            and detail["data_type"] == "AGENT_CONTEXT_UPDATE" and detail["data_schema_version"] == 2,
            "history_fact_source_denied", "Update lacks its exact official source declaration", 403)
    packet = validate_context_update_v2(detail["value"])
    receipts, owner = packet["receipts"], detail["producer"]
    require(packet["owner"] == receipts["owner"] == owner
            and receipts["executor_ref"] == AGENT_EXECUTOR_REF_V2,
            "history_fact_source_denied", "Update facts belong to another actual producer", 403)
    frozen = authority._details[detail["output_id"]]
    require(frozen["_descriptor"].get("executor_ref") == receipts["executor_ref"],
            "history_fact_source_denied", "Producer has no exact frozen executor declaration", 403)

    def require_model_grant(descriptor, capability, operations):
        service = MODEL_REQUEST_SERVICE_REF.to_dict()
        grants = [grant for grant in descriptor.get("service_requirements", [])
                  if type(grant) is dict and grant.get("service_id") == service["service_id"]
                  and grant.get("exact_version") == service["exact_version"]
                  and grant.get("capability") == capability]
        require(capability in descriptor.get("capabilities", []) and len(grants) == 1
                and set(grants[0]) == {"service_id", "exact_version", "capability", "operations"}
                and type(grants[0]["operations"]) is list and set(operations) <= set(grants[0]["operations"]),
                "history_fact_source_denied", "Original node lacks its exact model-service grant", 403)

    require_model_grant(frozen["_descriptor"], "models:call", (
        "prepare-request", "dispatch-request", "discard-prepared-request"))
    if any(operation["kind"] == "compact" for operation in packet["operations"]):
        require_model_grant(frozen["_descriptor"], "model-projection:read", ("measure-context",))
    inputs = detail["input_refs"]

    def original_input(port, reference):
        bindings = inputs.get(port, [])
        require(len(bindings) == 1 and bindings[0]["output_id"] == reference["output_id"],
                "history_fact_source_denied", "Proof reference differs from original accepted input", 403)
        original = authority._detail(reference["output_id"])
        output = authority._record("workflow_output", output_id=reference["output_id"])
        require(original["producer"]["chain_run_id"] == owner["chain_run_id"],
                "history_fact_source_denied", "Proof input belongs to another original chain", 403)
        edges = frozen["_document"]["edges"]
        edge = next((item for item in edges if item["edge_id"] == bindings[0]["edge_id"]), None)
        chain = authority._record("chain_run", chain_run_id=owner["chain_run_id"])
        target_port = next((item for item in authority._ports(
            frozen["_descriptor"], detail["config"], "inputs") if item["port_id"] == port), None)
        require(edge is not None and edge["target_node_id"] == owner["node_binding_id"]
                and edge["target_port_id"] == port
                and edge["source_node_id"] == original["producer"]["node_binding_id"]
                and edge["source_port_id"] == output["port_id"]
                and edge["order"] == bindings[0]["order"]
                and target_port is not None and (
                    target_port["data_type"], target_port.get("data_schema_version", 1))
                    == (original["data_type"], original["data_schema_version"])
                and chain["ordered_nodes"].index(original["producer"]["node_binding_id"])
                    < chain["ordered_nodes"].index(owner["node_binding_id"]),
                "history_fact_source_denied", "Proof input lacks its exact frozen graph edge", 403)
        # This local branch does not add all producer inputs to the general closure.
        return original

    prompt = original_input("prompt", receipts["frozen_prompt_ref"])["value"]
    binding_detail = original_input("model", receipts["model_ref"])
    require((binding_detail["component_id"], binding_detail["component_version"]) in authority._model_sources
            and binding_detail["data_type"] == "MODEL_BINDING" and binding_detail["data_schema_version"] == 5,
            "history_fact_source_denied", "Model binding lacks its exact official source", 403)
    binding = validate_model_binding(binding_detail["value"])
    require_model_grant(binding_detail["_descriptor"], "models:resolve", ("bind-native-model",))
    require(binding_detail["config"] == {"reference": binding["reference"]},
            "history_fact_source_denied", "Model binding differs from its original source configuration", 403)
    policy_ref = packet["compaction_policy_ref"]
    require(len(inputs.get("compaction_policy", [])) == (0 if policy_ref is None else 1),
            "history_fact_source_denied", "Policy proof differs from the original input", 403)
    policy = None if policy_ref is None else original_input("compaction_policy", policy_ref)["value"]
    stream = "executor:" + receipts["executor_ref"]["executor_id"] + "@" + receipts["executor_ref"]["exact_version"]
    executor_facts = []
    for sequence, identity in enumerate(receipts["fact_ids"], 1):
        row = connection.execute(
            "SELECT fact_id,sequence,payload FROM workflow_runtime_facts "
            "WHERE session_id=? AND chain_id=? AND node_run_id=? AND stream_id=? AND fact_id=?",
            (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"], stream, identity)).fetchone()
        require(row is not None, "history_fact_denied", "Original executor fact is unavailable", 403)
        fact = loads_strict(row["payload"])
        require(row["sequence"] == fact.get("sequence") == sequence
                and row["fact_id"] == fact.get("fact_id") == identity
                and fact.get("owner") == owner and fact.get("executor_ref") == receipts["executor_ref"],
                "history_fact_storage_invalid", "Executor SQL and original fact coordinates differ", 500)
        executor_facts.append(fact)
    model_facts = {"requests": [{
        "request_id": request["request_id"], "settings_id": request["settings_id"],
        "facts": ModelRequestFactReader._facts(connection, owner, binding, request, inputs),
    } for request in receipts["model_requests"]]}
    require(callable(authority._project_messages) and callable(authority._resolve_transport),
            "history_proof_unavailable", "Complete proof requires trusted pure projectors", 409)
    validate_execution_update_v2(
        packet, executor_facts, prompt, binding, model_facts=model_facts, policy=policy,
        policy_ref=policy_ref, project_messages=authority._project_messages,
        resolve_transport=authority._resolve_transport)
    return {"producer": deepcopy(owner), "executor_facts": deepcopy(executor_facts),
            "model_facts": deepcopy(model_facts)}
