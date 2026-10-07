"""One-time v14 removal of explicitly retired fixed/compat test data."""

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict
from .graph_records import validate_graph_record


_FIXED_FAMILIES = {
    "node_definition", "node_binding", "workflow_definition_revision", "workflow_session",
    "node_session", "visible_message", "visible_message_ref", "agent_message", "node_input", "input_snapshot",
    "run_record", "turn", "candidate_group", "candidate_selection", "node_run",
    "workflow_checkpoint", "chain_run", "chain_input_origin", "execution_closeout",
    "execution_continuation", "workflow_output", "output_delivery", "fork_anchor",
    "control_command", "run_event", "state_snapshot", "workflow_commit", "workflow_ref",
    "session_selection", "workflow_candidate", "workflow_operation",
}
_COMPAT_NODES = {
    (f"workflow.{name}", str(version)) for name, version in (
        ("text", 1), ("current-input", 1), ("output", 1), ("regex", 1),
        ("text-to-prompt", 1), ("prompt-to-text", 1), ("json-to-text", 1),
        ("prompt-item", 1), ("prompt-group", 1), ("prompt-source", 1),
        ("prompt-summary", 1), ("prompt-summary", 2), ("tool", 1), ("tool-summary", 1),
        ("global-content", 1), ("global-content", 2), ("variable-register", 1),
        ("variable-assign", 1), ("variable-replace", 1), ("session-data-read", 1),
        ("session-data-write", 1), ("object-read", 1), ("object-write", 1),
        ("object-delete", 1), ("agent", 1), ("agent", 2), ("model-provider", 1),
        ("model-provider", 2), ("context", 1), ("prompt-assembly", 1),
    )
}
_RETIRED_TABLES = (
    "execution_facts", "prompt_heads", "prompt_revisions", "prompt_mutations",
    "variable_bindings", "variable_heads", "variable_states", "variable_registries",
    "variable_preparations", "result_port_submissions", "result_port_releases",
    "model_configuration_revisions", "model_configuration_mutations",
    "exposure_configuration_revisions", "exposure_configuration_mutations",
    "workbench_session_owners", "workbench_resource_heads", "workbench_resource_revisions",
    "workbench_resource_receipts", "program_node_outputs",
)
_OWNER_FIELDS = {
    "workflow_definition_id", "workflow_session_id", "session_id", "chain_run_id", "chain_id", "node_run_id",
    "run_id", "output_id", "commit_id", "state_snapshot_id", "owner_id",
    "head_commit_id", "parent_commit_id", "base_commit_id", "active_chain_run_id",
    "selected_chain_run_id", "revision_id",
}
_OWNER_LISTS = {"history_refs", "node_run_ids", "completed_chains"}


def _parse(payload):
    try:
        return loads_strict(payload)
    except ContractValidationError:
        return None


def _compat(document):
    if type(document) is not dict:
        return False
    lock, nodes = document.get("package_lock"), document.get("nodes")
    return (type(lock) is list and any(type(item) is dict and item.get("package_id") == "workflow.compat"
                                     for item in lock)
            or type(nodes) is list and any(type(node) is dict
                and (node.get("component_id"), node.get("component_version")) in _COMPAT_NODES for node in nodes))


def _references_removed(value, identities):
    if type(value) is dict:
        return any((key in _OWNER_FIELDS and type(item) is str and item in identities)
                   or (key in _OWNER_LISTS and type(item) is list
                       and any(type(identity) is str and identity in identities for identity in item))
                   or _references_removed(item, identities) for key, item in value.items())
    if type(value) is list:
        return any(_references_removed(item, identities) for item in value)
    return False


def _retired_document(value):
    if type(value) is dict:
        return _compat(value) or any(_retired_document(item) for item in value.values())
    return type(value) is list and any(_retired_document(item) for item in value)


def _fixed(kind, value):
    return (kind in _FIXED_FAMILIES and value.get("execution_model") != "graph"
            and type(value.get("schema_version")) is int and value["schema_version"] in (1, 2, 3))


def _session_id(value):
    if type(value) is not dict:
        return None
    identity = value.get("workflow_session_id", value.get("session_id"))
    return identity if type(identity) is str else None


def remove_retired_test_data(connection):
    """Remove retired test families and all affiliated workflow data in one transaction."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = [(row["record_type"], row["record_id"], _parse(row["payload"]))
            for row in connection.execute("SELECT record_type,record_id,payload FROM records")]
    retired_revisions = {
        (value.get("workflow_definition_id"), value.get("revision"))
        for kind, _, value in rows
        if kind == "workflow_definition_revision" and type(value) is dict and _compat(value.get("document"))
    }
    sessions = {
        _session_id(value) for kind, _, value in rows
        if kind == "workflow_session" and _session_id(value) is not None
        and (_fixed(kind, value)
             or (value.get("workflow_definition_id"), value.get("definition_revision")) in retired_revisions)
    }
    removed = set(sessions)
    discarded = []
    for kind, identity, value in rows:
        if kind in {"graph_definition_acceptance", "graph_run_acceptance"}:
            discarded.append((kind, identity))
            removed.add(identity)
            continue
        if type(value) is not dict:
            continue
        retired = (
            _fixed(kind, value)
            or _session_id(value) in sessions
            or kind == "workflow_definition_revision"
            and (value.get("workflow_definition_id"), value.get("revision")) in retired_revisions
            or kind == "node_binding"
            and (value.get("workflow_definition_id"), value.get("workflow_definition_revision")) in retired_revisions
            or kind == "node_definition"
            and (value.get("component_id"), value.get("component_version")) in _COMPAT_NODES
        )
        if retired:
            discarded.append((kind, identity))
            removed.add(identity)
            removed.update(item for field, item in value.items() if field in _OWNER_FIELDS and type(item) is str)
    discarded_set = set(discarded)
    object_members = []
    for table in ("session_object_revision_membership", "session_object_bindings"):
        for row in connection.execute(
                f"SELECT m.session_id,m.revision_id,v.value FROM {table} m "
                "LEFT JOIN session_object_values v ON v.revision_id=m.revision_id"):
            object_members.append((row["session_id"], row["revision_id"], _parse(row["value"])))
    manifests = [(row["owner_kind"], row["owner_id"], _parse(row["payload"]))
                 for row in connection.execute("SELECT owner_kind,owner_id,payload FROM graph_state_manifests")]
    facts = [(row["session_id"], row["chain_id"], row["node_run_id"], _parse(row["payload"]))
             for row in connection.execute("SELECT * FROM workflow_runtime_facts")] \
        if "workflow_runtime_facts" in tables else []
    discarded_manifests = set()
    # Delete whole affiliated test workflows rather than repairing historical references.
    while True:
        before = (len(sessions), len(removed), len(discarded_set), len(discarded_manifests))
        for kind, identity, value in rows:
            if ((kind, identity) in discarded_set or _session_id(value) in sessions
                    or _references_removed(value, removed)):
                discarded_set.add((kind, identity))
                removed.add(identity)
                if type(value) is dict:
                    removed.update(item for field, item in value.items()
                                   if field in _OWNER_FIELDS and type(item) is str)
                if _session_id(value) is not None:
                    sessions.add(_session_id(value))
        for sid, revision, value in object_members:
            if sid in sessions or revision in removed or _references_removed(value, removed):
                sessions.add(sid)
                removed.update((sid, revision))
        for kind, identity, value in manifests:
            owner = identity.split(":", 1)[0]
            if owner in removed or _references_removed(value, removed):
                discarded_manifests.add((kind, identity))
                removed.add(owner)
                if _session_id(value) is not None:
                    sessions.add(_session_id(value))
                    removed.add(_session_id(value))
        for sid, chain, node_run, value in facts:
            if (sid in sessions or chain in removed or node_run in removed
                    or _references_removed(value, removed)):
                sessions.add(sid)
                removed.update((sid, chain, node_run))
        if before == (len(sessions), len(removed), len(discarded_set), len(discarded_manifests)):
            break
    connection.executemany("DELETE FROM records WHERE record_type=? AND record_id=?", discarded_set)
    for kind, identity, value in rows:
        if ((kind, identity) not in discarded_set and kind == "node_run"
                and type(value) is dict and value.get("execution_model") == "graph"
                and value.get("schema_version") == 4 and "agent" in value
                and value["agent"] is None):
            current = {key: item for key, item in value.items() if key != "agent"}
            current["schema_version"] = 5
            validate_graph_record(kind, current)
            connection.execute("UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
                               (canonical_bytes(current).decode("utf-8"), kind, identity))
    for table in _RETIRED_TABLES:
        if table in tables:
            connection.execute("DROP TABLE " + table)
    object_revisions = set()
    for table in ("session_object_revision_membership", "session_object_bindings"):
        for sid in sessions:
            object_revisions.update(row[0] for row in connection.execute(
                f"SELECT revision_id FROM {table} WHERE session_id=?", (sid,)))
    for table, column in (
        ("program_variable_states", "session_id"), ("program_variable_heads", "session_id"),
        ("program_variable_bindings", "session_id"), ("session_object_bindings", "session_id"),
        ("session_object_revision_membership", "session_id"), ("session_object_receipts", "session_id"),
        ("workflow_runtime_facts", "session_id"),
    ):
        if table in tables:
            connection.executemany(f"DELETE FROM {table} WHERE {column}=?", [(sid,) for sid in sessions])
    connection.executemany("DELETE FROM graph_state_manifests WHERE owner_kind=? AND owner_id=?", discarded_manifests)
    connection.executemany(
        "DELETE FROM session_object_values WHERE revision_id=? AND revision_id NOT IN "
        "(SELECT revision_id FROM session_object_revision_membership UNION SELECT revision_id FROM session_object_bindings)",
        [(identity,) for identity in object_revisions])
    deleted_receipts = set()
    for table, payload_column, key_columns in (
        ("idempotency", "result_payload", ("operation", "key")),
        ("program_variable_receipts", "payload", ("idempotency_key",)),
        ("global_resource_receipts", "payload", ("idempotency_key",)),
    ):
        for row in connection.execute(f"SELECT * FROM {table}").fetchall():
            value = _parse(row[payload_column])
            old_resource = (type(value) is dict and type(value.get("reference")) is dict
                            and value["reference"].get("type_id") == "workflow.global-content")
            old_operation = (table == "idempotency" and (row["operation"].startswith("workflow.")
                             or row["operation"] in {"pre_dispatch", "archive", "graph.legacy.migrate"}))
            if old_operation or old_resource or _references_removed(value, removed) or _retired_document(value):
                clause = " AND ".join(f"{column}=?" for column in key_columns)
                connection.execute(f"DELETE FROM {table} WHERE {clause}", tuple(row[column] for column in key_columns))
                deleted_receipts.add((table, row["operation"] if table == "idempotency" else None,
                                      row["key"] if table == "idempotency" else row["idempotency_key"]))
    for row in connection.execute(
        "SELECT key,result_payload FROM idempotency WHERE operation='graph.application.command-identity.v1'").fetchall():
        value = _parse(row["result_payload"])
        if type(value) is list and len(value) == 1 and type(value[0]) is dict:
            native = value[0].get("application_identity", {}).get("native_receipt", {})
            coordinate = (native.get("table"), native.get("operation") if native.get("table") == "idempotency" else None,
                          native.get("key"))
            if coordinate in deleted_receipts:
                connection.execute(
                    "DELETE FROM idempotency WHERE operation='graph.application.command-identity.v1' AND key=?",
                    (row["key"],))
    connection.execute("DELETE FROM global_resource_current WHERE type_id='workflow.global-content'")
    connection.execute("DELETE FROM registered_type_contracts WHERE scope='global' AND type_id='workflow.global-content'")
    row = connection.execute(
        "SELECT payload FROM graph_project_packages WHERE configuration_id='current-execution'").fetchone()
    if row is None:
        historical = connection.execute(
            "SELECT payload FROM graph_project_packages WHERE configuration_id='project'").fetchone()
        selected = _parse(historical["payload"]) if historical else None
        if (type(selected) is dict
                and all(type(key) is str and type(version) is str for key, version in selected.items())):
            selected.pop("workflow.compat", None)
            connection.execute("INSERT INTO graph_project_packages VALUES('current-execution',?)",
                               (canonical_bytes(selected).decode("utf-8"),))
    else:
        selected = _parse(row["payload"])
        if type(selected) is dict and "workflow.compat" in selected:
            selected.pop("workflow.compat")
            connection.execute("UPDATE graph_project_packages SET payload=? WHERE configuration_id='current-execution'",
                               (canonical_bytes(selected).decode("utf-8"),))
    connection.execute("DELETE FROM graph_project_packages WHERE configuration_id='project'")
