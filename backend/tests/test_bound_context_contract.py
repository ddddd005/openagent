"""Bound ownership and official frozen producer proofs."""

from copy import deepcopy

import pytest

from phase1_agent.context_v2 import (
    read_bound_context, validate_bound_context_adoption_proof, window_bound_context,
)
from phase1_agent.graph_contracts import GraphDiagnosticError

from test_context_package import record, ref, uid


@pytest.mark.parametrize("mutation,reason", [
    (lambda records: records[uid(61)].update(component_id="custom.forged-view"),
     "context_adoption_source_invalid"),
    (lambda records: records[uid(60)]["config"].update(agent_node_id=uid(99)),
     "context_agent_binding_mismatch"),
    (lambda records: records[uid(60)]["config"].update(object_key="foreign"),
     "context_agent_binding_mismatch"),
])
def test_bound_adoption_rejects_unofficial_or_rebound_producer(mutation, reason):
    state = record()
    state["schema_version"] = 2
    view = read_bound_context(state, uid(1), "context", uid(3))
    window = window_bound_context(view, ref(60), last_units=0)
    records = {
        uid(60): {"value": view, "component_id": "context.output", "component_version": "1",
                  "config": {"object_key": "context", "agent_node_id": uid(3)}},
        uid(61): {"value": window, "component_id": "context.window", "component_version": "2",
                  "config": {"last_units": 0}},
    }
    resolve = lambda reference: deepcopy(records[reference["output_id"]])
    assert validate_bound_context_adoption_proof(ref(61), resolve) == window
    mutation(records)
    with pytest.raises(GraphDiagnosticError) as rejected:
        validate_bound_context_adoption_proof(ref(61), resolve)
    assert rejected.value.reason_code == reason
