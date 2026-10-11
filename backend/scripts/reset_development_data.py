"""Explicit offline reset of selected development data, excluding the private vault."""

from argparse import ArgumentParser
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


@contextmanager
def database_lease(database):
    lock_path = Path(str(database) + ".graph.lock")
    with lock_path.open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Development database has a live owner; stop its service first") from None
        yield


_PRODUCT_TABLES = frozenset({
    "records", "program_variable_states", "program_variable_heads", "program_variable_receipts",
    "program_variable_bindings", "session_object_bindings", "session_object_values",
    "session_object_revision_membership", "session_object_receipts", "graph_state_manifests",
    "workflow_runtime_facts", "local_editor_drafts",
    "idempotency", "global_resource_current", "global_resource_receipts",
    "registered_type_contracts", "graph_project_packages", "provider_key_requests",
    "workflow_information_bindings",
    "workflow_management",
    "workflow_session_metadata", "content_resource_metadata", "pending_commands",
    "execution_facts", "prompt_heads", "prompt_revisions", "prompt_mutations",
    "variable_bindings", "variable_heads", "variable_states", "variable_registries",
    "variable_preparations", "result_port_submissions", "result_port_releases",
    "model_configuration_revisions", "model_configuration_mutations",
    "exposure_configuration_revisions", "exposure_configuration_mutations",
    "workbench_session_owners", "workbench_resource_heads", "workbench_resource_revisions",
    "workbench_resource_receipts", "program_node_outputs",
})
_RECEIPT_TABLES = frozenset({
    "idempotency", "program_variable_receipts", "session_object_receipts",
    "global_resource_receipts", "provider_key_requests", "workbench_resource_receipts",
    "model_configuration_mutations", "exposure_configuration_mutations",
})
_UNCERTAIN_STATUSES = {"running", "archive_failed", "recovery_unavailable"}


def selected_paths(workspace, database=None, product_archives=()):
    workspace = Path(workspace).resolve(strict=True)
    directory = (workspace / ".local" / "develop").resolve()
    selected = Path(database or directory / "workflow.sqlite").resolve(strict=True)
    if selected.parent != directory or selected.name != "workflow.sqlite":
        raise ValueError("Only this workspace's .local/develop/workflow.sqlite may be reset")
    if not directory.is_relative_to(workspace) or selected == (
            workspace / ".local" / "dev" / "workflow.sqlite").resolve():
        raise ValueError("Development reset target must not escape the workspace")
    archives = (workspace / ".local" / "archives").resolve()
    evidence_root = (workspace / ".local" / "request-evidence").resolve()
    if not archives.is_relative_to(workspace) or not evidence_root.is_relative_to(workspace):
        raise ValueError("Archive and evidence directories must remain inside the workspace")
    selected_archives = []
    for path in product_archives:
        path = Path(path).resolve(strict=True)
        if (path.parent.parent != archives or path.name != "original.sqlite"
                or not path.parent.name.startswith("development-reset-")):
            raise ValueError("Only explicitly named development-reset original.sqlite archives may be removed")
        if path not in selected_archives:
            selected_archives.append(path)
    return selected, selected_archives, evidence_root


def _tables(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if not {"records", "idempotency", "global_resource_current"} <= tables:
        raise ValueError("Target is not a recognized development database")
    unknown = tables - _PRODUCT_TABLES
    if unknown:
        raise ValueError("Unclassified tables; reset not performed: " + ", ".join(sorted(unknown)))
    return tables


def _counts(connection, tables):
    return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(tables)}


def inspect_development_database(workspace, database=None, product_archives=()):
    selected, archives, _ = selected_paths(workspace, database, product_archives)
    with ExitStack() as stack:
        for path in (selected, *archives):
            stack.enter_context(database_lease(path))
        sources = []
        for path in (selected, *archives):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                sources.append({"database": str(path), "counts": _counts(connection, _tables(connection))})
    return {"database": str(selected), "product_archives": [str(path) for path in archives],
            "sources": sources, "private_vault_touched": False, "inspection_only": True}


def _draft_evidence(row):
    record = json.loads(row["payload"])
    value = record.get("value")
    if not isinstance(value, dict):
        return []
    address = record.get("address", {})
    if (value.get("request") is not None or value.get("pending") is not None
            or str(address.get("key", "")).startswith("archive:resource-request:")):
        return [dict(row)]
    graph = value.get("graph")
    entries = graph.get("entries", {}) if isinstance(graph, dict) else {}
    if not isinstance(entries, dict):
        return []
    return [{"address": row["address"], "sequence": row["sequence"],
             "workflow_definition_id": identity, "pending": entry["pending"]}
            for identity, entry in entries.items()
            if isinstance(entry, dict) and entry.get("pending") is not None]


def _preserve_evidence(source, connection, tables, destination):
    counts = {}

    def save(table, row):
        destination.execute("INSERT INTO evidence_rows VALUES(?,?,?)",
                            (str(source), table, json.dumps(row, ensure_ascii=True, sort_keys=True)))
        counts[table] = counts.get(table, 0) + 1

    for table in sorted(tables & _RECEIPT_TABLES):
        for row in connection.execute(f'SELECT * FROM "{table}"'):
            save(table, dict(row))
    for table in ("workflow_runtime_facts", "execution_facts"):
        if table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                save(table, dict(row))
    uncertain_runs, uncertain_chains = set(), set()
    for row in connection.execute(
            "SELECT * FROM records WHERE record_type IN ('node_run','chain_run')"):
        try:
            value = json.loads(row["payload"])
        except (ValueError, TypeError):
            save("records", dict(row))
            continue
        if not isinstance(value, dict) or value.get("status") in _UNCERTAIN_STATUSES:
            save("records", dict(row))
            if isinstance(value, dict):
                uncertain_runs.add(value.get("run_id"))
                uncertain_chains.add(value.get("chain_run_id"))
    # Keep results associated with uncertain runs, not ordinary historical content.
    for row in connection.execute("SELECT * FROM records WHERE record_type='workflow_output'"):
        value = json.loads(row["payload"])
        if value.get("run_id") in uncertain_runs or value.get("chain_run_id") in uncertain_chains:
            save("records", dict(row))
    if "local_editor_drafts" in tables:
        for row in connection.execute("SELECT * FROM local_editor_drafts"):
            for evidence in _draft_evidence(row):
                save("local_editor_drafts", evidence)
    if "pending_commands" in tables:
        for row in connection.execute("SELECT * FROM pending_commands"):
            save("pending_commands", dict(row))
    return counts


def reset_development_database(workspace, database=None, product_archives=()):
    selected, archives, evidence_root = selected_paths(workspace, database, product_archives)
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = evidence_root / ("development-reset-" + batch)
    evidence_path = directory / "requests.sqlite"
    with ExitStack() as stack:
        # Acquire all selected leases before creating evidence or changing product data.
        for path in (selected, *archives):
            stack.enter_context(database_lease(path))
        connection = stack.enter_context(closing(sqlite3.connect(selected)))
        connection.row_factory = sqlite3.Row
        tables = _tables(connection)
        before = _counts(connection, tables)
        directory.mkdir(parents=True, exist_ok=False)
        evidence_counts = {}
        with closing(sqlite3.connect(evidence_path)) as evidence, evidence:
            evidence.execute("CREATE TABLE evidence_rows ("
                             "source_database TEXT NOT NULL, source_table TEXT NOT NULL, row_json TEXT NOT NULL)")
            evidence_counts[str(selected)] = _preserve_evidence(selected, connection, tables, evidence)
            for path in archives:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original:
                    original.row_factory = sqlite3.Row
                    evidence_counts[str(path)] = _preserve_evidence(path, original, _tables(original), evidence)
        with closing(sqlite3.connect(evidence_path.as_uri() + "?mode=ro", uri=True)) as evidence:
            if evidence.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Request evidence failed verification; reset not performed")
            if evidence.execute("SELECT COUNT(*) FROM evidence_rows").fetchone()[0] != sum(
                    sum(counts.values()) for counts in evidence_counts.values()):
                raise RuntimeError("Request evidence count differs; reset not performed")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in sorted(tables):
            connection.execute(f'DELETE FROM "{table}"')
        after = _counts(connection, tables)
        if any(after.values()):
            raise RuntimeError("Development reset verification failed")
        connection.commit()
        connection.execute("VACUUM")
        removed_archives, failures = [], []
        for path in archives:
            try:
                path.unlink()
                removed_archives.append(str(path))
            except OSError as exc:
                failures.append({"path": str(path), "error_type": type(exc).__name__})
    digest = hashlib.sha256()
    with evidence_path.open("rb") as saved:
        for block in iter(lambda: saved.read(1 << 20), b""):
            digest.update(block)
    report = {"schema": "openagent.development-reset@2", "batch": batch,
              "database": str(selected), "evidence": str(evidence_path),
              "evidence_sha256": digest.hexdigest(), "evidence_counts": evidence_counts,
              "before": before, "after": after, "removed_product_archives": removed_archives,
              "cleanup_failures": failures, "private_vault_touched": False}
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--product-archive", type=Path, action="append", default=[])
    parser.add_argument("--inspect", action="store_true", help="Verify selected leases and report counts only")
    arguments = parser.parse_args()
    action = inspect_development_database if arguments.inspect else reset_development_database
    report = action(arguments.workspace, arguments.database, arguments.product_archive)
    print(json.dumps(report, indent=2))
    if report.get("cleanup_failures"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
