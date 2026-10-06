"""Optional producer outputs retain known failure evidence without weakening storage."""

from contextlib import closing
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.graph_records import is_graph_record, validate_graph_bundle
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore


def uid(number):
    return str(UUID(int=number, version=4))


def node(registry, component, number, **config):
    entry = registry.get(component, "1")
    return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
            "title": component, "position": {"x": number * 100, "y": 0},
            "config": {**deepcopy(entry.definition.default_config), **config}}


def edge(source, target, number, source_port="output", target_port="input", order=0):
    return {"edge_id": uid(1000 + number), "source_node_id": source["node_binding_id"],
            "source_port_id": source_port, "target_node_id": target["node_binding_id"],
            "target_port_id": target_port, "order": order}


def create(service, nodes, edges):
    document = {"schema_version": 1, "workflow_definition_id": str(uuid4()), "revision": 1,
                "name": "Missing optional output", "nodes": nodes, "edges": edges}
    service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def start(service, initial):
    return service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                         idempotency_key="execute-once")["active_chain_run_id"]


def unresolved(edge):
    return {key: edge[key] for key in
            ("edge_id", "source_node_id", "source_port_id", "target_port_id", "order")}


@pytest.fixture
def service(tmp_path):
    def no_model(*args, **kwargs):
        raise AssertionError("Zero-Agent regression must not request a model")
    with closing(GraphWorkflowService(tmp_path / "evidence.sqlite", registry=create_default_registry(),
                                      model_factory=no_model)) as instance:
        yield instance


def simple_graph(service):
    producer = node(service.registry, "workflow.variable-register", 1, name="pending", hasInitialValue=False)
    consumer = node(service.registry, "workflow.output", 2)
    link = edge(producer, consumer, 1, source_port="text")
    return create(service, [producer, consumer], [link]), link


def test_optional_output_failure_retains_diagnostic_effect_and_replay(service):
    initial, link = simple_graph(service)
    sid = initial["workflow_session_id"]
    chain_id = start(service, initial)
    service.wait(chain_id)
    view = service.get_session(sid)
    history = service.get_run(sid, chain_id)
    producer, failed = history["node_runs"]
    assert view["status"] == history["chain"]["status"] == failed["status"] == "failed"
    assert producer["status"] == "succeeded" and producer["output_refs"] == {}
    assert producer["effects"] and view["data"]["values"]["pending"] == {"type": "string", "source": "unassigned"}
    assert failed["input_values"] == failed["input_refs"] == {}
    assert failed["effects"] == failed["reads"] == []
    for diagnostic in (failed["diagnostic"], history["chain"]["diagnostic"]):
        assert diagnostic["code"] == "graph_linked_output_missing"
        assert diagnostic["node_id"] == link["target_node_id"]
        assert diagnostic["port_id"] == link["target_port_id"]
        assert diagnostic["edge_id"] == link["edge_id"]
    assert failed["diagnostic"]["unresolved_inputs"] == [unresolved(link)]
    assert start(service, initial) == chain_id
    assert service.get_run(sid, chain_id) == history
    path, registry = service.database, service.registry
    service.close()
    with closing(GraphWorkflowService(path, registry=registry)) as restored:
        assert restored.get_run(sid, chain_id) == history
        assert start(restored, initial) == chain_id
        current = restored.get_session(sid)
        closed = restored.control(sid, action="close", expected_revision=current["revision"],
                                  idempotency_key="close-once")
        assert restored.control(sid, action="close", expected_revision=current["revision"],
                                idempotency_key="close-once") == closed
        assert closed["can_submit"] and closed["data"] == view["data"]
        closed_history = restored.get_run(sid, chain_id)
        assert closed_history["node_runs"][1]["status"] == "closed"
        assert closed_history["node_runs"][1]["diagnostic"] == failed["diagnostic"]
        assert closed_history["node_runs"][0] == producer
    with closing(GraphWorkflowService(path, registry=registry)) as restored:
        assert restored.get_run(sid, chain_id) == closed_history


def test_multiple_edges_keep_available_refs_without_claiming_partial_input(service):
    calls = []
    definition = NodeDefinition("test.multiple-input", "1", "Multiple input", "Test", {},
                                {"type": "object", "additionalProperties": False},
                                inputs=(NodePort("head", "TEXT"), NodePort("items", "TEXT", multiple=True),
                                        NodePort("later", "TEXT")),
                                outputs=(NodePort("output", "TEXT"),))
    def execute(config, inputs, context):
        calls.append(inputs)
        return {"output": text_value("must not run")}
    service.registry.register(definition, execute)
    text = node(service.registry, "workflow.text", 1, text="available")
    left = node(service.registry, "workflow.variable-register", 2, name="left", hasInitialValue=False)
    right = node(service.registry, "workflow.variable-register", 3, name="right", hasInitialValue=False)
    consumer = node(service.registry, definition.component_id, 4)
    output = node(service.registry, "workflow.output", 5)
    other = node(service.registry, "workflow.text", 6, text="other")
    other_output = node(service.registry, "workflow.output", 7)
    available = [edge(text, consumer, 1, target_port="head"),
                 edge(text, consumer, 2, target_port="items", order=0)]
    missing = [edge(left, consumer, 3, "text", "items", 1),
               edge(right, consumer, 4, "text", "items", 2),
               edge(right, consumer, 5, "text", "later")]
    initial = create(service, [text, left, right, consumer, output, other, other_output],
                     available + missing + [edge(consumer, output, 6), edge(other, other_output, 7)])
    chain_id = start(service, initial)
    service.wait(chain_id)
    history = service.get_run(initial["workflow_session_id"], chain_id)
    runs = history["node_runs"]
    assert [run["status"] for run in runs] == ["succeeded"] * 3 + ["failed"] + ["prepared"] * 3
    assert calls == []
    failed = runs[3]
    assert failed["input_values"] == {"head": text_value("available")}
    for link in available:
        assert failed["input_refs"][link["target_port_id"]] == [{
            "edge_id": link["edge_id"], "order": link["order"], "output_id": runs[0]["output_refs"]["output"]}]
    assert failed["diagnostic"]["edge_id"] == missing[0]["edge_id"]
    assert failed["diagnostic"]["unresolved_inputs"] == [unresolved(link) for link in missing]
    assert history["chain"]["next_node_index"] == 3
    assert all(run["effects"] for run in runs[1:3])
    values = service.get_session(initial["workflow_session_id"])["data"]["values"]
    assert values["left"] == values["right"] == {"type": "string", "source": "unassigned"}
    with closing(SqliteStore(service.database)) as store:
        bundle = {kind: [row for row in rows if is_graph_record(kind, row)]
                  for kind, rows in store.read_bundle(include_graph=True).items()}
    persisted = next(row for row in bundle["node_run"] if row["run_id"] == failed["run_id"])
    persisted["input_refs"].pop("items")
    with pytest.raises(ContractValidationError, match="Failed input evidence is incomplete"):
        validate_graph_bundle(bundle)


@pytest.mark.parametrize("forgery", ["status", "source_node", "source_port", "target_port",
                                   "order", "diagnostic_edge", "duplicate", "missing", "input_value",
                                   "phantom_output", "source_run", "published_source"])
def test_unresolved_evidence_cannot_escape_failed_dependency(service, forgery):
    initial, link = simple_graph(service)
    chain_id = start(service, initial)
    service.wait(chain_id)
    with closing(SqliteStore(service.database)) as store:
        bundle = {kind: [row for row in rows if is_graph_record(kind, row)]
                  for kind, rows in store.read_bundle(include_graph=True).items()}
    failed = next(row for row in bundle["node_run"] if row["node_binding_id"] == link["target_node_id"])
    diagnostic = failed["diagnostic"]
    item = diagnostic["unresolved_inputs"][0]
    if forgery == "status":
        failed["status"] = "succeeded"
    elif forgery == "source_node":
        item["source_node_id"] = link["target_node_id"]
    elif forgery == "source_port":
        item["source_port_id"] = "invented"
    elif forgery == "target_port":
        item["target_port_id"] = "invented"
    elif forgery == "order":
        item["order"] += 1
    elif forgery == "diagnostic_edge":
        diagnostic["edge_id"] = uid(999)
    elif forgery == "duplicate":
        diagnostic["unresolved_inputs"].append(deepcopy(item))
    elif forgery == "input_value":
        failed["input_values"]["input"] = text_value("not received")
    elif forgery == "phantom_output":
        failed["input_refs"]["input"] = [{"edge_id": link["edge_id"], "output_id": uid(999), "order": 0}]
    elif forgery == "source_run":
        next(row for row in bundle["node_run"] if row["node_binding_id"] == link["source_node_id"])["status"] = "closed"
    elif forgery == "published_source":
        bundle["chain_run"][0]["outputs"][link["source_node_id"]]["text"] = uid(999)
    else:
        diagnostic["unresolved_inputs"] = []
    with pytest.raises(ContractValidationError) as error:
        validate_graph_bundle(bundle)
    assert error.value.reason_code == "storage_contract_violation"


@pytest.mark.parametrize("event_kind", ["running", "succeeded", "failed"])
def test_only_known_missing_output_failure_can_omit_durable_inputs(service, monkeypatch, event_kind):
    initial, _ = simple_graph(service)
    original = service._node_event
    def callback(sid, chain_id, event):
        if event["event"] == "failed":
            event["event"] = event_kind
            if event_kind == "failed":
                event["diagnostic"]["code"] = "graph_node_failed"
        return original(sid, chain_id, event)
    monkeypatch.setattr(service, "_node_event", callback)
    chain_id = start(service, initial)
    with pytest.raises(ContractValidationError) as error:
        service.wait(chain_id)
    assert error.value.reason_code == "storage_contract_violation"
    history = service.get_run(initial["workflow_session_id"], chain_id)
    assert history["chain"]["status"] == "recovery_unavailable"
    assert history["chain"]["diagnostic"]["reason_code"] == "execution_result_unknown"
    assert history["node_runs"][1]["status"] == "prepared"
    assert history["node_runs"][1]["revision"] == 1
    assert history["node_runs"][0]["status"] == "succeeded" and history["node_runs"][0]["effects"]


@pytest.mark.parametrize("boundary", ["after_graph_write", "before_commit"])
def test_failure_persistence_fault_rolls_back_and_remains_unknown(service, monkeypatch, boundary):
    initial, _ = simple_graph(service)
    original = service._node_event
    def inject(actual):
        if actual == boundary:
            raise RuntimeError("failure evidence transaction fault")
    def callback(sid, chain_id, event):
        if event["event"] != "failed":
            return original(sid, chain_id, event)
        service._fault_injector = inject
        try:
            return original(sid, chain_id, event)
        finally:
            service._fault_injector = None
    monkeypatch.setattr(service, "_node_event", callback)
    chain_id = start(service, initial)
    with pytest.raises(RuntimeError, match="failure evidence transaction fault"):
        service.wait(chain_id)
    history = service.get_run(initial["workflow_session_id"], chain_id)
    assert history["chain"]["status"] == "recovery_unavailable"
    assert history["chain"]["diagnostic"]["reason_code"] == "execution_result_unknown"
    assert history["chain"]["next_node_index"] == 1
    assert history["node_runs"][0]["effects"]
    failed = history["node_runs"][1]
    assert failed["status"] == "prepared" and failed["revision"] == 1
    assert failed["diagnostic"] is None and failed["input_refs"] == {}
    with closing(SqliteStore(service.database)) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM idempotency WHERE operation='graph.node.failed'").fetchone()[0] == 0
