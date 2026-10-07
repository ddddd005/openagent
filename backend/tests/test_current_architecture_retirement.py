"""Current graph operations remain complete after fixed/compat retirement."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES
from phase1_agent.graph_application_identity import native_command_request
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_records import validate_graph_bundle
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore


def _document(registry):
    nodes = []
    for index, component in enumerate(("tools.current-input", "tools.output")):
        entry = registry.get(component, "1")
        nodes.append({
            "node_binding_id": str(uuid4()), "component_id": component,
            "component_version": "1", "title": component,
            "position": {"x": index * 100, "y": 0},
            "config": deepcopy(entry.definition.default_config),
        })
    nodes[1]["public_outputs"] = ["output"]
    return {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Current graph retirement coverage", "nodes": nodes,
        "edges": [{
            "edge_id": str(uuid4()), "source_node_id": nodes[0]["node_binding_id"],
            "source_port_id": "output", "target_node_id": nodes[1]["node_binding_id"],
            "target_port_id": "input", "order": 0,
        }],
        "object_bindings": [], "package_lock": list(registry.package_lock),
    }


def _run(service, session, text):
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()), inputs={"text": text},
    )
    service.wait(started["active_chain_run_id"])
    final = service.get_session(session["workflow_session_id"])
    assert final["status"] == "succeeded"
    return final


def test_current_registry_and_protocol_do_not_expose_retired_capabilities(tmp_path):
    registry = create_package_registry().registry
    assert not any(row["package_id"] == "workflow.compat" for row in registry.package_lock)
    assert registry.get("workflow.agent", "1") is None
    assert registry.get("workflow.prompt-assembly", "1") is None
    assert not {"legacy.migrate", "resource.import"} & {spec.name for spec in COMMANDS}
    assert "archive.read" not in {spec.name for spec in QUERIES}
    with closing(GraphWorkflowService(tmp_path / "protocol.sqlite")) as service:
        assert not hasattr(service, "_native_runtime")
        assert not hasattr(service, "_agent_runtime")
        assert not hasattr(service, "migrate_legacy_session")
        application = GraphApplication(service)
        for operation in ("legacy.migrate", "resource.import"):
            with pytest.raises(ContractValidationError) as failure:
                application.command(operation, {})
            assert failure.value.reason_code == "not_found"
        for route in ("/api/graph/migrate", "/api/graph/resources/import"):
            with pytest.raises(ContractValidationError) as failure:
                dispatch_graph(service, "POST", route, {})
            assert failure.value.reason_code == "not_found"


def test_current_graph_multiround_copy_candidate_and_history_reopen(tmp_path):
    database = tmp_path / "current-operations.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        document = _document(service.registry)
        service.save_definition(document, expected_revision=0, idempotency_key="definition")
        initial = service.create_session(document["workflow_definition_id"], 1, idempotency_key="session")
        first = _run(service, initial, "first")
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        second = _run(service, first, "second")
        fork = service.fork_graph_candidate(
            second["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key="fork",
        )
        branch = _run(service, fork, "branch")
        assert len(branch["history_refs"]) == 2
        selected = service.select_graph_candidate(
            second["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key="select",
        )
        assert selected["selected_chain_run_id"] == first["selected_chain_run_id"]
        copied_document = deepcopy(document)
        copied_document["workflow_definition_id"] = str(uuid4())
        copied = service.copy_session(
            selected["workflow_session_id"], document=copied_document,
            expected_session_revision=selected["revision"],
            expected_data_revision=selected["data_revision"],
            expected_definition_revision=selected["definition_revision"],
            expected_head_revision=selected["head_revision"], idempotency_key="copy",
        )
        assert copied["history_refs"] == selected["history_refs"]
        sid, chain = selected["workflow_session_id"], first["selected_chain_run_id"]
        history = service.get_run(sid, chain)
        assert all(row["schema_version"] == 5 and "agent" not in row for row in history["node_runs"])
    with closing(GraphWorkflowService(database)) as service:
        assert service.get_run(sid, chain) == history
        assert service.get_session(sid)["selected_chain_run_id"] == chain


def test_current_node_records_require_exact_input_evidence(tmp_path):
    database = tmp_path / "input-evidence.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        document = _document(service.registry)
        service.save_definition(document, expected_revision=0, idempotency_key="definition")
        initial = service.create_session(document["workflow_definition_id"], 1, idempotency_key="session")
        _run(service, initial, "input")
    with closing(SqliteStore(database)) as store:
        bundle = store.read_bundle(include_graph=True)
    validate_graph_bundle(bundle)
    target = next(row for row in bundle["node_run"] if row["node_binding_id"] == document["nodes"][1]["node_binding_id"])
    target["input_refs"] = {}
    with pytest.raises(ContractValidationError) as failure:
        validate_graph_bundle(bundle)
    assert failure.value.reason_code == "storage_contract_violation"


def test_current_control_identity_uses_only_current_service_fields():
    sid = str(uuid4())
    expected = {"session": sid, "action": "pause", "expected_revision": 3}
    native = native_command_request("run.control", {
        "session_id": sid, "action": "pause", "expected_revision": 3, "idempotency_key": "pause",
    })
    assert native["request_digest"] == "workflow-op-v1:" + content_digest(expected).rsplit(":", 1)[1]


def test_unknown_dormant_nodes_remain_drafts_and_never_gain_an_executor(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "unknown-draft.sqlite")) as service:
        document = _document(service.registry)
        unknown = {
            "node_binding_id": str(uuid4()), "component_id": "unknown.custom-node",
            "component_version": "1", "title": "Unresolved draft",
            "position": {"x": 200, "y": 0}, "config": {"custom": True},
        }
        document["nodes"].append(unknown)
        service.save_definition(document, expected_revision=0, idempotency_key="dormant-definition")
        initial = service.create_session(
            document["workflow_definition_id"], 1, idempotency_key="dormant-session",
        )
        assert initial["can_submit"] and not initial["readonly"]
        final = _run(service, initial, "current path")
        assert final["nodes"][-1]["status"] == "idle"
        document["workflow_definition_id"] = str(uuid4())
        document["edges"][0]["source_node_id"] = unknown["node_binding_id"]
        service.save_definition(document, expected_revision=0, idempotency_key="unresolved-definition")
        blocked = service.create_session(
            document["workflow_definition_id"], 1, idempotency_key="unresolved-session",
        )
        with pytest.raises(ContractValidationError) as failure:
            service.start(
                blocked["workflow_session_id"], expected_revision=blocked["revision"],
                idempotency_key="unknown-start", inputs={"text": "not dispatched"},
            )
        assert failure.value.reason_code == "graph_missing_node_type"
        assert service.get_session(blocked["workflow_session_id"])["chains"] == []
