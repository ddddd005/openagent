from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.context_contract import (
    EFFECTIVE_CONTEXT_TYPE,
    project_agent_context_message,
    project_agent_context_value,
)
from phase1_agent.context_v4 import read_native_context
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphDiagnosticError
from phase1_agent.graph_service import GraphWorkflowService

from test_context_native_integration import NativeTransport, native_graph, node_output
from test_gemini_history import canonical_text
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import run


def test_source_free_projection_preserves_protocol_content_and_consumption_ids():
    accepted_id, applied_id = str(uuid4()), str(uuid4())
    canonical = canonical_text()
    message = project_agent_context_message(canonical)
    view = {
        "schema_version": 4,
        "kind": "workflow.context-view",
        "owner": {
            "workflow_session_id": str(uuid4()),
            "object_key": "context",
            "agent_node_id": str(uuid4()),
        },
        "basis": {"revision_id": str(uuid4()), "head_revision": 2},
        "messages": [message],
        "layout": [{"kind": "history", "round_id": str(uuid4()), "compaction": "never"}],
        "accepted_delta_ids": [accepted_id],
        "applied_delta_ids": [applied_id],
        "generation": 1,
        "derivation": {"operation": "read", "input_view_ref": None, "update_ref": None},
    }

    projected = project_agent_context_value(view)

    assert projected["messages"] == [{
        "message_id": canonical["message_id"],
        "role": canonical["role"],
        "blocks": deepcopy(canonical["blocks"]),
        "thinking_summary": canonical["thinking_summary"],
        "provider_metadata": deepcopy(canonical["provider_metadata"]),
    }]
    assert "source" not in projected["messages"][0]
    assert "schema_version" not in projected["messages"][0]
    assert projected["accepted_delta_ids"] == [accepted_id, applied_id]
    assert projected["generation"] == view["generation"]


def test_revised_inline_root_read_reconstructs_owner_and_cas_without_artifact_resolution():
    session_id, agent_id, revision_id = str(uuid4()), str(uuid4()), str(uuid4())
    root_value = {
        "messages": [{
            "message_id": str(uuid4()),
            "role": "user",
            "blocks": [{"kind": "text", "text": "Persisted inline context"}],
        }],
        "layout": [{"kind": "history", "round_id": str(uuid4()), "compaction": "allowed"}],
        "accepted_delta_ids": [str(uuid4())],
        "generation": 1,
    }
    initial = {
        "type_id": EFFECTIVE_CONTEXT_TYPE,
        "schema_version": 4,
        "deleted": False,
        "revision_id": str(uuid4()),
        "revision": 1,
        "value": {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0},
    }
    revised = {
        **initial,
        "revision_id": revision_id,
        "revision": 2,
        "value": root_value,
    }

    view = read_native_context(revised, session_id, "context", agent_id, root_value)

    assert initial["revision"] == 1
    assert view["owner"] == {
        "workflow_session_id": session_id,
        "object_key": "context",
        "agent_node_id": agent_id,
    }
    assert view["basis"] == {"revision_id": revision_id, "head_revision": 2}
    assert view["messages"] == root_value["messages"]
    assert view["accepted_delta_ids"] == root_value["accepted_delta_ids"]
    assert view["applied_delta_ids"] == []


def test_context_merge_writer_is_authorized_but_agent_and_stale_cas_are_not(tmp_path):
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "inline-root-authority.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, entries = native_graph(service, policy=False, once=False)
        first = run(service, create(service, document), "First accepted request.")
        assert first["status"] == "succeeded", first["chains"]

        session_id = first["workflow_session_id"]
        merge_id = entries["a"]["merge"]["node_binding_id"]
        agent_id = entries["a"]["agent"]["node_binding_id"]
        view_ref = node_output(first, entries["a"]["merge"], "commit")["view_ref"]
        view_output_id = view_ref["output_id"]
        assert first["objects"]["context-a"]["revision"] == 2

        accepted = service.adopt_context_view(
            session_id,
            node_id=merge_id,
            view_output_id=view_output_id,
            expected_revision=first["revision"],
            idempotency_key=str(uuid4()),
        )
        assert accepted["session"]["objects"]["context-a"] == first["objects"]["context-a"]

        with pytest.raises(ContractValidationError) as denied:
            service.adopt_context_view(
                session_id,
                node_id=agent_id,
                view_output_id=view_output_id,
                expected_revision=accepted["session"]["revision"],
                idempotency_key=str(uuid4()),
            )
        assert denied.value.reason_code == "context_adoption_writer_denied"

        second = run(service, accepted["session"], "Second accepted request.")
        assert second["status"] == "succeeded", second["chains"]
        with pytest.raises(GraphDiagnosticError) as stale:
            service.adopt_context_view(
                session_id,
                node_id=merge_id,
                view_output_id=view_output_id,
                expected_revision=second["revision"],
                idempotency_key=str(uuid4()),
            )
        assert stale.value.reason_code == "context_stale_basis"
