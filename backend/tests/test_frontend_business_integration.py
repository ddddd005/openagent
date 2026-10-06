"""Frontend state joins ordinary Agent, context and checkpoint settlement."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_business import register_frontend_business
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from test_agent_integration import output, run
from test_context_summary_integration import SummaryTransport, base_graph
from test_graph_service import create, edge, node
from test_models_service_integration import ModelDatabaseFixture


def frontend_package():
    return CapabilityPackage(PackageManifest(
        "sample.frontend-business", "1.0.0", schema_version=2,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),),
    ), register_frontend_business)


@pytest.fixture
def frontend_service(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-frontend-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(
        tmp_path / "frontend.sqlite", public_model_factory=transport.factory,
        capability_packages=[frontend_package()],
        enabled_packages={**{key: value for key, value in DEFAULT_PACKAGES.items()
                              if key not in {"workflow.frontend-business", "workflow.frontend"}},
                          "sample.frontend-business": "1.0.0"},
    )) as service:
        ModelDatabaseFixture.write(service, 1)
        yield service, transport


def frontend_graph(service):
    doc = base_graph(service)
    agent = next(item for item in doc["nodes"] if item["component_id"] == "agents.execute")
    merge = next(item for item in doc["nodes"] if item["component_id"] == "context.merge")
    display = next(item for item in doc["nodes"] if item["component_id"] == "tools.output")
    read = node(service.registry, "frontend.state.output", 1501)
    append = node(service.registry, "frontend.state.append", 1502)
    processed = node(service.registry, "tools.regex", 1503,
                     pattern="accepted", replacement="presented")
    append["public_outputs"] = ["view"]
    doc["nodes"] += [read, processed, append]
    doc["edges"] += [
        edge(agent, processed, 1501, source_port="result"),
        edge(read, append, 1502, source_port="view", target_port="view"),
        edge(processed, append, 1503, target_port="content"),
    ]
    # The required context merge completes after the frontend business call.
    doc.update(schema_version=2, control_edges=[{
        "edge_id": str(uuid4()), "source_node_id": append["node_binding_id"],
        "target_node_id": merge["node_binding_id"],
    }], execution_roots=[merge["node_binding_id"], append["node_binding_id"]])
    doc["object_bindings"].append(ObjectBinding(
        "frontend", "workflow.frontend-state", 1, "shared",
        readers=(read["node_binding_id"], append["node_binding_id"]),
        writers=(append["node_binding_id"],),
    ).to_dict())
    return doc, append, merge, processed, display


def count_frontend_calls(service):
    entry = service.registry.get("frontend.state.append", "1")
    calls = []

    def execute(config, inputs, context):
        calls.append(context.node_run_id)
        return entry.executor(config, inputs, context)

    service.registry._nodes[("frontend.state.append", "1")] = replace(entry, executor=execute)
    return calls


def retry(service, pending, action):
    started = service.control(
        pending["workflow_session_id"], action=action,
        expected_revision=pending["revision"], idempotency_key=str(uuid4()),
    )
    service.wait(started["active_chain_run_id"])
    return service.get_session(pending["workflow_session_id"])


def test_agent_postprocessing_frontend_then_context_waits_for_whole_plan(frontend_service):
    service, transport = frontend_service
    entered, proceed = Event(), Event()
    merge_entry = service.registry.get("context.merge", "2")

    def delayed_merge(config, inputs, context):
        entered.set()
        assert proceed.wait(10), "Test did not release the required context merge"
        return merge_entry.executor(config, inputs, context)

    service.registry._nodes[("context.merge", "2")] = replace(merge_entry, executor=delayed_merge)
    doc, append, merge, processed, _ = frontend_graph(service)
    initial = create(service, doc)
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"],
        inputs={"text": "ORION question"}, idempotency_key=str(uuid4()),
    )
    try:
        assert entered.wait(10)
        active = service.get_session(initial["workflow_session_id"])
        assert active["status"] == "running"
        assert active["head_commit_id"] == initial["head_commit_id"]
        assert service.list_graph_candidates(initial["workflow_session_id"])["candidates"] == []
        assert active["objects"]["frontend"]["revision"] == 2
        assert len(active["objects"]["frontend"]["value"]["entries"]) == 1
        assert active["objects"]["context"]["revision"] == 1
        assert len(transport.calls) == 1
    finally:
        proceed.set()
    service.wait(started["active_chain_run_id"])
    final = service.get_session(initial["workflow_session_id"])
    assert final["status"] == "succeeded", final["chains"]
    assert final["head_commit_id"] != initial["head_commit_id"]
    assert final["objects"]["context"]["revision"] == 2
    assert output(final, "tools.regex")["text"] == "presented ORION answer"
    history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
    ordered = [item["node_binding_id"] for item in history["node_runs"]]
    assert ordered.index(append["node_binding_id"]) < ordered.index(merge["node_binding_id"])
    processed_run = next(item for item in history["node_runs"]
                         if item["node_binding_id"] == processed["node_binding_id"])
    entry = final["objects"]["frontend"]["value"]["entries"][0]
    assert entry["source_ref"] == {
        "scope": "artifact", "output_id": processed_run["output_refs"]["output"],
    }
    assert set(entry) == {"entry_id", "role", "source_ref"}
    end = history["state_manifests"]["end"]
    assert end["objects"]["frontend"]["revision_id"] == final["objects"]["frontend"]["revision_id"]
    assert end["objects"]["context"]["revision_id"] == final["objects"]["context"]["revision_id"]
    assert end["definition_revision"] == doc["revision"]
    assert service.get_definition(final["workflow_definition_id"], 1) == doc


def test_frontend_acceptance_rollback_reuses_business_and_model_result(frontend_service, monkeypatch):
    service, transport = frontend_service
    calls = count_frontend_calls(service)
    doc, append, _, _, _ = frontend_graph(service)
    original = service._save_effects
    attempts = []

    def fail_after_frontend_write(repo, sid, document, event):
        result = original(repo, sid, document, event)
        if event["node_binding_id"] == append["node_binding_id"]:
            attempts.append(deepcopy(event["object_writes"]))
            if len(attempts) == 1:
                raise OSError("frontend object transaction interrupted")
        return result

    monkeypatch.setattr(service, "_save_effects", fail_after_frontend_write)
    initial = create(service, doc)
    pending = run(service, initial, "ORION question")
    assert pending["status"] == "archive_failed"
    assert pending["available_actions"] == ["retry_acceptance", "close"]
    assert pending["objects"]["frontend"]["revision"] == 1
    assert pending["objects"]["frontend"]["value"] == {"entries": [], "view_ref": None}
    assert pending["objects"]["context"]["revision"] == 1
    assert pending["head_commit_id"] == initial["head_commit_id"]
    assert len(calls) == len(transport.calls) == 1
    final = retry(service, pending, "retry_acceptance")
    assert final["status"] == "succeeded", final["chains"]
    assert len(calls) == len(transport.calls) == 1
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert final["objects"]["frontend"]["revision"] == 2
    assert len(final["objects"]["frontend"]["value"]["entries"]) == 1
    assert final["objects"]["context"]["revision"] == 2


def test_frontend_checkpoint_retry_keeps_objects_and_actual_definition(frontend_service, monkeypatch):
    service, transport = frontend_service
    calls = count_frontend_calls(service)
    doc, _, _, _, _ = frontend_graph(service)
    original = service._commit
    attempts = []

    def fail_checkpoint(repo, session, *, source, **kwargs):
        original(repo, session, source=source, **kwargs)
        if source["kind"] == "completed_execution":
            attempts.append(deepcopy(source))
            if len(attempts) == 1:
                raise OSError("frontend checkpoint interrupted")

    monkeypatch.setattr(service, "_commit", fail_checkpoint)
    initial = create(service, doc)
    doc = deepcopy(doc)
    doc["revision"] = 2
    doc["name"] = "Actual second definition"
    service.save_definition(doc, expected_revision=1, idempotency_key=str(uuid4()))
    initial = service.rebind_session(
        initial["workflow_session_id"], definition_revision=2,
        expected_revision=initial["revision"], expected_data_revision=initial["data_revision"],
        expected_head_revision=initial["head_revision"], idempotency_key=str(uuid4()),
    )
    pending = run(service, initial, "ORION question")
    assert pending["status"] == "archive_failed"
    assert pending["available_actions"] == ["retry_archive", "close"]
    assert pending["head_commit_id"] == initial["head_commit_id"]
    assert pending["objects"]["frontend"]["revision"] == 2
    assert pending["objects"]["context"]["revision"] == 2
    prior = deepcopy(pending["objects"])
    final = retry(service, pending, "retry_archive")
    assert final["status"] == "succeeded", final["chains"]
    assert final["objects"] == prior
    assert len(calls) == len(transport.calls) == 1
    assert len(attempts) == 2
    candidate = service.list_graph_candidates(final["workflow_session_id"])["candidates"][0]
    assert candidate["source_definition_revision"] == doc["revision"]
    assert service.get_definition(final["workflow_definition_id"], final["definition_revision"]) == doc


def test_frontend_failure_never_checkpoints_or_updates_its_object(frontend_service):
    service, transport = frontend_service
    entry = service.registry.get("frontend.state.append", "1")
    calls = []

    def fail_after_business(config, inputs, context):
        calls.append(context.node_run_id)
        entry.executor(config, inputs, context)
        raise ValueError("frontend business rejected before return")

    service.registry._nodes[("frontend.state.append", "1")] = replace(entry, executor=fail_after_business)
    doc, _, _, _, _ = frontend_graph(service)
    initial = create(service, doc)
    failed = run(service, initial, "ORION question")
    assert failed["status"] == "failed"
    assert failed["head_commit_id"] == initial["head_commit_id"]
    assert failed["objects"]["frontend"]["value"] == {"entries": [], "view_ref": None}
    assert failed["objects"]["context"]["revision"] == 1
    assert service.list_graph_candidates(failed["workflow_session_id"])["candidates"] == []
    assert len(calls) == len(transport.calls) == 1


def test_frontend_candidate_fork_keeps_refs_and_branch_state(frontend_service):
    service, transport = frontend_service
    doc, append, _, _, _ = frontend_graph(service)
    first = run(service, create(service, doc), "ORION first question")
    assert first["status"] == "succeeded", first["chains"]
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    second = run(service, first, "ORION second question")
    assert second["status"] == "succeeded", second["chains"]
    child = service.fork_graph_candidate(
        second["workflow_session_id"], candidate_id=candidate["candidate_id"],
        expected_revision=second["revision"], expected_data_revision=second["data_revision"],
        expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()),
    )
    assert child["objects"]["frontend"]["value"] == first["objects"]["frontend"]["value"]
    inherited = service.read_public_output(
        child["workflow_session_id"], workflow_definition_id=doc["workflow_definition_id"],
        definition_revision=1, node_id=append["node_binding_id"], port_id="view",
    )["output"]
    assert inherited["source"]["workflow_session_id"] == first["workflow_session_id"]
    assert inherited["payload"] == output(first, "frontend.state.append", "view")
    completed = run(service, child, "ORION child question")
    assert completed["status"] == "succeeded", completed["chains"]
    child_entries = completed["objects"]["frontend"]["value"]["entries"]
    parent_entries = service.get_session(second["workflow_session_id"])["objects"]["frontend"]["value"]["entries"]
    assert child_entries[0] == parent_entries[0]
    assert child_entries[1]["source_ref"] != parent_entries[1]["source_ref"]
    assert len(transport.calls) == 3


def test_frontend_object_artifact_reader_authorizes_exact_object_closure(frontend_service):
    service, _ = frontend_service
    doc, append, _, _, display = frontend_graph(service)
    first = run(service, create(service, doc), "ORION first question")
    assert first["status"] == "succeeded", first["chains"]
    reference = first["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    app = GraphApplication(service)
    parameters = {
        "session_id": first["workflow_session_id"], "object_key": "frontend",
        "node_id": append["node_binding_id"], "reference": reference,
    }
    artifact = app.query("object.artifact.read", parameters)
    assert artifact["value"] == output(first, "tools.regex")
    with pytest.raises(ContractValidationError) as denied:
        app.for_consumer().query("object.artifact.read", parameters)
    assert denied.value.reason_code == "application_scope_denied"
    with pytest.raises(ContractValidationError) as denied:
        app.query("object.artifact.read", {**parameters, "node_id": display["node_binding_id"]})
    assert denied.value.reason_code == "session_object_access_denied"
    other = run(service, service.create_session(
        first["workflow_definition_id"], 1, idempotency_key=str(uuid4())),
        "ORION independent question")
    foreign = other["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    with pytest.raises(ContractValidationError) as denied:
        app.query("object.artifact.read", {**parameters, "reference": foreign})
    assert denied.value.reason_code == "session_object_reference_denied"


def test_agent_registered_reader_projects_original_facts_without_model_dispatch(frontend_service):
    service, transport = frontend_service
    doc, _, _, _, _ = frontend_graph(service)
    final = run(service, create(service, doc), "ORION question")
    assert final["status"] == "succeeded", final["chains"]
    app = GraphApplication(service)
    directory = app.query("registration.list", {
        "session_id": final["workflow_session_id"], "kind": "information_binding",
    })["items"]
    route = next(item for item in directory
                 if item["declaration"]["component_id"] == "agents.execute"
                 and item["declaration"]["component_version"] == "3")
    history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
    original = [item for item in history["runtime_facts"]
                if item.get("owner") == route["owner"] and item.get("generation") == route["generation"]]
    assert original
    parameters = {
        "session_id": final["workflow_session_id"], "reference": route["registration_ref"],
        "owner": route["owner"], "generation": route["generation"],
        "source_scope": "history", "limit": 2,
    }
    pages, cursor = [], None
    while True:
        response = app.query("information.read", {**parameters, "cursor": cursor})
        pages.extend(response["items"])
        cursor = response["next_cursor"]
        if cursor is None:
            break
    assert pages == original
    assert len(transport.calls) == 1
    assert service.get_run(final["workflow_session_id"], final["selected_chain_run_id"]) == history
    with pytest.raises(ContractValidationError) as denied:
        app.for_consumer().query("information.read", parameters)
    assert denied.value.reason_code == "information_read_denied"
    service.close()
    with closing(GraphWorkflowService(
        service.database, capability_packages=[frontend_package()],
        public_model_factory=transport.factory,
    )) as reopened:
        reopened_app = GraphApplication(reopened)
        pages, cursor = [], None
        while True:
            response = reopened_app.query("information.read", {**parameters, "cursor": cursor})
            pages.extend(response["items"])
            cursor = response["next_cursor"]
            if cursor is None:
                break
        assert pages == original
        assert len(transport.calls) == 1
