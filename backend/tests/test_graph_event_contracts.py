"""Event declarations compile explicit dependency closures and scoped requests."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import (
    GraphCompiler, GraphDiagnosticError, validate_graph_document,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.host_sdk import ObjectBinding

from test_graph_execution import document, edge, identity, node


def binding(*, event_id="frontend.append", schema_version=1, targets=None, **changes):
    return {
        "event_id": event_id, "schema_version": schema_version, "display_name": "Append display",
        "audience": "consumer", "target_node_ids": targets or [identity(2)],
        "payload_schema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        }, **changes,
    }


def event_graph(*, events=None):
    value = document([
        node(1, "workflow.current-input"), node(2, "workflow.output"),
        node(3, config={"text": "ordinary"}), node(4, "workflow.output"),
        node(5, "missing.event-plugin"),
    ], [edge(10, 1, 2), edge(11, 3, 4)])
    value.update(schema_version=2, object_bindings=[], execution_roots=[identity(4)],
                 event_bindings=events if events is not None else [binding()])
    return value


def compiler():
    return GraphCompiler(create_default_registry())


def test_event_compilation_uses_only_declared_targets_and_preserves_definition():
    value = event_graph()
    original = deepcopy(value)
    event = compiler().compile_event(value, "frontend.append", 1, external_inputs={"text": "event"})
    assert event.document == original == value
    assert event.target_node_ids == (identity(2),)
    assert event.ordered_node_ids == (identity(1), identity(2))
    assert set(event.definitions) == {identity(1), identity(2)}
    ordinary = compiler().compile(value, external_inputs={})
    assert ordinary.target_node_ids == (identity(4),)
    assert ordinary.ordered_node_ids == (identity(3), identity(4))


def test_event_control_dependency_and_shared_data_ancestor_execute_once():
    value = event_graph()
    value["nodes"].append(node(6, "workflow.output"))
    value["edges"].append(edge(12, 1, 6))
    value["control_edges"] = [{"edge_id": identity(13), "source_node_id": identity(3),
                               "target_node_id": identity(2)}]
    value["event_bindings"][0]["target_node_ids"] = [identity(6), identity(2)]
    plan = compiler().compile_event(value, "frontend.append", 1, external_inputs={"text": "event"})
    assert plan.target_node_ids == (identity(2), identity(6))
    assert set(plan.ordered_node_ids) == {identity(1), identity(2), identity(3), identity(6)}
    assert plan.ordered_node_ids.count(identity(1)) == 1
    assert plan.ordered_node_ids.index(identity(3)) < plan.ordered_node_ids.index(identity(2))


def test_unrelated_broken_root_cycle_and_external_inputs_do_not_block_event():
    value = event_graph()
    value["nodes"][2] = node(3, "workflow.current-input", {"input_name": "unrelated"})
    value["control_edges"] = [
        {"edge_id": identity(13), "source_node_id": identity(4), "target_node_id": identity(5)},
        {"edge_id": identity(14), "source_node_id": identity(5), "target_node_id": identity(4)},
    ]
    plan = compiler().compile_event(value, "frontend.append", 1, external_inputs={"text": "event"})
    assert plan.ordered_node_ids == (identity(1), identity(2))
    with pytest.raises(GraphDiagnosticError):
        compiler().compile(value, external_inputs={})


def test_empty_ordinary_roots_save_without_activating_event_output():
    value = event_graph()
    value["execution_roots"] = []
    assert validate_graph_document(value) == value
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile(value)
    assert denied.value.reason_code == "graph_no_outputs"
    assert compiler().compile_event(value, "frontend.append", 1).ordered_node_ids
    del value["execution_roots"]
    with pytest.raises(GraphDiagnosticError) as denied:
        validate_graph_document(value)
    assert denied.value.reason_code == "graph_invalid_execution_roots"


def test_empty_event_declarations_keep_legacy_automatic_output_targets():
    value = event_graph(events=[])
    del value["execution_roots"]
    assert compiler().compile(value, external_inputs={"text": "both"}).target_node_ids == (
        identity(2), identity(4))
    del value["event_bindings"]
    assert compiler().compile(value, external_inputs={"text": "both"}).target_node_ids == (
        identity(2), identity(4))


def test_event_identity_is_exact_pair_and_versions_do_not_silently_convert():
    first = binding()
    second = binding(schema_version=2, targets=[identity(4)],
                     payload_schema={"type": "object", "additionalProperties": False})
    value = event_graph(events=[first, second])
    assert compiler().compile_event(value, "frontend.append", 1, {"text": "first"}).target_node_ids == (
        identity(2),)
    assert compiler().compile_event(value, "frontend.append", 2, {}).target_node_ids == (identity(4),)
    for event_id, version in (("missing", 1), ("frontend.append", 3), ("frontend.append", True),
                              ("frontend.append", 0), ([], 1)):
        with pytest.raises(GraphDiagnosticError) as denied:
            compiler().compile_event(value, event_id, version)
        assert denied.value.reason_code == "graph_event_not_declared"


@pytest.mark.parametrize("change", [
    {"unknown": 1}, {"event_id": ""}, {"event_id": " "}, {"event_id": []},
    {"schema_version": True}, {"schema_version": 0}, {"schema_version": 2**53},
    {"display_name": ""}, {"display_name": " "}, {"display_name": "x" * 129},
    {"audience": "public"}, {"audience": []},
    {"target_node_ids": []}, {"target_node_ids": [identity(2), identity(2)]},
    {"target_node_ids": [identity(999)]}, {"target_node_ids": [True]},
    {"payload_schema": True}, {"payload_schema": {"type": "string"}},
    {"payload_schema": {"type": ["object", "null"]}},
    {"payload_schema": {"type": "object", "properties": []}},
])
def test_invalid_event_binding_structure_is_rejected_before_compile(change):
    value = event_graph(events=[binding(**change)])
    with pytest.raises(GraphDiagnosticError) as denied:
        validate_graph_document(value)
    assert denied.value.reason_code == "graph_invalid_event_binding"


def test_duplicate_event_pair_and_v1_event_fields_are_rejected():
    value = event_graph(events=[binding(), binding()])
    with pytest.raises(GraphDiagnosticError) as denied:
        validate_graph_document(value)
    assert denied.value.reason_code == "graph_invalid_event_binding"
    value = document([node(1)])
    value["event_bindings"] = []
    with pytest.raises(GraphDiagnosticError) as denied:
        validate_graph_document(value)
    assert denied.value.reason_code == "graph_invalid_document"


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize("location", ["root", "property", "definition", "array"])
def test_external_schema_references_are_rejected_without_resolution(keyword, location):
    reference = {keyword: "https://unresolved.invalid/schema"}
    schema = {"type": "object"}
    if location == "root":
        schema.update(reference)
    elif location == "property":
        schema["properties"] = {"text": reference}
    elif location == "definition":
        schema["$defs"] = {"external": reference}
    else:
        schema["allOf"] = [reference]
    with pytest.raises(GraphDiagnosticError) as denied:
        validate_graph_document(event_graph(events=[binding(payload_schema=schema)]))
    assert denied.value.reason_code == "graph_invalid_event_binding"


def test_local_schema_references_validate_event_payload_without_mutation():
    schema = {
        "type": "object", "$defs": {"text": {"type": "string", "minLength": 1}},
        "properties": {"text": {"$ref": "#/$defs/text"}},
        "required": ["text"], "additionalProperties": False,
    }
    value = event_graph(events=[binding(payload_schema=schema)])
    inputs = {"text": "local"}
    assert compiler().compile_event(value, "frontend.append", 1, inputs).ordered_node_ids
    assert inputs == {"text": "local"}
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(value, "frontend.append", 1, {"text": ""})
    assert denied.value.reason_code == "graph_event_payload_invalid"
    # A reference-shaped constant is business data, not a schema reference.
    schema["properties"]["literal"] = {"const": {"$ref": "https://literal.invalid"}}
    validate_graph_document(event_graph(events=[binding(payload_schema=schema)]))


def test_unresolved_local_schema_reference_is_a_payload_diagnostic():
    value = event_graph(events=[binding(payload_schema={
        "type": "object", "properties": {"text": {"$ref": "#/$defs/missing"}},
    })])
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(value, "frontend.append", 1, {"text": "event"})
    assert denied.value.reason_code == "graph_event_payload_invalid"


@pytest.mark.parametrize("payload", [
    [], {}, {"text": 1}, {"text": "ok", "extra": True},
    {"text": "ok", "_workflow_frozen_resources": {}},
    {"text": "ok", "bad": float("nan")}, {"text": "ok", "bad": {1}},
])
def test_event_payload_is_strict_named_json_with_declared_schema(payload):
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(event_graph(), "frontend.append", 1, payload)
    assert denied.value.reason_code == "graph_event_payload_invalid"


def test_event_payload_runs_participating_input_validator_and_preserves_reserved_nested_business_key():
    value = event_graph(events=[binding(payload_schema={"type": "object"})])
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(value, "frontend.append", 1, {})
    assert denied.value.reason_code == "graph_external_input_missing"
    payload = {"text": "valid", "nested": {"_workflow_frozen_resources": "business data"}}
    assert compiler().compile_event(value, "frontend.append", 1, payload).ordered_node_ids


def test_event_compilation_keeps_full_package_and_object_default_contract_checks():
    value = event_graph()
    value["package_lock"] = [{"package_id": "missing.event", "version": "1.0.0"}]
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(value, "frontend.append", 1, {"text": "event"})
    assert denied.value.reason_code == "graph_missing_package"
    value["package_lock"] = []
    value["object_bindings"] = [ObjectBinding(
        "dormant", "missing.type", 1, "shared", readers=(identity(5),)).to_dict()]
    with pytest.raises(GraphDiagnosticError) as denied:
        compiler().compile_event(value, "frontend.append", 1, {"text": "event"})
    assert denied.value.reason_code == "host_unknown_type"


class EventServiceProbe:
    def __init__(self):
        self.calls = []

    def _response(self, method, session_id, parameters):
        self.calls.append((method, session_id, deepcopy(parameters)))
        return {"workflow_definition_id": identity(900), "definition_revision": 1,
                "workflow_session_id": session_id, "revision": 2}

    def submit_event(self, session_id, **parameters):
        return self._response("submit_event", session_id, parameters)

    def submit_consumer_event(self, session_id, **parameters):
        response = self._response("submit_consumer_event", session_id, parameters)
        response["session_revision"] = response.pop("revision")
        return {"schema_version": 1, "kind": "workflow.consumer.receipt",
                "receipt": deepcopy(response), "consumer": response}

    def list_event_bindings(self, session_id, **parameters):
        return self._response("list_event_bindings", session_id, parameters)

    def list_consumer_event_bindings(self, session_id, **parameters):
        return self._response("list_consumer_event_bindings", session_id, parameters)

    def read_event(self, session_id, **parameters):
        return self._response("read_event", session_id, parameters)

    def read_consumer_event(self, session_id, **parameters):
        return self._response("read_consumer_event", session_id, parameters)


def request():
    return {
        "session_id": identity(901), "workflow_definition_id": identity(900), "definition_revision": 1,
        "event_id": "frontend.append", "event_schema_version": 1, "payload": {"text": "event"},
        "expected_revision": 1, "idempotency_key": "opaque:event-command",
    }


def test_event_operations_call_distinct_authorities_and_discovery_scopes():
    service = EventServiceProbe()
    management = GraphApplication(service)
    consumer = management.for_consumer()
    original = request()
    submitted = management.command("event.submit", original)
    public = consumer.command("consumer.event.submit", original)
    assert original == request()
    assert submitted["receipt"]["authority"] == public["receipt"]["authority"] == "service_receipt"
    assert submitted["receipt"]["idempotency_key"] == "opaque:event-command"
    assert public["receipt"]["accepted"]["session_revision"] == 2
    scope = {"session_id": original["session_id"], "workflow_definition_id": original["workflow_definition_id"],
             "definition_revision": 1}
    management.query("event.bindings", scope)
    consumer.query("consumer.event.bindings", scope)
    management.query("event.read", {"session_id": original["session_id"], "chain_id": identity(903)})
    consumer.query("consumer.event.read", {"session_id": original["session_id"], "chain_id": identity(903)})
    assert [row[0] for row in service.calls] == [
        "submit_event", "submit_consumer_event", "list_event_bindings", "list_consumer_event_bindings",
        "read_event", "read_consumer_event"]
    assert all(row[1] == identity(901) and "session_id" not in row[2] for row in service.calls)
    description = consumer.describe()
    assert "consumer.event.submit" in {item["name"] for item in description["commands"]}
    assert {"consumer.event.bindings", "consumer.event.read"} <= {item["name"] for item in description["queries"]}
    assert {"event.bindings", "event.read"}.isdisjoint(item["name"] for item in description["queries"])


@pytest.mark.parametrize("operation", ["event.submit", "event.bindings", "event.read"])
def test_consumer_cannot_invoke_management_event_operations(operation):
    app = GraphApplication(EventServiceProbe()).for_consumer()
    with pytest.raises(ContractValidationError) as denied:
        (app.command if operation.endswith("submit") else app.query)(operation, {})
    assert denied.value.reason_code == "application_scope_denied"


@pytest.mark.parametrize("change", [
    {"target_node_ids": [identity(5)]}, {"node_id": identity(5)}, {"schema_version": 1},
    {"audience": "management"}, {"event_id": " "}, {"event_schema_version": True},
    {"event_schema_version": 0}, {"event_schema_version": 2**53},
    {"definition_revision": True}, {"expected_revision": True}, {"idempotency_key": " "},
    {"payload": []}, {"payload": {"_workflow_frozen_resources": {}}},
    {"payload": {"non_json": {1}}}, {"payload": {"non_finite": float("nan")}},
])
def test_event_request_cannot_override_targets_identity_scope_or_payload_format(change):
    service = EventServiceProbe()
    with pytest.raises(ContractValidationError) as denied:
        GraphApplication(service).for_consumer().command("consumer.event.submit", {**request(), **change})
    assert denied.value.reason_code == "invalid_request"
    assert service.calls == []


@pytest.mark.parametrize("consumer", [False, True])
def test_generic_http_and_compatibility_routes_dispatch_same_scoped_event_methods(consumer):
    service = EventServiceProbe()
    prefix = "consumer." if consumer else ""
    path_prefix = "/consumer" if consumer else ""
    parameters = request()
    status, generic = dispatch_graph(service, "POST", "/api/graph" + path_prefix + "/commands", {
        "operation": prefix + "event.submit", "parameters": parameters})
    assert status == 202
    routed_parameters = {key: value for key, value in parameters.items() if key != "session_id"}
    path = "/api/graph/sessions/" + parameters["session_id"] + path_prefix + "/events"
    assert dispatch_graph(service, "POST", path + "/submit", routed_parameters)[0] == 202
    assert service.calls[-1] == service.calls[-2]
    scope = {"workflow_definition_id": parameters["workflow_definition_id"], "definition_revision": 1}
    dispatch_graph(service, "POST", "/api/graph" + path_prefix + "/queries", {
        "operation": prefix + "event.bindings", "parameters": {**scope, "session_id": parameters["session_id"]}})
    assert dispatch_graph(service, "POST", path + "/bindings", scope)[0] == 200
    assert service.calls[-1] == service.calls[-2]
    dispatch_graph(service, "POST", path + "/read", {"chain_id": identity(903)})
    assert service.calls[-1][0] == ("read_consumer_event" if consumer else "read_event")
    with pytest.raises(ContractValidationError) as denied:
        dispatch_graph(service, "POST", path + "/submit", parameters)
    assert denied.value.reason_code == "invalid_request"
    assert generic["receipt"]["accepted"]["workflow_session_id"] == parameters["session_id"]
