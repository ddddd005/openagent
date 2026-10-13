"""Uninstalled checkpoint/history primitives sharing one real SQLite transaction."""

from contextlib import closing, contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository, initialize_checkpoint_tables
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import GRAPH_VERSIONS, graph_record
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.historical_artifact_reader import HistoryArtifactAuthority, readonly_history_connection
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from phase1_agent.program_variable_store import ProgramVariableStore
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import STORAGE_VERSION, SqliteStore
from phase1_agent.type_contract_store import TypeContractStore
from phase1_agent.workflow_configuration import adjustable_configuration_projection
from test_workflow_configuration import document as prompt_document, registry as prompt_registry


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


class HistoryFixture:
    """Synthetic accepted graph evidence, not installed D nodes or round closure."""

    def __init__(self, path):
        self.path = path
        self.store = SqliteStore(path)
        self.connection = self.store._connection
        self.workflow, self.source, self.node, self.chain, self.run, self.output = (
            str(uuid4()) for _ in range(6))
        self.registry = prompt_registry()
        self.types = self.registry.data_types
        self.types.register(DataTypeDefinition(
            "test.counter", 1,
            {"type": "object", "required": ["count"], "additionalProperties": False,
             "properties": {"count": {"type": "integer", "minimum": 0}}},
            default_value={"count": 0}))
        self.types.register(DataTypeDefinition(
            "test.links", 1,
            {"type": "object", "required": ["refs"], "additionalProperties": False,
             "properties": {"refs": {"type": "array"}}},
            default_value={"refs": []}, references=lambda value: value["refs"]))
        self.types.register(DataTypeDefinition(
            "test.result", 1,
            {"type": "object", "required": ["schema_version", "value"], "additionalProperties": False,
             "properties": {"schema_version": {"const": 1}, "value": {"type": "integer"}}},
            scope="content"))
        self.definition = NodeDefinition(
            "test.success", "1", "Success", "Test", {}, {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "test.result", data_schema_version=1),),
            capabilities=("objects:read", "objects:write"))
        self.registry.register(self.definition, lambda *args: None)
        self.prompt = prompt_document()["nodes"][0]
        self.document = {
            "schema_version": 3, "workflow_definition_id": self.workflow, "revision": 1,
            "name": "History primitive integration", "edges": [],
            "nodes": [{"node_binding_id": self.node, "component_id": "test.success",
                       "component_version": "1", "title": "Success", "position": {"x": 0, "y": 0},
                       "config": {}, "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}},
                      self.prompt],
            "object_bindings": [
                ObjectBinding(key, identity, 1, "shared", readers=(self.node,), writers=(self.node,)).to_dict()
                for key, identity in (("counter", "test.counter"), ("links", "test.links"))],
        }
        self.objects = SessionObjectStore(self.store, self.types)
        self.variables = ProgramVariableStore(self.store)
        self.facts = RuntimeFactStore(self.store)
        with transaction(self.connection):
            initialize_checkpoint_tables(self.connection)
            self.store._write("workflow_definition_revision", graph_record(
                "workflow_definition_revision", workflow_definition_id=self.workflow, revision=1,
                bindings=[node["node_binding_id"] for node in self.document["nodes"]],
                edges=[], document=self.document))
            for node in self.document["nodes"]:
                self.store._write("node_binding", graph_record(
                    "node_binding", workflow_definition_id=self.workflow, workflow_definition_revision=1,
                    **{key: node[key] for key in (
                        "node_binding_id", "component_id", "component_version", "config")}))
                self.store._write("node_definition", graph_record(
                    "node_definition", component_id=node["component_id"], component_version=node["component_version"],
                    descriptor=next(item for item in self.registry.catalog()
                                    if (item["component_id"], item["component_version"])
                                    == (node["component_id"], node["component_version"]))))
            TypeContractStore(self.store).check_registry(self.types)
            self.add_session(self.source)
            self.objects.initialize(self.source, self.document)
            self.repo = self.repository(self.source)
            self.initial_state = self.state(0, [])
            self.root = self.checkpoint(self.source, self.initial_state)
            self.repo.insert(self.root)
            self.repo.initialize_head(self.root["checkpoint_id"], workflow_ref_id=str(uuid4()))
            self.persist_execution()
            self.success_state = self.state(1, [{"scope": "artifact", "output_id": self.output}])
            self.success = self.checkpoint(
                self.source, self.success_state, root=self.root, before=self.initial_state)
            self.repo.insert(self.success)
            self.repo.retain_checkpoint_history(self.success["checkpoint_id"])
            current_prompt = {**self.prompt["config"], "text": "Current adjustable text"}
            declaration = self.registry.get("prompts.item", "3").definition.adjustable_configuration
            GraphRecordStore(self.store).put_current_configuration({
                "workflow_definition_id": self.workflow, "edit_sequence": 8,
                "fixed_definition_revision": 1, "display_name": "Current adjustable configuration",
                "node_presentation": {
                    node["node_binding_id"]: {key: node[key] for key in ("title", "position")}
                    for node in self.document["nodes"]},
                "adjustable_config": {
                    self.node: {},
                    self.prompt["node_binding_id"]: adjustable_configuration_projection(
                        current_prompt, declaration, self.prompt["configuration_policy"])}})
            self.facts.accept(
                owner={"workflow_session_id": self.source, "chain_run_id": self.chain,
                       "node_binding_id": self.node, "node_run_id": self.run},
                stream_id="test:original", fact={
                    "fact_id": str(uuid4()), "sequence": 1, "original": "unchanged"})

    def repository(self, sid, connection=None):
        return CheckpointRepository(
            connection or self.connection, workflow_definition_id=self.workflow,
            workflow_session_id=sid, fixed_definition_revision=1)

    def authority(self, sid, checkpoint, connection=None):
        authority = HistoryArtifactAuthority(
            connection or self.connection, self.types, workflow_definition_id=self.workflow,
            workflow_session_id=sid, fixed_definition_revision=1, registry=self.registry)
        authority.add_checkpoint_root(checkpoint["checkpoint_id"])
        return authority

    def add_session(self, sid):
        self.store._write("workflow_session", graph_record(
            "workflow_session", workflow_session_id=sid, workflow_definition_id=self.workflow,
            definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None))

    def state(self, count, refs):
        return {
            "schema_version": 1, "kind": "workflow.runtime-state", "node_states": {},
            "program_state": {"values": {"name": {
                "type": "string", "source": "assignment", "value": "baseline" if count == 0 else "historical"}},
                "data": {}},
            "objects": {
                "counter": {"type_id": "test.counter", "schema_version": 1, "deleted": False,
                            "value": {"count": count}},
                "links": {"type_id": "test.links", "schema_version": 1, "deleted": False,
                          "value": {"refs": deepcopy(refs)}}},
            "history": {"selected_round_id": None, "visible_entry_ids": []},
        }

    def checkpoint(self, sid, state, *, root=None, before=None):
        return {
            "schema_version": 1, "checkpoint_id": str(uuid4()),
            "workflow_definition_id": self.workflow, "workflow_session_id": sid,
            "fixed_definition_revision": 1, "kind": "initial" if root is None else "successful_round",
            "parent_checkpoint_id": None if root is None else root["checkpoint_id"],
            "round_id": None if root is None else str(uuid4()),
            "chain_run_id": None if root is None else self.chain,
            "assistant_entry_id": None if root is None else str(uuid4()),
            "state_storage": {"mode": "full", "state": deepcopy(state)} if root is None else {
                "mode": "diff", "base_checkpoint_id": root["checkpoint_id"], **make_diff(before, state)},
        }

    def persist_execution(self):
        for kind, value in (
            ("chain_run", graph_record(
                "chain_run", chain_run_id=self.chain, workflow_session_id=self.source,
                workflow_definition_id=self.workflow, definition_revision=1, status="succeeded",
                revision=3, targets=[self.node], ordered_nodes=[self.node], node_run_ids=[self.run],
                completed_nodes=[self.node], next_node_index=1, inputs={},
                outputs={self.node: {"output": self.output}}, diagnostic=None)),
            ("node_run", graph_record(
                "node_run", profile="node", run_id=self.run, workflow_session_id=self.source,
                node_binding_id=self.node, chain_run_id=self.chain, status="succeeded", revision=2,
                input_refs={}, input_values={}, output_refs={"output": self.output},
                reads=[], effects=[], config={}, diagnostic=None)),
            ("workflow_output", graph_record(
                "workflow_output", output_id=self.output, workflow_session_id=self.source,
                chain_run_id=self.chain, run_id=self.run, node_binding_id=self.node,
                port_id="output", payload={"schema_version": 1, "value": 1})),
        ):
            self.store._write(kind, value)

    def evidence(self):
        return [tuple(row) for row in self.connection.execute(
            "SELECT * FROM records WHERE record_type IN ('chain_run','node_run','workflow_output') ORDER BY record_type")
        ], [tuple(row) for row in self.connection.execute("SELECT * FROM workflow_runtime_facts")]

    def configuration(self):
        return tuple(self.connection.execute("SELECT * FROM workflow_current_configuration").fetchone())

    def restore_objects(self, sid, checkpoint, authority):
        state = self.repository(sid).materialize(checkpoint["checkpoint_id"])
        return self.objects.restore_logical(
            sid, self.document, state["objects"], object_keys=["counter", "links"],
            history_authority=authority)

    def close(self):
        self.store.close()


@pytest.fixture
def history(tmp_path):
    fixture = HistoryFixture(tmp_path / "history.sqlite")
    try:
        yield fixture
    finally:
        fixture.close()


def test_checkpoint_authority_restore_and_head_share_transaction_without_rewriting_facts(history):
    evidence, configuration = history.evidence(), history.configuration()
    with transaction(history.connection):
        authority = history.authority(history.source, history.success)
        receipts = history.restore_objects(history.source, history.success, authority)
        variable_state = history.variables.current(history.source)
        history.variables.write_in_transaction(
            history.source, {"revision": variable_state["revision"], **history.success_state["program_state"]},
            expected_revision=variable_state["revision"], key="restore:variables",
            request={"checkpoint_id": history.success["checkpoint_id"]})
        head = history.repo.move_head(
            history.success["checkpoint_id"], expected_revision=1,
            expected_checkpoint_id=history.root["checkpoint_id"])
        assert head["revision"] == 2
        assert all(receipt["revision"] == 2 for receipt in receipts)
    assert history.objects.current(history.source)["counter"]["value"] == {"count": 1}
    assert history.variables.current(history.source)["revision"] == 1
    assert history.evidence() == evidence
    assert history.configuration() == configuration
    assert history.connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION == 19
    with transaction(history.connection):
        authority = history.authority(history.source, history.success)
        assert history.restore_objects(history.source, history.success, authority) == receipts
        assert history.repo.materialize(history.root["checkpoint_id"]) == history.initial_state
    with transaction(history.connection):
        authority = history.authority(history.source, history.root)
        earlier = history.restore_objects(history.source, history.root, authority)
        assert all(receipt["revision"] == 3 for receipt in earlier)
        history.variables.write_in_transaction(
            history.source, {"revision": 1, **history.initial_state["program_state"]},
            expected_revision=1, key="restore:baseline:variables",
            request={"checkpoint_id": history.root["checkpoint_id"]})
        assert history.repo.move_head(
            history.root["checkpoint_id"], expected_revision=2,
            expected_checkpoint_id=history.success["checkpoint_id"])["revision"] == 3
        assert history.repo.materialize(history.success["checkpoint_id"]) == history.success_state
    assert history.objects.current(history.source)["counter"]["value"] == {"count": 0}
    assert history.variables.current(history.source)["revision"] == 2
    assert history.variables.current(history.source)["values"]["name"]["value"] == "baseline"
    assert history.evidence() == evidence
    assert history.configuration() == configuration


def test_stale_head_after_object_write_rolls_back_every_current_change(history):
    objects, evidence, configuration = (
        history.objects.current(history.source), history.evidence(), history.configuration())
    with pytest.raises(ContractValidationError):
        with transaction(history.connection):
            authority = history.authority(history.source, history.success)
            history.restore_objects(history.source, history.success, authority)
            history.variables.write_in_transaction(
                history.source, {"revision": 0, **history.success_state["program_state"]},
                expected_revision=0, key="rollback:variables", request={"restore": "stale"})
            history.repo.move_head(
                history.success["checkpoint_id"], expected_revision=2,
                expected_checkpoint_id=history.root["checkpoint_id"])
    assert history.objects.current(history.source) == objects
    assert history.variables.current(history.source)["revision"] == 0
    assert history.evidence() == evidence
    assert history.configuration() == configuration
    with transaction(history.connection):
        assert history.repo.get_head()["checkpoint_id"] == history.root["checkpoint_id"]


def test_target_own_checkpoint_and_membership_survive_source_session_removal(history):
    target = str(uuid4())
    evidence = history.evidence()
    with transaction(history.connection):
        history.add_session(target)
        history.objects.initialize(target, history.document)
        root = history.checkpoint(target, history.initial_state)
        success = history.checkpoint(target, history.success_state, root=root, before=history.initial_state)
        repo = history.repository(target)
        repo.insert(root)
        repo.insert(success)
        repo.retain_checkpoint_history(success["checkpoint_id"])
        repo.initialize_head(root["checkpoint_id"], workflow_ref_id=str(uuid4()))
        history.connection.execute(
            "DELETE FROM records WHERE record_type='workflow_session' AND record_id=?", (history.source,))
        history.connection.execute("DELETE FROM session_object_bindings WHERE session_id=?", (history.source,))
        history.connection.execute(
            "DELETE FROM workflow_checkpoints WHERE workflow_session_id=?", (history.source,))
        history.connection.execute(
            "DELETE FROM workflow_history_membership WHERE workflow_session_id=?", (history.source,))
        authority = history.authority(target, success)
        detail = authority.resolve_detail({"scope": "artifact", "output_id": history.output})
        assert detail["producer"]["workflow_session_id"] == history.source
        history.restore_objects(target, success, authority)
        repo.move_head(success["checkpoint_id"], expected_revision=1,
                       expected_checkpoint_id=root["checkpoint_id"])
    assert history.evidence() == evidence
    assert history.objects.current(target)["links"]["value"]["refs"] == [
        {"scope": "artifact", "output_id": history.output}]


def test_history_readonly_transaction_never_writes_or_exposes_unrelated_uuid(history):
    sql = []
    with readonly_history_connection(history.path) as connection:
        connection.set_trace_callback(sql.append)
        authority = history.authority(history.source, history.success, connection)
        assert authority.resolve_detail({"scope": "artifact", "output_id": history.output})["value"] == {
            "schema_version": 1, "value": 1}
        with pytest.raises(ContractValidationError):
            authority.resolve_detail({"scope": "artifact", "output_id": str(uuid4())})
    assert not any(statement.lstrip().split(" ", 1)[0].upper() in {
        "CREATE", "INSERT", "UPDATE", "DELETE", "ALTER"} for statement in sql)


def test_typed_but_forged_restore_body_cannot_borrow_a_formal_authority(history):
    before = history.objects.current(history.source)
    with pytest.raises(ContractValidationError):
        with transaction(history.connection):
            authority = history.authority(history.source, history.success)
            forged = deepcopy(history.success_state["objects"])
            forged["counter"]["value"]["count"] = 99
            history.objects.restore_logical(
                history.source, history.document, forged, object_keys=["counter", "links"],
                history_authority=authority)
    assert history.objects.current(history.source) == before


def test_default_store_does_not_install_checkpoint_tables_or_new_record_versions(tmp_path):
    with closing(SqliteStore(tmp_path / "ordinary.sqlite")) as store:
        tables = {row["name"] for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"workflow_checkpoints", "workflow_history_membership"} & tables
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 19
    assert {kind: GRAPH_VERSIONS[kind] for kind in (
        "workflow_ref", "node_session", "chain_run", "node_binding", "workflow_definition_revision")} == {
            "workflow_ref": 2, "node_session": 2, "chain_run": 5,
            "node_binding": 2, "workflow_definition_revision": 2}
