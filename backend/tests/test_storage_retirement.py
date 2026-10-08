"""Retired and affiliated test data is removed without touching unrelated workflows."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore, STORAGE_VERSION

from test_graph_service import create, run, text_graph
from test_graph_resource_host import prompt_record
from phase1_agent.storage_retirement import _COMPAT_NODES, _RETIRED_NODES, _compat


def seed_old(connection, *, lock=True):
    sid, definition, chain = str(uuid4()), str(uuid4()), str(uuid4())
    document = {"schema_version": 2 if lock else 1, "workflow_definition_id": definition,
                "revision": 1, "nodes": [{"component_id": "workflow.text", "component_version": "1"}]}
    if lock:
        document["package_lock"] = [{"package_id": "workflow.compat", "version": "1.0.0"}]
    records = [
        ("workflow_session", sid, {"execution_model": "graph", "workflow_session_id": sid,
                                  "workflow_definition_id": definition, "definition_revision": 1}),
        ("workflow_definition_revision", definition, {"execution_model": "graph",
                "workflow_definition_id": definition, "revision": 1, "document": document}),
        ("chain_run", chain, {"execution_model": "graph", "workflow_session_id": sid, "chain_run_id": chain}),
        ("visible_message", "fixed-message", {"schema_version": 1, "workflow_session_id": "fixed-test"}),
        ("agent_message", "fixed-agent-message", {"schema_version": 1, "message_id": "fixed-agent-message"}),
        ("node_definition", "compat-node", {"execution_model": "graph",
                                          "component_id": "workflow.text", "component_version": "1"}),
    ]
    connection.executemany("INSERT INTO records VALUES(?,?,?,1,'test')",
                           [(kind, identity, canonical_bytes(value).decode()) for kind, identity, value in records])
    connection.execute("INSERT INTO graph_state_manifests VALUES(?,?,?)",
                       ("chain_boundary", chain + ":7", canonical_bytes({"workflow_session_id": sid}).decode()))
    connection.execute("CREATE TABLE workbench_resource_heads(resource_id TEXT)")
    connection.execute("INSERT INTO workbench_resource_heads VALUES('old-content')")
    connection.execute("CREATE TABLE execution_facts(payload TEXT)")
    connection.execute("INSERT INTO execution_facts VALUES('old-dispatch')")
    connection.execute("PRAGMA user_version=13")
    return sid, chain, document


@pytest.mark.parametrize("component,version", sorted(_RETIRED_NODES - _COMPAT_NODES))
def test_retirement_classifies_only_explicit_old_node_declarations(component, version):
    assert _compat({"nodes": [{"component_id": component, "component_version": version}]})
    assert not _compat({"nodes": [{"component_id": component, "component_version": "999"}]})


@pytest.mark.parametrize("component,version", [
    ("models.source", "2"), ("models.source", "4"), ("models.chat", "3"), ("models.chat", "4"),
    ("agents.execute", "4"), ("agents.execute", "8"), ("prompts.assembly", "2"),
    ("context.output", "4"), ("context.assembly", "4"), ("context.merge", "4"),
])
def test_retirement_preserves_current_protocol_and_native_declarations(component, version):
    assert not _compat({"nodes": [{"component_id": component, "component_version": version}]})


@pytest.mark.parametrize("retired", ["models.source", "agents.execute", "prompts.assembly", "retired-package"])
def test_v15_removes_retired_node_workflow_and_receipts_but_keeps_unrelated_current(tmp_path, retired):
    path = tmp_path / "route-retirement.sqlite"
    with closing(GraphWorkflowService(path)) as service:
        app = GraphApplication(service)
        document = text_graph(service.registry)
        app.command("definition.save", {
            "document": document, "expected_revision": 0, "idempotency_key": "current-save"})
        created = app.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"], "definition_revision": 1,
            "idempotency_key": "current-session"})["result"]
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        before = connection.execute("SELECT * FROM records ORDER BY record_type,record_id").fetchall()
        receipts = connection.execute("SELECT * FROM idempotency ORDER BY operation,key").fetchall()
        sid, chain, old = seed_old(connection)
        old["nodes"] = [{"component_id": retired, "component_version": "1"}]
        old["package_lock"] = [{"package_id": "workflow.context-compression", "version": "1.0.0"}] \
            if retired == "retired-package" else []
        connection.execute(
            "UPDATE records SET payload=? WHERE record_type='workflow_definition_revision' AND record_id=?",
            (canonical_bytes({"execution_model": "graph", "workflow_definition_id": old["workflow_definition_id"],
                              "revision": 1, "document": old}).decode(), old["workflow_definition_id"]))
        connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?)", (
            "graph.definition.save", "old-save", "old-digest", "[]",
            canonical_bytes([{"graph_result": old}]).decode(), "old-digest"))
        connection.execute(
            "UPDATE graph_project_packages SET payload=? WHERE configuration_id='current-execution'",
            (canonical_bytes({**DEFAULT_PACKAGES, "workflow.context-compression": "1.0.0"}).decode(),))
        connection.execute("PRAGMA user_version=14")
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert [tuple(row) for row in store._connection.execute(
            "SELECT * FROM records ORDER BY record_type,record_id")] == before
        assert [tuple(row) for row in store._connection.execute(
            "SELECT * FROM idempotency ORDER BY operation,key")] == receipts
        assert store._connection.execute("SELECT * FROM records WHERE record_id IN (?,?)", (sid, chain)).fetchone() is None
        assert store._connection.execute(
            "SELECT payload FROM graph_project_packages WHERE configuration_id='current-execution'"
        ).fetchone()[0] == canonical_bytes(DEFAULT_PACKAGES).decode()
    with closing(GraphWorkflowService(path)) as service:
        assert service.get_session(created["workflow_session_id"]) == created


def test_v15_removes_resource_one_and_its_graph_without_breaking_current_resource_list(tmp_path):
    path = tmp_path / "resource-retirement.sqlite"
    current = prompt_record("Current lifecycle resource")
    old = deepcopy(current)
    old.update(scope="project:retired", data_schema_version=1)
    for member in old["value"]["members"]:
        del member["lifecycle"], member["compaction"]
    identity = ResourceIdentity(old["scope"], old["type_id"], old["resource_id"]).to_dict()
    with closing(GraphWorkflowService(path)) as service:
        service.save_global_resource(current, expected_sequence=0, idempotency_key=str(uuid4()))
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        before = connection.execute("SELECT * FROM global_resource_current").fetchall()
        connection.execute("INSERT INTO global_resource_current VALUES(?,?,?,1,0,?)",
                           (old["scope"], old["type_id"], old["resource_id"], canonical_bytes(old).decode()))
        connection.execute("INSERT INTO global_resource_receipts VALUES(?,?,?)",
                           ("old-prompt", "old-digest", canonical_bytes({
                               "reference": identity, "update_sequence": 1, "deleted": False}).decode()))
        sid, _, retired = seed_old(connection)
        retired["package_lock"] = []
        retired["nodes"] = [{"component_id": "prompts.global-reference", "component_version": "2",
                             "config": {"reference": identity}}]
        connection.execute(
            "UPDATE records SET payload=? WHERE record_type='workflow_definition_revision' AND record_id=?",
            (canonical_bytes({"execution_model": "graph", "workflow_definition_id": retired["workflow_definition_id"],
                              "revision": 1, "document": retired}).decode(), retired["workflow_definition_id"]))
        connection.execute("PRAGMA user_version=14")
    with closing(GraphWorkflowService(path)) as service:
        assert service.list_global_resources(type_id=current["type_id"]) == [current]
        assert service.get_global_resource(identity) is None
        with closing(SqliteStore(path)) as store:
            assert [tuple(row) for row in store._connection.execute("SELECT * FROM global_resource_current")] == before
            assert store._connection.execute("SELECT 1 FROM global_resource_receipts WHERE idempotency_key='old-prompt'").fetchone() is None
            assert store._connection.execute("SELECT 1 FROM records WHERE record_id=?", (sid,)).fetchone() is None


@pytest.mark.parametrize("lock", [True, False], ids=["full-compat-lock", "exact-schema1-components"])
def test_cleanup_removes_fixed_and_compat_data_and_keeps_current_bytes(tmp_path, lock):
    path = tmp_path / "mixed.sqlite"
    with closing(GraphWorkflowService(path)) as service:
        app = GraphApplication(service)
        document = text_graph(service.registry)
        app.command("definition.save", {"document": document, "expected_revision": 0, "idempotency_key": "current-save"})
        created = app.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"], "definition_revision": 1,
            "idempotency_key": "current-session"})["result"]
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        before = connection.execute("SELECT * FROM records ORDER BY record_type,record_id").fetchall()
        receipts = connection.execute("SELECT * FROM idempotency ORDER BY operation,key").fetchall()
        old_sid, old_chain, old_document = seed_old(connection, lock=lock)
        connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?)",
                           ("graph.definition.save", "old-save", "old-digest", "[]",
                            canonical_bytes([{"graph_result": old_document}]).decode(), "old-digest"))
        connection.execute("INSERT INTO records VALUES('custom.future','future','{\"schema_version\":99}',1,'test')")
        connection.execute("INSERT INTO records VALUES('workflow_session','unknown-version','{\"schema_version\":99}',1,'test')")
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION
        assert store._connection.execute("SELECT 1 FROM records WHERE record_id IN (?,?)", (old_sid, old_chain)).fetchone() is None
        current = store._connection.execute(
            "SELECT * FROM records WHERE record_type!='custom.future' AND record_id!='unknown-version' ORDER BY record_type,record_id").fetchall()
        assert [tuple(row) for row in current] == before
        assert [tuple(row) for row in store._connection.execute("SELECT * FROM idempotency ORDER BY operation,key")] == receipts
        assert store._connection.execute("SELECT payload FROM records WHERE record_id='future'").fetchone()[0] == '{"schema_version":99}'
        assert store._connection.execute("SELECT payload FROM records WHERE record_id='unknown-version'").fetchone()[0] == '{"schema_version":99}'
        assert store._connection.execute("SELECT 1 FROM records WHERE record_id='fixed-agent-message'").fetchone() is None
        assert store._connection.execute("SELECT 1 FROM graph_state_manifests WHERE owner_id=?", (old_chain + ":7",)).fetchone() is None
        names = {row[0] for row in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "execution_facts" not in names and "workbench_resource_heads" not in names
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.get_session(created["workflow_session_id"]) == created
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION


def test_cleanup_deletes_the_entire_workflow_linked_to_retired_history(tmp_path):
    path = tmp_path / "dependency.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        sid, chain, _ = seed_old(connection)
        current_sid, current_definition = str(uuid4()), str(uuid4())
        current = {"execution_model": "graph", "workflow_session_id": current_sid,
                   "workflow_definition_id": current_definition, "history_refs": [chain]}
        connection.execute("INSERT INTO records VALUES('state_snapshot','current',?,1,'test')",
                           (canonical_bytes(current).decode(),))
        connection.execute("INSERT INTO records VALUES('workflow_session',?, ?,1,'test')",
                           (current_sid, canonical_bytes({"execution_model": "graph",
                            "workflow_session_id": current_sid, "workflow_definition_id": current_definition}).decode()))
        connection.execute("INSERT INTO records VALUES('workflow_definition_revision',?, ?,1,'test')",
                           (current_definition, canonical_bytes({"execution_model": "graph",
                            "workflow_definition_id": current_definition, "revision": 1}).decode()))
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION
        assert store._connection.execute("SELECT * FROM records WHERE record_id IN (?,?,?)",
                                         ("current", current_sid, current_definition)).fetchone() is None
        assert store._connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_resource_heads'").fetchone() is None


def test_object_cleanup_also_deletes_sessions_sharing_old_revisions(tmp_path):
    path = tmp_path / "object-ownership.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        sid, _, _ = seed_old(connection)
        connection.executemany("INSERT INTO session_object_values VALUES(?,?,1,0,'{}')",
                               [(identity, "example.object") for identity in ("old-only", "shared", "unknown-orphan")])
        connection.executemany("INSERT INTO session_object_revision_membership VALUES(?,?,?)",
                               [(sid, "old", "old-only"), (sid, "shared", "shared"),
                                ("current-session", "shared", "shared")])
        connection.execute("INSERT INTO session_object_bindings VALUES(?,?,'{}',1,?)", (sid, "old", "old-only"))
        connection.execute("INSERT INTO session_object_receipts VALUES(?,?,'digest','{}')", (sid, "old-write"))
    with closing(SqliteStore(path)) as store:
        assert {row[0] for row in store._connection.execute("SELECT revision_id FROM session_object_values")} == {
            "unknown-orphan"}
        assert store._connection.execute("SELECT * FROM session_object_revision_membership").fetchone() is None
        assert store._connection.execute("SELECT * FROM session_object_bindings").fetchone() is None
        assert store._connection.execute("SELECT * FROM session_object_receipts").fetchone() is None


@pytest.mark.parametrize("boundary", ["object", "manifest"])
def test_related_object_and_manifest_test_data_is_deleted_instead_of_blocking_cleanup(tmp_path, boundary):
    path = tmp_path / (boundary + "-dependency.sqlite")
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        _, chain, _ = seed_old(connection)
        reference = canonical_bytes({"workflow_session_id": "related-session", "history_refs": [chain]}).decode()
        if boundary == "object":
            connection.execute("INSERT INTO session_object_values VALUES('current-value','example.object',1,0,?)",
                               (reference,))
            connection.execute("INSERT INTO session_object_revision_membership VALUES('related-session','context','current-value')")
        else:
            connection.execute("INSERT INTO graph_state_manifests VALUES('state_snapshot',?,?)",
                               (str(uuid4()), reference))
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION
        assert store._connection.execute("SELECT * FROM graph_state_manifests").fetchone() is None
        assert store._connection.execute("SELECT * FROM session_object_values WHERE revision_id='current-value'").fetchone() is None
        assert store._connection.execute("SELECT * FROM session_object_revision_membership").fetchone() is None


@pytest.mark.parametrize("link", ["payload", "chain-owner", "node-owner"])
def test_fact_links_remove_their_workflow_and_receipts_but_preserve_independent_facts(tmp_path, link):
    path = tmp_path / (link + "-fact-dependency.sqlite")
    with closing(SqliteStore(path)) as store:
        RuntimeFactStore(store)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        _, old_chain, _ = seed_old(connection)
        sid, definition = str(uuid4()), str(uuid4())
        session = {"execution_model": "graph", "workflow_session_id": sid,
                   "workflow_definition_id": definition}
        connection.executemany("INSERT INTO records VALUES(?,?,?,1,'test')", [
            ("workflow_session", sid, canonical_bytes(session).decode()),
            ("workflow_definition_revision", definition, canonical_bytes({
                "execution_model": "graph", "workflow_definition_id": definition, "revision": 1,
            }).decode()),
        ])
        chain = old_chain if link == "chain-owner" else "related-chain"
        node_run = old_chain if link == "node-owner" else "related-node"
        payload = {"history_refs": [old_chain]} if link == "payload" else {"kind": "test-fact"}
        connection.execute("INSERT INTO workflow_runtime_facts VALUES(?,?,?,?,?,?,?)",
                           ("related-fact", sid, chain, node_run, "test", 1, canonical_bytes(payload).decode()))
        connection.execute("INSERT INTO workflow_runtime_facts VALUES(?,?,?,?,?,?,?)",
                           ("independent-fact", "independent-session", "independent-chain",
                            "independent-node", "test", 1, '{"keep":"current"}'))
        independent = connection.execute(
            "SELECT * FROM workflow_runtime_facts WHERE fact_id='independent-fact'").fetchone()
        connection.execute("INSERT INTO graph_state_manifests VALUES('state_snapshot','related-manifest',?)",
                           (canonical_bytes({"workflow_session_id": sid}).decode(),))
        connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?)",
                           ("graph.start", "related-start", "digest", "[]",
                            canonical_bytes([session]).decode(), "digest"))
        identity = {"application_identity": {"native_receipt": {
            "table": "idempotency", "operation": "graph.start", "key": "related-start",
        }}}
        connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?)",
                           ("graph.application.command-identity.v1", "related-identity", "digest", "[]",
                            canonical_bytes([identity]).decode(), "digest"))
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("SELECT 1 FROM records WHERE record_id IN (?,?)",
                                         (sid, definition)).fetchone() is None
        assert [tuple(row) for row in store._connection.execute("SELECT * FROM workflow_runtime_facts")] == [
            independent]
        assert store._connection.execute("SELECT * FROM graph_state_manifests").fetchone() is None
        assert store._connection.execute("SELECT * FROM idempotency").fetchone() is None


def test_actual_storage_failure_rolls_back_the_whole_cleanup(tmp_path, monkeypatch):
    from phase1_agent import storage_retirement

    path = tmp_path / "atomic-failure.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        seed_old(connection)
        before = connection.execute("SELECT * FROM records ORDER BY record_type,record_id").fetchall()
    remove = storage_retirement.remove_retired_test_data

    def fail_after_cleanup(connection):
        remove(connection)
        raise sqlite3.OperationalError("injected cleanup failure")

    monkeypatch.setattr(storage_retirement, "remove_retired_test_data", fail_after_cleanup)
    with pytest.raises(ContractValidationError, match="initialization failed"):
        SqliteStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert connection.execute("SELECT * FROM records ORDER BY record_type,record_id").fetchall() == before
        assert connection.execute("SELECT * FROM workbench_resource_heads").fetchall() == [("old-content",)]


def test_fresh_storage_does_not_create_old_tables_or_old_write_methods(tmp_path):
    with closing(SqliteStore(tmp_path / "fresh.sqlite")) as store:
        names = {row[0] for row in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"records", "idempotency", "session_object_bindings", "global_resource_current"} <= names
        assert not names & {"prompt_heads", "variable_heads", "execution_facts", "workbench_resource_heads"}
        assert not hasattr(store, "save_pre_dispatch") and not hasattr(store, "archive_success")


def test_current_node_run_v4_history_is_upgraded_and_can_run_again(tmp_path):
    path = tmp_path / "current-v4.sqlite"
    with closing(GraphWorkflowService(path)) as service:
        initial = create(service, text_graph(service.registry))
        finished = run(service, initial)
        original = service.get_run(initial["workflow_session_id"], finished["selected_chain_run_id"])
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        receipts = connection.execute("SELECT * FROM idempotency ORDER BY operation,key").fetchall()
        for row in original["node_runs"]:
            previous = {**deepcopy(row), "schema_version": 4, "agent": None}
            connection.execute("UPDATE records SET payload=? WHERE record_type='node_run' AND record_id=?",
                               (canonical_bytes(previous).decode(), row["run_id"]))
        connection.execute("PRAGMA user_version=13")
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.get_run(initial["workflow_session_id"], finished["selected_chain_run_id"]) == original
        current = reopened.get_session(initial["workflow_session_id"])
        assert run(reopened, current)["status"] == "succeeded"
    with closing(SqliteStore(path)) as store:
        assert all(row["schema_version"] == 5 and "agent" not in row
                   for row in GraphRecordStore(store).rows("node_run"))
        for receipt in receipts:
            assert tuple(store._connection.execute(
                "SELECT * FROM idempotency WHERE operation=? AND key=?", receipt[:2]).fetchone()) == receipt


@pytest.mark.parametrize("historical,current,expected", [
    (' {"workflow.tools":"1.0.0"}\n', None, '{"workflow.tools":"1.0.0"}'),
    ('{"workflow.compat":"1.0.0","workflow.tools":"1.0.0"}', None, '{"workflow.tools":"1.0.0"}'),
    ('{"workflow.compat":"1.0.0"}', None, '{}'),
    ('{}', None, '{}'),
    ('{"workflow.tools":"1.0.0"}', ' \n{}\n ', ' \n{}\n '),
    ('{', None, None),
])
def test_selection_upgrade_preserves_current_choice_without_project_fallback(tmp_path, historical, current, expected):
    path = tmp_path / "selection-upgrade.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("INSERT INTO graph_project_packages VALUES('project',?)", (historical,))
        if current is not None:
            connection.execute("INSERT INTO graph_project_packages VALUES('current-execution',?)", (current,))
        connection.execute("PRAGMA user_version=13")
    with closing(SqliteStore(path)) as store:
        rows = dict(store._connection.execute("SELECT configuration_id,payload FROM graph_project_packages"))
        assert rows == ({} if expected is None else {"current-execution": expected})
