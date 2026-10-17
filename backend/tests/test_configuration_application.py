"""Formal configuration commands, frozen receipts and consumed execution facts."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_execution import execute_graph
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_records import graph_record, validate_graph_bundle
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_configuration import PROMPT_CONFIGURATION_FIELDS


def prompt_registry(calls=None):
    registry = create_package_registry(enabled={"workflow.content": "1.0.0"}).registry.detached()

    def execute(config, inputs, context):
        if calls is not None:
            calls.append(deepcopy(config))
        return {"output": text_content(config["text"])}

    registry.register(NodeDefinition(
        "test.configuration-output", "1", "Configuration output", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
    ), lambda config, inputs, context: {"output": inputs["input"]})
    registry.register(NodeDefinition(
        "prompts.item", "3", "Configuration fixture", "Test",
        {"id": str(uuid4()), "text": "original"},
        {"type": "object", "properties": {
            "id": {"type": "string", "format": "uuid"},
            "text": {"type": "string"},
        }, "required": ["id", "text"], "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        is_output=True,
        adjustable_configuration={
            "schema_version": 1, "policy": "prompt", "fields": list(PROMPT_CONFIGURATION_FIELDS),
        },
    ), execute)
    return registry


def configuration_document(registry, *, connected_output=False):
    document = {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Configuration", "object_bindings": [], "edges": [],
        "nodes": [{
            "node_binding_id": str(uuid4()), "component_id": "prompts.item", "component_version": "3",
            "title": "Prompt", "position": {"x": 0, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "adjustable"},
            "config": deepcopy(registry.get("prompts.item", "3").definition.default_config),
        }],
    }
    if connected_output:
        output_id = str(uuid4())
        document["nodes"].append({
            "node_binding_id": output_id, "component_id": "test.configuration-output",
            "component_version": "1", "title": "Output", "position": {"x": 100, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
            "config": {},
        })
        document["edges"].append({
            "edge_id": str(uuid4()), "source_node_id": document["nodes"][0]["node_binding_id"],
            "source_port_id": "output", "target_node_id": output_id,
            "target_port_id": "input", "order": 0,
        })
    return document


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "configuration.sqlite", registry=prompt_registry())) as instance:
        yield instance


def submit(app, document, sequence, *, key=None, confirm=None):
    request = {
        "workflow_definition_id": document["workflow_definition_id"], "document": deepcopy(document),
        "expected_edit_sequence": sequence, "idempotency_key": key or str(uuid4()),
    }
    if confirm is not None:
        request["confirm_derivation"] = confirm
    accepted = app.command("definition.update", request)
    receipt = read_graph_application_receipt(app._service.database, "definition.update", request)
    assert receipt["outcome"] == "matched", receipt
    assert receipt["receipt"] == accepted["receipt"]
    assert receipt["result"] == accepted["result"]
    assert app.query("receipt.read", {"operation": "definition.update", "parameters": request}) == receipt
    return accepted["result"], request


def test_formal_reads_cas_and_frozen_receipts_follow_actual_target(service):
    app = GraphApplication(service)
    created, initial_request = submit(app, configuration_document(service.registry), 0)
    assert created["edit_sequence"] == created["definition_revision"] == 1
    assert app.query("definition.read", {"identity": created["workflow_definition_id"]}) == created["document"]
    changed = deepcopy(created["document"])
    changed["nodes"][0]["config"]["text"] = "current"
    saved, _ = submit(app, changed, 1)
    assert saved["outcome"] == "saved" and saved["changed_categories"] == ["adjustable"]
    assert saved["edit_sequence"] == 2 and saved["definition_revision"] == 1
    unchanged, _ = submit(app, saved["document"], 2)
    assert unchanged["outcome"] == "unchanged" and unchanged["edit_sequence"] == 2
    assert app.command("definition.update", initial_request)["result"] == created
    assert read_graph_application_receipt(service.database, "definition.update", initial_request)["result"] == created
    assert app.query("definition.list")[0]["document"] == saved["document"]
    assert app.query("definition.read", {"identity": created["workflow_definition_id"], "revision": 1})[
        "nodes"][0]["config"]["text"] == "original"


def test_mixed_derived_save_and_idempotence_leave_source_and_session_unchanged(service):
    app = GraphApplication(service)
    created, _ = submit(app, configuration_document(service.registry), 0)
    original = created["document"]
    session = service.create_session(original["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    changed = deepcopy(original)
    changed["nodes"][0]["config"]["text"] = "new adjustable body"
    changed["nodes"][0]["config"]["id"] = str(uuid4())
    asked, ask_request = submit(app, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    assert asked["changed_categories"] == ["adjustable", "fixed"]
    assert asked["source_has_effective_session"]
    # The ask wrote nothing: no derivation, no new target, source and session unchanged.
    assert service.get_definition(original["workflow_definition_id"]) == original
    assert service.get_session(session["workflow_session_id"]) == session
    assert len(service.list_definitions()) == 1
    derived, request = submit(app, changed, 1, confirm=True)
    assert derived["outcome"] == "derived"
    assert derived["changed_categories"] == ["adjustable", "fixed"]
    assert derived["source_has_effective_session"]
    assert derived["edit_sequence"] == derived["definition_revision"] == 1
    target = derived["workflow_definition_id"]
    assert target != original["workflow_definition_id"]
    assert service.get_definition(original["workflow_definition_id"]) == original
    assert service.get_session(session["workflow_session_id"]) == session
    assert service.list_sessions(target) == []
    assert app.command("definition.update", request)["result"] == derived
    assert app.command("definition.update", ask_request)["result"] == asked
    assert len(service.list_definitions()) == 2


def test_reordered_node_array_preserves_fixed_order_and_matchable_receipt(service):
    app = GraphApplication(service)
    doc = configuration_document(service.registry)
    extra = deepcopy(doc["nodes"][0])
    extra["node_binding_id"] = str(uuid4())
    extra["config"]["id"] = str(uuid4())
    doc["nodes"].append(extra)
    created, _ = submit(app, doc, 0)
    changed = deepcopy(created["document"])
    changed["nodes"].reverse()
    saved, _ = submit(app, changed, 1)
    assert saved["outcome"] == "saved" and saved["changed_categories"] == ["fixed"]
    assert saved["definition_revision"] == 2
    assert saved["document"]["nodes"] == changed["nodes"]


def test_http_entry_and_consumer_scope(service):
    doc = configuration_document(service.registry)
    request = {
        "workflow_definition_id": doc["workflow_definition_id"], "document": doc,
        "expected_edit_sequence": 0, "idempotency_key": str(uuid4()),
    }
    status, result = dispatch_graph(service, "POST", "/api/graph/definitions/update", request)
    assert status == 200 and result["outcome"] == "saved"
    with pytest.raises(ContractValidationError) as error:
        GraphApplication(service).for_consumer().command("definition.update", request)
    assert error.value.reason_code == "application_scope_denied"
    for sequence in (-1, True, 2**53):
        with pytest.raises(ContractValidationError):
            GraphApplication(service).command("definition.update", {
                **request, "expected_edit_sequence": sequence,
            })


def test_confirmation_ask_is_recorded_and_replayable_while_nothing_is_written(service):
    """The question is a real receipt: replayable, and proven not to have written."""
    app = GraphApplication(service)
    created, _ = submit(app, configuration_document(service.registry), 0)
    original = created["document"]
    service.create_session(original["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    before_read = service.get_definition(original["workflow_definition_id"])
    changed = deepcopy(original)
    changed["nodes"][0]["config"]["id"] = str(uuid4())
    asked, ask_request = submit(app, changed, 1, key="ask-receipt")
    assert asked["outcome"] == "confirmation_required"
    assert asked["workflow_definition_id"] == original["workflow_definition_id"]
    assert asked["edit_sequence"] == 1 and asked["definition_revision"] == 1
    # The ask is itself a durable receipt and replays identically.
    assert app.command("definition.update", ask_request)["result"] == asked
    assert service.get_definition(original["workflow_definition_id"]) == before_read
    assert len(service.list_definitions()) == 1
    # The same document with an explicit confirmation derives, under its own key.
    derived, _ = submit(app, changed, 1, confirm=True)
    assert derived["outcome"] == "derived"
    assert len(service.list_definitions()) == 2
    # An unconfirmed repeat of the document now asks again for the untouched source.
    again, _ = submit(app, changed, 1)
    assert again["outcome"] == "confirmation_required"
    assert service.get_definition(original["workflow_definition_id"]) == before_read


def test_confirmation_flag_shape_is_validated_before_effects(service):
    app = GraphApplication(service)
    created, _ = submit(app, configuration_document(service.registry), 0)
    service.create_session(created["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    changed = deepcopy(created["document"])
    changed["nodes"][0]["config"]["id"] = str(uuid4())
    before = service.get_definition(created["workflow_definition_id"])
    for value in ("true", 1, None):
        with pytest.raises(ContractValidationError) as error:
            app.command("definition.update", {
                "workflow_definition_id": created["workflow_definition_id"], "document": deepcopy(changed),
                "expected_edit_sequence": 1, "idempotency_key": str(uuid4()), "confirm_derivation": value,
            })
        assert error.value.reason_code == "invalid_request"
    with pytest.raises(ContractValidationError):
        app.command("definition.update", {
            "workflow_definition_id": created["workflow_definition_id"], "document": deepcopy(changed),
            "expected_edit_sequence": 1, "idempotency_key": str(uuid4()), "confirm": True,
        })
    assert service.get_definition(created["workflow_definition_id"]) == before
    assert len(service.list_definitions()) == 1


def test_old_save_entry_is_retired_after_configuration_authority(service):
    app = GraphApplication(service)
    created, _ = submit(app, configuration_document(service.registry), 0)
    assert not hasattr(service, "save_definition")
    with pytest.raises(ContractValidationError) as error:
        app.command("definition.save", {})
    assert error.value.reason_code == "not_found"
    assert service.get_definition(created["workflow_definition_id"]) == created["document"]


def test_definition_delete_removes_current_configuration(service):
    created, _ = submit(GraphApplication(service), configuration_document(service.registry), 0)
    identity = created["workflow_definition_id"]
    service.delete_definition(identity, expected_revision=1, idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError):
        service.get_definition(identity)
    recreated, _ = submit(GraphApplication(service), {
        **created["document"], "edit_sequence": 0,
    }, 0)
    assert recreated["edit_sequence"] == 1


def run_bundle(doc, descriptor, consumed):
    sid, chain_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    node = doc["nodes"][0]
    identity = node["node_binding_id"]
    return {
        "node_definition": [graph_record("node_definition", component_id=node["component_id"],
                                        component_version=node["component_version"], descriptor=descriptor)],
        "workflow_definition_revision": [graph_record(
            "workflow_definition_revision", workflow_definition_id=doc["workflow_definition_id"],
            revision=1, bindings=[identity], edges=[], document=doc)],
        "node_binding": [graph_record(
            "node_binding", workflow_definition_id=doc["workflow_definition_id"],
            workflow_definition_revision=1, node_binding_id=identity, component_id=node["component_id"],
            component_version=node["component_version"], config=node["config"])],
        "workflow_session": [graph_record(
            "workflow_session", workflow_session_id=sid, workflow_definition_id=doc["workflow_definition_id"],
            definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=chain_id)],
        "chain_run": [graph_record(
            "chain_run", chain_run_id=chain_id, workflow_session_id=sid,
            workflow_definition_id=doc["workflow_definition_id"], definition_revision=1, status="succeeded",
            revision=2, targets=[identity], ordered_nodes=[identity], node_run_ids=[run_id],
            completed_nodes=[identity], next_node_index=1, inputs={}, outputs={identity: {}}, diagnostic=None)],
        "node_run": [graph_record(
            "node_run", profile="node", run_id=run_id, workflow_session_id=sid, node_binding_id=identity,
            chain_run_id=chain_id, status="succeeded", revision=2, input_refs={}, input_values={},
            output_refs={}, reads=[], effects=[], config=consumed, diagnostic=None)],
    }


def test_run_facts_validate_consumed_config_against_trusted_fixed_projection():
    registry = prompt_registry()
    doc = configuration_document(registry)
    descriptor = registry.get("prompts.item", "3").definition.to_dict()
    consumed = {**doc["nodes"][0]["config"], "text": "historically consumed"}
    bundle = run_bundle(doc, descriptor, consumed)
    validate_graph_bundle(bundle)
    for config in ({**consumed, "id": str(uuid4())}, {**consumed, "text": 42}):
        forged = deepcopy(bundle)
        forged["node_run"][0]["config"] = config
        with pytest.raises(ContractValidationError):
            validate_graph_bundle(forged)
    forged = deepcopy(bundle)
    forged.pop("node_definition")
    with pytest.raises(ContractValidationError):
        validate_graph_bundle(forged)
    fixed = deepcopy(bundle)
    fixed["workflow_definition_revision"][0]["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"
    with pytest.raises(ContractValidationError):
        validate_graph_bundle(fixed)


def test_retained_result_keeps_consumed_config_after_adjustable_edit():
    calls = []
    registry = prompt_registry(calls)
    doc = configuration_document(registry, connected_output=True)
    run_id, sid, chain_id = str(uuid4()), str(uuid4()), str(uuid4())
    cache = {}

    def refuse(event):
        if event["event"] == "succeeded":
            raise OSError("synthetic output write failure")

    def execute(document, on_node):
        plan = GraphCompiler(registry).compile(document)
        return execute_graph(
            plan, registry, workflow_session_id=sid, chain_run_id=chain_id,
            node_run_ids={
                node["node_binding_id"]: run_id if index == 0 else node["node_binding_id"]
                for index, node in enumerate(document["nodes"])
            },
            state={"revision": 0, "values": {}}, acceptance_cache=cache, on_node=on_node,
        )

    with pytest.raises(OSError):
        execute(doc, refuse)
    consumed = deepcopy(cache[run_id]["basis"]["config"])
    changed = deepcopy(doc)
    changed["nodes"][0]["config"]["text"] = "later saved value"
    fixed_change = deepcopy(changed)
    fixed_change["nodes"][0]["config"]["id"] = str(uuid4())
    failed = execute(fixed_change, lambda event: None)
    assert failed.status == "failed"
    assert failed.diagnostic["code"] == "graph_acceptance_basis_changed"
    # Rejection clears the candidate; generate a separate known result for exact retry.
    with pytest.raises(OSError):
        execute(doc, refuse)
    result = execute(changed, lambda event: None)
    assert result.status == "succeeded"
    assert result.outputs[doc["nodes"][0]["node_binding_id"]]["output"]["text"] == "original"
    assert calls == [consumed, consumed]
    assert not cache
