from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore


def _empty_document():
    return {
        "schema_version": 2,
        "workflow_definition_id": str(uuid4()),
        "revision": 1,
        "name": "Empty definition",
        "nodes": [],
        "edges": [],
        "object_bindings": [],
    }


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(tmp_path / "node-definition-retirement.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def test_empty_definition_save_has_no_descriptor_side_effect_and_replays_receipt(service):
    document = _empty_document()
    key = str(uuid4())
    request = {"document": deepcopy(document), "expected_revision": 0}

    first = service._change(
        "test.node_definition_retirement.empty_save",
        key,
        request,
        lambda repo: service._save_definition(repo, request["document"], 0),
    )
    replay = service._change(
        "test.node_definition_retirement.empty_save",
        key,
        request,
        lambda _repo: pytest.fail("receipt replay must not repeat the write"),
    )

    assert replay == first
    assert service._document_available(first) is True
    with closing(service._store()) as store:
        assert GraphRecordStore(store).rows("node_definition") == []


def test_unavailable_definition_does_not_hide_active_controls(service, monkeypatch):
    node_id = str(uuid4())
    document = {
        "nodes": [{"node_binding_id": node_id, "title": "Typed node"}],
    }
    session = {
        "workflow_session_id": str(uuid4()),
        "workflow_definition_id": str(uuid4()),
        "definition_revision": 1,
        "revision": 3,
        "active_chain_run_id": str(uuid4()),
    }
    chain = {
        "chain_run_id": session["active_chain_run_id"],
        "workflow_session_id": session["workflow_session_id"],
        "status": "running",
        "ordered_nodes": [node_id],
        "node_run_ids": [str(uuid4())],
    }

    data = SimpleNamespace(
        records=SimpleNamespace(
            session=lambda _sid: session,
            chains=lambda _sid: [chain],
            history=lambda _sid: [],
            head=lambda _sid: {"revision": 1, "head_commit_id": str(uuid4())},
            session_metadata=lambda _sid: {},
            archived=lambda _kind, _identity: False,
            private_states=lambda _sid: {},
            node_runs=lambda _chain_id: [],
            outputs=lambda _sid: [],
        ),
        state=SimpleNamespace(
            variables=lambda _sid: {"revision": 0},
            objects=lambda _sid: {},
        ),
        configuration=SimpleNamespace(fixed_document=lambda _identity, _revision: document),
    )
    monkeypatch.setattr(service, "_selected_chain", lambda _records, _sid: None)
    monkeypatch.setattr(service, "_document_available", lambda _document: False)
    view = service._view(data, session["workflow_session_id"])

    assert view["readonly"] is True
    assert view["can_submit"] is False
    assert view["available_actions"] == ["pause"]
