"""Logical restoration with real owned checkpoint roots, without installed D nodes."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository, initialize_checkpoint_tables
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_records import graph_record
from phase1_agent.historical_artifact_reader import HistoryArtifactAuthority
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, ObjectBinding, TypeRegistry, WriteIntent
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


class LogicalFixture:
    def __init__(self, path):
        self.store = SqliteStore(path)
        self.connection = self.store._connection
        self.sid, self.workflow, self.writer, self.private_owner, self.chain, self.run = (
            str(uuid4()) for _ in range(6))
        self.hooks, self.block_put = [], False
        self.types = TypeRegistry()
        self.types.register(DataTypeDefinition(
            "test.restore-counter", 1,
            {"type": "object", "properties": {"count": {"type": "integer"}},
             "required": ["count"], "additionalProperties": False},
            default_value={"count": 0}, write_validator=self.write_guard))
        self.types.register(DataTypeDefinition(
            "test.restore-links", 1,
            {"type": "object", "properties": {"refs": {"type": "array"}},
             "required": ["refs"], "additionalProperties": False},
            default_value={"refs": []}, references=lambda value: value["refs"]))
        self.document = {
            "schema_version": 2, "workflow_definition_id": self.workflow, "revision": 1,
            "name": "Logical restore fixture", "edges": [],
            "nodes": [{"node_binding_id": self.writer, "component_id": "test.fixture",
                       "component_version": "1", "title": "Fixture", "position": {"x": 0, "y": 0}, "config": {}},
                      {"node_binding_id": self.private_owner, "component_id": "test.fixture",
                       "component_version": "1", "title": "Private", "position": {"x": 1, "y": 0}, "config": {}}],
            "object_bindings": [
                ObjectBinding("counter", "test.restore-counter", 1, "shared",
                              readers=(self.writer, self.private_owner), writers=(self.writer,)).to_dict(),
                ObjectBinding("other", "test.restore-counter", 1, "shared",
                              readers=(self.writer, self.private_owner), writers=(self.writer,)).to_dict(),
                ObjectBinding("links", "test.restore-links", 1, "shared",
                              readers=(self.writer, self.private_owner), writers=(self.writer,)).to_dict(),
                ObjectBinding("secret", "test.restore-counter", 1, "private",
                              readers=(self.private_owner,), writers=(self.private_owner,),
                              owner_node_id=self.private_owner).to_dict(),
            ],
        }
        self.objects = SessionObjectStore(self.store, self.types)
        self.repo = CheckpointRepository(
            self.connection, workflow_definition_id=self.workflow, workflow_session_id=self.sid,
            fixed_definition_revision=1)
        with transaction(self.connection):
            initialize_checkpoint_tables(self.connection)
            self.store._write("workflow_definition_revision", graph_record(
                "workflow_definition_revision", workflow_definition_id=self.workflow, revision=1,
                bindings=[node["node_binding_id"] for node in self.document["nodes"]],
                edges=[], document=self.document))
            definition = NodeDefinition(
                "test.fixture", "1", "Fixture", "Test", {}, {"type": "object", "additionalProperties": False})
            self.store._write("node_definition", graph_record(
                "node_definition", component_id="test.fixture", component_version="1",
                descriptor=definition.to_dict()))
            for node in self.document["nodes"]:
                self.store._write("node_binding", graph_record(
                    "node_binding", workflow_definition_id=self.workflow, workflow_definition_revision=1,
                    **{key: node[key] for key in ("node_binding_id", "component_id", "component_version", "config")}))
            self.store._write("workflow_session", graph_record(
                "workflow_session", workflow_session_id=self.sid, workflow_definition_id=self.workflow,
                definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None))
            TypeContractStore(self.store).check_registry(self.types)
            self.objects.initialize(self.sid, self.document)
            self.initial = self.state()
            self.root = self.point(self.initial)
            self.repo.insert(self.root)
            self.store._write("chain_run", graph_record(
                "chain_run", chain_run_id=self.chain, workflow_session_id=self.sid,
                workflow_definition_id=self.workflow, definition_revision=1, status="succeeded",
                revision=3, targets=[self.writer], ordered_nodes=[self.writer], node_run_ids=[self.run],
                completed_nodes=[self.writer], next_node_index=1, inputs={},
                outputs={self.writer: {}}, diagnostic=None))
            RuntimeFactStore(self.store).accept(
                owner={"workflow_session_id": self.sid, "chain_run_id": self.chain,
                       "node_run_id": self.run},
                stream_id="test:original", fact={"fact_id": str(uuid4()), "sequence": 1, "request": "Original"})
            self.connection.execute(
                "INSERT INTO workflow_current_configuration VALUES(?,?,?,?,?,?)",
                (self.workflow, 12, 1, "Current configuration", "{}", '{"prompt":"current adjustable text"}'))

    def write_guard(self, value, context):
        self.hooks.append(context["operation"])
        if self.block_put and context["operation"] != "initialize":
            raise HostContractError("test_new_write_denied", "Logical restore cannot pose as a new put")

    def current(self):
        return self.objects.current(self.sid)

    def state(self, *, omitted=()):
        return {
            "schema_version": 1, "kind": "workflow.runtime-state", "node_states": {},
            "program_state": {"values": {}, "data": {}},
            "objects": {key: {field: value[field] for field in ("type_id", "schema_version", "deleted", "value")}
                        for key, value in self.current().items() if key not in omitted},
            "history": {"selected_round_id": None, "visible_entry_ids": []},
        }

    def point(self, state, *, successful=False):
        return {
            "schema_version": 1, "checkpoint_id": str(uuid4()), "workflow_definition_id": self.workflow,
            "workflow_session_id": self.sid, "fixed_definition_revision": 1,
            "kind": "successful_round" if successful else "initial",
            "parent_checkpoint_id": self.root["checkpoint_id"] if successful else None,
            "round_id": str(uuid4()) if successful else None, "chain_run_id": self.chain if successful else None,
            "assistant_entry_id": str(uuid4()) if successful else None,
            "state_storage": {"mode": "diff", "base_checkpoint_id": self.root["checkpoint_id"],
                              **make_diff(self.initial, state)} if successful else {"mode": "full", "state": state},
        }

    def save_current(self, *, omitted=()):
        point = self.point(self.state(omitted=omitted), successful=True)
        self.repo.insert(point)
        return point

    def write(self, key, value=None, *, deleted=False):
        return self.objects.apply(self.sid, node_id=self.private_owner if key == "secret" else self.writer, writes=[
            WriteIntent(key, self.current()[key]["revision"], str(uuid4()), value,
                        operation="delete" if deleted else "put").to_dict()])[0]

    def authority(self, point):
        authority = HistoryArtifactAuthority(
            self.connection, self.types, workflow_definition_id=self.workflow, workflow_session_id=self.sid,
            fixed_definition_revision=1)
        authority.add_checkpoint_root(point["checkpoint_id"])
        return authority

    def restore(self, point=None, *, keys=None, authority=None, values=None, document=None):
        point = point or self.root
        keys = keys if keys is not None else list(self.current())
        selected = self.repo.materialize(point["checkpoint_id"])["objects"]
        return self.objects.restore_logical(
            self.sid, document or self.document,
            values if values is not None else {key: selected[key] for key in keys if key in selected},
            object_keys=keys, history_authority=authority or self.authority(point))

    def immutable(self):
        return {table: [tuple(row) for row in self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("records", "workflow_runtime_facts", "workflow_current_configuration",
                              "session_object_receipts", "workflow_checkpoints")}


@pytest.fixture
def fixture(tmp_path):
    value = LogicalFixture(tmp_path / "logical.sqlite")
    try:
        yield value
    finally:
        value.store.close()


def test_restore_requires_transaction_and_real_owned_authority(fixture):
    with pytest.raises(ContractValidationError) as missing:
        fixture.objects.restore_logical(
            fixture.sid, fixture.document, {}, object_keys=[], history_authority=None)
    assert missing.value.reason_code == "storage_contract_violation"
    with transaction(fixture.connection):
        fake = SimpleNamespace(connection=fixture.connection, validate_object_restore=lambda *args: None,
                               resolve_artifact=lambda ref: {"value": ref})
        with pytest.raises(ContractValidationError) as denied:
            fixture.restore(authority=fake)
        assert denied.value.reason_code == "session_object_reference_denied"
        authority = HistoryArtifactAuthority(
            fixture.connection, fixture.types, workflow_definition_id=fixture.workflow,
            workflow_session_id=fixture.sid, fixed_definition_revision=1)
        with pytest.raises(ContractValidationError) as unrooted:
            fixture.restore(authority=authority)
        assert unrooted.value.reason_code == "history_root_required"


def test_restore_changes_live_cas_without_rewinding_revision_or_creating_new_request(fixture):
    with transaction(fixture.connection):
        changed = fixture.write("counter", {"count": 9})
    original_membership = [tuple(row) for row in fixture.connection.execute(
        "SELECT * FROM session_object_revision_membership")]
    immutable = fixture.immutable()
    fixture.hooks.clear()
    fixture.block_put = True
    with transaction(fixture.connection):
        receipt = fixture.restore(keys=["counter"])[0]
    current = fixture.current()["counter"]
    assert current["value"] == {"count": 0} and current["revision"] == changed["revision"] + 1
    assert current["revision_id"] != changed["revision_id"]
    assert receipt == {field: current[field] for field in ("revision", "revision_id", "deleted")} | {"object_key": "counter"}
    assert fixture.immutable() == immutable and not fixture.hooks
    assert all(fixture.connection.execute(
        "SELECT 1 FROM session_object_revision_membership WHERE session_id=? AND object_key=? AND revision_id=?",
        row).fetchone() for row in original_membership)
    assert fixture.objects._value(changed["revision_id"])["value"] == {"count": 9}


def test_unchanged_state_does_not_add_value_or_advance_cas_even_at_limit(fixture):
    with transaction(fixture.connection):
        fixture.connection.execute("UPDATE session_object_bindings SET revision=? WHERE session_id=?",
                                   (2**53 - 1, fixture.sid))
    before = fixture.current()
    count = fixture.connection.execute("SELECT COUNT(*) FROM session_object_values").fetchone()[0]
    with transaction(fixture.connection):
        receipts = fixture.restore()
    assert fixture.current() == before
    assert all(receipt["revision"] == 2**53 - 1 for receipt in receipts)
    assert fixture.connection.execute("SELECT COUNT(*) FROM session_object_values").fetchone()[0] == count


def test_tombstone_and_absent_selected_entry_retain_fixed_binding(fixture):
    with transaction(fixture.connection):
        fixture.write("counter", deleted=True)
        point = fixture.save_current(omitted=["other"])
        fixture.write("counter", {"count": 9})
        fixture.write("other", {"count": 8})
    bindings = {key: item["binding"] for key, item in fixture.current().items()}
    with transaction(fixture.connection):
        fixture.restore(point, keys=["counter", "other"])
    current = fixture.current()
    assert current["counter"]["deleted"] and current["counter"]["value"] is None
    assert current["other"]["deleted"] and current["other"]["value"] is None
    assert {key: item["binding"] for key, item in current.items()} == bindings


def test_partial_restore_leaves_unselected_current_values_and_configuration_untouched(fixture):
    with transaction(fixture.connection):
        fixture.write("counter", {"count": 2})
        fixture.write("other", {"count": 3})
    before, immutable = fixture.current(), fixture.immutable()
    with transaction(fixture.connection):
        assert len(fixture.restore(keys=["counter"])) == 1
    assert fixture.current()["other"] == before["other"]
    assert fixture.current()["counter"]["value"] == {"count": 0}
    assert fixture.immutable() == immutable


@pytest.mark.parametrize("mode", ["body", "manifest", "type", "keys", "binding"])
def test_caller_cannot_replace_proven_root_or_fixed_binding(fixture, mode):
    before = fixture.current()
    with transaction(fixture.connection):
        authority = fixture.authority(fixture.root)
        values = deepcopy(fixture.initial["objects"])
        keys = list(values)
        document = deepcopy(fixture.document)
        if mode == "body":
            values["counter"]["value"]["count"] = 999
        elif mode == "manifest":
            values["counter"].update(revision=1, revision_id=before["counter"]["revision_id"])
        elif mode == "type":
            values["counter"]["schema_version"] = 2
        elif mode == "keys":
            keys.append("unknown")
        else:
            document["object_bindings"][0]["readers"] = [fixture.writer]
        with pytest.raises((ContractValidationError, HostContractError)):
            fixture.restore(authority=authority, values=values, keys=keys, document=document)
        assert fixture.current() == before


def test_type_package_missing_refuses_entire_restore_before_writing(fixture):
    with transaction(fixture.connection):
        fixture.write("counter", {"count": 7})
        authority = fixture.authority(fixture.root)
        fixture.objects.types = TypeRegistry()
        before = fixture.current()
        with pytest.raises(HostContractError) as missing:
            fixture.restore(authority=authority)
        assert missing.value.reason_code == "host_unknown_type"
        assert fixture.current() == before


@pytest.mark.parametrize("mode", ["private", "foreign", "membership", "artifact"])
def test_owned_checkpoint_reference_proof_rejects_private_or_ungranted_targets(fixture, mode):
    before = fixture.current()
    with pytest.raises(ContractValidationError):
        with transaction(fixture.connection):
            if mode == "artifact":
                reference = {"scope": "artifact", "output_id": str(uuid4())}
            else:
                key = "secret" if mode == "private" else "counter"
                reference = {"scope": "session",
                             "workflow_session_id": str(uuid4()) if mode == "foreign" else fixture.sid,
                             "object_key": key, "revision_id": before[key]["revision_id"]}
            value_id = fixture.objects._put_value(
                ObjectBinding.from_dict(before["links"]["binding"]), {"refs": [reference]})
            fixture.objects._grant_revision(fixture.sid, "links", value_id)
            fixture.connection.execute(
                "UPDATE session_object_bindings SET revision_id=? WHERE session_id=? AND object_key='links'",
                (value_id, fixture.sid))
            point = fixture.save_current()
            if mode == "membership":
                fixture.connection.execute(
                    "DELETE FROM session_object_revision_membership WHERE session_id=? AND object_key='counter'",
                    (fixture.sid,))
            fixture.restore(point)
    assert fixture.current() == before


def test_owned_session_revision_reference_uses_existing_membership_not_old_head(fixture, monkeypatch):
    with transaction(fixture.connection):
        original = fixture.current()["counter"]
        fixture.write("links", {"refs": [{"scope": "session", "workflow_session_id": fixture.sid,
                                         "object_key": "counter", "revision_id": original["revision_id"]}]})
        point = fixture.save_current()
        fixture.write("counter", {"count": 9})
        fixture.write("links", {"refs": []})
    monkeypatch.setattr(fixture.objects, "_artifact_history", lambda *args: pytest.fail("Old Head permission fallback"))
    with transaction(fixture.connection):
        fixture.restore(point, keys=["links"])
    assert fixture.current()["counter"]["value"] == {"count": 9}
    assert fixture.current()["links"]["value"]["refs"][0]["revision_id"] == original["revision_id"]
    assert fixture.objects.read(fixture.sid, "counter", node_id=fixture.writer,
                                revision_id=original["revision_id"])["value"] == {"count": 0}


def test_caller_rollback_discards_all_new_values_and_membership(fixture):
    with transaction(fixture.connection):
        fixture.write("counter", {"count": 7})
        fixture.write("other", {"count": 8})
    before = fixture.current()
    values = [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_values")]
    memberships = [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_revision_membership")]
    with pytest.raises(OSError):
        with transaction(fixture.connection):
            fixture.restore(keys=["counter", "other"])
            raise OSError("Controlled later Head failure")
    assert fixture.current() == before
    assert [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_values")] == values
    assert [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_revision_membership")] == memberships


def test_revision_exhaustion_is_prechecked_before_any_object_write(fixture):
    with transaction(fixture.connection):
        fixture.write("counter", {"count": 7})
        fixture.write("other", {"count": 8})
        fixture.connection.execute(
            "UPDATE session_object_bindings SET revision=? WHERE session_id=? AND object_key='other'",
            (2**53 - 1, fixture.sid))
    before = fixture.current()
    with transaction(fixture.connection):
        with pytest.raises(ContractValidationError) as exhausted:
            fixture.restore(keys=["counter", "other"])
        assert exhausted.value.reason_code == "session_object_revision_exhausted"
        assert fixture.current() == before


def test_real_object_cas_rejects_head_change_during_write_and_caller_rolls_back(fixture, monkeypatch):
    with transaction(fixture.connection):
        fixture.write("counter", {"count": 7})
    before = fixture.current()
    put = fixture.objects._put_value

    def change_head(binding, value, *, deleted=False):
        identity = put(binding, value, deleted=deleted)
        fixture.connection.execute(
            "UPDATE session_object_bindings SET revision=revision+1 WHERE session_id=? AND object_key=?",
            (fixture.sid, binding.object_key))
        return identity

    monkeypatch.setattr(fixture.objects, "_put_value", change_head)
    with pytest.raises(ContractValidationError) as stale:
        with transaction(fixture.connection):
            fixture.restore(keys=["counter"])
    assert stale.value.reason_code == "stale_object_revision"
    assert fixture.current() == before


@pytest.mark.parametrize("mode", ["deleted-bit", "tombstone-body", "revision-identity", "typed-body", "membership"])
def test_logical_restore_reports_corrupt_current_value_instead_of_masking_it(fixture, mode, monkeypatch):
    with transaction(fixture.connection):
        authority = fixture.authority(fixture.root)
        identity = fixture.current()["counter"]["revision_id"]
        if mode in ("deleted-bit", "tombstone-body"):
            fixture.connection.execute(
                "UPDATE session_object_values SET deleted=? WHERE revision_id=?",
                (2 if mode == "deleted-bit" else 1, identity))
        elif mode == "revision-identity":
            fixture.connection.execute(
                "UPDATE session_object_bindings SET revision_id='not-a-revision' WHERE session_id=? AND object_key='counter'",
                (fixture.sid,))
        elif mode == "typed-body":
            fixture.connection.execute(
                "UPDATE session_object_values SET value=? WHERE revision_id=?", ('{"count":"corrupt"}', identity))
        else:
            fixture.connection.execute(
                "DELETE FROM session_object_revision_membership WHERE session_id=? AND object_key='counter'",
                (fixture.sid,))
        before = [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_bindings")]
        monkeypatch.setattr(fixture.objects, "_put_value", lambda *args, **kwargs: pytest.fail("Corruption was masked"))
        with pytest.raises((ContractValidationError, HostContractError)):
            fixture.restore(keys=["counter"], authority=authority)
        assert [tuple(row) for row in fixture.connection.execute("SELECT * FROM session_object_bindings")] == before
