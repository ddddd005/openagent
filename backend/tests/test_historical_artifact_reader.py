"""Formal historical roots and original execution proof in temporary SQLite."""

from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository, initialize_checkpoint_tables
from phase1_agent.content_contracts import text_content
from phase1_agent.context_prompt_v6 import native_context_prompt_references, validate_native_context_prompt
from phase1_agent.context_update import context_update_references
from phase1_agent.context_update_v2 import validate_context_update_v2
from phase1_agent.context_v4 import native_context_references, validate_native_context_view
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import graph_record
from phase1_agent.historical_artifact_reader import (
    HistoryArtifactAuthority, read_context_update_facts, readonly_history_connection,
)
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, ObjectBinding, TypeRegistry
from phase1_agent.model_execution_contracts import AGENT_EXECUTOR_REF_V2
from phase1_agent.model_request_contracts import MODEL_REQUEST_SERVICE_REF, validate_model_binding
from phase1_agent.runtime import CanonicalModelAdapter
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore, _record_id
from phase1_agent.type_contract_store import TypeContractStore
from test_agent_executor_v2 import Callbacks, ExecutorHarness
from test_checkpoint_history_integration import HistoryFixture, transaction


def write_record(connection, kind, value):
    connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(record_type,record_id) "
        "DO UPDATE SET payload=excluded.payload,immutable=excluded.immutable",
        (kind, _record_id(kind, value), canonical_bytes(value).decode("utf-8"),
         int(kind not in ("workflow_session", "node_run", "chain_run", "workflow_ref")), "synthetic"))


def read_record(connection, kind, identity):
    return loads_strict(connection.execute(
        "SELECT payload FROM records WHERE record_type=? AND record_id=?", (kind, identity)).fetchone()[0])


@pytest.fixture
def history(tmp_path):
    fixture = HistoryFixture(tmp_path / "history-reader.sqlite")
    try:
        yield fixture
    finally:
        fixture.close()


def authority(history, connection, checkpoint=None):
    result = HistoryArtifactAuthority(
        connection, history.types, workflow_definition_id=history.workflow,
        workflow_session_id=history.source, fixed_definition_revision=1, registry=history.registry)
    if checkpoint is not None:
        result.add_checkpoint_root(checkpoint["checkpoint_id"])
    return result


def test_formal_checkpoint_closure_is_internal_detached_and_zero_ddl(history):
    statements = []
    with readonly_history_connection(history.path) as connection:
        connection.set_trace_callback(statements.append)
        reader = authority(history, connection, history.success)
        detail = reader.resolve_detail({"scope": "artifact", "output_id": history.output})
        assert detail["value"] == {"schema_version": 1, "value": 1}
        assert detail["config"] == {} and detail["input_refs"] == {}
        detail["value"]["value"] = 999
        assert reader.resolve_detail({"scope": "artifact", "output_id": history.output})["value"]["value"] == 1
        with pytest.raises(HostContractError) as error:
            history.types.public_artifact_references("test.result", 1, detail["value"])
        assert error.value.reason_code == "output_reference_denied"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM workflow_runtime_facts")
    assert not any(statement.lstrip().upper().startswith((
        "CREATE", "ALTER", "INSERT", "UPDATE", "PRAGMA USER_VERSION =")) for statement in statements)


def test_membership_alone_or_self_reported_uuid_does_not_grant_a_root(history):
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection, history.root)
        with pytest.raises(ContractValidationError) as error:
            reader.resolve_detail({"scope": "artifact", "output_id": history.output})
        assert error.value.reason_code == "history_artifact_denied"
        with pytest.raises(TypeError):
            reader.add_checkpoint_root(history.success["checkpoint_id"], state=history.success_state)


@pytest.mark.parametrize("change", ["membership", "checkpoint", "session"])
def test_authority_rechecks_formal_roots_after_a_new_transaction(history, change):
    connection = history.connection
    connection.execute("BEGIN")
    reader = authority(history, connection, history.success)
    connection.execute("COMMIT")
    with transaction(connection):
        if change == "membership":
            connection.execute("DELETE FROM workflow_history_membership")
        elif change == "checkpoint":
            connection.execute("DELETE FROM workflow_checkpoints WHERE checkpoint_id=?",
                               (history.success["checkpoint_id"],))
        else:
            connection.execute("DELETE FROM records WHERE record_type='workflow_session'")
    connection.execute("BEGIN")
    try:
        with pytest.raises(ContractValidationError):
            reader.resolve_detail({"scope": "artifact", "output_id": history.output})
    finally:
        connection.execute("ROLLBACK")


def test_same_read_snapshot_keeps_its_actual_membership(history):
    history.connection.execute("PRAGMA journal_mode=WAL")
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection, history.success)
        with transaction(history.connection):
            history.connection.execute("DELETE FROM workflow_history_membership")
        assert reader.resolve_detail({"scope": "artifact", "output_id": history.output})["value"]["value"] == 1
    with readonly_history_connection(history.path) as connection:
        with pytest.raises(ContractValidationError):
            authority(history, connection, history.success)


@pytest.mark.parametrize("change", ["output_id", "producer", "chain_progress", "type_digest", "fixed_binding"])
def test_sql_coordinates_acceptance_and_declarations_are_checked(history, change):
    with transaction(history.connection):
        if change in ("output_id", "producer"):
            output = read_record(history.connection, "workflow_output", history.output)
            output["output_id" if change == "output_id" else "run_id"] = str(uuid4())
            history.connection.execute(
                "UPDATE records SET payload=? WHERE record_type='workflow_output' AND record_id=?",
                (canonical_bytes(output).decode(), history.output))
        elif change == "chain_progress":
            chain = read_record(history.connection, "chain_run", history.chain)
            chain["outputs"][history.node] = {}
            write_record(history.connection, "chain_run", chain)
        elif change == "type_digest":
            history.connection.execute(
                "UPDATE registered_type_contracts SET digest='wrong' WHERE scope='content' AND type_id='test.result'")
        else:
            history.connection.execute(
                "DELETE FROM session_object_bindings WHERE session_id=? AND object_key='links'", (history.source,))
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection)
        with pytest.raises(ContractValidationError):
            reader.add_checkpoint_root(history.success["checkpoint_id"])
        with pytest.raises(ContractValidationError):
            reader.resolve_detail({"scope": "artifact", "output_id": history.output})


def test_restore_requires_exact_materialized_bodies_and_fixed_structure(history):
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection, history.success)
        objects = deepcopy(history.success_state["objects"])
        reader.validate_object_restore(history.source, history.document, objects, ["counter", "links"])
        objects["counter"]["value"]["count"] = 100
        with pytest.raises(ContractValidationError):
            reader.validate_object_restore(history.source, history.document, objects, ["counter", "links"])
        changed = deepcopy(history.document)
        changed["edges"].append({"edge_id": str(uuid4()), "source_node_id": history.node,
                                 "source_port_id": "output", "target_node_id": history.node,
                                 "target_port_id": "other", "order": 0})
        with pytest.raises(ContractValidationError):
            reader.validate_object_restore(history.source, changed, history.success_state["objects"],
                                           ["counter", "links"])


def test_absent_selected_object_still_requires_existing_type_evidence(history):
    with transaction(history.connection):
        row = history.connection.execute(
            "SELECT state_storage_json FROM workflow_checkpoints WHERE checkpoint_id=?",
            (history.success["checkpoint_id"],)).fetchone()
        storage = loads_strict(row[0])
        storage["upsert"]["objects"].pop("counter")
        storage["delete"]["objects"].append("counter")
        history.connection.execute(
            "UPDATE workflow_checkpoints SET state_storage_json=? WHERE checkpoint_id=?",
            (canonical_bytes(storage).decode(), history.success["checkpoint_id"]))
        history.connection.execute(
            "DELETE FROM registered_type_contracts WHERE scope='session' AND type_id='test.counter'")
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection, history.success)
        with pytest.raises(ContractValidationError) as error:
            reader.validate_object_restore(history.source, history.document,
                                           reader.checkpoint_state["objects"], ["counter", "links"])
        assert error.value.reason_code == "history_type_evidence_missing"


def install_bridge(history, *, owner, secondary_reader=False):
    connection = history.connection
    secret_revision = str(uuid4())
    secret = ObjectBinding(
        "secret", "test.links", 1, "private", readers=(owner,), writers=(owner,), owner_node_id=owner)
    bridge = NodeDefinition(
        "test.bridge", "1", "Bridge", "Test", {}, {"type": "object", "additionalProperties": False},
        outputs=(NodePort("output", "test.bridge", data_schema_version=1),))
    history.types.register(DataTypeDefinition(
        "test.bridge", 1, {"type": "object"}, scope="content", references=lambda body: body["refs"]))
    history.registry.register(bridge, lambda *args: {})
    document = deepcopy(history.document)
    document["nodes"][0].update(component_id=bridge.component_id, component_version="1")
    document["object_bindings"].append(secret.to_dict())
    if secondary_reader:
        reader_id, reader_run = document["nodes"][1]["node_binding_id"], str(uuid4())
        reader_definition = NodeDefinition(
            "test.reader", "1", "Reader", "Test", {"object_key": "other"}, {"type": "object"},
            capabilities=("objects:read",), object_accesses=({
                "config_field": "object_key", "multiple": False, "access": "read",
                "type_id": "test.links", "schema_version": 1},))
        history.registry.register(reader_definition, lambda *args: {})
        document["nodes"][1].update(
            component_id="test.reader", component_version="1", config={"object_key": "other"},
            configuration_policy={"schema_version": 1, "prompt_mode": "fixed"})
        other = ObjectBinding("other", "test.links", 1, "shared", readers=(reader_id,), writers=(reader_id,))
        document["object_bindings"].append(other.to_dict())
    with transaction(connection):
        write_record(connection, "workflow_definition_revision", graph_record(
            "workflow_definition_revision", workflow_definition_id=history.workflow, revision=1,
            bindings=[node["node_binding_id"] for node in document["nodes"]], edges=[], document=document))
        for node in document["nodes"]:
            write_record(connection, "node_binding", graph_record(
                "node_binding", workflow_definition_id=history.workflow, workflow_definition_revision=1,
                **{key: node[key] for key in ("node_binding_id", "component_id", "component_version", "config")}))
            descriptor = history.registry.get(node["component_id"], node["component_version"]).definition.to_dict()
            write_record(connection, "node_definition", graph_record(
                "node_definition", component_id=node["component_id"], component_version=node["component_version"],
                descriptor=descriptor))
        TypeContractStore(history.store).check_registry(history.types)
        connection.execute("INSERT INTO session_object_values VALUES(?,?,?,?,?)",
                           (secret_revision, "test.links", 1, 0, canonical_bytes({"refs": []}).decode()))
        connection.execute("INSERT INTO session_object_bindings VALUES(?,?,?,?,?)",
                           (history.source, "secret", canonical_bytes(secret.to_dict()).decode(), 1, secret_revision))
        connection.execute("INSERT INTO session_object_revision_membership VALUES(?,?,?)",
                           (history.source, "secret", secret_revision))
        output = read_record(connection, "workflow_output", history.output)
        output["payload"] = {"schema_version": 1, "refs": [{
            "scope": "session", "workflow_session_id": history.source,
            "object_key": "secret", "revision_id": secret_revision}]}
        write_record(connection, "workflow_output", output)
        if secondary_reader:
            other_revision = str(uuid4())
            body = {"refs": [{"scope": "artifact", "output_id": history.output}]}
            connection.execute("INSERT INTO session_object_values VALUES(?,?,?,?,?)",
                               (other_revision, "test.links", 1, 0, canonical_bytes(body).decode()))
            connection.execute("INSERT INTO session_object_bindings VALUES(?,?,?,?,?)",
                               (history.source, "other", canonical_bytes(other.to_dict()).decode(), 1, other_revision))
            connection.execute("INSERT INTO session_object_revision_membership VALUES(?,?,?)",
                               (history.source, "other", other_revision))
            chain = read_record(connection, "chain_run", history.chain)
            chain["ordered_nodes"].append(reader_id)
            chain["node_run_ids"].append(reader_run)
            chain["node_run_attempts"].append([reader_run])
            chain["completed_nodes"].append(reader_id)
            chain["next_node_index"] += 1
            chain["outputs"][reader_id] = {}
            write_record(connection, "chain_run", chain)
            write_record(connection, "node_run", graph_record(
                "node_run", profile="node", run_id=reader_run, workflow_session_id=history.source,
                node_binding_id=reader_id, chain_run_id=history.chain, status="succeeded", revision=1,
                input_refs={}, input_values={}, output_refs={}, effects=[], config={"object_key": "other"},
                reads=[{"kind": "object_read", "object_key": "other", "revision": 1,
                        "revision_id": other_revision}], diagnostic=None))
    history.document = document
    return None if not secondary_reader else reader_run


def test_shared_artifact_cannot_hide_a_private_session_reference(history):
    install_bridge(history, owner=history.document["nodes"][1]["node_binding_id"])
    with readonly_history_connection(history.path) as connection:
        with pytest.raises(ContractValidationError) as error:
            authority(history, connection, history.success)
        assert error.value.reason_code == "history_object_denied"


def test_same_artifact_is_checked_under_each_root_reader_context(history):
    reader_run = install_bridge(history, owner=history.node, secondary_reader=True)
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection, history.success)
        with pytest.raises(ContractValidationError):
            reader.add_object_read_root(node_run_id=reader_run, object_key="other")
        assert reader.resolve_detail({"scope": "artifact", "output_id": history.output})["data_type"] == "test.bridge"


def test_nested_revision_permission_is_rechecked_after_transaction_change(history):
    install_bridge(history, owner=history.node)
    connection = history.connection
    connection.execute("BEGIN")
    reader = authority(history, connection, history.success)
    connection.execute("COMMIT")
    with transaction(connection):
        connection.execute(
            "DELETE FROM session_object_revision_membership WHERE session_id=? AND object_key='secret'",
            (history.source,))
    connection.execute("BEGIN")
    try:
        with pytest.raises(ContractValidationError) as error:
            reader.resolve_detail({"scope": "artifact", "output_id": history.output})
        assert error.value.reason_code == "history_object_revision_denied"
    finally:
        connection.execute("ROLLBACK")


def test_object_root_requires_the_actual_read_and_exact_revision_membership(history):
    reader_run = install_bridge(history, owner=history.node, secondary_reader=True)
    with transaction(history.connection):
        run = read_record(history.connection, "node_run", reader_run)
        revision = run["reads"][0]["revision_id"]
        history.connection.execute(
            "DELETE FROM session_object_revision_membership WHERE session_id=? AND object_key='other'",
            (history.source,))
    with readonly_history_connection(history.path) as connection:
        reader = authority(history, connection)
        with pytest.raises(ContractValidationError):
            reader.add_object_read_root(node_run_id=reader_run, object_key="other", revision_id=revision)
        with pytest.raises(ContractValidationError):
            reader.add_object_read_root(node_run_id=history.run, object_key="links")


def test_actual_object_read_root_rechecks_its_frozen_grant(history):
    owner = history.document["nodes"][1]["node_binding_id"]
    reader_run = install_bridge(history, owner=owner, secondary_reader=True)
    connection = history.connection
    connection.execute("BEGIN")
    reader = authority(history, connection)
    assert reader.add_object_read_root(node_run_id=reader_run, object_key="other")["refs"][0]["output_id"] == history.output
    assert reader.resolve_detail({"scope": "artifact", "output_id": history.output})["data_type"] == "test.bridge"
    connection.execute("COMMIT")
    with transaction(connection):
        identity = _record_id("node_definition", {"component_id": "test.reader", "component_version": "1"})
        declaration = read_record(connection, "node_definition", identity)
        declaration["descriptor"]["capabilities"] = []
        write_record(connection, "node_definition", declaration)
    connection.execute("BEGIN")
    try:
        with pytest.raises(ContractValidationError):
            reader.resolve_detail({"scope": "artifact", "output_id": history.output})
    finally:
        connection.execute("ROLLBACK")


class ExecutionFixture:
    """A real offline executor round plus exact synthetic frozen graph records."""

    def __init__(self, directory):
        self.host = ExecutorHarness(directory)
        host = self.host
        host.references["prompt"] = [{"edge_id": str(uuid4()), "output_id": host.references["prompt"][0]["output_id"],
                                     "order": 0}]
        host.mock.input_refs = deepcopy(host.references)
        host.host.resources.evidence["consumer_run"]["input_refs"] = deepcopy(host.references)
        host.host.resources.persist_evidence("consumer_run")
        callbacks = Callbacks()
        outputs = host.executor().advance(callbacks, None)
        self.packet = outputs["context"]
        self.owner = self.packet["owner"]
        self.path = host.host.resources.path
        self.store = SqliteStore(self.path)
        self.connection = self.store._connection
        self.workflow = host.host.resources.evidence["chain"]["workflow_definition_id"]
        self.sid, self.chain, self.agent, self.agent_run = (
            self.owner[key] for key in ("workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"))
        self.update_id = str(uuid4())
        source = host.host.resources.evidence["source_node"]
        model_node = source["node_binding_id"]
        view_node, text_node, assembly_node = (str(uuid4()) for _ in range(3))
        self.types = TypeRegistry()
        for identity, version, validator, references, scope in (
            ("TEXT", 2, None, None, "content"),
            ("MODEL_BINDING", 5, validate_model_binding, None, "content"),
            ("CONTEXT_VIEW", 4, validate_native_context_view, native_context_references, "content"),
            ("PROMPT", 6, validate_native_context_prompt, native_context_prompt_references, "content"),
            ("AGENT_CONTEXT_UPDATE", 2, validate_context_update_v2, context_update_references, "content"),
            ("test.proof-links", 1, None, lambda value: value["refs"], "session"),
        ):
            self.types.register(DataTypeDefinition(
                identity, version, {"type": "object"}, scope=scope, validator=validator, references=references))
        definitions = [
            NodeDefinition("models.source", "7", "Model", "Test", source["config"], {"type": "object"},
                           outputs=(NodePort("output", "MODEL_BINDING", data_schema_version=5),),
                           capabilities=("models:resolve",), service_requirements=({
                               **MODEL_REQUEST_SERVICE_REF.to_dict(), "capability": "models:resolve",
                               "operations": ["bind-native-model"]},)),
            NodeDefinition("context.output", "4", "Context", "Test",
                           {"object_key": "context", "agent_node_id": self.agent}, {"type": "object"},
                           outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),)),
            NodeDefinition("tools.current-input", "1", "Input", "Test", {}, {"type": "object"},
                           outputs=(NodePort("output", "TEXT", data_schema_version=2),)),
            NodeDefinition("context.assembly", "4", "Assembly", "Test", {}, {"type": "object"},
                           inputs=(NodePort("view", "CONTEXT_VIEW", data_schema_version=4),
                                   NodePort("current_input", "TEXT", data_schema_version=2)),
                           outputs=(NodePort("output", "PROMPT", data_schema_version=6),)),
            NodeDefinition("agents.execute", "9", "Agent", "Test", {}, {"type": "object"},
                           inputs=(NodePort("prompt", "PROMPT", data_schema_version=6),
                                   NodePort("model", "MODEL_BINDING", data_schema_version=5)),
                           outputs=(NodePort("context", "AGENT_CONTEXT_UPDATE", data_schema_version=2),),
                           capabilities=("models:call",), service_requirements=({
                               **MODEL_REQUEST_SERVICE_REF.to_dict(), "capability": "models:call",
                               "operations": ["prepare-request", "dispatch-request", "discard-prepared-request"]},)),
        ]
        node_ids = [model_node, view_node, text_node, assembly_node, self.agent]
        run_ids = [host.host.resources.evidence["producer_run"]["run_id"],
                   str(uuid4()), str(uuid4()), str(uuid4()), self.agent_run]
        output_ids = [host.references["model"][0]["output_id"], host.prompt["context_ref"]["output_id"],
                      host.prompt["current_input_ref"]["output_id"],
                      host.references["prompt"][0]["output_id"], self.update_id]
        bodies = [host.mock.binding, host.prompt["context"], text_content("Current task"), host.prompt, self.packet]
        refs = [{}, {}, {}, {
            "view": [{"edge_id": str(uuid4()), "output_id": output_ids[1], "order": 0}],
            "current_input": [{"edge_id": str(uuid4()), "output_id": output_ids[2], "order": 0}]},
            deepcopy(host.references)]
        edges = []
        for target, inputs in zip(node_ids, refs):
            for port, bindings in inputs.items():
                for binding in bindings:
                    source_index = output_ids.index(binding["output_id"])
                    edges.append({**{key: binding[key] for key in ("edge_id", "order")},
                                  "source_node_id": node_ids[source_index], "source_port_id": "output",
                                  "target_node_id": target, "target_port_id": port})
        document = {
            "schema_version": 2, "workflow_definition_id": self.workflow, "revision": 1,
            "name": "Original offline proof", "edges": edges,
            "nodes": [{"node_binding_id": node_id, "component_id": definition.component_id,
                       "component_version": definition.component_version, "config": definition.default_config,
                       "title": definition.display_name, "position": {"x": 0, "y": 0}}
                      for node_id, definition in zip(node_ids, definitions)],
            "object_bindings": [ObjectBinding(
                "links", "test.proof-links", 1, "shared", readers=(self.agent,), writers=(self.agent,),
                default_value={"refs": []}).to_dict()],
        }
        with transaction(self.connection):
            initialize_checkpoint_tables(self.connection)
            TypeContractStore(self.store).check_registry(self.types)
            write_record(self.connection, "workflow_session", graph_record(
                "workflow_session", workflow_session_id=self.sid, workflow_definition_id=self.workflow,
                definition_revision=1, revision=2, source={"kind": "new"}, active_chain_run_id=None))
            write_record(self.connection, "workflow_definition_revision", graph_record(
                "workflow_definition_revision", workflow_definition_id=self.workflow, revision=1,
                bindings=node_ids, edges=edges, document=document))
            chain_outputs = {}
            for node_id, run_id, output_id, definition, body, inputs in zip(
                    node_ids, run_ids, output_ids, definitions, bodies, refs):
                port = "context" if node_id == self.agent else "output"
                descriptor = definition.to_dict()
                if node_id == self.agent:
                    descriptor["executor_ref"] = deepcopy(AGENT_EXECUTOR_REF_V2)
                write_record(self.connection, "node_definition", graph_record(
                    "node_definition", component_id=definition.component_id,
                    component_version=definition.component_version, descriptor=descriptor))
                write_record(self.connection, "node_binding", graph_record(
                    "node_binding", workflow_definition_id=self.workflow, workflow_definition_revision=1,
                    node_binding_id=node_id, component_id=definition.component_id,
                    component_version=definition.component_version, config=definition.default_config))
                write_record(self.connection, "node_run", graph_record(
                    "node_run", profile="node", run_id=run_id, workflow_session_id=self.sid,
                    node_binding_id=node_id, chain_run_id=self.chain, status="succeeded", revision=2,
                    input_refs=inputs, input_values={}, output_refs={port: output_id}, reads=[], effects=[],
                    config=definition.default_config, diagnostic=None))
                write_record(self.connection, "workflow_output", graph_record(
                    "workflow_output", output_id=output_id, workflow_session_id=self.sid,
                    chain_run_id=self.chain, run_id=run_id, node_binding_id=node_id, port_id=port, payload=body))
                chain_outputs[node_id] = {port: output_id}
            write_record(self.connection, "chain_run", graph_record(
                "chain_run", chain_run_id=self.chain, workflow_session_id=self.sid,
                workflow_definition_id=self.workflow, definition_revision=1, status="succeeded", revision=3,
                targets=[self.agent], ordered_nodes=node_ids, node_run_ids=run_ids, completed_nodes=node_ids,
                next_node_index=len(node_ids), inputs={}, outputs=chain_outputs, diagnostic=None))
            facts = RuntimeFactStore(self.store)
            for sequence, identity in enumerate(self.packet["receipts"]["fact_ids"], 1):
                facts.accept(owner=self.owner, stream_id="executor:agents.snapshot-kernel@2.0.0", fact={
                    "schema_version": 1, "owner": self.owner, "executor_ref": AGENT_EXECUTOR_REF_V2,
                    "generation": 1, "sequence": sequence, "fact_id": identity,
                    "payload": callbacks.events[identity]})
            SessionObjectStore(self.store, self.types).initialize(self.sid, document)
            repository = self.repository(self.connection)
            initial = {
                "schema_version": 1, "kind": "workflow.runtime-state", "node_states": {},
                "program_state": {"values": {}, "data": {}}, "history": {
                    "selected_round_id": None, "visible_entry_ids": []},
                "objects": {"links": {"type_id": "test.proof-links", "schema_version": 1,
                                     "deleted": False, "value": {"refs": []}}},
            }
            root = {
                "schema_version": 1, "checkpoint_id": str(uuid4()), "workflow_definition_id": self.workflow,
                "workflow_session_id": self.sid, "fixed_definition_revision": 1, "kind": "initial",
                "parent_checkpoint_id": None, "round_id": None, "chain_run_id": None,
                "assistant_entry_id": None, "state_storage": {"mode": "full", "state": initial},
            }
            repository.insert(root)
            state = deepcopy(initial)
            state["objects"]["links"]["value"]["refs"] = [{"scope": "artifact", "output_id": self.update_id}]
            self.checkpoint = {
                **root, "checkpoint_id": str(uuid4()), "kind": "successful_round",
                "parent_checkpoint_id": root["checkpoint_id"], "round_id": str(uuid4()),
                "chain_run_id": self.chain, "assistant_entry_id": str(uuid4()),
                "state_storage": {"mode": "diff", "base_checkpoint_id": root["checkpoint_id"],
                                  **make_diff(initial, state)},
            }
            repository.insert(self.checkpoint)
            repository.retain_checkpoint_history(self.checkpoint["checkpoint_id"])

    def repository(self, connection):
        return CheckpointRepository(
            connection, workflow_definition_id=self.workflow, workflow_session_id=self.sid,
            fixed_definition_revision=1)

    def reader(self, connection, **options):
        parameters = {
            "workflow_definition_id": self.workflow, "workflow_session_id": self.sid,
            "fixed_definition_revision": 1, "context_sources": (("agents.execute", "9"),),
            "model_sources": (("models.source", "7"),),
            "project_messages": CanonicalModelAdapter._project,
            "resolve_transport": self.host.mock.resolve_transport,
        }
        parameters.update(options)
        reader = HistoryArtifactAuthority(connection, self.types, **parameters)
        reader.add_checkpoint_root(self.checkpoint["checkpoint_id"])
        return reader

    def close(self):
        self.store.close()
        self.host.close()


@pytest.fixture
def execution(tmp_path):
    fixture = ExecutionFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def test_original_executor_and_m2_facts_are_completely_proven_without_current_reads(execution):
    host = execution.host
    before = (host.host.read_count, len(host.mock.communications), len(host.mock.broker.freeze_calls))
    with readonly_history_connection(execution.path) as connection:
        reader = execution.reader(connection)
        facts = read_context_update_facts(connection, reader, {"scope": "artifact", "output_id": execution.update_id})
        assert facts["producer"] == execution.owner
        assert [fact["fact_id"] for fact in facts["executor_facts"]] == execution.packet["receipts"]["fact_ids"]
        assert len(facts["model_facts"]["requests"]) == 1
    assert before == (host.host.read_count, len(host.mock.communications), len(host.mock.broker.freeze_calls))


@pytest.mark.parametrize("fault", ["model_sql_sequence", "executor_owner", "packet_final", "no_projector"])
def test_fact_coordinates_and_full_execution_proof_reject_mutation(execution, fault):
    if fault != "no_projector":
        with transaction(execution.connection):
            if fault == "model_sql_sequence":
                execution.connection.execute(
                    "UPDATE workflow_runtime_facts SET sequence=99 WHERE stream_id LIKE 'service:workflow.models%' "
                    "AND sequence=1")
            elif fault == "executor_owner":
                row = execution.connection.execute(
                    "SELECT fact_id,payload FROM workflow_runtime_facts WHERE stream_id LIKE 'executor:%' LIMIT 1"
                ).fetchone()
                body = loads_strict(row["payload"])
                body["owner"]["node_binding_id"] = str(uuid4())
                execution.connection.execute("UPDATE workflow_runtime_facts SET payload=? WHERE fact_id=?",
                                             (canonical_bytes(body).decode(), row["fact_id"]))
            else:
                output = read_record(execution.connection, "workflow_output", execution.update_id)
                output["payload"]["operations"][-1]["message"]["blocks"][0]["content"]["text"] = "Forged answer"
                write_record(execution.connection, "workflow_output", output)
    with readonly_history_connection(execution.path) as connection:
        reader = execution.reader(connection, **({"resolve_transport": None} if fault == "no_projector" else {}))
        with pytest.raises(ContractValidationError):
            read_context_update_facts(connection, reader, {"scope": "artifact", "output_id": execution.update_id})


def test_new_fixture_component_is_not_implicitly_added_to_official_origin_whitelist(execution):
    with readonly_history_connection(execution.path) as connection:
        reader = execution.reader(connection, context_sources=(("agents.execute", "4"),))
        with pytest.raises(ContractValidationError) as error:
            read_context_update_facts(connection, reader, {"scope": "artifact", "output_id": execution.update_id})
        assert error.value.reason_code == "history_fact_source_denied"


@pytest.mark.parametrize("fault", ["required_input", "control_predecessor"])
def test_succeeded_producer_requires_its_actual_complete_frozen_dependencies(execution, fault):
    with transaction(execution.connection):
        if fault == "required_input":
            identity = _record_id("node_definition", {"component_id": "agents.execute", "component_version": "9"})
            definition = read_record(execution.connection, "node_definition", identity)
            definition["descriptor"]["inputs"].append(
                NodePort("missing", "TEXT", data_schema_version=2).to_dict())
            write_record(execution.connection, "node_definition", definition)
        else:
            identity = _record_id("workflow_definition_revision", {
                "workflow_definition_id": execution.workflow, "revision": 1})
            definition = read_record(execution.connection, "workflow_definition_revision", identity)
            definition["document"]["control_edges"] = [{
                "edge_id": str(uuid4()), "source_node_id": definition["document"]["nodes"][0]["node_binding_id"],
                "target_node_id": execution.agent}]
            write_record(execution.connection, "workflow_definition_revision", definition)
    with readonly_history_connection(execution.path) as connection:
        with pytest.raises(ContractValidationError) as error:
            execution.reader(connection)
        assert error.value.reason_code == "history_storage_invalid"


def test_original_execution_facts_remain_readable_after_source_session_is_missing(execution):
    target = str(uuid4())
    connection = execution.connection
    with transaction(connection):
        write_record(connection, "workflow_session", graph_record(
            "workflow_session", workflow_session_id=target, workflow_definition_id=execution.workflow,
            definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None))
        document = read_record(
            connection, "workflow_definition_revision", _record_id("workflow_definition_revision", {
                "workflow_definition_id": execution.workflow, "revision": 1}))["document"]
        SessionObjectStore(execution.store, execution.types).initialize(target, document)
        repository = CheckpointRepository(
            connection, workflow_definition_id=execution.workflow, workflow_session_id=target,
            fixed_definition_revision=1)
        prefix = execution.repository(connection).prefix(execution.checkpoint["checkpoint_id"])
        root_id, success_id = str(uuid4()), str(uuid4())
        for original, new_id in zip(prefix, (root_id, success_id)):
            checkpoint = deepcopy(original)
            checkpoint.update(workflow_session_id=target, checkpoint_id=new_id)
            if checkpoint["kind"] != "initial":
                checkpoint["parent_checkpoint_id"] = root_id
                checkpoint["state_storage"]["base_checkpoint_id"] = root_id
            repository.insert(checkpoint)
        repository.retain_checkpoint_history(success_id)
        connection.execute("DELETE FROM records WHERE record_type='workflow_session' AND record_id=?",
                           (execution.sid,))
    with readonly_history_connection(execution.path) as connection:
        reader = HistoryArtifactAuthority(
            connection, execution.types, workflow_definition_id=execution.workflow,
            workflow_session_id=target, fixed_definition_revision=1,
            context_sources=(("agents.execute", "9"),), model_sources=(("models.source", "7"),),
            project_messages=CanonicalModelAdapter._project, resolve_transport=execution.host.mock.resolve_transport)
        reader.add_checkpoint_root(success_id)
        facts = read_context_update_facts(
            connection, reader, {"scope": "artifact", "output_id": execution.update_id})
        assert facts["producer"]["workflow_session_id"] == execution.sid
        assert facts["model_facts"]["requests"][0]["facts"][0]["session_id"] == execution.sid
