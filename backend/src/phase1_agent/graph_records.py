"""Versioned graph records in the existing public-record storage namespace."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value


GRAPH_VERSIONS = {
    "node_definition": 2, "node_binding": 2, "workflow_definition_revision": 2,
    "workflow_session": 2, "node_session": 2, "node_run": 4,
    "chain_run": 5, "workflow_output": 2, "state_snapshot": 2,
    "workflow_commit": 2, "workflow_ref": 2,
}
_FIELDS = {
    "node_definition": {"component_id", "component_version", "descriptor"},
    "node_binding": {"workflow_definition_id", "workflow_definition_revision",
                     "node_binding_id", "component_id", "component_version", "config"},
    "workflow_definition_revision": {"workflow_definition_id", "revision", "bindings",
                                     "edges", "document"},
    "workflow_session": {"workflow_session_id", "workflow_definition_id", "definition_revision",
                         "revision", "source", "active_chain_run_id"},
    "node_session": {"workflow_session_id", "node_binding_id", "data_version", "private_data"},
    "node_run": {"profile", "run_id", "workflow_session_id", "node_binding_id", "chain_run_id",
                 "status", "revision", "input_refs", "input_values", "output_refs", "reads", "effects", "config", "diagnostic"},
    "chain_run": {"chain_run_id", "workflow_session_id", "workflow_definition_id",
                  "definition_revision", "status", "revision", "targets", "ordered_nodes",
                  "node_run_ids", "completed_nodes", "next_node_index", "inputs", "outputs",
                  "diagnostic"},
    "workflow_output": {"output_id", "workflow_session_id", "chain_run_id", "run_id",
                        "node_binding_id", "port_id", "payload"},
    "state_snapshot": {"state_snapshot_id", "workflow_session_id", "workflow_definition_id",
                       "definition_revision", "node_states", "data_revision", "history_refs"},
    "workflow_commit": {"commit_id", "workflow_session_id", "state_snapshot_id",
                        "parent_commit_id", "source"},
    "workflow_ref": {"workflow_ref_id", "workflow_session_id", "head_commit_id", "revision"},
}
_STATUSES = {"prepared", "running", "paused", "budget_exhausted", "archive_failed", "failed", "succeeded", "recovery_unavailable", "closed"}


def is_graph_record(record_type: str, value: Any) -> bool:
    return isinstance(value, dict) and value.get("execution_model") == "graph"


def graph_error(code: str, message: str, status: int = 400, **diagnostic: Any):
    error = ContractValidationError(message)
    error.reason_code = code
    error.status_code = status
    error.diagnostics = [{"reason_code": code, "message": message, **diagnostic}]
    return error


def require(condition: bool, code: str, message: str, status: int = 400, **diagnostic: Any):
    if not condition:
        raise graph_error(code, message, status, **diagnostic)


def uuid_value(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value, version=4)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def graph_record(record_type: str, **fields: Any) -> dict:
    if record_type == "node_run":
        fields.setdefault("agent", None)
        fields.setdefault("input_storage", "inline")
        fields.setdefault("control_refs", [])
    if record_type == "chain_run":
        fields.setdefault("execution_kind", "round")
        fields.setdefault("event", None)
        fields.setdefault("base_commit_id", None)
        fields.setdefault("node_run_attempts", [[identity] for identity in fields["node_run_ids"]])
    return {"schema_version": GRAPH_VERSIONS[record_type], "execution_model": "graph", **fields}


def validate_graph_record(record_type: str, value: Any) -> dict:
    validate_json_value(value)
    require(record_type in _FIELDS and type(value) is dict, "storage_contract_violation", "Unknown graph record", 500)
    required = _FIELDS[record_type] | {"schema_version", "execution_model"}
    if record_type == "node_run" and value.get("schema_version") in (3, 4):
        required = required | {"agent"}
    if record_type == "node_run" and value.get("schema_version") == 4:
        required |= {"input_storage", "control_refs"}
    if record_type == "chain_run" and value.get("schema_version") in (4, 5):
        required |= {"execution_kind", "event", "base_commit_id"}
    if record_type == "chain_run" and value.get("schema_version") == 5:
        required |= {"node_run_attempts"}
    require(set(value) in (required, required | {"created_at"})
            and type(value["schema_version"]) is int
            and (value["schema_version"] == GRAPH_VERSIONS[record_type]
                 or record_type == "node_run" and value["schema_version"] in (2, 3)
                 or record_type == "chain_run" and value["schema_version"] in (3, 4))
            and value["execution_model"] == "graph", "storage_contract_violation", "Graph record fields differ", 500)
    for key in ("workflow_definition_id", "workflow_session_id", "node_binding_id", "run_id",
                "chain_run_id", "output_id", "state_snapshot_id", "commit_id", "workflow_ref_id"):
        if key in value:
            require(uuid_value(value[key]), "storage_contract_violation", "Invalid graph identity", 500)
    for key in ("revision", "definition_revision", "workflow_definition_revision", "data_version"):
        if key in value:
            require(type(value[key]) is int and value[key] >= 1, "storage_contract_violation", "Invalid graph revision", 500)
    for key in ("data_revision", "next_node_index"):
        if key in value:
            require(type(value[key]) is int and value[key] >= 0, "storage_contract_violation", "Invalid graph state index", 500)
    if "status" in value:
        require(type(value["status"]) is str and value["status"] in _STATUSES, "storage_contract_violation", "Unknown graph status", 500)
    for key in ("config", "source", "descriptor", "private_data", "node_states", "inputs", "outputs", "input_values"):
        if key in value:
            require(type(value[key]) is dict, "storage_contract_violation", "Invalid graph object field", 500)
    for key in ("bindings", "edges", "targets", "ordered_nodes", "node_run_ids", "completed_nodes", "history_refs", "reads", "effects"):
        if key in value:
            require(type(value[key]) is list, "storage_contract_violation", "Invalid graph array field", 500)
    for key in ("targets", "ordered_nodes", "node_run_ids", "completed_nodes", "history_refs"):
        if key in value:
            require(all(uuid_value(item) for item in value[key]) and len(set(value[key])) == len(value[key]),
                    "storage_contract_violation", "Invalid graph identity list", 500)
    for key in ("active_chain_run_id", "parent_commit_id"):
        if key in value:
            require(value[key] is None or uuid_value(value[key]), "storage_contract_violation", "Invalid nullable graph identity", 500)
    if record_type == "node_session":
        require(set(value["private_data"]) == {"owner_component_id", "schema_version", "payload"}
                and value["private_data"]["schema_version"] == 1,
                "storage_contract_violation", "Invalid private state envelope", 500)
    if record_type == "node_run":
        if value["schema_version"] == 4:
            require(value["input_storage"] in ("inline", "references")
                    and (value["input_storage"] != "references" or value["input_values"] == {})
                    and type(value["control_refs"]) is list
                    and all(type(ref) is dict and set(ref) == {"edge_id", "run_id"}
                            and uuid_value(ref["edge_id"]) and uuid_value(ref["run_id"])
                            for ref in value["control_refs"]),
                    "storage_contract_violation", "Invalid node input or control storage", 500)
        require(value["profile"] == "node" and type(value["input_refs"]) is dict
                and type(value["output_refs"]) is dict, "storage_contract_violation", "Invalid generic run", 500)
        require(all(uuid_value(identity) for identity in value["output_refs"].values()),
                "storage_contract_violation", "Invalid output reference", 500)
        for references in value["input_refs"].values():
            require(type(references) is list and all(type(ref) is dict and set(ref) == {"edge_id", "output_id", "order"}
                    and uuid_value(ref["edge_id"]) and uuid_value(ref["output_id"])
                    and type(ref["order"]) is int and ref["order"] >= 0 for ref in references),
                    "storage_contract_violation", "Invalid named input reference", 500)
        diagnostic = value["diagnostic"]
        if type(diagnostic) is dict and "unresolved_inputs" in diagnostic:
            unresolved = diagnostic["unresolved_inputs"]
            require(value["status"] in ("failed", "closed")
                    and diagnostic.get("code") == "graph_linked_output_missing"
                    and diagnostic.get("node_id") == value["node_binding_id"]
                    and type(unresolved) is list and bool(unresolved)
                    and all(type(item) is dict
                            and set(item) == {"edge_id", "source_node_id", "source_port_id", "target_port_id", "order"}
                            and uuid_value(item["edge_id"]) and uuid_value(item["source_node_id"])
                            and type(item["source_port_id"]) is str and bool(item["source_port_id"])
                            and type(item["target_port_id"]) is str and bool(item["target_port_id"])
                            and type(item["order"]) is int and item["order"] >= 0 for item in unresolved),
                    "storage_contract_violation", "Invalid unresolved input evidence", 500)
            require(len({item["edge_id"] for item in unresolved}) == len(unresolved)
                    and any(item["edge_id"] == diagnostic.get("edge_id")
                            and item["target_port_id"] == diagnostic.get("port_id") for item in unresolved),
                    "storage_contract_violation", "Missing input diagnostic differs from unresolved sources", 500)
        if value.get("agent") is not None:
            agent = value["agent"]
            require(type(agent) is dict and set(agent) == {"snapshot", "facts", "progress", "limits", "accepted"},
                    "storage_contract_violation", "Agent evidence fields differ", 500)
            from .contracts_v2 import validate_record
            snapshot = validate_record("input_snapshot", agent["snapshot"])
            require(snapshot["workflow_session_id"] == value["workflow_session_id"]
                    and snapshot["node_binding_id"] == value["node_binding_id"],
                    "storage_contract_violation", "Agent snapshot owner differs", 500)
            from .graph_agent_runtime import GraphAgentIdentity, _validated_facts, _validate_limits
            identity = GraphAgentIdentity(**snapshot["config"]["payload"]["graph_identity"])
            require((identity.workflow_session_id, identity.node_binding_id, identity.chain_run_id, identity.node_run_id)
                    == (value["workflow_session_id"], value["node_binding_id"], value["chain_run_id"], value["run_id"]),
                    "storage_contract_violation", "Agent fact identity differs", 500)
            _validated_facts(agent["facts"], snapshot, identity)
            _validate_limits(agent["limits"])
            require(value["input_values"].get("prompt", {}).get("assembly") == snapshot["config"]["payload"]["graph_preparation"]
                    and snapshot["config"]["payload"]["graph_agent"] == value["config"],
                    "storage_contract_violation", "Agent snapshot differs from its graph inputs", 500)
            if agent["accepted"] is not None:
                from .graph_agent_runtime import validate_graph_agent_accepted
                accepted = validate_graph_agent_accepted(agent["accepted"])
                require(accepted["snapshot"] == snapshot and accepted["facts"] == agent["facts"]
                        and accepted["identity"]["node_run_id"] == value["run_id"]
                        and accepted["identity"]["chain_run_id"] == value["chain_run_id"],
                        "storage_contract_violation", "Agent accepted evidence differs", 500)
    if record_type == "workflow_definition_revision":
        from .graph_contracts import validate_graph_document
        doc = validate_graph_document(value["document"])
        require(doc["workflow_definition_id"] == value["workflow_definition_id"]
                and doc["revision"] == value["revision"]
                and value["bindings"] == [n["node_binding_id"] for n in doc["nodes"]]
                and value["edges"] == doc["edges"], "storage_contract_violation", "Definition projection differs", 500)
    if record_type == "chain_run":
        if value["schema_version"] == 5:
            attempts = value["node_run_attempts"]
            require(type(attempts) is list and len(attempts) == len(value["node_run_ids"])
                    and all(type(items) is list and items and items[-1] == identity
                            and all(uuid_value(item) for item in items)
                            for items, identity in zip(attempts, value["node_run_ids"]))
                    and len({item for items in attempts for item in items})
                        == sum(len(items) for items in attempts),
                    "storage_contract_violation", "Invalid execution attempt history", 500)
            permission = (value["diagnostic"] or {}).get("failure_retry") if type(value["diagnostic"]) is dict else None
            if permission is not None:
                require(type(permission) is dict and set(permission) == {
                    "allowed", "reason_code", "node_run_id", "classification"}
                    and type(permission["allowed"]) is bool
                    and type(permission["reason_code"]) is str and 0 < len(permission["reason_code"]) <= 128
                    and (permission["classification"] is None
                         or type(permission["classification"]) is str and 0 < len(permission["classification"]) <= 128)
                    and (permission["node_run_id"] is None or uuid_value(permission["node_run_id"]))
                    and (not permission["allowed"] or permission["classification"] is not None
                         and value["next_node_index"] < len(value["node_run_ids"])
                         and permission["node_run_id"] == value["node_run_ids"][value["next_node_index"]]),
                    "storage_contract_violation", "Invalid failed retry permission", 500)
        if value["schema_version"] in (4, 5):
            from .host_sdk import bounded_name
            event = value["event"]
            require(value["base_commit_id"] is None or uuid_value(value["base_commit_id"]),
                    "storage_contract_violation", "Execution baseline commit is invalid", 500)
            require(type(value["execution_kind"]) is str and value["execution_kind"] in ("round", "event"),
                    "storage_contract_violation", "Unknown execution kind", 500)
            if value["execution_kind"] == "round":
                require(event is None, "storage_contract_violation", "Ordinary execution cannot carry event identity", 500)
            else:
                require(uuid_value(value["base_commit_id"]), "storage_contract_violation",
                        "Event requires its frozen starting Head", 500)
                require(type(event) is dict and set(event) == {
                    "event_id", "schema_version", "audience", "idempotency_key",
                } and bounded_name(event["event_id"]) and type(event["schema_version"]) is int
                    and 1 <= event["schema_version"] <= 2**53 - 1
                    and type(event["audience"]) is str and event["audience"] in ("management", "consumer")
                    and type(event["idempotency_key"]) is str and 1 <= len(event["idempotency_key"]) <= 128
                    and bool(event["idempotency_key"].strip()),
                    "storage_contract_violation", "Event execution identity is invalid", 500)
        require(len(set(value["ordered_nodes"])) == len(value["ordered_nodes"])
                and len(value["node_run_ids"]) == len(value["ordered_nodes"])
                and value["next_node_index"] <= len(value["ordered_nodes"])
                and set(value["targets"]) <= set(value["ordered_nodes"])
                and value["completed_nodes"] == value["ordered_nodes"][:value["next_node_index"]],
                "storage_contract_violation", "Invalid execution plan progress", 500)
    return deepcopy(value)


def validate_graph_bundle(bundle: dict[str, list[dict]]) -> None:
    """Validate new references without relaxing the legacy Agent history rules."""
    rows = {kind: [validate_graph_record(kind, row) for row in values]
            for kind, values in bundle.items() if values}
    sessions = {row["workflow_session_id"]: row for row in rows.get("workflow_session", [])}
    definitions = {(row["workflow_definition_id"], row["revision"]): row
                   for row in rows.get("workflow_definition_revision", [])}
    bindings = {(row["workflow_definition_id"], row["workflow_definition_revision"], row["node_binding_id"]): row
                for row in rows.get("node_binding", [])}
    chains = {row["chain_run_id"]: row for row in rows.get("chain_run", [])}
    runs = {row["run_id"]: row for row in rows.get("node_run", [])}
    outputs = {row["output_id"]: row for row in rows.get("workflow_output", [])}
    snapshots = {row["state_snapshot_id"]: row for row in rows.get("state_snapshot", [])}
    commits = {row["commit_id"]: row for row in rows.get("workflow_commit", [])}
    for session in sessions.values():
        key = session["workflow_definition_id"], session["definition_revision"]
        require(key in definitions, "storage_contract_violation", "Session definition is missing", 500)
        active = session["active_chain_run_id"]
        require(active is None or active in chains
                and chains[active]["workflow_session_id"] == session["workflow_session_id"]
                and (chains[active]["workflow_definition_id"], chains[active]["definition_revision"]) == key,
                "storage_contract_violation", "Active execution owner differs", 500)
    for definition in definitions.values():
        for node in definition["document"]["nodes"]:
            binding = bindings.get((definition["workflow_definition_id"], definition["revision"], node["node_binding_id"]))
            require(binding is not None and all(binding[key] == node[key] for key in
                    ("node_binding_id", "component_id", "component_version", "config")),
                    "storage_contract_violation", "Node binding projection differs", 500)
    for kind in ("node_session", "node_run", "state_snapshot", "workflow_ref", "workflow_commit", "workflow_output", "chain_run"):
        for row in rows.get(kind, []):
            sid = row["workflow_session_id"]
            require(sid in sessions, "storage_contract_violation", "Graph record has no session", 500)
            session = sessions[sid]
            if kind == "node_session":
                binding = bindings.get((session["workflow_definition_id"], session["definition_revision"], row["node_binding_id"]))
                require(binding is not None and row["private_data"]["owner_component_id"] == binding["component_id"],
                        "storage_contract_violation", "Private state has no binding", 500)
            if kind == "node_run":
                chain = chains.get(row["chain_run_id"])
                attempts = chain.get("node_run_attempts", [[identity] for identity in chain["node_run_ids"]]) if chain else []
                require(chain is not None and chain["workflow_session_id"] == sid
                        and any(row["run_id"] in items for items in attempts),
                        "storage_contract_violation", "Node execution owner differs", 500)
                require(row["node_binding_id"] in chain["ordered_nodes"]
                        and row["run_id"] in attempts[chain["ordered_nodes"].index(row["node_binding_id"])],
                        "storage_contract_violation", "Node execution plan identity differs", 500)
                if row["run_id"] != chain["node_run_ids"][chain["ordered_nodes"].index(row["node_binding_id"])]:
                    require(row["status"] == "failed" and not row["output_refs"],
                            "storage_contract_violation", "Only rejected attempts may precede the current run", 500)
                binding = bindings.get((chain["workflow_definition_id"], chain["definition_revision"], row["node_binding_id"]))
                require(binding is not None and binding["config"] == row["config"],
                        "storage_contract_violation", "Node run configuration differs from its frozen definition", 500)
                if row.get("agent"):
                    base = row["agent"]["snapshot"]["config"]["payload"]["graph_identity"]["base_commit_id"]
                    require(base in commits and commits[base]["workflow_session_id"] == sid,
                            "storage_contract_violation", "Agent baseline commit has another owner", 500)
                document = definitions[(chain["workflow_definition_id"], chain["definition_revision"])]["document"]
                edges = {edge["edge_id"]: edge for edge in document["edges"]}
                if row["schema_version"] == 4:
                    controls = {edge["edge_id"]: edge for edge in document.get("control_edges", [])
                                if edge["target_node_id"] == row["node_binding_id"]}
                    refs = row["control_refs"]
                    require(len({ref["edge_id"] for ref in refs}) == len(refs)
                            and set(ref["edge_id"] for ref in refs) <= set(controls),
                            "storage_contract_violation", "Control evidence does not match this definition", 500)
                    for ref in refs:
                        predecessor = runs.get(ref["run_id"])
                        require(predecessor is not None and predecessor["status"] == "succeeded"
                                and predecessor["chain_run_id"] == chain["chain_run_id"]
                                and predecessor["node_binding_id"] == controls[ref["edge_id"]]["source_node_id"]
                                and predecessor["node_binding_id"] in chain["ordered_nodes"]
                                and chain["ordered_nodes"].index(predecessor["node_binding_id"])
                                    < chain["ordered_nodes"].index(row["node_binding_id"]),
                                "storage_contract_violation", "Control predecessor was not accepted", 500)
                    require(row["status"] not in ("running", "succeeded", "paused", "budget_exhausted", "archive_failed")
                            or len(refs) == len(controls),
                            "storage_contract_violation", "Node ran without all control dependencies", 500)
                for port, refs in row["input_refs"].items():
                    for ref in refs:
                        upstream = outputs.get(ref["output_id"])
                        edge = edges.get(ref["edge_id"])
                        require(upstream is not None and upstream["chain_run_id"] == chain["chain_run_id"]
                                and edge is not None and edge["source_node_id"] == upstream["node_binding_id"]
                                and edge["source_port_id"] == upstream["port_id"]
                                and edge["target_node_id"] == row["node_binding_id"] and edge["target_port_id"] == port
                                and edge["order"] == ref["order"]
                                and upstream["node_binding_id"] in chain["ordered_nodes"]
                                and chain["ordered_nodes"].index(upstream["node_binding_id"])
                                    < chain["ordered_nodes"].index(row["node_binding_id"]),
                                "storage_contract_violation", "Input reference escaped this execution", 500)
                if row["schema_version"] == 4:
                    resolved = [ref["edge_id"] for refs in row["input_refs"].values() for ref in refs]
                    require(len(resolved) == len(set(resolved)),
                            "storage_contract_violation", "Input binding evidence repeats an edge", 500)
                    if row["status"] in ("running", "succeeded", "paused", "budget_exhausted", "archive_failed"):
                        incoming = {edge["edge_id"] for edge in document["edges"]
                                    if edge["target_node_id"] == row["node_binding_id"]}
                        require(set(resolved) == incoming, "storage_contract_violation",
                                "Executed node is missing exact input bindings", 500)
                diagnostic = row["diagnostic"]
                if type(diagnostic) is dict and "unresolved_inputs" in diagnostic:
                    resolved_ids = [ref["edge_id"] for refs in row["input_refs"].values() for ref in refs]
                    unresolved_ids = []
                    for evidence in diagnostic["unresolved_inputs"]:
                        edge = edges.get(evidence["edge_id"])
                        require(edge is not None and edge["target_node_id"] == row["node_binding_id"]
                                and all(evidence[key] == edge[key] for key in
                                        ("source_node_id", "source_port_id", "target_port_id", "order"))
                                and evidence["target_port_id"] not in row["input_values"]
                                and evidence["source_node_id"] in chain["completed_nodes"]
                                and evidence["source_node_id"] in chain["outputs"]
                                and evidence["source_port_id"] not in chain["outputs"][evidence["source_node_id"]],
                                "storage_contract_violation", "Unresolved input escaped this execution", 500)
                        source_id = chain["node_run_ids"][chain["ordered_nodes"].index(evidence["source_node_id"])]
                        require(source_id in runs and runs[source_id]["status"] == "succeeded",
                                "storage_contract_violation", "Unresolved input source was not successful", 500)
                        unresolved_ids.append(evidence["edge_id"])
                    incoming = {edge["edge_id"] for edge in document["edges"]
                                if edge["target_node_id"] == row["node_binding_id"]}
                    evidence_ids = resolved_ids + unresolved_ids
                    require(len(evidence_ids) == len(set(evidence_ids)) and set(evidence_ids) == incoming,
                            "storage_contract_violation", "Failed input evidence is incomplete", 500)
                for output_id in row["output_refs"].values():
                    require(output_id in outputs and outputs[output_id]["run_id"] == row["run_id"],
                            "storage_contract_violation", "Node output owner differs", 500)
            if kind == "workflow_output":
                run = runs.get(row["run_id"])
                require(run is not None and run["status"] == "succeeded" and run["chain_run_id"] == row["chain_run_id"]
                        and run["workflow_session_id"] == sid and run["node_binding_id"] == row["node_binding_id"]
                        and run["output_refs"].get(row["port_id"]) == row["output_id"],
                        "storage_contract_violation", "Output release has no successful source", 500)
            if kind == "workflow_ref":
                require(row["head_commit_id"] in commits and commits[row["head_commit_id"]]["workflow_session_id"] == sid,
                        "storage_contract_violation", "Head owner differs", 500)
            if kind == "workflow_commit":
                require(row["state_snapshot_id"] in snapshots and snapshots[row["state_snapshot_id"]]["workflow_session_id"] == sid,
                        "storage_contract_violation", "Commit snapshot owner differs", 500)
                parent = row["parent_commit_id"]
                require(parent is None or parent in commits and commits[parent]["workflow_session_id"] == sid,
                        "storage_contract_violation", "Commit parent owner differs", 500)
                if row["source"].get("kind") in ("completed_execution", "closed_execution"):
                    source_chain = chains.get(row["source"].get("chain_run_id"))
                    require(source_chain is not None and source_chain.get("execution_kind", "round") == "round",
                            "storage_contract_violation", "Events cannot create completion checkpoints", 500)
            if kind == "state_snapshot":
                require((row["workflow_definition_id"], row["definition_revision"]) in definitions
                        and all(identity in chains and chains[identity]["status"] in ("succeeded", "closed")
                                for identity in row["history_refs"]),
                        "storage_contract_violation", "Snapshot refers to unsettled history", 500)
            if kind == "chain_run":
                if row.get("base_commit_id") is not None:
                    baseline = commits.get(row["base_commit_id"])
                    require(baseline is not None and baseline["workflow_session_id"] == sid,
                            "storage_contract_violation", "Execution baseline has another session owner", 500)
                definition = definitions.get((row["workflow_definition_id"], row["definition_revision"]))
                require(definition is not None and set(row["ordered_nodes"]) <= set(definition["bindings"])
                        and all(identity in runs for identity in row["node_run_ids"]),
                        "storage_contract_violation", "Execution definition or runs are missing", 500)
                require(all(identity in runs for items in row.get("node_run_attempts", []) for identity in items),
                        "storage_contract_violation", "Execution attempt history has a missing run", 500)
                require(set(row["outputs"]) == set(row["completed_nodes"])
                        and all(row["outputs"][node] == runs[identity]["output_refs"]
                                for node, identity in zip(row["ordered_nodes"], row["node_run_ids"])
                                if node in row["completed_nodes"]),
                        "storage_contract_violation", "Execution output projection differs", 500)
                if row.get("execution_kind", "round") == "event":
                    event = row["event"]
                    binding = next((item for item in definition["document"].get("event_bindings", [])
                                    if (item["event_id"], item["schema_version"])
                                       == (event["event_id"], event["schema_version"])), None)
                    require(binding is not None and binding["audience"] == event["audience"]
                            and set(binding["target_node_ids"]) == set(row["targets"]),
                            "storage_contract_violation", "Event differs from its frozen declaration", 500)
                    incoming = {}
                    for edge in [*definition["document"]["edges"],
                                 *definition["document"].get("control_edges", [])]:
                        incoming.setdefault(edge["target_node_id"], []).append(edge["source_node_id"])
                    closure, pending = set(), list(row["targets"])
                    while pending:
                        node_id = pending.pop()
                        if node_id not in closure:
                            closure.add(node_id)
                            pending.extend(incoming.get(node_id, []))
                    require(closure == set(row["ordered_nodes"]),
                            "storage_contract_violation", "Event plan escaped its declared dependency closure", 500)
            if kind == "chain_run" and row["status"] == "succeeded":
                require(row["next_node_index"] == len(row["ordered_nodes"])
                        and all(identity in runs and runs[identity]["status"] == "succeeded" for identity in row["node_run_ids"]),
                        "storage_contract_violation", "Completed execution contains unfinished nodes", 500)


def validate_graph_transition(kind: str, previous: dict | None, value: dict) -> None:
    if previous is None or canonical_bytes(previous) == canonical_bytes(value):
        return
    require(is_graph_record(kind, previous), "storage_contract_violation", "Record version cannot be overwritten", 500)
    mutable = {
        "workflow_session": {"revision", "active_chain_run_id", "workflow_definition_id", "definition_revision"},
        "node_session": {"data_version", "private_data"},
        "node_run": {"status", "revision", "input_refs", "input_values", "output_refs", "reads", "effects", "diagnostic",
                     "agent", "control_refs"},
        "chain_run": {"status", "revision", "completed_nodes", "next_node_index", "outputs", "diagnostic"},
        "workflow_ref": {"head_commit_id", "revision"},
    }.get(kind)
    retry_transition = (kind == "chain_run" and previous["schema_version"] == 5
                        and previous["status"] == "failed" and value["status"] == "prepared")
    if retry_transition:
        mutable = mutable | {"node_run_ids", "node_run_attempts"}
        index = previous["next_node_index"]
        before, after = previous["node_run_attempts"], value["node_run_attempts"]
        evidence = (previous.get("diagnostic") or {}).get("failure_retry") or {}
        require(index < len(before) and evidence.get("allowed") is True
                and evidence.get("node_run_id") == previous["node_run_ids"][index]
                and len(after[index]) == len(before[index]) + 1
                and after[index][:-1] == before[index]
                and all(after[item] == before[item] for item in range(len(before)) if item != index)
                and all(value["node_run_ids"][item] == previous["node_run_ids"][item]
                        for item in range(len(before)) if item != index)
                and value["outputs"] == previous["outputs"]
                and value["completed_nodes"] == previous["completed_nodes"]
                and value["next_node_index"] == index,
                "storage_contract_violation", "Failed continuation must append one evidenced fresh attempt", 500)
    require(mutable is not None and all(value.get(key) == item for key, item in previous.items() if key not in mutable),
            "storage_contract_violation", "Immutable graph facts changed", 500)
    revision_key = "data_version" if kind == "node_session" else "revision"
    require(value[revision_key] == previous[revision_key] + 1,
            "storage_contract_violation", "Graph revision must advance once", 500)
    if kind in ("chain_run", "node_run"):
        transitions = {"prepared": {"running", "paused", "failed", "recovery_unavailable", "closed"},
                       "running": {"running", "paused", "budget_exhausted", "archive_failed", "failed", "succeeded", "recovery_unavailable"},
                       "paused": {"paused", "prepared", "running", "closed", "recovery_unavailable"},
                       "budget_exhausted": {"budget_exhausted", "prepared", "running", "closed", "recovery_unavailable"},
                       "archive_failed": {"prepared", "running", "closed", "recovery_unavailable"},
                       "failed": {"closed"}, "recovery_unavailable": {"closed"}}
        require(retry_transition or value["status"] in transitions.get(previous["status"], set()),
                "storage_contract_violation", "Invalid graph lifecycle transition", 500)
        if kind == "chain_run":
            require(value["completed_nodes"][:len(previous["completed_nodes"])] == previous["completed_nodes"],
                    "storage_contract_violation", "Execution progress cannot rewind", 500)
        if kind == "node_run" and previous.get("agent") is not None:
            old, new = previous["agent"], value.get("agent")
            require(new is not None and new["snapshot"] == old["snapshot"]
                    and new["facts"][:len(old["facts"])] == old["facts"]
                    and (old["accepted"] is None or new["accepted"] == old["accepted"]),
                    "storage_contract_violation", "Frozen Agent evidence changed", 500)
