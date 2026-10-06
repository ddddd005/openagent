"""Trusted readers declare exact resources before graph effects are accepted."""

from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from test_graph_service import create, document, edge, node, run, service
from resource_fixtures import resource


def save_resource(service, text):
    record = resource(text)
    service.save_global_content(record, expected_revision=0, idempotency_key=str(uuid4()))
    return record


def register_reader(service, fields, *, declaration=True, requested_fields=None):
    def execute(config, inputs, context):
        records = [context.host_call("resources:read", "global-content", {"resource_id": config[field]})
                   for field in requested_fields or fields]
        return {"output": text_value(" | ".join(record["members"][0]["text"] for record in records))}

    callback = ((lambda config: [{"kind": "global-content", "resource_id": config[field]} for field in fields])
                if declaration is True else declaration)
    service.registry.register(NodeDefinition(
        "test.resource-reader", "1", "Resources", "Test", {},
        {"type": "object", "required": list(fields), "additionalProperties": False,
         "properties": {field: {"type": "string"} for field in fields}},
        inputs=(NodePort("input", "TEXT", required=False),), outputs=(NodePort("output", "TEXT"),),
        capabilities=("resources:read",),
    ), execute, resource_dependencies_declaration=callback)


def reader_graph(service, values):
    source = node(service.registry, "test.resource-reader", 2, **values)
    output = node(service.registry, "output", 3)
    return document([source, output], [edge(source, output, 1)])


@pytest.mark.parametrize("first_field", ["resource_id", "selected_content_id"])
def test_custom_field_and_multiple_exact_resources_are_supported(service, first_field):
    first, second = save_resource(service, "first"), save_resource(service, "second")
    register_reader(service, (first_field, "another_content_id"))
    doc = reader_graph(service, {first_field: first["resource_id"], "another_content_id": second["resource_id"]})
    completed = run(service, create(service, doc))
    assert completed["status"] == "succeeded"
    assert completed["nodes"][-1]["outputs"]["output"] == text_value("first | second")
    assert "resource_dependencies_declaration" not in service.registry.get("test.resource-reader", "1").definition.to_dict()


@pytest.mark.parametrize("declaration,reason", [
    (None, "graph_resource_dependencies_undeclared"),
    (lambda config: {"resource_id": config["selected_content_id"]}, "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "global-content", "resource_id": "invalid"}], "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "unsupported", "resource_id": config["selected_content_id"]}], "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "global-content", "resource_id": str(uuid4())}], "global_content_missing"),
])
def test_missing_or_invalid_dependencies_reject_before_any_node_effect(service, declaration, reason):
    content = save_resource(service, "content")
    register_reader(service, ("selected_content_id",), declaration=declaration)
    doc = reader_graph(service, {"selected_content_id": content["resource_id"]})
    writer = node(service.registry, "variable-register", 1, name="counter", hasInitialValue=True, initialValue="initial")
    doc["nodes"].insert(0, writer)
    doc["edges"].append(edge(writer, doc["nodes"][1], 2, source_port="text"))
    initial = create(service, doc)
    with pytest.raises(ContractValidationError) as failure:
        service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="start")
    assert failure.value.reason_code == reason
    assert failure.value.diagnostics[0]["node_id"] == doc["nodes"][1]["node_binding_id"]
    unchanged = service.get_session(initial["workflow_session_id"])
    assert unchanged == initial
    assert unchanged["data"]["values"] == {}


def test_runtime_request_cannot_escape_declared_resources(service):
    first, second = save_resource(service, "first"), save_resource(service, "second")
    register_reader(service, ("resource_id", "another_content_id"),
                    declaration=lambda config: [{"kind": "global-content", "resource_id": config["resource_id"]}])
    completed = run(service, create(service, reader_graph(service, {
        "resource_id": first["resource_id"], "another_content_id": second["resource_id"],
    })))
    assert completed["status"] == "failed"
    assert completed["chains"][-1]["diagnostic"]["code"] == "graph_resource_dependency_undeclared"
    assert completed["nodes"][-1]["outputs"] == {}


def test_declared_resources_remain_frozen_across_pause_and_resume(service):
    first, second = save_resource(service, "first v1"), save_resource(service, "second v1")
    register_reader(service, ("selected_content_id", "another_content_id"))
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return {"output": text_value("ready")}

    service.registry.register(NodeDefinition("test.resource-gate", "1", "Gate", "Test", {},
        {"type": "object"}, outputs=(NodePort("output", "TEXT"),)), gate)
    doc = reader_graph(service, {"selected_content_id": first["resource_id"], "another_content_id": second["resource_id"]})
    gate_node = node(service.registry, "test.resource-gate", 1)
    doc["nodes"].insert(0, gate_node)
    doc["edges"].append(edge(gate_node, doc["nodes"][1], 2))
    initial = create(service, doc)
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="start")
    try:
        assert entered.wait(10)
        current = service.get_session(initial["workflow_session_id"])
        service.control(current["workflow_session_id"], action="pause", expected_revision=current["revision"], idempotency_key="pause")
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
    paused = service.get_session(initial["workflow_session_id"])
    assert paused["status"] == "paused"
    for record in (first, second):
        changed = deepcopy(record)
        changed["revision"] = 2
        changed["members"][0]["text"] = changed["members"][0]["text"].replace("v1", "v2")
        service.save_global_content(changed, expected_revision=1, idempotency_key=str(uuid4()))
    service.control(paused["workflow_session_id"], action="resume", expected_revision=paused["revision"], idempotency_key="resume")
    service.wait(started["active_chain_run_id"])
    completed = service.get_session(initial["workflow_session_id"])
    assert completed["nodes"][-1]["outputs"]["output"] == text_value("first v1 | second v1")
    next_result = run(service, completed)
    assert next_result["nodes"][-1]["outputs"]["output"] == text_value("first v2 | second v2")


def test_older_single_record_freeze_is_read_by_exact_identity_on_resume(service, monkeypatch):
    content = save_resource(service, "old frozen")
    source = node(service.registry, "global-content", 1, resource_id=content["resource_id"])
    output = node(service.registry, "output", 3, mode="prompt")
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return {}

    service.registry.register(NodeDefinition("test.old-freeze-gate", "1", "Gate", "Test", {},
        {"type": "object"}, is_output=True), gate)
    target = node(service.registry, "test.old-freeze-gate", 0)
    original = service._preflight_capabilities

    def older_freeze(repo, sid, plan):
        frozen = original(repo, sid, plan)
        frozen.pop("history_heads")
        frozen["content"] = {node_id: next(iter(records.values())) for node_id, records in frozen["content"].items()}
        return frozen

    monkeypatch.setattr(service, "_preflight_capabilities", older_freeze)
    initial = create(service, document([target, source, output], [edge(source, output, 1)]))
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="start")
    try:
        assert entered.wait(10)
        current = service.get_session(initial["workflow_session_id"])
        service.control(current["workflow_session_id"], action="pause", expected_revision=current["revision"], idempotency_key="pause")
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
    paused = service.get_session(initial["workflow_session_id"])
    changed = deepcopy(content)
    changed.update(revision=2)
    changed["members"][0]["text"] = "new live"
    service.save_global_content(changed, expected_revision=1, idempotency_key=str(uuid4()))
    service.control(paused["workflow_session_id"], action="resume", expected_revision=paused["revision"], idempotency_key="resume")
    service.wait(started["active_chain_run_id"])
    completed = service.get_session(initial["workflow_session_id"])
    assert completed["status"] == "succeeded"
    assert completed["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "old frozen"


def test_unreachable_resource_reader_does_not_require_a_declaration(service):
    register_reader(service, ("selected_content_id",), declaration=None)
    source, output = node(service.registry, "text", 1, text="reachable"), node(service.registry, "output", 3)
    dormant = node(service.registry, "test.resource-reader", 2, selected_content_id=str(uuid4()))
    completed = run(service, create(service, document([source, dormant, output], [edge(source, output, 1)])))
    assert completed["status"] == "succeeded"
    assert completed["nodes"][1]["status"] == "idle"
