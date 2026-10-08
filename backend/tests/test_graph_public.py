"""Public graph consumers use declared ports and frozen session history."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.content_contracts import json_content, prompt_content, text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from workflow_test_support import AgentTransportFixture, graph as agent_graph
from workflow_test_support import run as run_agent
from test_graph_service import copy_current, create, document, edge, node, run, service, text_graph, uid
from test_graph_server import host_server, request
from test_models_service_integration import ModelDatabaseFixture


def expose(document, *node_ids):
    for item in document["nodes"]:
        if not node_ids or item["node_binding_id"] in node_ids:
            item["public_outputs"] = ["output"]
    return document


def query(document, node_id, port_id="output"):
    return {"workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": document["revision"], "node_id": node_id, "port_id": port_id}


def rebind(service, view, document, mappings=None):
    service.save_definition(document, expected_revision=document["revision"] - 1, idempotency_key=str(uuid4()))
    return service.rebind_session(view["workflow_session_id"], definition_revision=document["revision"],
        expected_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"], idempotency_key=str(uuid4()), mappings=mappings)


def test_declarations_zero_agent_no_output_and_no_private_state(service):
    doc = expose(text_graph(service.registry), uid(3))
    session = create(service, doc)
    consumer = service.get_consumer(session["workflow_session_id"])
    assert consumer["inputs"] == [] and consumer["can_submit"]
    assert consumer["outputs"][0]["availability"] == "unproduced"
    assert consumer["outputs"][0]["payload"] is None
    assert {"data", "private_states", "messages", "input_values", "facts", "snapshot"}.isdisjoint(consumer)
    assert all("outputs" not in row for row in consumer["nodes"])
    final = run(service, session)
    result = service.read_public_output(final["workflow_session_id"], **query(doc, uid(3)))["output"]
    assert result["payload"] == text_content("pear pear")
    assert result["source"]["workflow_definition_id"] == doc["workflow_definition_id"]
    assert not hasattr(service, "_native_runtime")
    empty = create(service, document([], []))
    view = service.get_consumer(empty["workflow_session_id"])
    assert not view["can_submit"] and view["diagnostics"][0]["code"] == "graph_no_outputs"


def test_relevant_external_inputs_are_discovered_by_registration(service):
    source = node(service.registry, "current-input", 1, input_name="question")
    dormant = node(service.registry, "current-input", 2, input_name="unrelated")
    target = node(service.registry, "output", 3)
    doc = document([source, dormant, target], [edge(source, target, 1)])
    session = create(service, doc)
    assert service.get_consumer(session["workflow_session_id"])["inputs"] == [
        {"name": "question", "data_type": "TEXT", "required": True, "node_ids": [uid(1)]}]
    original = service.registry.get("tools.current-input", "1").definition
    custom = replace(original, component_id="plugin.external-input", display_name="External probe")
    entry = service.registry.get("tools.current-input", "1")
    service.registry.register(custom, entry.executor, external_inputs_validator=entry.external_inputs_validator,
        external_inputs_declaration=entry.external_inputs_declaration)
    source["component_id"] = custom.component_id
    new_session = create(service, document([source, target], [edge(source, target, 2)]))
    assert service.get_consumer(new_session["workflow_session_id"])["inputs"][0]["name"] == "question"
    assert "external_inputs_declaration" not in custom.to_dict()


def test_same_type_multiple_ports_and_prompt_content(service):
    definition = NodeDefinition("plugin.public-ports", "1", "Two ports", "test", {},
        {"type": "object", "additionalProperties": False},
        outputs=(NodePort("left", "TEXT", data_schema_version=2),
                 NodePort("right", "TEXT", data_schema_version=2)),
        is_output=True)
    service.registry.register(definition, lambda config, inputs, context: {
        "left": text_content("left"), "right": text_content("right")})
    probe = node(service.registry, definition.component_id, 1)
    probe["public_outputs"] = ["right"]
    prompt = node(service.registry, "tools.text-to-prompt", 2)
    text = node(service.registry, "tools.text", 4, text="public prompt")
    target = node(service.registry, "output", 3, mode="prompt")
    target["public_outputs"] = ["output"]
    doc = document([probe, text, prompt, target], [edge(text, prompt, 2), edge(prompt, target, 1)])
    final = run(service, create(service, doc))
    sid = final["workflow_session_id"]
    assert service.read_public_output(sid, **query(doc, uid(1), "right"))["output"]["payload"] == text_content("right")
    payload = service.read_public_output(sid, **query(doc, uid(3)))["output"]["payload"]
    assert payload["kind"] == "workflow.prompt" and payload["items"][0]["text"] == "public prompt"
    with pytest.raises(ContractValidationError) as error:
        service.read_public_output(sid, **query(doc, uid(1), "left"))
    assert error.value.reason_code == "output_not_public"


def test_pending_chain_never_exposes_previous_round(service):
    entered, release = Event(), Event()
    block = False
    def execute(config, inputs, context):
        if block:
            entered.set()
            assert release.wait(10)
        return {"output": text_content("fresh")}
    service.registry.register(NodeDefinition("plugin.block", "1", "Block", "test", {},
        {"type": "object", "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), execute)
    source, target = node(service.registry, "plugin.block", 1), node(service.registry, "output", 2)
    target["public_outputs"] = ["output"]
    doc = document([source, target], [edge(source, target, 1)])
    final = run(service, create(service, doc))
    first_run = final["nodes"][-1]["run_id"]
    block = True
    started = service.start(final["workflow_session_id"], expected_revision=final["revision"], idempotency_key="next")
    try:
        assert entered.wait(5)
        current = service.get_consumer(final["workflow_session_id"])["outputs"][0]
        assert current["availability"] == "unproduced" and current["payload"] is None
        assert current["run_id"] != first_run
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])


def test_scope_revocation_and_original_declaration_both_apply(service):
    doc = expose(text_graph(service.registry), uid(3))
    first = run(service, create(service, doc))
    other = service.create_session(doc["workflow_definition_id"], doc["revision"], idempotency_key=str(uuid4()))
    original_run = first["nodes"][-1]["run_id"]
    with pytest.raises(ContractValidationError) as outside:
        service.read_public_output(other["workflow_session_id"], **query(doc, uid(3)), run_id=original_run)
    assert outside.value.status_code == 404
    revised = deepcopy(doc)
    revised["revision"] = 2
    revised["nodes"][-1]["public_outputs"] = []
    changed = rebind(service, first, revised)
    with pytest.raises(ContractValidationError) as revoked:
        service.read_public_output(changed["workflow_session_id"], **query(revised, uid(3)), run_id=original_run)
    assert revoked.value.reason_code == "output_not_public"
    with pytest.raises(ContractValidationError) as mismatch:
        service.read_public_output(changed["workflow_session_id"], **query(doc, uid(3)))
    assert mismatch.value.reason_code == "consumer_definition_mismatch"
    private_doc = text_graph(service.registry)
    private_doc["nodes"][-1]["public_outputs"] = []
    private = run(service, create(service, private_doc))
    public_doc = expose(deepcopy(private_doc), uid(3))
    public_doc["revision"] = 2
    changed = rebind(service, private, public_doc)
    with pytest.raises(ContractValidationError) as retroactive:
        service.read_public_output(changed["workflow_session_id"], **query(public_doc, uid(3)), run_id=private["nodes"][-1]["run_id"])
    assert retroactive.value.reason_code == "output_not_public"


def test_copy_freezes_scope_and_preserves_original_output_identity(service):
    doc = expose(text_graph(service.registry), uid(3))
    parent = run(service, create(service, doc))
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    target["nodes"][-1]["node_binding_id"] = uid(30)
    target["edges"][-1]["target_node_id"] = uid(30)
    copied = copy_current(service, parent, target, mappings=[{"action": "copy", "source_node_id": uid(3), "target_node_id": uid(30)}])
    old_run = parent["nodes"][-1]["run_id"]
    result = service.read_public_output(copied["workflow_session_id"], **query(target, uid(30)), run_id=old_run)["output"]
    assert result["node_binding_id"] == uid(30)
    assert result["source"]["node_binding_id"] == uid(3)
    assert result["source"]["workflow_session_id"] == parent["workflow_session_id"]
    later = run(service, parent)
    with pytest.raises(ContractValidationError) as future:
        service.read_public_output(copied["workflow_session_id"], **query(target, uid(30)), run_id=later["nodes"][-1]["run_id"])
    assert future.value.status_code == 404
    history = service.public_output_history(copied["workflow_session_id"], **query(target, uid(30)))
    assert [row["run_id"] for row in history["outputs"]] == [old_run]


def test_multi_generation_copy_keeps_history_for_every_cloned_target(service):
    doc = expose(text_graph(service.registry), uid(3))
    parent = run(service, create(service, doc))
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    first_output = target["nodes"][-1]
    first_output["node_binding_id"] = uid(30)
    target["edges"][-1]["target_node_id"] = uid(30)
    second_output = deepcopy(first_output)
    second_output["node_binding_id"] = uid(31)
    target["nodes"].append(second_output)
    target["edges"].append(edge(target["nodes"][1], second_output, 17))
    child = copy_current(service, parent, target, mappings=[
        {"source_node_id": uid(3), "target_node_id": uid(30), "action": "copy"},
        {"source_node_id": uid(3), "target_node_id": uid(31), "action": "copy"}])
    grand_document = deepcopy(target)
    grand_document["workflow_definition_id"] = str(uuid4())
    for item in grand_document["nodes"]:
        if item["node_binding_id"] in (uid(30), uid(31)):
            item["node_binding_id"] = uid(300 if item["node_binding_id"] == uid(30) else 310)
    for link in grand_document["edges"]:
        if link["target_node_id"] in (uid(30), uid(31)):
            link["target_node_id"] = uid(300 if link["target_node_id"] == uid(30) else 310)
    grand = copy_current(service, child, grand_document, mappings=[
        {"source_node_id": uid(30), "target_node_id": uid(300), "action": "copy"},
        {"source_node_id": uid(31), "target_node_id": uid(310), "action": "copy"}])
    original_run = parent["nodes"][-1]["run_id"]
    for target_id in (uid(300), uid(310)):
        result = service.read_public_output(grand["workflow_session_id"],
            **query(grand_document, target_id), run_id=original_run)["output"]
        assert result["payload"] == text_content("pear pear")
        assert result["source"]["node_binding_id"] == uid(3)
        assert result["source"]["workflow_session_id"] == parent["workflow_session_id"]
    reset_document = deepcopy(grand_document)
    reset_document["workflow_definition_id"] = str(uuid4())
    reset = copy_current(service, grand, reset_document,
        mappings=[{"target_node_id": uid(310), "action": "reset"}])
    assert service.read_public_output(reset["workflow_session_id"],
        **query(reset_document, uid(300)), run_id=original_run)["output"]["availability"] == "produced"
    with pytest.raises(ContractValidationError) as reset_branch:
        service.read_public_output(reset["workflow_session_id"], **query(reset_document, uid(310)), run_id=original_run)
    assert reset_branch.value.status_code == 404


def test_private_shared_data_cannot_be_exposed_directly_or_from_frozen_history(service):
    declaration = {
        "schema_version": 1, "definition_id": str(uuid4()), "revision": 1,
        "key": "example:private", "name": "Private sample",
        "schema": {"type": "string"}, "writable": True, "public": False,
    }

    def write(config, inputs, context):
        context.write_shared(config["definition"], config["value"])
        return {"json": json_content(config["value"])}

    service.registry.register(NodeDefinition(
        "sample.private-shared", "1", "Private shared data", "Sample",
        {"definition": declaration, "value": "private value"},
        {"type": "object", "required": ["definition", "value"], "additionalProperties": False,
         "properties": {"definition": {"type": "object"}, "value": {"type": "string"}}},
        outputs=(NodePort("json", "JSON", data_schema_version=2),), capabilities=("shared:write",),
    ), write)
    writer = node(service.registry, "sample.private-shared", 1)
    writer["public_outputs"] = ["json"]
    projection, output = node(service.registry, "json-to-text", 2), node(service.registry, "output", 3)
    output["public_outputs"] = ["output"]
    doc = document([writer, projection, output], [edge(writer, projection, 1, "json"), edge(projection, output, 2)])
    parent = run(service, create(service, doc))
    sid = parent["workflow_session_id"]
    assert service.get_consumer(sid)["can_submit"]
    with pytest.raises(ContractValidationError) as direct:
        service.read_public_output(sid, **query(doc, uid(1), "json"))
    assert direct.value.reason_code == "output_not_public"
    assert service.read_public_output(sid, **query(doc, uid(3)))["output"]["availability"] == "produced"
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, parent, target)
    with pytest.raises(ContractValidationError) as historical:
        service.read_public_output(copied["workflow_session_id"], **query(target, uid(1), "json"), run_id=parent["nodes"][0]["run_id"])
    assert historical.value.reason_code == "output_not_public"


def test_public_observation_follows_selected_candidate_instead_of_latest_chain(service):
    source, target = node(service.registry, "current-input", 1), node(service.registry, "output", 2)
    target["public_outputs"] = ["output"]
    doc = document([source, target], [edge(source, target, 1)])
    session = create(service, doc)
    for text in ("first", "second"):
        started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": text})
        service.wait(started["active_chain_run_id"])
        session = service.get_session(session["workflow_session_id"])
    first = service.list_graph_candidates(session["workflow_session_id"])["candidates"][0]
    selected = service.select_graph_candidate(session["workflow_session_id"], candidate_id=first["candidate_id"],
        expected_revision=session["revision"], expected_data_revision=session["data_revision"],
        expected_head_revision=session["head_revision"], idempotency_key="select-first")
    consumer = service.get_consumer(selected["workflow_session_id"])
    assert consumer["outputs"][0]["payload"] == text_content("first")
    assert consumer["outputs"][0]["chain_run_id"] == first["chain_run_id"]


def test_consumer_http_receipts_and_history_do_not_return_private_records(tmp_path, monkeypatch):
    with host_server(tmp_path) as (host, port):
        doc = expose(text_graph(host.graph_service.registry), uid(3))
        host.graph_service.save_definition(doc, expected_revision=0, idempotency_key="save")
        definition_path = "/api/graph/definitions/" + doc["workflow_definition_id"]
        _, description = request(port, "GET", definition_path + "/consumer")
        assert set(description) == {"schema_version", "kind", "workflow_definition_id", "definition_revision", "name"}
        status, created = request(port, "POST", "/api/graph/consumer/sessions", {
            "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1, "idempotency_key": "session"})
        assert status == 201 and created["receipt"]["operation"] == "create"
        sid = created["receipt"]["workflow_session_id"]
        path = "/api/graph/sessions/" + sid
        status, started = request(port, "POST", path + "/consumer/runs", {"expected_revision": 1, "idempotency_key": "start"})
        assert status == 202 and started["receipt"]["operation"] == "start"
        host.graph_service.wait(started["receipt"]["chain_run_id"])
        assert request(port, "POST", path + "/consumer/runs", {"expected_revision": 1, "idempotency_key": "start"})[1]["receipt"] == started["receipt"]
        _, discovered = request(port, "GET", path + "/outputs/public")
        assert discovered["outputs"][0]["availability"] == "produced"
        _, history = request(port, "POST", path + "/outputs/history", query(doc, uid(3)))
        assert len(history["outputs"]) == 1
        assert request(port, "POST", path + "/outputs/read", {**query(doc, uid(3)), "public": False})[0] == 400
        _, summaries = request(port, "GET", definition_path + "/consumer-sessions")
        assert set(summaries[0]) == {"workflow_definition_id", "definition_revision", "workflow_session_id", "session_revision", "status"}
        assert "private_states" not in created["consumer"] and "data" not in started["consumer"]
        assert not hasattr(host, "_legacy") and not hasattr(host.graph_service, "_native_runtime")


def test_current_agent_uses_same_public_ports_and_separate_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-public-agent")
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
        tmp_path / "public-agent.sqlite", public_model_factory=transport.factory,
    )) as instance:
        ModelDatabaseFixture.write(instance, 1)
        doc = agent_graph(instance)
        execute = next(row for row in doc["nodes"] if row["component_id"] == "agents.execute")
        execute["public_outputs"] = ["result", "context"]
        doc["nodes"][-1]["public_outputs"] = ["output"]
        view = run_agent(instance, create(instance, doc), "public question")
        consumer = instance.get_consumer(view["workflow_session_id"])
        assert [row["data_type"] for row in consumer["outputs"]] == ["TEXT", "AGENT_CONTEXT_UPDATE", "TEXT"]
        assert consumer["outputs"][0]["payload"] == text_content("accepted answer")
        assert consumer["outputs"][1]["payload"]["operations"]
        observed = next(row for row in consumer["nodes"] if row["node_binding_id"] == execute["node_binding_id"])
        assert observed["budget"] is None and "agent" not in observed and "snapshot" not in consumer
        assert view["messages"] == [] and view["status"] == "succeeded" and len(transport.calls) == 1


def test_public_control_receipts_preserve_current_pause_resume_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-public-agent")
    transport = AgentTransportFixture(gate=True)
    with closing(GraphWorkflowService(
        tmp_path / "public-pause.sqlite", public_model_factory=transport.factory,
    )) as instance:
        ModelDatabaseFixture.write(instance, 1)
        doc = agent_graph(instance)
        execute = next(row for row in doc["nodes"] if row["component_id"] == "agents.execute")
        doc["nodes"][-1]["public_outputs"] = ["output"]
        session = create(instance, doc)
        started = instance.start_consumer(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key="consumer-start", inputs={"text": "pause question"},
        )
        try:
            assert transport.entered.wait(5)
            current = instance.get_consumer(session["workflow_session_id"])
            paused = instance.control_consumer(
                session["workflow_session_id"], action="pause",
                expected_revision=current["session_revision"], idempotency_key="consumer-pause",
            )
            assert paused["receipt"]["operation"] == "control"
        finally:
            transport.proceed.set()
            instance.wait(started["receipt"]["chain_run_id"])
        current = instance.get_consumer(session["workflow_session_id"])
        assert current["status"] == "paused" and "resume" in current["available_actions"]
        active_run = next(row for row in current["nodes"]
                          if row["node_binding_id"] == execute["node_binding_id"])["run_id"]
        resumed = instance.control_consumer(
            session["workflow_session_id"], action="resume",
            expected_revision=current["session_revision"], idempotency_key="consumer-resume",
        )
        instance.wait(resumed["receipt"]["chain_run_id"])
        final_view = instance.get_consumer(session["workflow_session_id"])
        final_run = next(row for row in final_view["nodes"]
                         if row["node_binding_id"] == execute["node_binding_id"])["run_id"]
        assert final_view["status"] == "succeeded" and final_run == active_run
        assert len(transport.calls) == 1
