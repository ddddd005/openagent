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
    assert service._document_available(None, first) is True
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

    class Repo:
        store = object()

        def get(self, kind, **_kwargs):
            assert kind == "workflow_session"
            return session

        def rows(self, kind, **_kwargs):
            return [chain] if kind == "chain_run" else []

    monkeypatch.setattr(service, "_document", lambda _repo, _identity, _revision: document)
    monkeypatch.setattr(service, "_history", lambda _repo, _sid: [])
    monkeypatch.setattr(service, "_selected_chain", lambda _repo, _sid: None)
    monkeypatch.setattr(service, "_head", lambda _repo, _sid: {
        "revision": 1,
        "head_commit_id": str(uuid4()),
    })
    monkeypatch.setattr(service, "_session_metadata", lambda _repo, _sid: {})
    monkeypatch.setattr(service, "_archived", lambda _repo, _kind, _identity: False)
    monkeypatch.setattr(service, "_private", lambda _repo, _sid: {})
    monkeypatch.setattr(service, "_objects", lambda _repo: SimpleNamespace(current=lambda _sid: {}))
    monkeypatch.setattr(service, "_document_available", lambda _repo, _document: False)

    class Variables:
        def __init__(self, _store):
            pass

        def current(self, _sid):
            return {"revision": 0}

    monkeypatch.setattr("phase1_agent.graph_service.ProgramVariableStore", Variables)
    view = service._view(Repo(), session["workflow_session_id"])

    assert view["readonly"] is True
    assert view["can_submit"] is False
    assert view["available_actions"] == ["pause"]
