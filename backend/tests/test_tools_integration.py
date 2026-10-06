"""Zero-Agent general-tools acceptance in isolated graph-service databases."""

from contextlib import closing
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.tool_package import create_tool_package


KEY = "variable/value"


def uid(number):
    return str(UUID(int=number, version=4))


def new_registry():
    return CapabilityPackageLoader((create_tool_package(), create_content_package())).load(
        {"workflow.tools": "1.0.0"}).registry


def node(registry, name, number, **config):
    entry = registry.get("tools." + name, "1")
    return {"node_binding_id": uid(number), "component_id": "tools." + name, "component_version": "1",
            "title": name, "position": {"x": number * 100, "y": 0},
            "config": {**deepcopy(entry.definition.default_config), **config}}


def edge(source, target, number, *, source_port="output", target_port="input"):
    return {"edge_id": uid(1000 + number), "source_node_id": source["node_binding_id"],
            "source_port_id": source_port, "target_node_id": target["node_binding_id"],
            "target_port_id": target_port, "order": 0}


def control(source, target, number):
    return {"edge_id": uid(2000 + number), "source_node_id": source["node_binding_id"],
            "target_node_id": target["node_binding_id"]}


def document(registry, nodes, edges=(), *, roots=(), controls=(), bindings=()):
    return {"schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "General-tools acceptance", "nodes": nodes, "edges": list(edges),
            "execution_roots": [item["node_binding_id"] for item in roots],
            "control_edges": list(controls), "object_bindings": list(bindings),
            "package_lock": list(registry.package_lock)}


def variable_binding(*, readers, writers):
    return ObjectBinding(KEY, "workflow.variable", 1, "shared",
                         readers=tuple(item["node_binding_id"] for item in readers),
                         writers=tuple(item["node_binding_id"] for item in writers)).to_dict()


def create(service, doc):
    service.save_definition(doc, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def run(service, view, *, inputs=None, idempotency_key=None):
    response = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                             inputs=inputs or {}, idempotency_key=idempotency_key or str(uuid4()))
    service.wait(response["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def history(service, view):
    return service.get_run(view["workflow_session_id"], view["chains"][-1]["chain_run_id"])


def payload(view, source):
    return next(item for item in view["nodes"]
                if item["node_binding_id"] == source["node_binding_id"])["outputs"]["output"]


@pytest.fixture
def service(tmp_path):
    def no_model(*args, **kwargs):
        pytest.fail("A zero-Agent tools workflow attempted to construct a model")
    with closing(GraphWorkflowService(tmp_path / "tools.sqlite", registry=new_registry(),
                                      model_factory=no_model)) as instance:
        yield instance


def counter_graph(registry):
    register = node(registry, "variable-register", 1, value_type="integer",
                    has_initial=True, initial_value=0)
    increment = node(registry, "variable-assign", 2, operation="add", value=1)
    source = node(registry, "variable-to-content", 3)
    output = node(registry, "output", 4)
    source["public_outputs"] = ["output"]
    doc = document(registry, [register, increment, source, output], [edge(source, output, 1)],
                   controls=[control(register, increment, 1), control(increment, source, 2)],
                   bindings=[variable_binding(readers=[register, increment, source],
                                              writers=[register, increment])])
    return doc, register, increment, source, output


def test_control_dependencies_execute_no_output_writers_before_consumers(service):
    doc, register, increment, source, output = counter_graph(service.registry)
    initial = create(service, doc)
    assert initial["objects"][KEY]["value"]["registered"] is False
    final = run(service, initial, idempotency_key="first-round")
    assert final["status"] == "succeeded" and final["messages"] == []
    assert payload(final, output)["text"] == "1"
    assert final["objects"][KEY]["value"]["value"] == 1
    evidence = history(service, final)
    assert [item["node_binding_id"] for item in evidence["node_runs"]] == [
        item["node_binding_id"] for item in [register, increment, source, output]]
    for item in evidence["node_runs"]:
        assert item["schema_version"] == 4
        assert item["input_storage"] == "references" and item["input_values"] == {}
    assert evidence["node_runs"][0]["output_refs"] == {}
    assert evidence["node_runs"][1]["output_refs"] == {}
    assert [item["run_id"] for item in evidence["node_runs"][1]["control_refs"]] == [
        evidence["node_runs"][0]["run_id"]]
    assert evidence["node_runs"][2]["reads"][0]["revision"] == 3
    assert evidence["node_runs"][3]["input_refs"]["input"][0]["output_id"] == (
        evidence["node_runs"][2]["output_refs"]["output"])
    replay = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                           inputs={}, idempotency_key="first-round")
    assert replay["active_chain_run_id"] == final["chains"][-1]["chain_run_id"]
    assert service.get_session(final["workflow_session_id"])["objects"][KEY]["revision"] == 3


def test_explicit_no_output_root_can_run_without_any_content_output_node(service):
    register = node(service.registry, "variable-register", 1, has_initial=True, initial_value="ready")
    doc = document(service.registry, [register], roots=[register],
                   bindings=[variable_binding(readers=[register], writers=[register])])
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded"
    assert final["objects"][KEY]["value"]["value"] == "ready"
    evidence = history(service, final)
    assert evidence["outputs"] == []
    assert evidence["node_runs"][0]["output_refs"] == {}


def test_unconnected_registration_assignment_and_content_source_stay_dormant(service):
    register = node(service.registry, "variable-register", 1, has_initial=True, initial_value="unused")
    bad_assign = node(service.registry, "variable-assign", 2, object_key="unbound/must-not-run")
    bad_source = node(service.registry, "variable-to-content", 3, object_key="unbound/must-not-run")
    literal = node(service.registry, "text", 4, text="literal {{value}}")
    output = node(service.registry, "output", 5)
    doc = document(service.registry, [register, bad_assign, bad_source, literal, output],
                   [edge(literal, output, 1)],
                   bindings=[variable_binding(readers=[register], writers=[register])])
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded"
    assert payload(final, output)["text"] == "literal {{value}}"
    assert final["objects"][KEY]["value"]["registered"] is False
    assert {item["node_binding_id"] for item in history(service, final)["node_runs"]} == {
        literal["node_binding_id"], output["node_binding_id"]}


def test_cross_round_reopen_copy_fork_and_candidate_restore_preserve_variable_objects(service):
    doc, _, _, source, output = counter_graph(service.registry)
    first = run(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]["candidate_id"]
    second = run(service, first)
    assert second["objects"][KEY]["value"]["value"] == 2
    assert second["objects"][KEY]["revision"] == first["objects"][KEY]["revision"] + 1
    fork = service.fork_graph_candidate(second["workflow_session_id"], candidate_id=candidate,
        expected_revision=second["revision"], expected_data_revision=second["data_revision"],
        expected_head_revision=second["head_revision"], idempotency_key="fork-first")
    assert fork["objects"][KEY]["value"]["value"] == 1
    assert fork["objects"][KEY]["revision_id"] == first["objects"][KEY]["revision_id"]
    child = run(service, fork)
    assert payload(child, output)["text"] == "2"
    assert service.get_session(second["workflow_session_id"])["objects"][KEY]["value"]["value"] == 2
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    copied = service.copy_session(second["workflow_session_id"], document=target,
        expected_session_revision=second["revision"], expected_data_revision=second["data_revision"],
        expected_definition_revision=second["definition_revision"],
        expected_head_revision=second["head_revision"], idempotency_key="full-copy")
    assert copied["objects"][KEY]["value"]["value"] == 2
    copied_round = run(service, copied)
    assert copied_round["objects"][KEY]["value"]["value"] == 3
    restored = service.select_graph_candidate(second["workflow_session_id"], candidate_id=candidate,
        expected_revision=second["revision"], expected_data_revision=second["data_revision"],
        expected_head_revision=second["head_revision"], idempotency_key="restore-first")
    assert restored["objects"][KEY]["value"]["value"] == 1
    assert restored["objects"][KEY]["revision"] > second["objects"][KEY]["revision"]
    database = service.database
    service.close()
    with closing(GraphWorkflowService(database, registry=new_registry())) as reopened:
        saved = reopened.get_session(first["workflow_session_id"])
        assert saved["objects"] == restored["objects"]
        again = run(reopened, saved)
        assert payload(again, source)["text"] == "2"
        assert reopened.get_session(child["workflow_session_id"])["objects"][KEY]["value"]["value"] == 2


def test_later_assignment_does_not_rewrite_already_produced_replacement(service):
    register = node(service.registry, "variable-register", 1, has_initial=True, initial_value="Alice")
    literal = node(service.registry, "text", 2, text="hello {{value}}")
    replace = node(service.registry, "variable-replace-all", 3, object_keys=[KEY])
    assign = node(service.registry, "variable-assign", 4, value="Bob")
    late_source = node(service.registry, "variable-to-content", 5)
    early_output = node(service.registry, "output", 6)
    late_output = node(service.registry, "output", 7)
    replace["public_outputs"] = ["output"]
    doc = document(service.registry,
        [register, literal, replace, assign, late_source, early_output, late_output],
        [edge(literal, replace, 1), edge(replace, early_output, 2), edge(late_source, late_output, 3)],
        controls=[control(register, replace, 1), control(replace, assign, 2), control(assign, late_source, 3)],
        bindings=[variable_binding(readers=[register, replace, assign, late_source], writers=[register, assign])])
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded"
    assert payload(final, early_output)["text"] == "hello Alice"
    assert payload(final, late_output)["text"] == "Bob"
    evidence = history(service, final)
    replacement_run = next(row for row in evidence["node_runs"]
                           if row["node_binding_id"] == replace["node_binding_id"])
    source_run = next(row for row in evidence["node_runs"]
                      if row["node_binding_id"] == late_source["node_binding_id"])
    assert replacement_run["reads"][0]["revision"] == 2
    assert source_run["reads"][0]["revision"] == 3
    public = service.read_public_output(final["workflow_session_id"],
        workflow_definition_id=doc["workflow_definition_id"], definition_revision=1,
        node_id=replace["node_binding_id"], port_id="output")
    assert public["output"]["payload"]["text"] == "hello Alice"


def test_zero_agent_json_regex_prompt_roundtrip_uses_reference_inputs(service):
    current = node(service.registry, "current-input", 1)
    parse = node(service.registry, "text-to-json", 2)
    regex = node(service.registry, "regex", 3, mode="json", scope="all",
                 pattern="apple", replacement="pear")
    serialize = node(service.registry, "json-to-text", 4)
    prompt = node(service.registry, "text-to-prompt", 5)
    to_json = node(service.registry, "prompt-to-json", 6)
    to_prompt = node(service.registry, "json-to-prompt", 7)
    project = node(service.registry, "prompt-to-text", 8)
    output = node(service.registry, "output", 9)
    nodes = [current, parse, regex, serialize, prompt, to_json, to_prompt, project, output]
    doc = document(service.registry, nodes,
                   [edge(nodes[index], nodes[index + 1], index + 1) for index in range(len(nodes) - 1)])
    final = run(service, create(service, doc), inputs={"text": '{"apple":["apple",3,false]}'})
    assert final["status"] == "succeeded"
    assert payload(final, output)["text"] == '{"pear":["pear",3,false]}'
    evidence = history(service, final)
    for row in evidence["node_runs"][1:]:
        assert row["input_values"] == {} and row["input_refs"]["input"][0]["output_id"]


@pytest.mark.parametrize("failure", ["unregistered", "type", "unauthorized"])
def test_variable_failure_has_durable_state_without_a_success_candidate(service, failure):
    register = node(service.registry, "variable-register", 1, value_type="integer",
                    has_initial=True, initial_value=7)
    assign = node(service.registry, "variable-assign", 2, value=True if failure == "type" else 4)
    readers = [register, assign] if failure != "unauthorized" else [register]
    controls = [control(register, assign, 1)] if failure != "unregistered" else []
    doc = document(service.registry, [register, assign], roots=[assign], controls=controls,
                   bindings=[variable_binding(readers=readers, writers=[register, assign])])
    initial = create(service, doc)
    if failure == "unauthorized":
        with pytest.raises(ContractValidationError) as denied:
            run(service, initial)
        assert denied.value.reason_code == "session_object_access_denied"
        assert service.get_session(initial["workflow_session_id"]) == initial
        return
    final = run(service, initial)
    assert final["status"] == "failed"
    assert service.list_graph_candidates(final["workflow_session_id"])["candidates"] == []
    expected = {"unregistered": "variable_not_registered", "type": "variable_type_mismatch",
                "unauthorized": "session_object_access_denied"}[failure]
    assert history(service, final)["node_runs"][-1]["diagnostic"]["code"] == expected
    if failure == "unregistered":
        assert not final["objects"][KEY]["value"]["registered"]
    else:
        assert final["objects"][KEY]["value"]["value"] == 7
    assert final["head_commit_id"] == initial["head_commit_id"]


def test_tools_are_discovered_and_project_selection_survives_reopen(tmp_path):
    path = tmp_path / "package-selection.sqlite"
    entrypoints = ("phase1_agent.tool_package:create_tool_package",
                   "phase1_agent.content_contracts:create_content_package")
    with closing(GraphWorkflowService(path, trusted_package_entrypoints=entrypoints,
                enabled_packages={"workflow.tools": "1.0.0"})) as configured:
        doc, _, _, _, output = counter_graph(configured.registry)
        first = run(configured, create(configured, doc))
    with closing(GraphWorkflowService(path, trusted_package_entrypoints=entrypoints)) as reopened:
        assert {"package_id": "workflow.tools", "version": "1.0.0"} in (
            reopened.platform_capabilities()["package_lock"])
        final = run(reopened, reopened.get_session(first["workflow_session_id"]))
        assert payload(final, output)["text"] == "2"


def test_control_cycle_and_data_links_to_no_output_writers_are_rejected(service):
    doc, register, increment, source, _ = counter_graph(service.registry)
    cyclic = deepcopy(doc)
    cyclic["control_edges"].append(control(source, register, 9))
    from phase1_agent.graph_contracts import GraphCompiler
    with pytest.raises(ContractValidationError) as cycle:
        GraphCompiler(service.registry).compile(cyclic)
    assert cycle.value.reason_code == "graph_dependency_cycle"
    invalid = deepcopy(doc)
    invalid["edges"].append(edge(register, increment, 9, target_port="value"))
    with pytest.raises(ContractValidationError) as unknown_port:
        GraphCompiler(service.registry).compile(invalid)
    assert unknown_port.value.reason_code == "graph_unknown_port"
