"""A second disk tool uses the frozen public adapter without core changes."""

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import edge, node
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import EVENT_ID, KEY, VariableToolTransport, build_graph


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "example.numeric-clamp"
LOADER = "example.numeric-clamp-tools"
TARGET = "example.numeric-clamp"
METRICS_PACKAGE = "example.text-metrics"
METRICS_LOADER = "example.text-metrics-tools"
CORE_FILES = (
    "tool_binding_contract.py", "tool_bindings.py", "graph_tool_host.py",
    "graph_contracts.py", "runtime.py", "agent_executor.py",
)


def install_examples(tmp_path, *, with_metrics=False):
    root = tmp_path / "packages/workflow"
    names = ["numeric-clamp-1.0.0"]
    if with_metrics:
        names.append("text-metrics-1.0.0")
    for name in names:
        shutil.copytree(ROOT / "examples/workflow-packages" / name, root / name)


def frozen_core_hashes():
    return {name: sha256((ROOT / "backend/src/phase1_agent" / name).read_bytes()).hexdigest()
            for name in CORE_FILES}


def add_loaders(service, document, parts, *, with_metrics):
    components = [LOADER, METRICS_LOADER] if with_metrics else [LOADER]
    for index, component in enumerate(components, 601):
        loader = node(service.registry, component, index, component_version="1",
                      agent_node_id=parts["execute"]["node_binding_id"])
        document["nodes"].append(loader)
        connection = edge(
            loader, parts["assembly"], index + 1600,
            source_port="materials", target_port="materials")
        connection["order"] = index
        document["edges"].append(connection)
    document.update(schema_version=3, edit_sequence=0,
                    package_lock=deepcopy(list(service.registry.execution_package_lock)))
    for entry in document["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}


def configure_and_register(application, service, *, with_metrics):
    document, parts = build_graph(service, with_normal=False)
    add_loaders(service, document, parts, with_metrics=with_metrics)
    saved = application.command("definition.update", {
        "workflow_definition_id": document["workflow_definition_id"],
        "document": document, "expected_edit_sequence": 0, "idempotency_key": str(uuid4()),
    })["result"]
    assert saved["outcome"] == "saved"
    revision = saved["definition_revision"]
    reread = application.query("definition.read", {"identity": document["workflow_definition_id"]})
    for loader in (LOADER, METRICS_LOADER) if with_metrics else (LOADER,):
        assert next(row["config"] for row in reread["nodes"] if row["component_id"] == loader) == next(
            row["config"] for row in document["nodes"] if row["component_id"] == loader)
    session = application.command("session.create", {
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": revision, "idempotency_key": str(uuid4()),
    })["result"]
    started = application.command("event.submit", {
        "session_id": session["workflow_session_id"],
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": revision, "event_id": EVENT_ID, "event_schema_version": 1,
        "payload": {}, "expected_revision": session["revision"], "idempotency_key": str(uuid4()),
    })["result"]
    service.wait(started["active_chain_run_id"])
    session = application.query("session.read", {"session_id": session["workflow_session_id"]})
    return document, parts, reread, session


def run(application, service, session):
    sid = session["workflow_session_id"]
    started = application.command("run.start", {
        "session_id": sid, "expected_revision": session["revision"],
        "idempotency_key": str(uuid4()), "inputs": {"text": "Compute both independent tools"},
    })["result"]
    chain = started["active_chain_run_id"]
    service.wait(chain)
    return application.query("session.read", {"session_id": sid}), application.query(
        "run.read", {"session_id": sid, "chain_id": chain}), chain


def settled(run_record):
    return [fact["payload"]["payload"] for fact in run_record["runtime_facts"]
            if isinstance(fact.get("payload"), dict) and fact["payload"].get("kind") == "tool_settled"]


def test_two_independent_disk_tools_and_variable_tool_formal_execution_reopen(tmp_path):
    baseline = frozen_core_hashes()
    install_examples(tmp_path, with_metrics=True)
    database = tmp_path / "independent-tools.sqlite"
    selection = {**DEFAULT_PACKAGES, PACKAGE: "1.0.0", METRICS_PACKAGE: "1.0.0"}
    transport = VariableToolTransport([
        ("score_set", {"value": 13}),
        ("numeric_clamp", {"value": 27.5, "minimum": -2, "maximum": 8}),
        ("text_metrics", {"text": "a b\nc", "include_spaces": False}),
        ("score_read", {}),
        ("final_answer", {"answer": {"text": "both computed"}}),
    ])
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        assert service.registry.get(LOADER, "1") is None
        discovered = service.platform_capabilities()["local_package_sources"]
        assert discovered["diagnostics"] == []
        assert {row["manifest"]["package_id"] for row in discovered["packages"]} == {PACKAGE, METRICS_PACKAGE}
        application.command("packages.configure", {"enabled_packages": selection})
        service._service_options[MODEL_SERVICE_REF]["transport_factory"] = transport.factory
        ModelDatabaseFixture.write(service, 1)
        document, parts, reread, before = configure_and_register(
            application, service, with_metrics=True)
        view, history, chain = run(application, service, before)
        assert view["status"] == "succeeded", view["chains"]
        assert len(transport.calls) == 5
        assert view["objects"][KEY]["value"]["value"] == 13
        assert view["objects"][KEY]["revision"] == before["objects"][KEY]["revision"] + 1
        definitions = {row["function"]["name"]: row["function"] for row in transport.calls[0]["tools"]}
        assert {"numeric_clamp", "text_metrics", "score_read", "score_set", "final_answer"} <= definitions.keys()
        clamp = definitions["numeric_clamp"]
        assert clamp["parameters"]["required"] == ["value", "minimum", "maximum"]
        assert clamp["parameters"]["additionalProperties"] is False
        assert all(row["type"] == "number" for row in clamp["parameters"]["properties"].values())
        prompt = next(row["outputs"]["materials"] for row in view["nodes"]
                      if row["node_binding_id"] == next(
                          entry["node_binding_id"] for entry in document["nodes"]
                          if entry["component_id"] == LOADER))
        assert {row["purpose"] for row in prompt["items"]} == {"tool-description", "tool-schema"}
        assert all(row["source"]["tool_name"] == "numeric_clamp" and row["source"]["agent_node_id"]
                   == parts["execute"]["node_binding_id"] for row in prompt["items"])
        messages = str(transport.calls[0]["messages"])
        assert clamp["description"] in messages
        assert canonical_bytes(clamp["parameters"]).decode() in messages
        facts = settled(history)
        contents = [row["message"]["blocks"][0]["content"] for row in facts[:4]]
        assert contents[1:3] == [{"value": 8, "clamped": True}, {"characters": 4, "lines": 2}]
        for content in (contents[0], contents[3]):
            assert content == {
                "variable": "score", "object_key": KEY, "value": 13,
                "revision": before["objects"][KEY]["revision"] + 1}
        assert all(row["outcome"] == "success" for row in facts)
        assert len({row["tool_execution_id"] for row in facts}) == len(facts)
        sid = before["workflow_session_id"]
    with closing(WorkflowHost(database)) as reopened:
        application = GraphApplication(reopened)
        assert application.query("definition.read", {"identity": document["workflow_definition_id"]}) == reread
        assert application.query("run.read", {"session_id": sid, "chain_id": chain}) == history
        assert application.query("session.read", {"session_id": sid}) == view
        assert reopened.graph_management_service.registry.get(TARGET, "1") is not None
        assert reopened.graph_management_service.registry.get(METRICS_LOADER, "1") is not None
    assert frozen_core_hashes() == baseline


def test_numeric_package_resolves_own_dependencies_without_default_selection(tmp_path):
    install_examples(tmp_path)
    with closing(WorkflowHost(tmp_path / "exact-selection.sqlite")) as host:
        application = GraphApplication(host)
        application.command("packages.configure", {"enabled_packages": {PACKAGE: "1.0.0"}})
        service = host.graph_service
        assert service.registry.get(LOADER, "1") is not None
        assert service.registry.get("agents.execute", "10") is not None
        assert service.registry.get("example.text-metrics", "1") is None
        lock = {row["package_id"] for row in service.registry.package_lock}
        assert {PACKAGE, "workflow.content", "workflow.agents"} <= lock
        assert "workflow.tavern" not in lock and "workflow.frontend" not in lock


def test_model_call_reaches_package_bounds_rule_without_accepting_a_tool_result(tmp_path):
    install_examples(tmp_path)
    transport = VariableToolTransport([
        ("numeric_clamp", {"value": 1, "minimum": 9, "maximum": 2}),
    ])
    with closing(WorkflowHost(tmp_path / "bounds-model.sqlite")) as host:
        application, service = GraphApplication(host), host.graph_service
        application.command("packages.configure", {
            "enabled_packages": {**DEFAULT_PACKAGES, PACKAGE: "1.0.0"}})
        service._service_options[MODEL_SERVICE_REF]["transport_factory"] = transport.factory
        ModelDatabaseFixture.write(service, 1)
        _, _, _, session = configure_and_register(application, service, with_metrics=False)
        view, history, _ = run(application, service, session)
        assert view["status"] == "failed", view["chains"]
        assert len(transport.calls) == 1
        failures = [fact["payload"]["payload"] for fact in history["runtime_facts"]
                    if isinstance(fact.get("payload"), dict)
                    and fact["payload"].get("kind") == "execution_failed"]
        assert len(failures) == 1
        assert failures[0]["code"] == "tool_contract_error"
        assert failures[0]["category"] == "contract"
        assert not any(row["outcome"] == "success" for row in settled(history))
        assert view["objects"][KEY] == session["objects"][KEY]


@pytest.mark.parametrize("arguments", [
    {"value": 1, "minimum": 9, "maximum": 2},
    {"value": True, "minimum": 0, "maximum": 2},
    {"value": "1", "minimum": 0, "maximum": 2},
    {"value": float("nan"), "minimum": 0, "maximum": 2},
    {"value": float("inf"), "minimum": 0, "maximum": 2},
    {"value": float("-inf"), "minimum": 0, "maximum": 2},
], ids=["reversed-bounds", "boolean", "string", "nan", "infinity", "negative-infinity"])
def test_numeric_package_owns_finite_number_and_bounds_validation(tmp_path, arguments):
    install_examples(tmp_path)
    with closing(WorkflowHost(tmp_path / "invalid.sqlite")) as host:
        application = GraphApplication(host)
        application.command("packages.configure", {"enabled_packages": {PACKAGE: "1.0.0"}})
        entry = host.graph_service.registry.get(LOADER, "1")
        spec = entry.tool_binding.normalize(entry.definition.default_config)[0]
        with pytest.raises(ContractValidationError) as invalid:
            entry.tool_binding.call(spec, arguments)
        assert invalid.value.reason_code == (
            "numeric_clamp_bounds_invalid" if arguments["minimum"] > arguments["maximum"]
            else "numeric_clamp_arguments_invalid")
