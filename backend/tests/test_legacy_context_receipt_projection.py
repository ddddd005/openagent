"""Frozen v4 projection proof stays read-only and separate from active v5 nodes."""

from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.agent_receipt_contract import AGENT_EXECUTOR_REF, projection_id
from phase1_agent.content_contracts import text_content
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE, project_agent_context_value
from phase1_agent.context_prompt_v6 import (
    _assemble, _current_message, validate_native_context_prompt_legacy_v4,
)
from phase1_agent.context_update import validate_context_update
from phase1_agent.context_v4 import (
    build_context_update_view, merge_native_context, read_native_context,
)
from phase1_agent.graph_contracts import GraphDiagnosticError
from phase1_agent.graph_receipts import _context_adoption_projection
from phase1_agent.host_sdk import ObjectBinding

from test_context_update import tool_pair


def _reference():
    return {"scope": "artifact", "output_id": str(uuid4())}


def _frozen_projection():
    sid, workflow, agent, reader, writer, chain = (str(uuid4()) for _ in range(6))
    binding = ObjectBinding(
        "legacy-context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
        readers=(reader, writer), writers=(writer,),
    ).to_dict()
    old_record = {
        "type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4, "deleted": False,
        "revision": 1, "revision_id": str(uuid4()), "binding": binding,
        "value": {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0},
    }
    view_ref, prompt_ref, model_ref, update_ref, merged_ref, current_ref = (
        _reference() for _ in range(6)
    )
    loaded = read_native_context(old_record, sid, "legacy-context", agent)
    current = _current_message(text_content("original historical input"), current_ref)
    messages, provenance, layout = _assemble([], loaded, current)
    prompt = validate_native_context_prompt_legacy_v4({
        "schema_version": 6, "kind": "workflow.prompt", "stage": "assembled", "items": [],
        "context": loaded, "context_ref": view_ref, "current_input": current,
        "current_input_ref": current_ref, "source_output_refs": [],
        "messages": messages, "provenance": provenance, "layout": layout,
    })
    owner = {
        "workflow_session_id": sid, "chain_run_id": chain,
        "node_binding_id": agent, "node_run_id": str(uuid4()),
    }
    receipts = {
        "schema_version": 1, "kind": "workflow.agent-receipts", "owner": owner,
        "executor_ref": AGENT_EXECUTOR_REF.to_dict(), "snapshot_id": str(uuid4()),
        "frozen_prompt_ref": prompt_ref, "model_ref": model_ref,
        "unit_id": str(uuid4()), "fact_ids": [str(uuid4())],
    }
    call, settled = tool_pair()
    answer = {"text": "original historical answer"}
    call["blocks"] = [call["blocks"][-1]]
    call["blocks"][0].update(
        tool_name="final_answer", raw_arguments=json.dumps({"answer": answer}),
        parsed_arguments={"answer": answer},
    )
    settled["blocks"][0].update(content=answer, model_visible_text=json.dumps(answer))
    operations = [{"kind": "append", "message": message} for message in (call, settled)]
    final = {"message_id": call["message_id"], "value": answer}
    packet = validate_context_update({
        "schema_version": 1, "kind": "workflow.agent-context-update",
        "owner": owner, "update_id": projection_id(owner["node_run_id"], "context-update"),
        "basis_view_ref": view_ref, "basis_revision": loaded["basis"],
        "compaction_policy_ref": None, "operations": operations, "receipts": receipts,
        "next_view": build_context_update_view(prompt, operations, receipts, final),
    })
    merged = merge_native_context(loaded, view_ref, packet, update_ref, prompt)
    config = {"object_key": "legacy-context", "agent_node_id": agent}

    def detail(value, component, version, node_id, inputs=None):
        return {
            "value": value, "component_id": component, "component_version": version,
            "config": deepcopy(config) if component.startswith("context.") else {},
            "producer": {
                "workflow_session_id": sid, "chain_run_id": chain,
                "node_binding_id": node_id, "node_run_id": owner["node_run_id"]
                if node_id == agent else str(uuid4()),
            },
            "input_refs": {
                port: [{"output_id": reference["output_id"]}]
                for port, reference in (inputs or {}).items()
            },
        }

    artifacts = {
        view_ref["output_id"]: detail(loaded, "context.output", "4", reader),
        prompt_ref["output_id"]: detail(prompt, "context.assembly", "4", str(uuid4())),
        update_ref["output_id"]: detail(
            packet, "agents.execute", "4", agent, {"prompt": prompt_ref, "model": model_ref},
        ),
        merged_ref["output_id"]: detail(
            merged, "context.merge", "4", writer, {"view": view_ref, "context": update_ref},
        ),
    }
    definition = {
        "document": {"nodes": [{
            "node_binding_id": writer, "component_id": "context.merge",
            "component_version": "4", "config": config,
        }]},
    }
    expected = project_agent_context_value(merged)
    source = {
        "workflow_session_id": sid, "workflow_definition_id": workflow, "definition_revision": 1,
        "history_refs": [chain], "objects": {"legacy-context": {**old_record, "value": expected}},
    }
    parameters = {"session_id": sid, "node_id": writer, "view_output_id": merged_ref["output_id"]}
    return artifacts, definition, source, parameters, expected, merged_ref


def test_retired_receipt_projection_uses_its_exact_frozen_v4_contract(monkeypatch):
    artifacts, definition, source, parameters, expected, _ = _frozen_projection()
    before = deepcopy((artifacts, definition, source, parameters))
    monkeypatch.setattr(
        "phase1_agent.graph_receipts._context_output_detail",
        lambda connection, history, reference: deepcopy(artifacts[reference["output_id"]]),
    )
    monkeypatch.setattr(
        "phase1_agent.graph_receipts._receipt_graph_record", lambda *args, **kwargs: deepcopy(definition),
    )
    assert _context_adoption_projection(object(), parameters, source) == ("legacy-context", expected, 1)
    assert (artifacts, definition, source, parameters) == before


def test_retired_receipt_projection_does_not_accept_an_active_v5_view(monkeypatch):
    artifacts, definition, source, parameters, _, merged_ref = _frozen_projection()
    artifacts[merged_ref["output_id"]]["value"]["schema_version"] = 5
    del artifacts[merged_ref["output_id"]]["value"]["owner"]["agent_node_id"]
    monkeypatch.setattr(
        "phase1_agent.graph_receipts._context_output_detail",
        lambda connection, history, reference: deepcopy(artifacts[reference["output_id"]]),
    )
    with pytest.raises(GraphDiagnosticError):
        _context_adoption_projection(object(), parameters, source)
