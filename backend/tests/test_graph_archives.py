"""Scoped archive reads never initialize stores, migrate or recover executions."""

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_archives import read_graph_archive
from phase1_agent.graph_records import graph_record
from phase1_agent.storage import _record_id
from test_graph_agent_contracts import accepted_package, archive_bundle, uid
from test_graph_application_receipt_custody import forbidden, prohibit_coordinators, raw_database
from test_legacy_archive_contracts import FAMILIES, archive_records


def encoded(value):
    return canonical_bytes(value).decode("utf-8")


def seed_database(path, bundle, *, version=13):
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(
            "CREATE TABLE records(record_type TEXT,record_id TEXT,payload TEXT,"
            "immutable INTEGER,created_at TEXT,PRIMARY KEY(record_type,record_id))",
        )
        connection.executemany(
            "INSERT INTO records VALUES(?,?,?,1,'original timestamp')",
            [(kind, _record_id(kind, row), encoded(row))
             for kind, rows in bundle.items() for row in rows],
        )
        connection.execute(f"PRAGMA user_version={version}")


def legacy_bundle(family="basic", *, fallback=False):
    records = archive_records(family)
    bundle = archive_bundle(accepted_package())
    for kind in ("node_run", "chain_run", "workflow_output"):
        bundle[kind] = []
    bundle["workflow_session"][0]["source"] = {
        "kind": "legacy_migration",
        "source_session_id": records["input_snapshot"]["workflow_session_id"],
        "source_revision": 1,
        "legacy_archives": [{
            "source_node_id": records["input_snapshot"]["node_binding_id"],
            "target_node_id": uid(3), "turn_ids": [records["turn"]["turn_id"]],
        }],
    }
    for kind, row in records.items():
        bundle["node_run" if kind == "run_record" and fallback else kind] = [row]
    return bundle, records


def native_archive(bundle):
    package = bundle["node_run"][0]["agent"]["accepted"]
    return {
        "turn": package["turn"], "snapshot": package["snapshot"],
        "root": [package["snapshot"]["config"]["payload"]["graph_preparation"]["current_root"]],
    }


def assert_denied(path, archive_id, code, *, status=500, session_id=None):
    with pytest.raises(ContractValidationError) as rejected:
        read_graph_archive(path, session_id or uid(4), archive_id)
    assert rejected.value.reason_code == code
    assert rejected.value.status_code == status


def edit_record(path, kind, row, *, raw=None):
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        cursor = connection.execute(
            "UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
            (encoded(row) if raw is None else raw, kind, _record_id(kind, row)),
        )
        assert cursor.rowcount == 1


@pytest.mark.parametrize("record_version", [3, 4])
@pytest.mark.parametrize("storage_version", [0, 1, 6, 13])
def test_native_archive_retains_exact_records_without_initialization(
    tmp_path, monkeypatch, record_version, storage_version,
):
    bundle = archive_bundle(accepted_package(), version=record_version)
    path = tmp_path / "native.sqlite"
    seed_database(path, bundle, version=storage_version)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    original = native_archive(bundle)
    result = read_graph_archive(path, uid(4), original["turn"]["turn_id"])
    assert result == original
    result["turn"]["messages"].clear()
    result["root"][0]["blocks"][0]["text"] = "local copy"
    assert read_graph_archive(path, uid(4), original["turn"]["turn_id"]) == original
    assert raw_database(path) == before


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("fallback", [False, True])
def test_migrated_archive_uses_original_frozen_context_and_success_owner(
    tmp_path, monkeypatch, family, fallback,
):
    from phase1_agent.legacy_archive import read_closed_legacy_archive
    from test_legacy_archive_contracts import Records

    bundle, records = legacy_bundle(family, fallback=fallback)
    path = tmp_path / "legacy.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    expected = read_closed_legacy_archive(Records(records, fallback=fallback), records["turn"]["turn_id"])
    assert read_graph_archive(path, uid(4), records["turn"]["turn_id"]) == expected
    assert raw_database(path) == before


def copied_session(bundle):
    for kind, row in (
        ("workflow_session", graph_record(
            "workflow_session", workflow_session_id=uid(44), workflow_definition_id=uid(2),
            definition_revision=1, revision=1, active_chain_run_id=None,
            source={"kind": "copy_current", "workflow_session_id": uid(4),
                    "head_commit_id": uid(30),
                    "legacy_archives": deepcopy(bundle["workflow_session"][0]["source"].get("legacy_archives", []))})),
        ("state_snapshot", graph_record(
            "state_snapshot", state_snapshot_id=uid(314), workflow_session_id=uid(44),
            workflow_definition_id=uid(2), definition_revision=1,
            node_states={}, data_revision=0, history_refs=[uid(8)] if bundle["chain_run"] else [])),
        ("workflow_commit", graph_record(
            "workflow_commit", commit_id=uid(304), workflow_session_id=uid(44),
            state_snapshot_id=uid(314), parent_commit_id=None, source={"kind": "session_seed"})),
        ("workflow_ref", graph_record(
            "workflow_ref", workflow_ref_id=uid(324), workflow_session_id=uid(44),
            head_commit_id=uid(304), revision=1)),
    ):
        bundle[kind].append(row)
    return uid(44)


def remap(value, identities):
    if type(value) is dict:
        return {key: remap(item, identities) for key, item in value.items()}
    if type(value) is list:
        return [remap(item, identities) for item in value]
    return identities.get(value, value) if type(value) is str else value


def test_copy_reads_frozen_native_history_but_not_later_source_runs(tmp_path, monkeypatch):
    bundle = archive_bundle(accepted_package())
    sid = copied_session(bundle)
    original = native_archive(bundle)
    later = remap(archive_bundle(accepted_package()), {
        uid(7): uid(1007), uid(8): uid(1008), uid(30): uid(1030), uid(31): uid(1031),
        original["turn"]["turn_id"]: uid(1100),
    })
    for kind in ("node_run", "chain_run", "workflow_commit", "state_snapshot"):
        bundle[kind].extend(later[kind])
    path = tmp_path / "copied.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert read_graph_archive(path, sid, original["turn"]["turn_id"]) == original
    assert_denied(path, uid(1100), "graph_history_scope_mismatch", status=403, session_id=sid)
    assert raw_database(path) == before


def test_copied_legacy_refs_do_not_grant_later_or_foreign_turns(tmp_path, monkeypatch):
    bundle, records = legacy_bundle()
    sid = copied_session(bundle)
    later = deepcopy(records["turn"])
    later["turn_id"] = uid(1100)
    bundle["turn"].append(later)
    path = tmp_path / "legacy-copy.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert read_graph_archive(path, sid, records["turn"]["turn_id"])["turn"] == records["turn"]
    assert_denied(path, later["turn_id"], "graph_history_scope_mismatch", status=403, session_id=sid)
    assert raw_database(path) == before


@pytest.mark.parametrize("defect", [
    "head-owner", "snapshot-owner", "baseline-owner", "baseline-snapshot-owner",
    "chain-owner", "chain-node", "chain-run", "missing-head", "missing-chain",
    "unsettled-history", "duplicate-head", "run-owner", "accepted-result", "bad-json",
    "duplicate-json-key", "row-identity", "missing-baseline", "invalid-head-id",
])
def test_damaged_native_ownership_and_evidence_are_rejected_without_repair(
    tmp_path, monkeypatch, defect,
):
    bundle = archive_bundle(accepted_package())
    sid = copied_session(bundle)
    archive_id = native_archive(bundle)["turn"]["turn_id"]
    if defect in ("head-owner", "snapshot-owner", "baseline-owner", "baseline-snapshot-owner"):
        kind, index = {
            "head-owner": ("workflow_commit", 1), "snapshot-owner": ("state_snapshot", 1),
            "baseline-owner": ("workflow_commit", 0), "baseline-snapshot-owner": ("state_snapshot", 0),
        }[defect]
        bundle[kind][index]["workflow_session_id"] = uid(999)
    elif defect in ("chain-owner", "chain-node", "chain-run"):
        field, value = {
            "chain-owner": ("workflow_session_id", uid(999)),
            "chain-node": ("ordered_nodes", [uid(999)]),
            "chain-run": ("node_run_ids", [uid(999)]),
        }[defect]
        bundle["chain_run"][0][field] = value
        if defect == "chain-node":
            bundle["chain_run"][0]["completed_nodes"] = value
            bundle["chain_run"][0]["targets"] = value
        if defect == "chain-run":
            bundle["chain_run"][0]["node_run_attempts"] = [value]
    elif defect == "missing-head":
        bundle["workflow_ref"].pop()
    elif defect == "duplicate-head":
        another = deepcopy(bundle["workflow_ref"][1])
        another["workflow_ref_id"] = uid(999)
        bundle["workflow_ref"].append(another)
    elif defect == "missing-chain":
        bundle["chain_run"].clear()
    elif defect == "unsettled-history":
        bundle["chain_run"][0]["status"] = "paused"
    elif defect == "missing-baseline":
        bundle["workflow_commit"].pop(0)
    elif defect == "invalid-head-id":
        bundle["workflow_ref"][1]["head_commit_id"] = "not-a-uuid"
    elif defect == "run-owner":
        bundle["node_run"][0]["workflow_session_id"] = uid(999)
    elif defect == "accepted-result":
        bundle["node_run"][0]["agent"]["accepted"]["turn"]["final"]["value"]["text"] = "forged"
    path = tmp_path / "damaged.sqlite"
    seed_database(path, bundle)
    if defect in ("bad-json", "duplicate-json-key", "row-identity"):
        row = bundle["node_run"][0]
        raw = {
            "bad-json": "{",
            "duplicate-json-key": '{"run_id":"a","run_id":"b"}',
            "row-identity": encoded({**row, "run_id": uid(999)}),
        }[defect]
        edit_record(path, "node_run", row, raw=raw)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_denied(path, archive_id, "storage_contract_violation", session_id=sid)
    assert raw_database(path) == before


@pytest.mark.parametrize("defect", [
    "not-list", "not-object", "missing-turns", "extra-field", "invalid-node",
    "invalid-turn", "duplicate-turn", "ancestry",
])
def test_malformed_frozen_legacy_refs_never_become_scope_authority(tmp_path, monkeypatch, defect):
    bundle, records = legacy_bundle()
    source = bundle["workflow_session"][0]["source"]
    reference = source["legacy_archives"][0]
    if defect == "not-list":
        source["legacy_archives"] = {}
    elif defect == "not-object":
        source["legacy_archives"] = [True]
    elif defect == "missing-turns":
        del reference["turn_ids"]
    elif defect == "extra-field":
        reference["extra"] = True
    elif defect == "invalid-node":
        reference["source_node_id"] = "unknown"
    elif defect == "invalid-turn":
        reference["turn_ids"] = [True]
    elif defect == "duplicate-turn":
        reference["turn_ids"] *= 2
    else:
        reference["turn_ids"].append(uid(999))
    path = tmp_path / "legacy-invalid.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_denied(path, records["turn"]["turn_id"], "storage_contract_violation")
    assert raw_database(path) == before


@pytest.mark.parametrize("defect", ["foreign-node", "unclosed-owner", "missing-owner", "broken-owner"])
def test_legacy_archive_rejects_wrong_node_or_unclosed_owner(tmp_path, monkeypatch, defect):
    bundle, records = legacy_bundle()
    if defect == "foreign-node":
        bundle["workflow_session"][0]["source"]["legacy_archives"][0]["source_node_id"] = uid(999)
    elif defect == "unclosed-owner":
        bundle["run_record"][0]["status"] = "paused"
    elif defect == "missing-owner":
        bundle["run_record"].clear()
    else:
        del bundle["run_record"][0]["snapshot_id"]
    path = tmp_path / "legacy-owner.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    code, status = {
        "foreign-node": ("graph_history_scope_mismatch", 403),
        "unclosed-owner": ("graph_legacy_history_invalid", 409),
        "missing-owner": ("graph_legacy_history_invalid", 409),
        "broken-owner": ("storage_contract_violation", 500),
    }[defect]
    assert_denied(path, records["turn"]["turn_id"], code, status=status)
    assert raw_database(path) == before


@pytest.mark.parametrize("version", [-1, 14, 99])
def test_unsupported_schema_is_not_migrated(tmp_path, monkeypatch, version):
    path = tmp_path / "future.sqlite"
    seed_database(path, archive_bundle(accepted_package()), version=version)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_denied(path, uid(999), "graph_archive_schema_unsupported", status=409)
    assert raw_database(path) == before


def test_missing_or_incomplete_database_is_not_created_or_initialized(tmp_path, monkeypatch):
    prohibit_coordinators(monkeypatch)
    path = tmp_path / "missing.sqlite"
    assert_denied(path, uid(999), "not_found", status=404)
    assert not path.exists()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE records(record_id TEXT,payload TEXT)")
    before = raw_database(path)
    assert_denied(path, uid(999), "graph_archive_storage_unavailable", status=503)
    assert raw_database(path) == before


@pytest.mark.parametrize("sid,turn_id", [(True, uid(1)), (uid(4), "bad"), (uid(4), uid(1000).upper())])
def test_invalid_request_never_opens_database(tmp_path, monkeypatch, sid, turn_id):
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(ContractValidationError) as rejected:
        read_graph_archive(tmp_path / "missing.sqlite", sid, turn_id)
    assert rejected.value.reason_code == "invalid_request"


def test_valid_but_unknown_session_or_archive_does_not_gain_scope(tmp_path, monkeypatch):
    path = tmp_path / "unknown.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(path, bundle)
    before = raw_database(path)
    prohibit_coordinators(monkeypatch)
    assert_denied(path, uid(999), "not_found", status=404, session_id=uid(999))
    assert_denied(path, uid(999), "graph_history_scope_mismatch", status=403)
    assert raw_database(path) == before


def test_connection_is_read_only_even_inside_archive_reader(tmp_path, monkeypatch):
    from phase1_agent.graph_archives import _ReadOnlyArchiveRecords

    path = tmp_path / "readonly.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(path, bundle)
    before = raw_database(path)
    original = _ReadOnlyArchiveRecords.__init__

    def verify(self, connection):
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE records SET payload='changed'")
        original(self, connection)

    monkeypatch.setattr(_ReadOnlyArchiveRecords, "__init__", verify)
    assert read_graph_archive(path, uid(4), native_archive(bundle)["turn"]["turn_id"]) == native_archive(bundle)
    assert raw_database(path) == before


def test_all_reads_share_one_snapshot_when_a_writer_changes_head(tmp_path, monkeypatch):
    from phase1_agent.graph_archives import _ReadOnlyArchiveRecords

    path = tmp_path / "snapshot.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(path, bundle)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original = _ReadOnlyArchiveRecords.get
    changed = False

    def concurrent(self, kind, **identity):
        nonlocal changed
        result = original(self, kind, **identity)
        if kind == "workflow_session" and not changed:
            changed = True
            head = deepcopy(bundle["workflow_ref"][0])
            head["head_commit_id"] = uid(999)
            edit_record(path, "workflow_ref", head)
        return result

    monkeypatch.setattr(_ReadOnlyArchiveRecords, "get", concurrent)
    expected = native_archive(bundle)
    assert read_graph_archive(path, uid(4), expected["turn"]["turn_id"]) == expected
    assert changed
    assert_denied(path, expected["turn"]["turn_id"], "storage_contract_violation")


def test_direct_service_archive_query_does_not_construct_a_store(tmp_path, monkeypatch):
    from phase1_agent.graph_service import GraphWorkflowService

    path = tmp_path / "direct.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(path, bundle, version=0)
    with closing(GraphWorkflowService(path, model_factory=forbidden)) as service:
        before = raw_database(path)
        prohibit_coordinators(monkeypatch)
        monkeypatch.setattr(service, "_store", forbidden)
        expected = native_archive(bundle)
        assert service.get_agent_archive(uid(4), expected["turn"]["turn_id"]) == expected
        assert service._native_runtime is None
        assert raw_database(path) == before


@pytest.mark.parametrize("family", ["native", *FAMILIES])
def test_fresh_process_http_reads_with_old_execution_and_storage_imports_blocked(tmp_path, family):
    bundle = archive_bundle(accepted_package()) if family == "native" else legacy_bundle(family)[0]
    archive_id = (native_archive(bundle)["turn"]["turn_id"] if family == "native"
                  else bundle["turn"][0]["turn_id"])
    path = tmp_path / "fresh.sqlite"
    seed_database(path, bundle)
    before = raw_database(path)
    program = r'''
import importlib.abc
import http.client
import json
import sys
from threading import Thread

blocked = {
    "phase1_agent.workflow", "phase1_agent.graph_service", "phase1_agent.graph_agent_host",
    "phase1_agent.graph_agent_runtime", "phase1_agent.prepared_context",
    "phase1_agent.bindings", "phase1_agent.kernel", "phase1_agent.runtime",
    "phase1_agent.storage", "phase1_agent.workbench_resources",
    "phase1_agent.workflow_context_view",
}
class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("Archive HTTP imported execution or storage: " + fullname)
sys.meta_path.insert(0, NoExecutionImports())
from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost

host = WorkflowHost(sys.argv[1])
server = create_server(host, port=0)
worker = Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/graph/sessions/" + sys.argv[2] + "/archives/" + sys.argv[3])
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200, result
        assert result["turn"]["turn_id"] == sys.argv[3]
        assert set(result) == {"turn", "root", "snapshot"}
    finally:
        connection.close()
    assert host._graph is None and host._legacy is None
    assert not blocked.intersection(sys.modules)
finally:
    server.shutdown()
    worker.join(5)
    server.server_close()
    host.close()
assert not worker.is_alive()
print("archive HTTP read without execution or store imports")
'''
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), uid(4), archive_id],
        text=True, capture_output=True, env=environment, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "archive HTTP read without execution or store imports"
    assert raw_database(path) == before
