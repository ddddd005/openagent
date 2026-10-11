"""Schema setup does not replace the explicitly authorized development reset."""

from contextlib import closing
import sqlite3

from phase1_agent.storage import SqliteStore, STORAGE_VERSION


def test_fresh_storage_contains_current_tables_without_legacy_write_routes(tmp_path):
    with closing(SqliteStore(tmp_path / "fresh.sqlite")) as store:
        names = {row[0] for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"records", "idempotency", "session_object_bindings",
                "global_resource_current", "workflow_management", "local_editor_drafts"} <= names
        assert not names & {"prompt_heads", "variable_heads", "execution_facts", "workbench_resource_heads"}
        assert not hasattr(store, "save_pre_dispatch") and not hasattr(store, "archive_success")
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION


def test_initialization_does_not_scan_delete_or_upgrade_old_product_bytes(tmp_path):
    path = tmp_path / "raw-retained.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                           ("unknown.extension", "unchanged", '{"schema_version":1,"legacy":true}', 1, "original"))
        connection.execute("CREATE TABLE workbench_resource_heads(resource_id TEXT)")
        connection.execute("INSERT INTO workbench_resource_heads VALUES('explicit-reset-only')")
        connection.execute("PRAGMA user_version=13")
        before = connection.execute("SELECT * FROM records").fetchall()
    with closing(SqliteStore(path)) as store:
        assert [tuple(row) for row in store._connection.execute("SELECT * FROM records")] == before
        assert [tuple(row) for row in store._connection.execute(
            "SELECT * FROM workbench_resource_heads")] == [("explicit-reset-only",)]
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION
