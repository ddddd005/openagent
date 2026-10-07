"""Real current command origins, checked without entering the coordinator."""

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import sqlite3
from uuid import UUID

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageManifest,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import DataTypeDefinition
from phase1_agent.storage import SqliteStore


IDENTITY_OPERATION = "graph.application.command-identity.v1"
READ_KIND = "workflow.application-receipt-read"
TOOLS = {"workflow.tools": "1.0.0"}
PUBLIC_RECEIPT_FIELDS = {
    "workflow_definition_id", "definition_revision", "workflow_session_id",
    "session_revision", "status", "chain_run_id", "operation", "idempotency_key",
}


def uid(number):
    return str(UUID(int=number, version=4))


def encoded(value):
    return canonical_bytes(value).decode("utf-8")


def forbidden(*args, **kwargs):
    pytest.fail("Receipt custody entered storage initialization or execution")


def new_service(path, **options):
    return GraphWorkflowService(
        path, public_model_factory=forbidden, **options,
    )


def raw_database(path):
    """Snapshot schema/version and literal rows using SQLite's read-only URI."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        schema = tuple(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name",
        ))
        tables = {}
        for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute("SELECT * FROM " + quoted + " ORDER BY rowid")
            tables[name] = (tuple(column[0] for column in cursor.description), tuple(cursor))
        return connection.execute("PRAGMA user_version").fetchone()[0], schema, tables


def prohibit_coordinators(monkeypatch):
    monkeypatch.setattr(SqliteStore, "__init__", forbidden)
    monkeypatch.setattr(GraphWorkflowService, "__init__", forbidden)
    monkeypatch.setattr(GraphRecordStore, "atomic", forbidden)
    monkeypatch.setattr(CapabilityPackageLoader, "load", forbidden)


def read_receipt(path, operation, parameters, *, scope="management"):
    from phase1_agent.graph_receipts import read_graph_application_receipt

    return read_graph_application_receipt(path, operation, parameters, scope=scope)


def assert_unresolved(result):
    assert set(result) == {"schema_version", "kind", "outcome", "reason_code"}
    assert result["schema_version"] == 1 and result["kind"] == READ_KIND
    assert result["outcome"] == "unresolved"
    assert type(result["reason_code"]) is str and result["reason_code"]


def assert_not_trusted(callback):
    try:
        result = callback()
    except ContractValidationError:
        return
    assert_unresolved(result)


def assert_matched(result, origin, *, consumer=False):
    assert set(result) == {
        "schema_version", "kind", "outcome", "reason_code", "receipt", "result",
    }
    assert result["schema_version"] == 1 and result["kind"] == READ_KIND
    assert result["outcome"] == "matched" and result["reason_code"] == "receipt_matched"
    assert result["receipt"] == origin["receipt"]
    expected = {"receipt": origin["result"]["receipt"]} if consumer else origin["result"]
    assert result["result"] == expected
    if consumer:
        assert set(result["result"]["receipt"]) == PUBLIC_RECEIPT_FIELDS


def document(instance, *, revision=1):
    return {
        "schema_version": 2, "workflow_definition_id": uid(1), "revision": revision,
        "name": "Receipt custody", "object_bindings": [],
        "package_lock": deepcopy(list(instance.registry.execution_package_lock)),
        "nodes": [
            {"node_binding_id": uid(2), "component_id": "tools.text", "component_version": "1",
             "title": "Source", "position": {"x": 0, "y": 0}, "config": {"text": "private body"}},
            {"node_binding_id": uid(3), "component_id": "tools.output", "component_version": "1",
             "title": "Output", "position": {"x": 200, "y": 0}, "config": {"mode": "text"},
             "public_outputs": ["output"]},
        ],
        "edges": [{"edge_id": uid(4), "source_node_id": uid(2), "source_port_id": "output",
                   "target_node_id": uid(3), "target_port_id": "input", "order": 0}],
    }


def seed_created(path, *, consumer=False, direct=False):
    operation = "consumer.session.create" if consumer else "session.create"
    parameters = {
        "workflow_definition_id": uid(1), "definition_revision": 1,
        "idempotency_key": "custody-session",
    }
    with closing(new_service(path, enabled_packages=TOOLS)) as instance:
        app = GraphApplication(instance, scope="consumer" if consumer else "management")
        value = document(instance)
        GraphApplication(instance).command("definition.save", {
            "document": value, "expected_revision": 0, "idempotency_key": "custody-definition",
        })
        origin = (instance.create_session(**parameters) if direct else app.command(operation, parameters))
    return operation, parameters, origin


def companion_key(operation, scope, key):
    return sha256(canonical_bytes({
        "operation": operation, "operation_scope": scope, "idempotency_key": key,
    })).hexdigest()


def edit_receipt(path, operation, key, *, column, value):
    assert column in {"digest", "request_digest", "result_refs", "result_payload"}
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        cursor = connection.execute(
            "UPDATE idempotency SET " + column + "=? WHERE operation=? AND key=?",
            (value, operation, key),
        )
        assert cursor.rowcount == 1


def test_real_origin_matches_without_storage_initialization_and_preserves_every_row(
    tmp_path, monkeypatch,
):
    path = tmp_path / "origin.sqlite"
    operation, parameters, origin = seed_created(path)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_matched(read_receipt(path, operation, parameters), origin)
    assert_matched(read_receipt(path, operation, deepcopy(parameters)), origin)
    assert raw_database(path) == before


def test_original_create_receipt_survives_real_definition_rebind(tmp_path, monkeypatch):
    path = tmp_path / "rebind.sqlite"
    operation, parameters, origin = seed_created(path)
    original = origin["result"]
    with closing(new_service(path)) as instance:
        app = GraphApplication(instance)
        app.command("definition.save", {
            "document": document(instance, revision=2), "expected_revision": 1,
            "idempotency_key": "custody-definition-second",
        })
        rebound = app.command("session.rebind", {
            "session_id": original["workflow_session_id"], "definition_revision": 2,
            "expected_revision": original["revision"],
            "expected_data_revision": original["data_revision"],
            "expected_head_revision": original["head_revision"],
            "idempotency_key": "custody-rebind",
        })["result"]
        assert rebound["definition_revision"] == 2
        assert rebound["head_commit_id"] != original["head_commit_id"]
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_matched(read_receipt(path, operation, parameters), origin)
    assert raw_database(path) == before


def test_receipt_does_not_scan_or_repair_changed_active_status_heads(tmp_path, monkeypatch):
    path = tmp_path / "changed-current.sqlite"
    operation, parameters, origin = seed_created(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        for kind in ("workflow_session", "workflow_ref"):
            rows = connection.execute(
                "SELECT record_id,payload FROM records WHERE record_type=?", (kind,),
            ).fetchall()
            assert rows
            for identity, payload in rows:
                changed = loads_strict(payload)
                changed["revision"] += 20
                if kind == "workflow_session":
                    changed["active_chain_run_id"] = uid(101)
                    changed["status"] = "recovery_unavailable"
                else:
                    changed["head_commit_id"] = uid(102)
                connection.execute(
                    "UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
                    (encoded(changed), kind, identity),
                )
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_matched(read_receipt(path, operation, parameters), origin)
    assert raw_database(path) == before


def test_direct_native_receipt_and_its_application_replay_never_backfill_origin(tmp_path, monkeypatch):
    path = tmp_path / "old-origin.sqlite"
    operation, parameters, _ = seed_created(path, direct=True)
    with closing(new_service(path)) as instance:
        before_replay = raw_database(path)
        replay = GraphApplication(instance).command(operation, parameters)
        assert replay["receipt"]["operation"] == operation
        assert raw_database(path) == before_replay
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, operation, parameters))
    assert raw_database(path) == before


@pytest.mark.parametrize("defect", ["no-origin", "no-request-digest", "content-only", "unknown-key"])
def test_insufficient_existing_evidence_remains_unresolved_without_writes(tmp_path, monkeypatch, defect):
    path = tmp_path / "insufficient.sqlite"
    operation, parameters, _ = seed_created(path)
    if defect == "no-origin":
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute("DELETE FROM idempotency WHERE operation=?", (IDENTITY_OPERATION,))
    elif defect == "unknown-key":
        parameters["idempotency_key"] = "never-accepted"
    else:
        edit_receipt(path, "graph.session.create", parameters["idempotency_key"],
                     column="request_digest", value=None)
        if defect == "content-only":
            edit_receipt(path, "graph.session.create", parameters["idempotency_key"],
                         column="digest", value="sha256:" + "0" * 64)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, operation, parameters))
    assert raw_database(path) == before


@pytest.mark.parametrize("column,value", [
    ("result_payload", "{"),
    ("result_payload", '{"graph_result":{}}'),
    ("result_payload", '[{"graph_result":{}},{"graph_result":{}}]'),
    ("result_payload", '[{"graph_result":{},"extra":"untrusted"}]'),
    ("result_payload", '[{"graph_result":{},"graph_result":{}}]'),
    ("result_refs", '[["workflow_session","foreign"]]'),
    ("digest", "workflow-op-v1:" + "0" * 64),
], ids=["bad-json", "wrong-wrapper", "duplicate-results", "extra-fields",
        "duplicate-json-key", "foreign-refs", "wrong-digest"])
def test_damaged_native_rows_are_not_receipt_authority(tmp_path, monkeypatch, column, value):
    path = tmp_path / "damaged-native.sqlite"
    operation, parameters, _ = seed_created(path)
    edit_receipt(path, "graph.session.create", parameters["idempotency_key"], column=column, value=value)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_not_trusted(lambda: read_receipt(path, operation, parameters))
    assert raw_database(path) == before


@pytest.mark.parametrize("defect", ["bad-json", "target", "native-key", "native-operation", "scope"])
def test_damaged_companion_origin_is_not_trusted(tmp_path, monkeypatch, defect):
    path = tmp_path / "damaged-origin.sqlite"
    operation, parameters, _ = seed_created(path)
    key = companion_key(operation, "management", parameters["idempotency_key"])
    with closing(sqlite3.connect(path)) as connection:
        payload = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            (IDENTITY_OPERATION, key),
        ).fetchone()[0]
    if defect == "bad-json":
        payload = "{"
    else:
        value = loads_strict(payload)
        identity = value[0]["application_identity"]
        if defect == "target":
            identity["target"] = {"session_id": uid(201)}
        elif defect == "scope":
            identity["operation_scope"] = "consumer"
        else:
            field = "key" if defect == "native-key" else "operation"
            identity["native_receipt"][field] = "unrelated"
        payload = encoded(value)
    edit_receipt(path, IDENTITY_OPERATION, key, column="result_payload", value=payload)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_not_trusted(lambda: read_receipt(path, operation, parameters))
    assert raw_database(path) == before


@pytest.mark.parametrize("consumer", [False, True], ids=["management", "consumer"])
def test_scope_and_original_request_cannot_be_rebound(tmp_path, monkeypatch, consumer):
    path = tmp_path / "request-scope.sqlite"
    operation, parameters, _ = seed_created(path, consumer=consumer)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    changed = deepcopy(parameters)
    changed["definition_revision"] = 2
    assert_not_trusted(lambda: read_receipt(path, operation, changed,
                                          scope="consumer" if consumer else "management"))
    changed["workflow_definition_id"] = uid(301)
    assert_not_trusted(lambda: read_receipt(path, operation, changed,
                                          scope="consumer" if consumer else "management"))
    if not consumer:
        assert_not_trusted(lambda: read_receipt(path, operation, parameters, scope="consumer"))
    assert raw_database(path) == before


def test_consumer_origin_returns_only_original_eight_public_receipt_fields(tmp_path, monkeypatch):
    path = tmp_path / "consumer.sqlite"
    operation, parameters, origin = seed_created(path, consumer=True)
    assert "consumer" in origin["result"]
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    monkeypatch.setattr(GraphWorkflowService, "get_consumer", forbidden)
    assert_matched(read_receipt(path, operation, parameters, scope="consumer"), origin, consumer=True)
    assert raw_database(path) == before


@pytest.mark.parametrize("consumer", [False, True], ids=["management", "consumer"])
def test_original_start_request_keeps_its_cas_and_prepared_receipt_after_completion(
    tmp_path, monkeypatch, consumer,
):
    path = tmp_path / "completed-start.sqlite"
    _, _, created = seed_created(path)
    session = created["result"]
    operation = "consumer.run.start" if consumer else "run.start"
    parameters = {
        "session_id": session["workflow_session_id"], "expected_revision": session["revision"],
        "idempotency_key": "custody-start",
    }
    with closing(new_service(path)) as instance:
        origin = GraphApplication(instance, scope="consumer" if consumer else "management").command(
            operation, parameters,
        )
        chain_id = (origin["result"]["receipt"]["chain_run_id"] if consumer
                    else origin["result"]["active_chain_run_id"])
        instance.wait(chain_id)
        assert instance.get_session(parameters["session_id"])["status"] == "succeeded"
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    monkeypatch.setattr(GraphWorkflowService, "get_consumer", forbidden)
    scope = "consumer" if consumer else "management"
    assert_matched(read_receipt(path, operation, parameters, scope=scope), origin, consumer=consumer)
    changed = {**parameters, "expected_revision": parameters["expected_revision"] + 1}
    assert_not_trusted(lambda: read_receipt(path, operation, changed, scope=scope))
    assert raw_database(path) == before


def global_package():
    def register(host):
        host.register_data_type(DataTypeDefinition(
            "custody.resource", 1, {"type": "string"}, scope="global",
        ))

    return CapabilityPackage(PackageManifest("custody.global", "1"), register)


def test_direct_resource_replay_does_not_manufacture_application_origin(tmp_path, monkeypatch):
    path = tmp_path / "resource-direct.sqlite"
    parameters = {
        "record": {
            "envelope_version": 1, "scope": "workspace", "type_id": "custody.resource",
            "resource_id": uid(521), "data_schema_version": 1, "update_sequence": 1,
            "value": "direct native origin",
        },
        "expected_sequence": 0, "idempotency_key": uid(522),
    }
    with closing(new_service(
        path, capability_packages=(global_package(),),
        enabled_packages={**TOOLS, "custody.global": "1"},
    )) as instance:
        native = instance.save_global_resource(**parameters)
        before_replay = raw_database(path)
        replay = GraphApplication(instance).command("resource.save", parameters)
        assert replay["result"] == native
        assert raw_database(path) == before_replay
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, "resource.save", parameters))
    assert raw_database(path) == before


def test_resource_receipt_survives_body_replacement_and_tombstone_without_body_archive(
    tmp_path, monkeypatch,
):
    path = tmp_path / "resource.sqlite"
    record = {
        "envelope_version": 1, "scope": "workspace", "type_id": "custody.resource",
        "resource_id": uid(501), "data_schema_version": 1, "update_sequence": 1,
        "value": "original resource body must not enter receipts",
    }
    parameters = {"record": record, "expected_sequence": 0, "idempotency_key": uid(502)}
    with closing(new_service(
        path, capability_packages=(global_package(),),
        enabled_packages={**TOOLS, "custody.global": "1"},
    )) as instance:
        app = GraphApplication(instance)
        origin = app.command("resource.save", parameters)
        changed = {**record, "update_sequence": 2, "value": "replacement body"}
        app.command("resource.save", {
            "record": changed, "expected_sequence": 1, "idempotency_key": uid(503),
        })
        deleted = app.command("resource.delete", {
            "identity": origin["result"]["reference"], "expected_sequence": 2,
            "idempotency_key": uid(504),
        })
        assert deleted["result"]["deleted"] is True
    before = raw_database(path)
    assert record["value"] not in repr(before)
    assert changed["value"] not in repr(before)
    prohibit_coordinators(monkeypatch)
    assert_matched(read_receipt(path, "resource.save", parameters), origin)
    assert_not_trusted(lambda: read_receipt(path, "resource.save", parameters, scope="consumer"))
    assert raw_database(path) == before


def test_resource_native_mutation_rolls_back_when_application_origin_insert_fails(tmp_path):
    path = tmp_path / "resource-rollback.sqlite"
    with closing(new_service(
        path, capability_packages=(global_package(),),
        enabled_packages={**TOOLS, "custody.global": "1"},
    )) as instance:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute(
                "CREATE TRIGGER custody_reject_origin BEFORE INSERT ON idempotency "
                "WHEN NEW.operation='graph.application.command-identity.v1' "
                "BEGIN SELECT RAISE(ABORT,'custody origin write failed'); END",
            )
        before = raw_database(path)
        with pytest.raises(sqlite3.IntegrityError, match="custody origin write failed"):
            GraphApplication(instance).command("resource.save", {
                "record": {
                    "envelope_version": 1, "scope": "workspace", "type_id": "custody.resource",
                    "resource_id": uid(511), "data_schema_version": 1, "update_sequence": 1,
                    "value": "no partial body",
                },
                "expected_sequence": 0, "idempotency_key": uid(512),
            })
        assert raw_database(path) == before


def test_foreign_native_result_identity_is_rejected_even_with_matching_request_digest(tmp_path, monkeypatch):
    path = tmp_path / "foreign-result.sqlite"
    operation, parameters, origin = seed_created(path)
    value = deepcopy(origin["result"])
    value["workflow_definition_id"] = uid(401)
    edit_receipt(path, "graph.session.create", parameters["idempotency_key"],
                 column="result_payload", value=encoded([{"graph_result": value}]))
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_not_trusted(lambda: read_receipt(path, operation, parameters))
    assert raw_database(path) == before


def test_first_native_write_and_companion_insert_share_one_rollback(tmp_path):
    path = tmp_path / "atomic-origin.sqlite"
    with closing(new_service(path, enabled_packages=TOOLS)) as instance:
        app = GraphApplication(instance)
        app.command("definition.save", {
            "document": document(instance), "expected_revision": 0,
            "idempotency_key": "atomic-definition",
        })
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute(
                "CREATE TRIGGER custody_reject_origin BEFORE INSERT ON idempotency "
                "WHEN NEW.operation='graph.application.command-identity.v1' "
                "BEGIN SELECT RAISE(ABORT,'custody origin write failed'); END",
            )
        before = raw_database(path)
        with pytest.raises(sqlite3.IntegrityError, match="custody origin write failed"):
            app.command("session.create", {
                "workflow_definition_id": uid(1), "definition_revision": 1,
                "idempotency_key": "atomic-create",
            })
        assert raw_database(path) == before


def test_companion_identity_collision_rolls_back_a_new_native_transaction(tmp_path, monkeypatch):
    path = tmp_path / "origin-collision.sqlite"
    operation, parameters, _ = seed_created(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        cursor = connection.execute(
            "DELETE FROM idempotency WHERE operation='graph.session.create' AND key=?",
            (parameters["idempotency_key"],),
        )
        assert cursor.rowcount == 1
    with closing(new_service(path)) as instance:
        before = raw_database(path)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            GraphApplication(instance).command(operation, parameters)
        assert raw_database(path) == before
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, operation, parameters))
    assert raw_database(path) == before


def test_internal_variable_receipt_cannot_replace_missing_outer_command_evidence(tmp_path, monkeypatch):
    path = tmp_path / "inner-effect.sqlite"
    _, _, created = seed_created(path)
    session = created["result"]
    operation = "session.data.update"
    parameters = {
        "session_id": session["workflow_session_id"], "expected_revision": session["revision"],
        "expected_data_revision": session["data_revision"], "idempotency_key": "inner-data",
    }
    with closing(new_service(path)) as instance:
        GraphApplication(instance).command(operation, parameters)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_variable_receipts").fetchone()[0] >= 1
        connection.execute(
            "DELETE FROM idempotency WHERE operation='graph.session.data' AND key=?",
            (parameters["idempotency_key"],),
        )
        connection.execute(
            "DELETE FROM idempotency WHERE operation=? AND key=?",
            (IDENTITY_OPERATION, companion_key(operation, "management", parameters["idempotency_key"])),
        )
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, operation, parameters))
    assert raw_database(path) == before


def test_old_schema_without_request_digest_is_read_only_unresolved(tmp_path, monkeypatch):
    path = tmp_path / "schema-one.sqlite"
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(
            "CREATE TABLE idempotency(operation TEXT,key TEXT,digest TEXT,result_refs TEXT,"
            "result_payload TEXT,PRIMARY KEY(operation,key))",
        )
        connection.execute("PRAGMA user_version=1")
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, "session.create", {
        "workflow_definition_id": uid(1), "definition_revision": 1, "idempotency_key": "old",
    }))
    assert raw_database(path) == before


def test_missing_database_is_unresolved_and_is_never_created(tmp_path, monkeypatch):
    path = tmp_path / "does-not-exist.sqlite"
    prohibit_coordinators(monkeypatch)
    assert_unresolved(read_receipt(path, "session.create", {
        "workflow_definition_id": uid(1), "definition_revision": 1, "idempotency_key": "absent",
    }))
    assert not path.exists()
