"""Read closed legacy turn records without constructing an execution service."""

from .contract_graph import validate_turn_final
from .contracts_v2 import validate_record
from .graph_records import require
from .legacy_context_contracts import (
    BASIC_CONTEXT_COMPONENT, BASIC_CONTEXT_VERSION,
    PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
    project_basic_turn, project_prepared_turn,
)


def read_closed_legacy_archive(reader, turn_id):
    """The caller authorizes scope; this reader verifies the closed archive."""
    turn = validate_record("turn", reader.get_record("turn", {"turn_id": turn_id}))
    snapshot = validate_record(
        "input_snapshot", reader.get_record("input_snapshot", {"snapshot_id": turn["snapshot_id"]}),
    )
    node_input = validate_record(
        "node_input", reader.get_record("node_input", {"input_id": turn["input_id"]}),
    )
    run = reader.get_record("run_record", {"run_id": turn["run_id"]})
    if run is None:
        run = reader.get_record("node_run", {"run_id": turn["run_id"]})
    require(run is not None and run["status"] == "succeeded" and run["result_turn_id"] == turn_id
            and run["snapshot_id"] == snapshot["snapshot_id"]
            and run["workflow_session_id"] == snapshot["workflow_session_id"]
            and run["node_binding_id"] == snapshot["node_binding_id"],
            "graph_legacy_history_invalid", "Legacy archive has no closed owner", 409)
    descriptor = snapshot["config"]["payload"]["resolved"]["context"]
    key = descriptor["component_id"], descriptor["component_version"]
    require(key in (
        (BASIC_CONTEXT_COMPONENT, BASIC_CONTEXT_VERSION),
        (PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION),
    ), "graph_legacy_context_unsupported", "Legacy context implementation needs an explicit adapter", 409)
    project = project_basic_turn if key == (BASIC_CONTEXT_COMPONENT, BASIC_CONTEXT_VERSION) else project_prepared_turn
    projected = project(turn, node_input, snapshot)
    validate_turn_final(turn, snapshot)
    require(projected[1:] == turn["messages"], "graph_legacy_history_invalid", "Legacy projection changed the closed delta")
    return {"turn": turn, "root": projected[:1], "snapshot": snapshot}
