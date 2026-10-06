"""Durable graph integration without kernels, credentials, or live databases."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import UUID, uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from phase1_agent.workbench_resources import CORE_SESSION_NOTE
from graph_test_plugin import PROBE_COMPONENT, register_content_probe


def uid(number):
    return str(UUID(int=number, version=4))


def node(registry, type_name, number, **config):
    component = type_name if "." in type_name else "workflow." + type_name
    entry = registry.get(component, "1")
    return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
            "title": type_name, "position": {"x": number * 100, "y": 0},
            "config": {**deepcopy(entry.definition.default_config), **config} if entry else config}


def edge(source, target, number, source_port="output", target_port="input", order=0):
    return {"edge_id": uid(1000 + number), "source_node_id": source["node_binding_id"],
            "source_port_id": source_port, "target_node_id": target["node_binding_id"],
            "target_port_id": target_port, "order": order}


def document(nodes, edges):
    return {"schema_version": 1, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Graph integration", "nodes": nodes, "edges": edges}


@pytest.fixture
def service(tmp_path):
    registry = create_default_registry()
    register_content_probe(registry)
    instance = GraphWorkflowService(tmp_path / "graph.sqlite", registry=registry)
    yield instance
    instance.close()


def create(service, doc):
    service.save_definition(doc, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def run(service, session, key=None):
    started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                            idempotency_key=key or str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(session["workflow_session_id"])


def copy_current(service, view, doc, **options):
    return service.copy_session(view["workflow_session_id"], document=doc,
        expected_session_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_definition_revision=view["definition_revision"], expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4()), **options)


def text_graph(registry):
    text = node(registry, "text", 1, text="apple apple")
    regex = node(registry, "regex", 2, pattern="apple", replacement="pear")
    output = node(registry, "output", 3)
    return document([text, regex, output], [edge(text, regex, 1), edge(regex, output, 2)])


def test_zero_agent_save_run_restore_and_named_evidence(service):
    doc = text_graph(service.registry)
    initial = create(service, doc)
    final = run(service, initial, "once")
    assert final["status"] == "succeeded" and final["can_submit"]
    assert final["messages"] == []
    assert final["nodes"][-1]["outputs"]["output"]["text"] == "pear pear"
    history = service.get_run(final["workflow_session_id"], final["chains"][0]["chain_run_id"])
    assert [entry["status"] for entry in history["node_runs"]] == ["succeeded"] * 3
    assert history["node_runs"][1]["input_values"]["input"]["text"] == "apple apple"
    assert history["node_runs"][1]["input_refs"]["input"][0]["output_id"] == history["node_runs"][0]["output_refs"]["output"]
    replay = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="once")
    assert replay["active_chain_run_id"] == final["chains"][0]["chain_run_id"]
    assert len(service.get_session(initial["workflow_session_id"])["chains"]) == 1
    path = service.database
    service.close()
    with closing(GraphWorkflowService(path, registry=service.registry)) as restored:
        assert restored.get_definition(doc["workflow_definition_id"]) == doc
        assert restored.get_session(final["workflow_session_id"])["nodes"][-1]["outputs"] == final["nodes"][-1]["outputs"]
    with closing(SqliteStore(path)) as store:
        assert store.list_records("workflow_session") == []
        assert len(store.list_records("workflow_session", include_graph=True)) == 1


def test_save_no_outputs_start_rejected_without_run(service):
    text = node(service.registry, "text", 1)
    view = create(service, document([text], []))
    with pytest.raises(ContractValidationError) as failure:
        run(service, view)
    assert failure.value.reason_code == "graph_no_outputs"
    assert service.get_session(view["workflow_session_id"])["chains"] == []


def test_dormant_unknown_agent_model_writer_ignored(service):
    doc = text_graph(service.registry)
    doc["nodes"] += [node(service.registry, "agent", 4), node(service.registry, "model-provider", 5),
                     node(service.registry, "session-data-write", 6), node(service.registry, "missing.plugin", 7)]
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded"
    assert [item["status"] for item in final["nodes"]][3:] == ["idle"] * 4
    assert not final["data"].get("data")


def test_all_outputs_prevalidated_before_any_effect(service):
    writer = node(service.registry, "session-data-write", 1, value="must not write")
    converter = node(service.registry, "json-to-text", 2)
    good = node(service.registry, "output", 3)
    bad = node(service.registry, "output", 4)
    doc = document([writer, converter, good, bad], [edge(writer, converter, 1, "json"), edge(converter, good, 2)])
    view = create(service, doc)
    with pytest.raises(ContractValidationError) as error:
        run(service, view)
    assert error.value.reason_code == "graph_required_input_missing"
    assert service.get_session(view["workflow_session_id"])["data"]["revision"] == 0


def test_external_input_prevalidation_happens_before_write(service):
    writer = node(service.registry, "session-data-write", 1)
    converter = node(service.registry, "json-to-text", 2)
    good = node(service.registry, "output", 3)
    external = node(service.registry, "current-input", 4)
    bad = node(service.registry, "output", 5)
    doc = document([writer, converter, good, external, bad],
                   [edge(writer, converter, 1, "json"), edge(converter, good, 2), edge(external, bad, 3)])
    view = create(service, doc)
    with pytest.raises(ContractValidationError) as error:
        run(service, view)
    assert error.value.reason_code == "graph_external_input_missing"
    assert service.get_session(view["workflow_session_id"])["chains"] == []


def test_independent_plugin_same_type_ports_once_and_copy_isolation(service):
    text, probe = node(service.registry, "text", 1, text="x"), node(service.registry, PROBE_COMPONENT, 2)
    left, right = node(service.registry, "output", 3), node(service.registry, "output", 4)
    doc = document([text, probe, left, right], [edge(text, probe, 1), edge(probe, left, 2, "left"), edge(probe, right, 3, "right")])
    final = run(service, create(service, doc))
    assert final["private_states"][probe["node_binding_id"]] == {"count": 1}
    assert [item["outputs"]["output"]["text"] for item in final["nodes"][-2:]] == ["left:x", "right:x"]
    other = service.create_session(doc["workflow_definition_id"], 1, idempotency_key="other")
    copied_doc = deepcopy(doc)
    copied_doc["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, final, copied_doc)
    inherited = copied["history_refs"][:]
    assert copied["source"]["workflow_session_id"] == final["workflow_session_id"]
    assert copied["private_states"] == final["private_states"] and copied["chains"] == []
    assert len(service.list_sessions(copied_doc["workflow_definition_id"])) == 1
    copied_final = run(service, copied)
    assert copied_final["private_states"][probe["node_binding_id"]]["count"] == 2
    assert service.get_session(final["workflow_session_id"])["private_states"][probe["node_binding_id"]]["count"] == 1
    parent_later = run(service, final)
    assert parent_later["chains"][-1]["chain_run_id"] not in service.get_session(copied["workflow_session_id"])["history_refs"]
    assert other["workflow_session_id"] != copied["workflow_session_id"]
    historical = service.get_run(copied["workflow_session_id"], inherited[0])
    assert historical["chain"]["workflow_session_id"] == final["workflow_session_id"]
    with pytest.raises(ContractValidationError):
        service.get_run(other["workflow_session_id"], inherited[0])


def test_copy_manual_variables_shared_and_schema_changes(service):
    register = node(service.registry, "variable-register", 1, name="counter", valueType="integer", hasInitialValue=True, initialValue=5)
    output = node(service.registry, "output", 2)
    writer, convert, second = node(service.registry, "session-data-write", 3, value="original"), node(service.registry, "json-to-text", 4), node(service.registry, "output", 5)
    doc = document([register, output, writer, convert, second],
                   [edge(register, output, 1, "text"), edge(writer, convert, 2, "json"), edge(convert, second, 3)])
    final = run(service, create(service, doc))
    manual = service.update_data(final["workflow_session_id"], expected_revision=final["revision"],
        expected_data_revision=final["data_revision"], idempotency_key="m" * 128,
        variables={"counter": 42}, shared={CORE_SESSION_NOTE["key"]: "manual"})
    assert manual["head_revision"] == final["head_revision"]
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, manual, target)
    assert copied["data"]["values"]["counter"]["value"] == 42
    assert copied["data"]["data"][CORE_SESSION_NOTE["key"]]["value"] == "manual"
    target["workflow_definition_id"] = str(uuid4())
    target["nodes"][0]["config"].update(valueType="string", hasInitialValue=True, initialValue="new")
    with pytest.raises(ContractValidationError) as failure:
        copy_current(service, manual, target)
    assert failure.value.reason_code == "state_migration_required"
    with pytest.raises(ContractValidationError):
        service.get_definition(target["workflow_definition_id"])
    reset = copy_current(service, manual, target, mappings=[{"target_node_id": register["node_binding_id"], "action": "reset"}])
    assert reset["data"]["values"]["counter"]["value"] == "new"
    assert service.get_session(manual["workflow_session_id"])["data"]["values"]["counter"]["value"] == 42


def test_private_mapping_add_delete_replace_and_rebind(service):
    text, probe, output = node(service.registry, "text", 1), node(service.registry, PROBE_COMPONENT, 2), node(service.registry, "output", 3)
    doc = document([text, probe, output], [edge(text, probe, 1), edge(probe, output, 2, "left")])
    final = run(service, create(service, doc))
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    target["nodes"][1] = node(service.registry, "text", 2, text="replacement")
    target["edges"] = [edge(target["nodes"][1], output, 2)]
    target["nodes"].append(node(service.registry, PROBE_COMPONENT, 4))
    with pytest.raises(ContractValidationError) as failure:
        copy_current(service, final, target)
    assert failure.value.reason_code == "state_migration_required"
    copied = copy_current(service, final, target, mappings=[{"target_node_id": uid(2), "action": "reset"}])
    assert copied["private_states"][uid(2)] == {} and copied["private_states"][uid(4)] == {"count": 0}
    removed = deepcopy(doc)
    removed["revision"] = 2
    removed["nodes"] = [text, output]
    removed["edges"] = [edge(text, output, 1)]
    service.save_definition(removed, expected_revision=1, idempotency_key="rev2")
    rebound = service.rebind_session(final["workflow_session_id"], definition_revision=2,
        expected_revision=final["revision"], expected_data_revision=final["data_revision"],
        expected_head_revision=final["head_revision"], idempotency_key="rebind")
    assert uid(2) not in rebound["private_states"]
    assert service.get_run(rebound["workflow_session_id"], rebound["history_refs"][0])["node_runs"][1]["effects"]


def test_declared_private_migrator_used_without_core_branch(service):
    text, probe, output = node(service.registry, "text", 1), node(service.registry, PROBE_COMPONENT, 2), node(service.registry, "output", 3)
    doc = document([text, probe, output], [edge(text, probe, 1), edge(probe, output, 2, "left")])
    final = run(service, create(service, doc))
    service.registry.register(NodeDefinition("test.migrated", "1", "Migrated", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), capabilities=("private:read",),
        private_state_schema={"type": "object", "required": ["visits"], "additionalProperties": False,
                              "properties": {"visits": {"type": "integer"}}}, private_state_default={"visits": 0}),
        lambda config, inputs, context: {"output": text_value(str(context.private_read()["visits"]))},
        state_migrator=lambda state, source: {"visits": state["count"]})
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    target["nodes"][1] = node(service.registry, "test.migrated", 2)
    target["edges"] = [edge(target["nodes"][1], output, 2)]
    copied = copy_current(service, final, target)
    assert copied["private_states"][uid(2)] == {"visits": 1}


def test_failed_node_rejects_effect_and_stops_other_branch_then_closes(service):
    def fail(config, inputs, context):
        context.write_shared(CORE_SESSION_NOTE, "written before error")
        raise ValueError("test failure")
    service.registry.register(NodeDefinition("test.fail", "1", "Fail", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), capabilities=("shared:write",)), fail)
    failing, output = node(service.registry, "test.fail", 1), node(service.registry, "output", 2)
    later, second = node(service.registry, "text", 3), node(service.registry, "output", 4)
    view = run(service, create(service, document([failing, output, later, second], [edge(failing, output, 1), edge(later, second, 2)])))
    assert view["status"] == "failed"
    assert CORE_SESSION_NOTE["key"] not in view["data"].get("data", {})
    assert [entry["status"] for entry in view["nodes"]] == ["failed", "prepared", "prepared", "prepared"]
    closed = service.control(view["workflow_session_id"], action="close", expected_revision=view["revision"], idempotency_key="close")
    assert closed["can_submit"] and closed["status"] == "closed"
    assert service.get_run(closed["workflow_session_id"], closed["history_refs"][0])["node_runs"][0]["effects"]


def gate_graph(service, *, variable=False):
    entered, release = Event(), Event()
    def execute(config, inputs, context):
        entered.set()
        assert release.wait(5), "test gate timed out"
        return {"output": text_value("{{counter}}") if variable else inputs["input"]}
    service.registry.register(NodeDefinition("test.gate", "1", "Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),)), execute)
    first = node(service.registry, "variable-register", 1, name="counter", hasInitialValue=True, initialValue="old") if variable else node(service.registry, "text", 1)
    gate, replace, output = node(service.registry, "test.gate", 2), node(service.registry, "variable-replace", 3), node(service.registry, "output", 4)
    nodes = [first, gate, replace, output] if variable else [first, gate, output]
    edges = [edge(first, gate, 1, "text" if variable else "output"), edge(gate, replace if variable else output, 2)]
    if variable:
        edges.append(edge(replace, output, 3))
    initial = create(service, document(nodes, edges))
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="gate-start")
    assert entered.wait(5)
    return initial["workflow_session_id"], started["active_chain_run_id"], release


def test_pause_continue_once_at_boundary(service):
    sid, chain_id, release = gate_graph(service)
    try:
        view = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=view["revision"], idempotency_key="pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused" and paused["chains"][0]["next_node_index"] == 2
    request = {"action": "resume", "expected_revision": paused["revision"], "idempotency_key": "resume"}
    service.control(sid, **request)
    service.control(sid, **request)
    service.wait(chain_id)
    assert service.get_session(sid)["status"] == "succeeded"
    assert len(service.get_run(sid, chain_id)["node_runs"]) == 3


def test_replaying_original_pause_after_resume_does_not_pause_again(service):
    entered = {phase: Event() for phase in (1, 2)}
    released = {phase: Event() for phase in (1, 2)}
    calls = []

    def execute(config, inputs, context):
        phase = config["phase"]
        calls.append(phase)
        entered[phase].set()
        assert released[phase].wait(5), "test did not release the controlled node"
        return {"output": text_value("completed")}

    service.registry.register(NodeDefinition(
        "test.replay-gate", "1", "Replay gate", "Test", {"phase": 1},
        {"type": "object", "required": ["phase"], "additionalProperties": False,
         "properties": {"phase": {"enum": [1, 2]}}},
        inputs=(NodePort("input", "TEXT", required=False),),
        outputs=(NodePort("output", "TEXT"),)), execute)
    first = node(service.registry, "test.replay-gate", 1, phase=1)
    second = node(service.registry, "test.replay-gate", 2, phase=2)
    output = node(service.registry, "output", 3)
    initial = create(service, document(
        [first, second, output], [edge(first, second, 1), edge(second, output, 2)]))
    sid = initial["workflow_session_id"]
    started = service.start(sid, expected_revision=initial["revision"], idempotency_key="start")
    chain_id = started["active_chain_run_id"]
    try:
        assert entered[1].wait(5)
        current = service.get_session(sid)
        pause = {"action": "pause", "expected_revision": current["revision"], "idempotency_key": "pause"}
        receipt = service.control(sid, **pause)
        released[1].set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused"
        service.control(sid, action="resume", expected_revision=paused["revision"], idempotency_key="resume")
        assert entered[2].wait(5)
        before_replay = service.get_session(sid)
        assert service.control(sid, **pause) == receipt
        assert service.get_session(sid) == before_replay
    finally:
        for gate in released.values():
            gate.set()
        service.wait(chain_id)
    final = service.get_session(sid)
    assert final["status"] == "succeeded"
    assert calls == [1, 2]
    assert final["nodes"][-1]["outputs"]["output"]["text"] == "completed"
    assert len(service.get_run(sid, chain_id)["node_runs"]) == 3
    assert len(service.list_graph_candidates(sid)["candidates"]) == 1


def test_dynamic_variable_read_at_replacement_boundary(service):
    sid, chain_id, release = gate_graph(service, variable=True)
    try:
        view = service.get_session(sid)
        service.update_data(sid, expected_revision=view["revision"], expected_data_revision=view["data_revision"],
                            idempotency_key="manual-during-gate", variables={"counter": "new"})
    finally:
        release.set()
    service.wait(chain_id)
    final = service.get_session(sid)
    assert final["nodes"][-1]["outputs"]["output"]["text"] == "new"
    assert service.get_run(sid, chain_id)["node_runs"][2]["reads"][0]["revision"] == final["data_revision"]


def test_restart_does_not_replay_or_copy_active_state(service):
    sid, chain_id, release = gate_graph(service)
    try:
        view = service.get_session(sid)
        target = service.get_definition(view["workflow_definition_id"])
        target["workflow_definition_id"] = str(uuid4())
        with pytest.raises(ContractValidationError) as failure:
            copy_current(service, view, target)
        assert failure.value.reason_code == "active_execution_not_copyable"
        service.control(sid, action="pause", expected_revision=view["revision"], idempotency_key="pause")
    finally:
        release.set()
    service.wait(chain_id)
    service.close()
    with closing(GraphWorkflowService(service.database, registry=service.registry)) as restored:
        view = restored.get_session(sid)
        assert view["status"] == "recovery_unavailable" and len(view["chains"]) == 1
        with pytest.raises(ContractValidationError):
            restored.control(sid, action="resume", expected_revision=view["revision"], idempotency_key="restart-resume")
        closed = restored.control(sid, action="close", expected_revision=view["revision"], idempotency_key="restart-close")
        assert closed["can_submit"]


def test_fault_rollback_idempotency_conflict_and_cas(service):
    doc = text_graph(service.registry)
    initial = create(service, doc)
    with pytest.raises(ContractValidationError) as failure:
        service.create_session(doc["workflow_definition_id"], 1, idempotency_key="other")
        service.create_session(doc["workflow_definition_id"], 2, idempotency_key="other")
    assert failure.value.reason_code == "idempotency_conflict"
    def fault(boundary):
        if boundary == "before_commit":
            raise RuntimeError("test rollback")
    service._fault_injector = fault
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    with pytest.raises(RuntimeError):
        copy_current(service, initial, target)
    service._fault_injector = None
    with pytest.raises(ContractValidationError):
        service.get_definition(target["workflow_definition_id"])
    assert service.get_session(initial["workflow_session_id"]) == initial
    with pytest.raises(ContractValidationError) as stale:
        service.start(initial["workflow_session_id"], expected_revision=0, idempotency_key="stale")
    assert stale.value.reason_code == "invalid_request"


def test_single_live_graph_owner_and_legacy_coexistence(service):
    with pytest.raises(ContractValidationError) as failure:
        GraphWorkflowService(service.database)
    assert failure.value.reason_code == "service_already_owned"
    from phase1_agent.workflow import WorkflowService
    generic = run(service, create(service, text_graph(service.registry)))
    with closing(WorkflowService(database_path=service.database, mode="offline")) as legacy:
        legacy_sessions = legacy.list_sessions()
        assert generic["workflow_session_id"] not in {view["workflow_session_id"] for view in legacy_sessions}
        assert service.get_session(generic["workflow_session_id"])["status"] == "succeeded"
        from phase1_agent.contract_graph import validate_bundle
        with closing(SqliteStore(service.database)) as store:
            bundle = store.read_bundle(include_graph=True)
            assert validate_bundle(bundle) == bundle


def test_changed_same_version_declaration_cannot_execute_saved_definition(service):
    doc = text_graph(service.registry)
    session = create(service, doc)
    original = service.registry.get("workflow.text", "1")
    replacement = create_default_registry()
    replacement._nodes[("workflow.text", "1")] = replace(original, definition=replace(original.definition, display_name="Changed"))
    service.registry = replacement
    with pytest.raises(ContractValidationError) as failure:
        run(service, session)
    assert failure.value.reason_code == "node_definition_changed"
    assert service.get_session(session["workflow_session_id"])["chains"] == []
