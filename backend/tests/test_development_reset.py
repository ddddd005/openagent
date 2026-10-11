from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from phase1_agent.storage import SqliteStore


spec = importlib.util.spec_from_file_location(
    "development_reset_script", Path(__file__).parents[1] / "scripts" / "reset_development_data.py")
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)


def database(workspace):
    path = workspace / ".local" / "develop" / "workflow.sqlite"
    path.parent.mkdir(parents=True)
    with closing(SqliteStore(path)) as store:
        store._connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            ("workflow_session", "old-test", "old product data", 0, "2026-10-10"))
        store._connection.execute(
            "INSERT INTO idempotency VALUES(?,?,?,?,?,?)",
            ("graph.run.start", "old-unknown", "digest", "[]", "[{}]", "digest"))
    return path


def seed_resource(connection, type_id, identity, value):
    connection.execute("INSERT INTO global_resource_current VALUES(?,?,?,?,?,?)",
                       ("workspace", type_id, identity, 1, 0, json.dumps(value)))


def evidence_rows(report, table):
    with closing(sqlite3.connect(report["evidence"])) as saved:
        return [json.loads(row[0]) for row in saved.execute(
            "SELECT row_json FROM evidence_rows WHERE source_table=?", (table,))]


def test_reset_clears_all_product_data_preserves_evidence_vault_and_new_content(tmp_path):
    path = database(tmp_path)
    vault = Path(str(path) + ".credentials.sqlite")
    vault.write_bytes(b"private vault must remain untouched")
    with sqlite3.connect(path) as connection:
        for type_id in ("workflow.chat-provider", "workflow.model-profile",
                        "workflow.prompt-resource", "tavern.lorebook"):
            seed_resource(connection, type_id, type_id, {"data_schema_version": 3})
        connection.execute("INSERT INTO graph_project_packages VALUES('current-execution','{}')")
        connection.execute("INSERT INTO registered_type_contracts VALUES('global','type',1,'digest','{}')")
    result = reset.reset_development_database(tmp_path)
    assert result["private_vault_touched"] is False
    assert result["before"]["records"] == 1
    assert result["before"]["global_resource_current"] == 4
    assert all(value == 0 for value in result["after"].values())
    assert result["cleanup_failures"] == []
    assert vault.read_bytes() == b"private vault must remain untouched"
    assert evidence_rows(result, "records") == []
    assert evidence_rows(result, "idempotency")[0]["key"] == "old-unknown"
    with closing(SqliteStore(path)) as store:
        store._connection.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                                  ("workflow_session", "new-test", "new batch", 0, "2026-10-10"))
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT record_id FROM records").fetchone()[0] == "new-test"


def test_reset_refuses_stable_external_and_vault_targets(tmp_path):
    path = database(tmp_path)
    for target in (tmp_path / ".local" / "dev" / "workflow.sqlite", tmp_path / "other.sqlite",
                   Path(str(path) + ".credentials.sqlite")):
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
        with pytest.raises(ValueError, match="Only this workspace"):
            reset.reset_development_database(tmp_path, target)
        with sqlite3.connect(target) as untouched:
            assert untouched.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_reset_refuses_live_owner_before_creating_evidence(tmp_path):
    path = database(tmp_path)
    with reset.database_lease(path), pytest.raises(RuntimeError, match="live owner"):
        reset.reset_development_database(tmp_path)
    assert not (tmp_path / ".local" / "request-evidence").exists()
    with sqlite3.connect(path) as untouched:
        assert untouched.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_all_provider_formats_and_unrelated_old_content_are_deleted_not_migrated(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as connection:
        for version in (1, 2, 3):
            seed_resource(connection, "workflow.chat-provider", str(version),
                          {"data_schema_version": version})
        seed_resource(connection, "workflow.model-profile", "profile", {"provider": "3"})
        seed_resource(connection, "workflow.prompt-resource", "prompt", {"text": "old"})
    result = reset.reset_development_database(tmp_path)
    assert result["before"]["global_resource_current"] == 5
    assert result["after"]["global_resource_current"] == 0
    assert evidence_rows(result, "global_resource_current") == []


def seed_draft(connection, kind, key, value):
    address = {"scope": "management", "kind": kind, "key": key,
               "workflow_definition_id": None, "workflow_session_id": None}
    connection.execute("INSERT INTO local_editor_drafts VALUES(?,?,?,?,?,?,?)",
                       (json.dumps(address), "management", kind, None, None, 1,
                        json.dumps({"address": address, "sequence": 1, "value": value})))


def test_pending_requests_and_uncertain_results_are_separate_from_old_drafts(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as connection:
        seed_draft(connection, "intent", "resource", {"request": {"idempotency_key": "unknown"}})
        seed_draft(connection, "form", "ordinary", {"text": "old editor content"})
        seed_draft(connection, "form", "archive:resource-request:old", {"raw": "original request"})
        seed_draft(connection, "workbench", "workbench", {"catalog": ["old catalog"],
            "graph": {"entries": {"workflow": {"document": "old document",
                                             "pending": {"body": {"idempotency_key": "graph-unknown"}}}}}})
        for kind, identity, value in (
            ("node_run", "uncertain", {"run_id": "uncertain", "chain_run_id": "chain", "status": "running"}),
            ("workflow_output", "unconfirmed-result", {"run_id": "uncertain", "chain_run_id": "chain"}),
            ("workflow_output", "ordinary-result", {"run_id": "old", "chain_run_id": "old"}),
        ):
            connection.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                               (kind, identity, json.dumps(value), 0, "2026-10-10"))
    result = reset.reset_development_database(tmp_path)
    draft_evidence = evidence_rows(result, "local_editor_drafts")
    assert len(draft_evidence) == 3
    assert draft_evidence[-1]["pending"]["body"]["idempotency_key"] == "graph-unknown"
    assert "old document" not in json.dumps(draft_evidence)
    assert "old editor content" not in json.dumps(draft_evidence)
    assert {row["record_id"] for row in evidence_rows(result, "records")} == {
        "uncertain", "unconfirmed-result"}
    assert result["after"]["local_editor_drafts"] == 0


def test_selected_product_archive_is_removed_after_evidence_while_stable_archive_stays(tmp_path):
    path = database(tmp_path)
    archives = tmp_path / ".local" / "archives"
    selected = archives / "development-reset-old" / "original.sqlite"
    stable = archives / "main-before-change" / "stable-workflow.sqlite"
    for target in (selected, stable):
        target.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    result = reset.reset_development_database(tmp_path, product_archives=[selected])
    assert not selected.exists()
    assert stable.exists()
    assert result["removed_product_archives"] == [str(selected.resolve())]
    assert len(evidence_rows(result, "idempotency")) == 2
    assert evidence_rows(result, "records") == []


def test_reset_refuses_archive_outside_explicit_development_reset_scope(tmp_path):
    path = database(tmp_path)
    target = tmp_path / ".local" / "archives" / "stable" / "original.sqlite"
    target.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    with pytest.raises(ValueError, match="explicitly named"):
        reset.reset_development_database(tmp_path, product_archives=[target])
    assert target.exists()


def test_archive_delete_failure_is_reported_not_claimed_as_success(tmp_path, monkeypatch):
    path = database(tmp_path)
    archive = tmp_path / ".local" / "archives" / "development-reset-old" / "original.sqlite"
    archive.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(archive)) as destination:
        source.backup(destination)
    original_unlink = Path.unlink

    def deny_selected(target, *args, **kwargs):
        if target == archive:
            raise PermissionError("archive deletion denied")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_selected)
    result = reset.reset_development_database(tmp_path, product_archives=[archive])
    assert result["removed_product_archives"] == []
    assert result["cleanup_failures"] == [{"path": str(archive.resolve()), "error_type": "PermissionError"}]
    assert archive.exists()
    assert all(value == 0 for value in result["after"].values())


def test_inspect_reports_counts_without_reset_or_evidence(tmp_path):
    path = database(tmp_path)
    result = reset.inspect_development_database(tmp_path)
    assert result["inspection_only"] is True
    assert result["sources"][0]["counts"]["records"] == 1
    assert not (tmp_path / ".local" / "request-evidence").exists()
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_unclassified_table_stops_reset_without_deleting_it(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_data (value TEXT)")
        connection.execute("INSERT INTO unrelated_data VALUES('keep')")
    with pytest.raises(ValueError, match="Unclassified tables"):
        reset.reset_development_database(tmp_path)
    assert not (tmp_path / ".local" / "request-evidence").exists()
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT value FROM unrelated_data").fetchone()[0] == "keep"
