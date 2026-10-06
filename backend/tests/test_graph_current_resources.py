"""Current resource identities integrate with execution, checkpoints and fork."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import global_resource_reference, legacy_content_to_current
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.storage import SqliteStore
from test_graph_service import create, document, edge, node, run
from test_workbench_resources import resource


def identity(record):
    return ResourceIdentity(record["scope"], record["type_id"], record["resource_id"]).to_dict()


def changed(record, text):
    result = deepcopy(record)
    result["update_sequence"] += 1
    result["value"]["members"][0]["text"] = text
    return result


def registry_with_consumer(record, *, business_output=False, gate=None):
    registry = create_default_registry()
    reference = identity(record)
    config = {"reference": reference} if business_output else {}
    schema = {"type": "object", "additionalProperties": False,
              "required": list(config), "properties": {"reference": {"type": "object"}} if config else {}}

    def consume(config, inputs, context):
        if not business_output:
            return {"output": deepcopy(inputs["input"])}
        # This plugin explicitly produces a business artifact. The global source
        # and its input/read evidence still carry only resource identity.
        assert inputs["input"]["reference"] == config["reference"]
        current = context.host_call("resources:read", "current-global-resource", config["reference"])
        context.reads.append({"kind": "global_resource_read", "reference": deepcopy(config["reference"])})
        return {"output": text_value(current["value"]["members"][0]["text"])}

    registry.register(NodeDefinition(
        "test.current-resource-consumer", "1", "Current resource consumer", "test", config, schema,
        inputs=(NodePort("input", "GLOBAL_RESOURCE_REF"),),
        outputs=(NodePort("output", "TEXT" if business_output else "GLOBAL_RESOURCE_REF"),),
        is_output=True, capabilities=("resources:read",) if business_output else (),
    ), consume, resource_dependencies_declaration=(
        (lambda config: [{"kind": "global-resource", "reference": deepcopy(config["reference"])}])
        if business_output else None
    ))
    if gate is not None:
        entered, release = gate

        def wait_for_update(config, inputs, context):
            entered.set()
            assert release.wait(10)
            return {"output": text_value("ready")}

        registry.register(NodeDefinition(
            "test.current-resource-gate", "1", "Gate", "test", {},
            {"type": "object", "additionalProperties": False}, outputs=(NodePort("output", "TEXT"),),
            is_output=True,
        ), wait_for_update)
    return registry


def graph(registry, record, *, gate=False):
    source = node(registry, "global-content", 1, resource_id=record["resource_id"])
    source["component_version"] = "2"
    source["public_outputs"] = ["output"]
    consumer = node(registry, "test.current-resource-consumer", 2)
    consumer["public_outputs"] = ["output"]
    nodes = [source, consumer]
    if gate:
        nodes.insert(0, node(registry, "test.current-resource-gate", 3))
    return document(nodes, [edge(source, consumer, 1)])


def save(service, record):
    return service.save_global_resource(record, expected_sequence=record["update_sequence"] - 1,
                                        idempotency_key=str(uuid4()))


def test_new_resource_reference_flow_records_only_identity_and_reopens_without_global_cache(tmp_path):
    path = tmp_path / "identity.sqlite"
    record = legacy_content_to_current(resource("never-archive-global-body"))
    registry = registry_with_consumer(record)
    with closing(GraphWorkflowService(path, registry=registry)) as service:
        save(service, record)
        doc = graph(registry, record)
        completed = run(service, create(service, doc))
        assert completed["status"] == "succeeded"
        expected = global_resource_reference(identity(record))
        assert [item["outputs"]["output"] for item in completed["nodes"]] == [expected, expected]
        chain_id = completed["chains"][-1]["chain_run_id"]
        historical = service.get_run(completed["workflow_session_id"], chain_id)
        assert "never-archive-global-body" not in canonical_bytes(historical).decode("utf-8")
        source_run, consumer_run = historical["node_runs"]
        assert source_run["reads"] == [{"kind": "global_resource_read", "reference": identity(record)}]
        assert consumer_run["input_values"] == {"input": expected}
        start = service.get_state_manifest(completed["workflow_session_id"], chain_id=chain_id, boundary="start")
        end = service.get_state_manifest(completed["workflow_session_id"], chain_id=chain_id, boundary="end")
        assert "never-archive-global-body" not in canonical_bytes([start, end]).decode("utf-8")
        assert service._resource_frames == {}
        assert service._native_runtime is None
        with closing(SqliteStore(path)) as store:
            persisted = [dict(row) for row in store._connection.execute("SELECT payload FROM records")]
            receipts = [dict(row) for row in store._connection.execute("SELECT result_payload FROM idempotency")]
            assert "never-archive-global-body" not in canonical_bytes(persisted + receipts).decode("utf-8")
    with closing(GraphWorkflowService(path, registry=registry)) as service:
        restored = service.get_run(completed["workflow_session_id"], chain_id)
        assert restored == historical
        assert service.get_global_resource(identity(record)) == record
        again = run(service, service.get_session(completed["workflow_session_id"]))
        assert again["status"] == "succeeded"


def test_start_freezes_current_for_same_process_then_next_run_reads_latest(tmp_path):
    entered, release = Event(), Event()
    record = legacy_content_to_current(resource("start-frozen-original"))
    registry = registry_with_consumer(record, business_output=True, gate=(entered, release))
    with closing(GraphWorkflowService(tmp_path / "frozen.sqlite", registry=registry)) as service:
        save(service, record)
        doc = graph(registry, record, gate=True)
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            newest = changed(record, "next-run-updated")
            save(service, newest)
            assert service.get_global_resource(identity(record)) == newest
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])
        completed = service.get_session(initial["workflow_session_id"])
        assert completed["status"] == "succeeded"
        assert completed["nodes"][-1]["outputs"]["output"] == text_value("start-frozen-original")
        evidence = service.get_run(completed["workflow_session_id"], completed["chains"][-1]["chain_run_id"])
        assert "start-frozen-original" not in canonical_bytes(evidence["chain"]).decode("utf-8")
        source_run = next(item for item in evidence["node_runs"] if item["node_binding_id"] == doc["nodes"][1]["node_binding_id"])
        assert "start-frozen-original" not in canonical_bytes(source_run).decode("utf-8")
        newer_result = run(service, completed)
        assert newer_result["nodes"][-1]["outputs"]["output"] == text_value("next-run-updated")
        assert service._resource_frames == {}


def test_fork_inherits_global_identity_and_new_run_uses_current_head(tmp_path):
    record = legacy_content_to_current(resource("before-fork"))
    registry = registry_with_consumer(record, business_output=True)
    with closing(GraphWorkflowService(tmp_path / "fork.sqlite", registry=registry)) as service:
        save(service, record)
        doc = graph(registry, record)
        first = run(service, create(service, doc))
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        newest = changed(record, "global-current-after-fork")
        save(service, newest)
        parent = run(service, first)
        forked = service.fork_graph_candidate(
            parent["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=parent["revision"], expected_data_revision=parent["data_revision"],
            expected_head_revision=parent["head_revision"], idempotency_key=str(uuid4()),
        )
        assert service.get_global_resource(identity(record)) == newest
        assert forked["nodes"][-1]["outputs"]["output"] == text_value("before-fork")
        child = run(service, forked)
        assert child["nodes"][-1]["outputs"]["output"] == text_value("global-current-after-fork")
        assert service.get_global_resource(identity(record)) == newest
        assert service.get_session(parent["workflow_session_id"]) == parent


def test_deleted_current_resource_rejects_new_run_and_keeps_saved_identity_results(tmp_path):
    record = legacy_content_to_current(resource("will-be-deleted"))
    registry = registry_with_consumer(record)
    with closing(GraphWorkflowService(tmp_path / "delete.sqlite", registry=registry)) as service:
        save(service, record)
        first = run(service, create(service, graph(registry, record)))
        historical = service.get_run(first["workflow_session_id"], first["chains"][-1]["chain_run_id"])
        command = dict(expected_sequence=1, idempotency_key=str(uuid4()))
        deleted = service.delete_global_resource(identity(record), **command)
        assert service.delete_global_resource(identity(record), **command) == deleted
        assert service.get_global_resource(identity(record)) is None
        with pytest.raises(ContractValidationError) as missing:
            run(service, first)
        assert missing.value.reason_code == "global_resource_missing"
        assert service.get_session(first["workflow_session_id"]) == first
        assert service.get_run(first["workflow_session_id"], first["chains"][-1]["chain_run_id"]) == historical


def test_uncommitted_start_releases_ephemeral_global_frames(tmp_path):
    record = legacy_content_to_current(resource("uncommitted-start-body"))
    registry = registry_with_consumer(record)
    armed = False

    def fail_before_commit(stage):
        if armed and stage == "before_commit":
            raise RuntimeError("injected start rollback")

    with closing(GraphWorkflowService(tmp_path / "rollback.sqlite", registry=registry,
                                      fault_injector=fail_before_commit)) as service:
        save(service, record)
        initial = create(service, graph(registry, record))
        armed = True
        with pytest.raises(RuntimeError, match="injected start rollback"):
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        armed = False
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert service._resource_frames == {}
        assert service._execution_registries == {}


def test_dormant_incomplete_reference_does_not_block_unrelated_workflow(tmp_path):
    record = legacy_content_to_current(resource())
    registry = registry_with_consumer(record)
    registry.register(NodeDefinition(
        "test.dormant-current-reader", "1", "Dormant current reader", "test",
        {"reference": identity(record)},
        {"type": "object", "required": ["reference"], "properties": {"reference": {"type": "object"}}},
        outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
    ), lambda config, inputs, context: {"output": global_resource_reference(config["reference"])},
        resource_dependencies_declaration=lambda config: [
            {"kind": "global-resource", "reference": config["reference"]},
        ])
    source = node(registry, "text", 10, text="unrelated")
    target = node(registry, "output", 11)
    dormant = node(registry, "test.dormant-current-reader", 12)
    dormant["config"] = {}
    doc = document([source, target, dormant], [edge(source, target, 10)])
    with closing(GraphWorkflowService(tmp_path / "dormant.sqlite", registry=registry)) as service:
        completed = run(service, create(service, doc))
        assert completed["status"] == "succeeded"
        assert completed["nodes"][1]["outputs"]["output"] == text_value("unrelated")
        assert completed["nodes"][2]["status"] == "idle"


def test_disabled_reference_rejects_before_earlier_shared_write(tmp_path):
    record = legacy_content_to_current(resource("disabled-current"))
    record["value"]["enabled"] = False
    registry = registry_with_consumer(record)
    doc = graph(registry, record)
    writer = node(registry, "session-data-write", 10, value="must-not-save")
    convert = node(registry, "json-to-text", 11)
    target = node(registry, "output", 12)
    doc["nodes"] = [writer, convert, target, *doc["nodes"]]
    doc["edges"] += [edge(writer, convert, 10, source_port="json"), edge(convert, target, 11)]
    with closing(GraphWorkflowService(tmp_path / "disabled.sqlite", registry=registry)) as service:
        save(service, record)
        initial = create(service, doc)
        with pytest.raises(ContractValidationError) as disabled:
            run(service, initial)
        assert disabled.value.reason_code == "global_content_disabled"
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert service._resource_frames == {}
