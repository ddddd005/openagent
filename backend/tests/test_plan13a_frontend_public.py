"""13-A explicit frontend presentation and bounded public artifact reads."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.content_contracts import text_content
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, ObjectBinding, TypeRegistry

from test_graph_public import rebind
from test_graph_service import create, edge, node, uid
from graph_test_plugin import current_document as document, run_current_graph as run


@pytest.fixture
def service(tmp_path):
    def no_model(*args, **kwargs):
        pytest.fail("Frontend presentation attempted to construct a model")

    with closing(GraphWorkflowService(
        tmp_path / "frontend-public.sqlite", public_model_factory=no_model,
    )) as instance:
        yield instance


def frontend_graph(service, *, public=True):
    source = node(service.registry, "tools.current-input", 1301)
    read = node(service.registry, "frontend.state.output", 1302)
    append = node(service.registry, "frontend.state.append", 1303)
    presentation = node(service.registry, "frontend.presentation", 1304)
    if public:
        presentation["public_outputs"] = ["display"]
    value = document(service.registry, [source, read, append, presentation], [
        edge(source, append, 1301, target_port="content"),
        edge(read, append, 1302, source_port="view", target_port="view"),
        edge(append, presentation, 1303, source_port="view", target_port="view"),
    ], roots=[presentation], bindings=[ObjectBinding(
        "frontend", FRONTEND_STATE_TYPE, 1, "shared",
        readers=(read["node_binding_id"], append["node_binding_id"]),
        writers=(append["node_binding_id"],),
    ).to_dict()])
    return value, source, read, append, presentation


def root_parameters(service, session, doc, presentation, *, run_id=None):
    output = service.read_public_output(
        session["workflow_session_id"], workflow_definition_id=doc["workflow_definition_id"],
        definition_revision=doc["revision"], node_id=presentation["node_binding_id"],
        port_id="display", run_id=run_id,
    )["output"]
    return {"session_id": session["workflow_session_id"],
            "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
            "node_id": presentation["node_binding_id"], "port_id": "display", "run_id": output["run_id"]}


def artifact(app, parameters, reference):
    return app.query("output.artifact.read", {**parameters, "reference": reference})


def test_default_business_package_requires_explicit_root_and_has_no_new_endpoint(service):
    assert DEFAULT_PACKAGES["workflow.frontend-business"] == "1.0.0"
    assert service.registry.get("frontend.presentation", "1").definition.is_output is False
    doc, _, _, _, presentation = frontend_graph(service)
    doc["execution_roots"] = []
    dormant = create(service, doc)
    with pytest.raises(ContractValidationError) as denied:
        run(service, dormant, inputs={"text": "dormant"})
    assert denied.value.reason_code == "graph_no_outputs"
    assert service.get_session(dormant["workflow_session_id"])["objects"]["frontend"]["revision"] == 1
    operation = next(row for row in GraphApplication(service).for_consumer().describe()["queries"]
                     if row["name"] == "output.artifact.read")
    assert operation["scope"] == "consumer"
    assert set(operation["required_fields"]) == {
        "session_id", "workflow_definition_id", "definition_revision", "node_id", "port_id", "run_id", "reference"}


def test_public_presentation_returns_only_entries_and_artifact_payload_type_producer(service):
    doc, source, _, append, presentation = frontend_graph(service)
    final = run(service, create(service, doc), inputs={"text": "public accepted text"})
    assert final["status"] == "succeeded", final["chains"]
    app = GraphApplication(service)
    parameters = root_parameters(service, final, doc, presentation)
    display = app.query("output.read", {key: value for key, value in parameters.items() if key != "run_id"})["output"]
    assert set(display["payload"]) == {"schema_version", "kind", "entries"}
    assert display["payload"]["kind"] == "workflow.frontend-display"
    entry = display["payload"]["entries"][0]
    assert set(entry) == {"entry_id", "role", "source_ref"}
    assert entry["role"] == "assistant"
    expected_fields = {
        "schema_version", "kind", "workflow_definition_id", "definition_revision", "workflow_session_id",
        "session_revision", "node_id", "port_id", "run_id", "reference", "data_type", "data_schema_version",
        "value", "producer"}
    management = artifact(app, parameters, entry["source_ref"])
    assert management == artifact(app.for_consumer(), parameters, entry["source_ref"])
    assert set(management) == expected_fields
    assert management["value"] == text_content("public accepted text")
    assert (management["data_type"], management["data_schema_version"]) == ("TEXT", 2)
    assert set(management["producer"]) == {
        "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"}
    assert management["producer"]["node_binding_id"] == source["node_binding_id"]
    assert management["producer"]["workflow_session_id"] == final["workflow_session_id"]
    assert management["reference"] == entry["source_ref"]
    assert not hasattr(service, "_native_runtime")
    history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
    private_view = next(row for row in history["node_runs"] if row["node_binding_id"] == append["node_binding_id"])
    for ref in (
        {"scope": "artifact", "output_id": private_view["output_refs"]["view"]},
        {"scope": "artifact", "output_id": private_view["output_refs"]["commit"]},
        {"scope": "artifact", "output_id": private_view["input_refs"]["view"][0]["output_id"]},
    ):
        with pytest.raises(ContractValidationError) as denied:
            artifact(app.for_consumer(), parameters, ref)
        assert denied.value.reason_code == "output_reference_denied"


def test_multiple_rounds_fork_restore_and_reopen_keep_exact_ancestor_sources(service):
    doc, _, _, _, presentation = frontend_graph(service)
    first = run(service, create(service, doc), inputs={"text": "first"})
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]["candidate_id"]
    second = run(service, first, inputs={"text": "second"})
    first_ref, second_ref = [entry["source_ref"] for entry in second["objects"]["frontend"]["value"]["entries"]]
    app = GraphApplication(service).for_consumer()
    parameters = root_parameters(service, second, doc, presentation)
    assert [artifact(app, parameters, ref)["value"]["text"] for ref in (first_ref, second_ref)] == ["first", "second"]
    child = service.fork_graph_candidate(
        second["workflow_session_id"], candidate_id=candidate, expected_revision=second["revision"],
        expected_data_revision=second["data_revision"], expected_head_revision=second["head_revision"],
        idempotency_key=str(uuid4()))
    inherited = root_parameters(service, child, doc, presentation)
    assert artifact(app, inherited, first_ref)["producer"]["workflow_session_id"] == first["workflow_session_id"]
    with pytest.raises(ContractValidationError):
        artifact(app, inherited, second_ref)
    completed = run(service, child, inputs={"text": "child"})
    child_parameters = root_parameters(service, completed, doc, presentation)
    assert artifact(app, child_parameters, first_ref)["value"]["text"] == "first"
    assert len(completed["objects"]["frontend"]["value"]["entries"]) == 2
    restored = service.select_graph_candidate(
        second["workflow_session_id"], candidate_id=candidate, expected_revision=second["revision"],
        expected_data_revision=second["data_revision"], expected_head_revision=second["head_revision"],
        idempotency_key=str(uuid4()))
    restored_parameters = root_parameters(service, restored, doc, presentation)
    assert artifact(app, restored_parameters, first_ref)["value"]["text"] == "first"
    with pytest.raises(ContractValidationError):
        artifact(app, restored_parameters, second_ref)
    database = service.database
    service.close()
    with closing(GraphWorkflowService(database)) as reopened:
        response = artifact(GraphApplication(reopened).for_consumer(), child_parameters, first_ref)
        assert response["value"]["text"] == "first"
        assert response["producer"]["workflow_session_id"] == first["workflow_session_id"]


@pytest.mark.parametrize("original_public", [True, False])
def test_original_and_current_root_declarations_both_authorize(service, original_public):
    doc, _, _, _, presentation = frontend_graph(service, public=original_public)
    final = run(service, create(service, doc), inputs={"text": "root"})
    ref = final["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    root_run = next(row["run_id"] for row in final["nodes"]
                    if row["node_binding_id"] == presentation["node_binding_id"])
    revised = deepcopy(doc)
    revised["revision"] = 2
    revised["nodes"][-1]["public_outputs"] = [] if original_public else ["display"]
    changed = rebind(service, final, revised)
    parameters = {
        "session_id": changed["workflow_session_id"], "workflow_definition_id": doc["workflow_definition_id"],
        "definition_revision": 2, "node_id": presentation["node_binding_id"],
        "port_id": "display", "run_id": root_run}
    with pytest.raises(ContractValidationError) as denied:
        artifact(GraphApplication(service).for_consumer(), parameters, ref)
    assert denied.value.reason_code in ("output_not_public", "output_reference_denied")


def test_root_and_target_from_another_session_are_denied(service):
    doc, _, _, _, presentation = frontend_graph(service)
    own = run(service, create(service, doc), inputs={"text": "own"})
    other = run(service, service.create_session(
        doc["workflow_definition_id"], 1, idempotency_key=str(uuid4())), inputs={"text": "foreign"})
    parameters = root_parameters(service, own, doc, presentation)
    foreign_root = root_parameters(service, other, doc, presentation)
    foreign_ref = other["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    app = GraphApplication(service).for_consumer()
    for arguments in (parameters, {**parameters, "run_id": foreign_root["run_id"]}):
        with pytest.raises(ContractValidationError) as denied:
            artifact(app, arguments, foreign_ref)
        assert denied.value.reason_code == "output_reference_denied"


def test_public_view_is_retention_evidence_and_cannot_authorize_artifact_reads(service):
    doc, _, _, append, presentation = frontend_graph(service)
    next(row for row in doc["nodes"] if row["node_binding_id"] == append["node_binding_id"])[
        "public_outputs"] = ["view"]
    final = run(service, create(service, doc), inputs={"text": "visible only through presentation"})
    ref = final["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    app = GraphApplication(service).for_consumer()
    public_view = app.query("output.read", {
        "session_id": final["workflow_session_id"], "workflow_definition_id": doc["workflow_definition_id"],
        "definition_revision": 1, "node_id": append["node_binding_id"], "port_id": "view"})["output"]
    with pytest.raises(ContractValidationError) as denied:
        artifact(app, {"session_id": final["workflow_session_id"],
                       "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
                       "node_id": append["node_binding_id"], "port_id": "view",
                       "run_id": public_view["run_id"]}, ref)
    assert denied.value.reason_code == "output_reference_denied"
    display_parameters = root_parameters(service, final, doc, presentation)
    with pytest.raises(ContractValidationError) as denied:
        artifact(app, display_parameters, {"scope": "artifact", "output_id": next(
            row for row in service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])["node_runs"]
            if row["run_id"] == display_parameters["run_id"])["output_refs"]["display"]})
    assert denied.value.reason_code == "output_reference_denied"


def test_disabled_exporter_keeps_historical_payload_but_grants_no_artifact_reads(service):
    doc, _, _, _, presentation = frontend_graph(service)
    final = run(service, create(service, doc), inputs={"text": "retained"})
    ref = final["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    parameters = root_parameters(service, final, doc, presentation)
    service.configure_capability_packages({
        key: value for key, value in DEFAULT_PACKAGES.items()
        if key not in {"workflow.frontend-business", "workflow.frontend"}})
    app = GraphApplication(service).for_consumer()
    assert app.query("output.read", parameters)["output"]["payload"]["entries"][0]["source_ref"] == ref
    with pytest.raises(ContractValidationError) as denied:
        artifact(app, parameters, ref)
    assert denied.value.reason_code == "output_reference_denied"


def test_public_reference_hooks_are_direct_retained_exports_and_survive_detach():
    refs = [{"scope": "artifact", "output_id": uid(1310)}, {"scope": "artifact", "output_id": uid(1311)}]
    value = {"schema_version": 1, "direct": refs[0], "private": refs[1]}
    types = TypeRegistry()
    types.register(DataTypeDefinition(
        "sample.exports", 1, {"type": "object"}, scope="content",
        references=lambda payload: [payload["direct"], payload["private"]],
        public_references=lambda payload: [payload["direct"]]))
    frozen = types.detached(frozen=True)
    assert frozen.get("sample.exports", 1, scope="content").to_dict()["has_public_references"] is True
    assert frozen.public_artifact_references("sample.exports", 1, value) == [refs[0]]
    types.register(DataTypeDefinition(
        "sample.retention", 1, {"type": "object"}, scope="content", references=lambda payload: refs))
    with pytest.raises(HostContractError) as denied:
        types.public_artifact_references("sample.retention", 1, value)
    assert denied.value.reason_code == "output_reference_denied"
    types.register(DataTypeDefinition(
        "sample.invalid-export", 1, {"type": "object"}, scope="content",
        references=lambda payload: [refs[0]], public_references=lambda payload: [refs[1]]))
    with pytest.raises(HostContractError) as denied:
        types.public_artifact_references("sample.invalid-export", 1, value)
    assert denied.value.reason_code == "host_invalid_references"


def test_artifact_read_does_not_expand_nested_reference_retention(service):
    service.registry.data_types.register(DataTypeDefinition(
        "sample.public-pointer", 1, {"type": "object"}, scope="content",
        references=lambda value: [value["reference"]], public_references=lambda value: [value["reference"]]))
    service.registry.register(NodeDefinition(
        "sample.pointer", "1", "Pointer", "test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "sample.public-pointer"),),
        input_storage="references"), lambda config, inputs, context: {
            "output": {"schema_version": 1, "reference": {
                "scope": "artifact", "output_id": context.input_artifact_refs("input")[0]["output_id"]}}})
    service.registry.register(NodeDefinition(
        "sample.outer", "1", "Outer", "test", {}, {"type": "object"},
        inputs=(NodePort("input", "sample.public-pointer"),),
        outputs=(NodePort("output", "sample.public-pointer"),),
        input_storage="references"), lambda config, inputs, context: {
            "output": {"schema_version": 1, "reference": {
                "scope": "artifact", "output_id": context.input_artifact_refs("input")[0]["output_id"]}}})
    source = node(service.registry, "tools.text", 1320, text="private nested")
    inner = node(service.registry, "sample.pointer", 1321)
    outer = node(service.registry, "sample.outer", 1322)
    outer["public_outputs"] = ["output"]
    doc = document(service.registry, [source, inner, outer], [edge(source, inner, 1320), edge(inner, outer, 1321)],
                   roots=[outer])
    final = run(service, create(service, doc))
    app = GraphApplication(service).for_consumer()
    response = service.read_public_output(final["workflow_session_id"],
        workflow_definition_id=doc["workflow_definition_id"], definition_revision=1,
        node_id=outer["node_binding_id"], port_id="output")["output"]
    parameters = {"session_id": final["workflow_session_id"], "workflow_definition_id": doc["workflow_definition_id"],
                  "definition_revision": 1, "node_id": outer["node_binding_id"], "port_id": "output",
                  "run_id": response["run_id"]}
    direct = artifact(app, parameters, response["payload"]["reference"])
    assert direct["data_type"] == "sample.public-pointer"
    with pytest.raises(ContractValidationError) as denied:
        artifact(app, parameters, direct["value"]["reference"])
    assert denied.value.reason_code == "output_reference_denied"


def test_presentation_waits_for_atomic_append_acceptance(service, monkeypatch):
    doc, _, _, append, presentation = frontend_graph(service)
    entered, release = Event(), Event()
    calls = []
    original = service._save_effects
    presentation_entry = service.registry.get("frontend.presentation", "1")

    def execute(config, inputs, context):
        calls.append(context.node_run_id)
        return presentation_entry.executor(config, inputs, context)

    def delayed(repo, sid, document, event):
        result = original(repo, sid, document, event)
        if event["node_binding_id"] == append["node_binding_id"]:
            entered.set()
            assert release.wait(10)
        return result

    service.registry._nodes[("frontend.presentation", "1")] = replace(presentation_entry, executor=execute)
    monkeypatch.setattr(service, "_save_effects", delayed)
    initial = create(service, doc)
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()), inputs={"text": "accepted"})
    try:
        assert entered.wait(10)
        assert calls == []
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
    final = service.get_session(initial["workflow_session_id"])
    assert final["status"] == "succeeded"
    assert len(calls) == 1
    assert final["head_commit_id"] != initial["head_commit_id"]
    assert final["objects"]["frontend"]["revision"] == 2
    parameters = root_parameters(service, final, doc, presentation)
    ref = final["objects"]["frontend"]["value"]["entries"][0]["source_ref"]
    assert artifact(GraphApplication(service).for_consumer(), parameters, ref)["value"]["text"] == "accepted"


def test_saved_package_selection_does_not_implicitly_enable_new_business_package(tmp_path):
    path = tmp_path / "saved-package-selection.sqlite"
    selected = {key: value for key, value in DEFAULT_PACKAGES.items()
                if key not in {"workflow.frontend-business", "workflow.frontend"}}
    with closing(GraphWorkflowService(path, enabled_packages=selected)) as configured:
        assert configured.registry.get("frontend.presentation", "1") is None
        lock = deepcopy(configured.platform_capabilities()["package_lock"])
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.platform_capabilities()["package_lock"] == lock
        assert reopened.registry.get("frontend.presentation", "1") is None
