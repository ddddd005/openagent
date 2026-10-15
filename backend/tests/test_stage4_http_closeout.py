"""Current output items and successful checkpoints round-trip through HTTP."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.host_sdk import ObjectBinding

from test_graph_fresh_http import command, query, request, serve
from test_stage4_model_request_installation import _edge, _node


def output_document(service, *, append=True):
    nodes, edges, bindings, branches = [], [], [], []
    for label in ("alpha", "beta"):
        key = "display-" + label
        source = _node(service, "tools.text", "1", text="HTTP " + label)
        output = _node(service, "tools.output", "2")
        output["public_outputs"] = ["output"]
        nodes += [source, output]
        edges.append(_edge(source, output))
        readers, writers = [], []
        branch = {"key": key, "output": output, "text": "HTTP " + label}
        if append:
            read = _node(service, "frontend.state.output", "1", object_key=key)
            writer = _node(service, "frontend.state.append", "1", object_key=key)
            presentation = _node(service, "frontend.presentation", "1")
            presentation["public_outputs"] = ["display"]
            nodes += [read, writer, presentation]
            edges += [
                _edge(read, writer, source_port="view", target_port="view"),
                _edge(output, writer, target_port="content"),
                _edge(writer, presentation, source_port="view", target_port="view"),
            ]
            readers = [read["node_binding_id"], writer["node_binding_id"]]
            writers = [writer["node_binding_id"]]
            branch.update(read=read, writer=writer, presentation=presentation)
        bindings.append(ObjectBinding(
            key, FRONTEND_STATE_TYPE, 1, "shared",
            readers=tuple(readers), writers=tuple(writers),
            registrar_node_id=output["node_binding_id"],
        ).to_dict())
        branches.append(branch)
    document = {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "D HTTP closeout", "nodes": nodes, "edges": edges,
        "object_bindings": bindings,
        "package_lock": deepcopy(list(service.registry.execution_package_lock)),
    }
    return document, branches


def _accept(port, operation, status, **parameters):
    actual_status, packet = command(port, operation, **parameters)
    assert actual_status == status, packet
    assert packet["receipt"]["operation"] == operation
    assert packet["receipt"]["idempotency_key"] == parameters["idempotency_key"]
    return packet


def _create(port, document):
    return _accept(
        port, "session.create", 201,
        workflow_definition_id=document["workflow_definition_id"],
        definition_revision=document["revision"], idempotency_key=str(uuid4()),
    )["result"]


def _run(host, port, session):
    packet = _accept(
        port, "run.start", 202, session_id=session["workflow_session_id"],
        expected_revision=session["revision"], idempotency_key=str(uuid4()),
    )
    host.graph_service.wait(packet["result"]["active_chain_run_id"])
    completed = query(port, "session.read", session_id=session["workflow_session_id"])
    assert completed["status"] == "succeeded", completed["chains"]
    return completed


def _output(session, node, port):
    return next(row["outputs"][port] for row in session["nodes"]
                if row["node_binding_id"] == node["node_binding_id"])


def _assert_display_entries(port, session, branch, count):
    current = session["objects"][branch["key"]]
    assert len(current["value"]["entries"]) == count
    assert _output(session, branch["output"], "output") == text_content(branch["text"])
    for entry in current["value"]["entries"]:
        artifact = query(
            port, "object.artifact.read",
            session_id=session["workflow_session_id"], object_key=branch["key"],
            node_id=branch["read"]["node_binding_id"], revision_id=current["revision_id"],
            reference=entry["source_ref"],
        )
        assert artifact["value"] == text_content(branch["text"])
        assert artifact["producer"]["workflow_session_id"] == session["workflow_session_id"]
        assert artifact["producer"]["node_binding_id"] == branch["output"]["node_binding_id"]


@pytest.mark.parametrize("append", [False, True])
def test_current_http_outputs_require_explicit_append_and_keep_sessions_separate(tmp_path, append):
    database = tmp_path / "current-output-http.sqlite"
    with serve(database) as (host, port):
        platform = query(port, "platform")
        assert host._graph is None
        assert platform["package_diagnostics"] == []
        catalog = query(port, "catalog.node-types", protocol_version=2)
        installed = {(row["component_id"], row["component_version"])
                     for row in catalog["node_types"]}
        assert {
            ("models.source", "7"), ("models.chat", "5"), ("agents.execute", "10"),
            ("context.output", "5"), ("context.assembly", "5"), ("context.merge", "6"),
            ("tools.output", "2"), ("frontend.state.append", "1"),
        } <= installed
        assert not {
            ("models.source", "6"), ("agents.execute", "9"), ("context.merge", "5"),
        } & installed

        document, branches = output_document(host.graph_service, append=append)
        saved = _accept(
            port, "definition.update", 200,
            workflow_definition_id=document["workflow_definition_id"], document=document,
            expected_edit_sequence=0, idempotency_key=str(uuid4()),
        )["result"]
        assert saved["outcome"] == "saved"
        assert query(port, "definition.read", identity=document["workflow_definition_id"]) == saved["document"]
        source, other = _create(port, document), _create(port, document)
        other_before = deepcopy(other["objects"])
        first = _run(host, port, source)
        for branch in branches:
            current = first["objects"][branch["key"]]
            assert current["revision"] == (2 if append else 1)
            _assert_display_entries(port, first, branch, 1 if append else 0)
            if append:
                assert _output(first, branch["read"], "view")["entries"] == []
                assert _output(first, branch["presentation"], "display")["entries"] == current["value"]["entries"]
        assert query(port, "session.read", session_id=other["workflow_session_id"])["objects"] == other_before
        points = query(port, "checkpoint.list", session_id=source["workflow_session_id"])
        assert len(points) == 1
        history = query(
            port, "run.read", session_id=source["workflow_session_id"],
            chain_id=first["selected_chain_run_id"],
        )
        assert history["chain"]["completed_nodes"] == history["chain"]["ordered_nodes"]
        assert set(history["chain"]["completed_nodes"]) == {
            node["node_binding_id"] for node in document["nodes"]
        }
        frozen_first = deepcopy(first)
        second = _run(host, port, first)
        assert first == frozen_first
        assert len(query(port, "checkpoint.list", session_id=source["workflow_session_id"])) == 2
        for branch in branches:
            current = second["objects"][branch["key"]]
            _assert_display_entries(port, second, branch, 2 if append else 0)
            if append:
                assert _output(second, branch["read"], "view")["entries"] == first["objects"][branch["key"]]["value"]["entries"]
        source_before_fork = deepcopy(second)
        parameters = {
            "session_id": source["workflow_session_id"], "checkpoint_id": points[0]["checkpoint_id"],
            "expected_session_revision": second["revision"],
            "expected_data_revision": second["data_revision"],
            "expected_head_revision": second["head_revision"], "idempotency_key": str(uuid4()),
        }
        status, packet = command(port, "checkpoint.fork", **parameters)
        assert status in (200, 201), packet
        assert packet["receipt"]["operation"] == "checkpoint.fork"
        fork = packet["result"]
        assert query(port, "session.read", session_id=source["workflow_session_id"]) == source_before_fork
        target = query(port, "session.read", session_id=fork["target_session_id"])
        assert target["active_chain_run_id"] is None
        assert len(query(port, "checkpoint.list", session_id=fork["target_session_id"])) == 1
        assert all(target["objects"][branch["key"]]["value"]["entries"] == [] for branch in branches)
        receipt_status, receipt = request(
            port, "/api/graph/queries", "receipt.read",
            {"operation": "checkpoint.fork", "parameters": parameters},
        )
        assert receipt_status == 200 and receipt["outcome"] == "matched"
        assert receipt["result"] == fork
        target = _run(host, port, target)
        for branch in branches:
            _assert_display_entries(port, target, branch, 1 if append else 0)
        assert len(query(port, "checkpoint.list", session_id=fork["target_session_id"])) == 2
        assert query(port, "session.read", session_id=source["workflow_session_id"]) == source_before_fork

    with serve(database) as (host, port):
        assert query(port, "session.read", session_id=source["workflow_session_id"]) == source_before_fork
        assert query(port, "run.read", session_id=source["workflow_session_id"],
                     chain_id=first["selected_chain_run_id"]) == history
        assert len(query(port, "checkpoint.list", session_id=fork["target_session_id"])) == 2
        assert host._graph is None
