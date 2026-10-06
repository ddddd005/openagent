"""Public runtime boundaries: ordering, references, authorization and rejection."""

from contextlib import closing
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import (
    create_content_package, prompt_content, text_content,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import GlobalResourceStore, global_resource_reference
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_records import is_graph_record, validate_graph_bundle
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, ResourceIdentity
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE, assemble_prompt, create_prompt_package
from phase1_agent.storage import SqliteStore
from phase1_agent.tool_package import create_tool_package


STATE_TYPE = "boundary.counter"
STATE_KEY = "counter/main"
VARIABLE_KEY = "variable/value"
EMPTY_SCHEMA = {"type": "object", "additionalProperties": False}


def uid(number):
    return str(UUID(int=number, version=4))


def node(registry, component, number, **config):
    entry = registry.get(component, "1")
    return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
            "title": component, "position": {"x": 0, "y": 0},
            "config": {**deepcopy(entry.definition.default_config), **config}}


def edge(number, source, target, *, source_port="output", target_port="input", order=0):
    return {"edge_id": uid(number), "source_node_id": source["node_binding_id"],
            "source_port_id": source_port, "target_node_id": target["node_binding_id"],
            "target_port_id": target_port, "order": order}


def control(number, source, target):
    return {"edge_id": uid(number), "source_node_id": source["node_binding_id"],
            "target_node_id": target["node_binding_id"]}


def document(nodes, edges=(), *, bindings=(), roots=(), controls=()):
    return {"schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Runtime boundaries", "nodes": list(nodes), "edges": list(edges),
            "object_bindings": list(bindings), "execution_roots": list(roots),
            "control_edges": list(controls)}


def installed():
    loaded = CapabilityPackageLoader((
        create_content_package(), create_tool_package(), create_prompt_package(),
    )).load({"workflow.tools": "1.0.0", "workflow.prompts": "1.0.0"})
    return loaded.registry.detached()


def create(service, doc):
    service.save_definition(doc, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def execute(service, view):
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def history(service, view):
    return service.get_run(view["workflow_session_id"], view["selected_chain_run_id"])


def bundle(service):
    with closing(SqliteStore(service.database)) as store:
        complete = store.read_bundle(include_graph=True)
    return {kind: [row for row in rows if is_graph_record(kind, row)] for kind, rows in complete.items()}


def register_counter_type(registry):
    registry.data_types.register(DataTypeDefinition(
        STATE_TYPE, 1,
        {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0}},
         "required": ["count"], "additionalProperties": False},
        default_value={"count": 0},
    ))


def counter_access():
    return ({"config_field": "key", "multiple": False, "access": "read_write",
             "type_id": STATE_TYPE, "schema_version": 1},)


def counter_binding(*nodes, key=STATE_KEY):
    identities = tuple(item["node_binding_id"] for item in nodes)
    return ObjectBinding(key, STATE_TYPE, 1, "shared",
                         readers=identities, writers=identities).to_dict()


def register_writer(registry, *, component="boundary.write", failure=None, calls=None, input_port=False):
    def write(config, inputs, context):
        if calls is not None:
            calls.append(context.node_binding_id)
        basis = context.object_read(config["key"])
        value = {"count": basis["value"]["count"] + 1}
        context.object_write(config["key"], value, expected_revision=basis["revision"])
        if failure == "raises":
            raise ValueError("Rejected after creating the write intent")
        return {"output": {"schema_version": 2, "kind": "workflow.text", "text": 7}
                if failure == "invalid-output" else text_content(str(value["count"]))}

    registry.register(NodeDefinition(
        component, "1", "Counter", "Test", {"key": STATE_KEY},
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
         "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),) if input_port else (),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("objects:read", "objects:write"), input_storage="references",
        object_accesses=counter_access(),
    ), write)


def materials(number, text):
    return prompt_content([{
        "item_instance_id": uid(number), "text": text, "role": "system",
        "placement": "before", "depth": None, "order": 0, "enabled": True,
        "purpose": "prompt", "protected": False,
        "source": {"kind": "boundary-fixture", "id": uid(number)}, "metadata": {},
    }])


def prompt_resource(service, reference):
    record = {**reference, "data_schema_version": 1, "update_sequence": 1,
              "value": {"enabled": True, "members": []}}
    with closing(SqliteStore(service.database)) as store:
        GlobalResourceStore(store, service.registry.data_types).write(
            record, expected_sequence=0, idempotency_key=str(uuid4()))


def test_multi_identity_source_cannot_expand_downstream_authority_or_commit_earlier_effects(tmp_path):
    registry = installed()
    register_counter_type(registry)
    calls = []
    register_writer(registry, calls=calls)
    references = [ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, uid(number)).to_dict()
                  for number in (800, 801)]
    registry.register(NodeDefinition(
        "boundary.multi-resource", "1", "Two references", "Test", {}, EMPTY_SCHEMA,
        outputs=(NodePort("a", "GLOBAL_RESOURCE_REF"), NodePort("b", "GLOBAL_RESOURCE_REF")),
        capabilities=("resources:read",), input_storage="references",
    ), lambda config, inputs, context: {
        "a": global_resource_reference(references[0]), "b": global_resource_reference(references[1]),
    }, resource_dependencies_declaration=lambda config: [
        {"kind": "global-resource", "reference": deepcopy(reference)} for reference in references])
    writer = node(registry, "boundary.write", 1)
    source = node(registry, "boundary.multi-resource", 2)
    resolve = node(registry, "prompts.global-resolve", 3)
    output = node(registry, "tools.output", 4, mode="prompt")
    doc = document([writer, source, resolve, output], [
        edge(100, source, resolve, source_port="a"), edge(101, resolve, output),
    ], bindings=[counter_binding(writer)], roots=[writer["node_binding_id"]])
    with closing(GraphWorkflowService(tmp_path / "resource.sqlite", registry=registry)) as service:
        for reference in references:
            prompt_resource(service, reference)
        initial = create(service, doc)
        with pytest.raises(ContractValidationError) as caught:
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "graph_resource_dependency_undeclared"
        after = service.get_session(initial["workflow_session_id"])
        assert calls == []
        assert after["objects"] == initial["objects"]
        assert after["active_chain_run_id"] is None and after["chains"] == []
        assert bundle(service).get("node_run", []) == []
        assert not service._resource_frames


def variable_graph(registry, *, ordered=True, reverse_ids=False):
    ids = (10, 20, 80, 100) if reverse_ids else (100, 80, 20, 10)
    register = node(registry, "tools.variable-register", ids[0], has_initial=True, initial_value="seed")
    assign = node(registry, "tools.variable-assign", ids[1], value="assigned")
    read = node(registry, "tools.variable-to-content", ids[2])
    output = node(registry, "tools.output", ids[3])
    binding = ObjectBinding(
        VARIABLE_KEY, "workflow.variable", 1, "shared",
        readers=(register["node_binding_id"], assign["node_binding_id"], read["node_binding_id"]),
        writers=(register["node_binding_id"], assign["node_binding_id"]),
    ).to_dict()
    doc = document([output, read, assign, register], [edge(500, read, output)], bindings=[binding],
                   roots=[register["node_binding_id"], assign["node_binding_id"]],
                   controls=[control(600, register, assign), control(601, assign, read)] if ordered else [])
    return doc, register, assign, read, output


@pytest.mark.parametrize("reverse_ids", [False, True])
def test_unordered_variable_read_write_is_rejected_independently_of_uuid_order(reverse_ids):
    registry = installed()
    doc, *_ = variable_graph(registry, ordered=False, reverse_ids=reverse_ids)
    with pytest.raises(ContractValidationError) as caught:
        GraphCompiler(registry).compile(doc)
    assert caught.value.reason_code == "graph_unordered_object_access"


@pytest.mark.parametrize("reverse_ids", [False, True])
def test_control_dependencies_run_registration_assignment_and_read_in_that_order(tmp_path, reverse_ids):
    registry = installed()
    doc, register, assign, read, output = variable_graph(registry, reverse_ids=reverse_ids)
    with closing(GraphWorkflowService(tmp_path / "ordered.sqlite", registry=registry)) as service:
        final = execute(service, create(service, doc))
        assert final["status"] == "succeeded"
        records = history(service, final)
        assert records["chain"]["ordered_nodes"] == [
            entry["node_binding_id"] for entry in (register, assign, read, output)]
        assert final["objects"][VARIABLE_KEY]["value"]["value"] == "assigned"
        assert next(entry for entry in final["nodes"]
                    if entry["node_binding_id"] == output["node_binding_id"])["outputs"]["output"] == text_content("assigned")


@pytest.fixture
def accepted_bundle(tmp_path):
    registry = installed()
    doc, register, assign, read, output = variable_graph(registry)
    with closing(GraphWorkflowService(tmp_path / "records.sqlite", registry=registry)) as service:
        final = execute(service, create(service, doc))
        assert final["status"] == "succeeded"
        records = bundle(service)
    validate_graph_bundle(records)
    return records, read["node_binding_id"], output["node_binding_id"]


@pytest.mark.parametrize("tamper", [
    "empty-inputs", "duplicate-input", "unknown-input-output", "wrong-input-output",
    "unknown-input-edge", "empty-controls", "wrong-control-run", "wrong-output-owner",
])
def test_successful_reference_only_records_cannot_lose_or_forge_binding_evidence(accepted_bundle, tamper):
    original, read_id, output_id = accepted_bundle
    records = deepcopy(original)
    runs = {row["node_binding_id"]: row for row in records["node_run"]}
    read, output = runs[read_id], runs[output_id]
    assert output["schema_version"] == 4 and output["input_storage"] == "references"
    assert output["input_values"] == {}
    if tamper == "empty-inputs":
        output["input_refs"] = {}
    elif tamper == "duplicate-input":
        output["input_refs"]["input"].append(deepcopy(output["input_refs"]["input"][0]))
    elif tamper == "unknown-input-output":
        output["input_refs"]["input"][0]["output_id"] = uid(9990)
    elif tamper == "wrong-input-output":
        output["input_refs"]["input"][0]["output_id"] = output["output_refs"]["output"]
    elif tamper == "unknown-input-edge":
        output["input_refs"]["input"][0]["edge_id"] = uid(9991)
    elif tamper == "empty-controls":
        read["control_refs"] = []
    elif tamper == "wrong-control-run":
        read["control_refs"][0]["run_id"] = read["run_id"]
    else:
        output["output_refs"]["output"] = read["output_refs"]["output"]
    with pytest.raises(ContractValidationError) as caught:
        validate_graph_bundle(records)
    assert caught.value.reason_code == "storage_contract_violation"


@pytest.mark.parametrize("foreign", [False, True], ids=["unknown-output", "another-session-output"])
def test_json_prompt_conversion_rejects_unknown_or_other_session_artifact_references(tmp_path, foreign):
    registry = installed()
    with closing(GraphWorkflowService(tmp_path / "foreign.sqlite", registry=registry)) as service:
        if foreign:
            source = node(registry, "prompts.source", 1, value=materials(1000, "past"))
            assembly = node(registry, "prompts.assembly", 2)
            output = node(registry, "tools.output", 3, mode="prompt")
            earlier = execute(service, create(service, document([source, assembly, output], [
                edge(30, source, assembly), edge(31, assembly, output)])))
            assert earlier["status"] == "succeeded"
            frozen = next(entry for entry in earlier["nodes"]
                          if entry["node_binding_id"] == assembly["node_binding_id"])["outputs"]["output"]
        else:
            frozen = assemble_prompt([materials(1000, "fake")],
                source_output_refs=[{"edge_id": uid(30), "output_id": uid(9999), "order": 0}])
        text = node(registry, "tools.text", 11, text=canonical_bytes(frozen).decode("utf-8"))
        parse = node(registry, "tools.text-to-json", 12)
        convert = node(registry, "tools.json-to-prompt", 13)
        output = node(registry, "tools.output", 14, mode="prompt")
        doc = document([text, parse, convert, output], [
            edge(40, text, parse), edge(41, parse, convert), edge(42, convert, output)])
        final = execute(service, create(service, doc))
        assert final["status"] == "failed"
        records = history(service, final)
        rejected = next(row for row in records["node_runs"]
                        if row["node_binding_id"] == convert["node_binding_id"])
        assert rejected["diagnostic"]["code"] == "graph_artifact_reference_denied"
        assert rejected["output_refs"] == {}
        assert all(row["node_binding_id"] != convert["node_binding_id"] for row in records["outputs"])


@pytest.mark.parametrize("tamper", ["valid", "edge", "order", "source-port"])
def test_accepted_output_ids_still_require_matching_graph_edge_order_and_source_port(tmp_path, tamper):
    registry = installed()
    registry.register(NodeDefinition(
        "boundary.dual", "1", "Two materials", "Test", {}, EMPTY_SCHEMA,
        outputs=(NodePort("left", "PROMPT", data_schema_version=2),
                 NodePort("right", "PROMPT", data_schema_version=2)), input_storage="references",
    ), lambda config, inputs, context: {"left": materials(1001, "left"), "right": materials(1002, "right")})

    def assemble(config, inputs, context):
        ref = context.input_artifact_refs("left")[0]
        if tamper == "edge":
            ref["edge_id"] = uid(9995)
        elif tamper == "order":
            ref["order"] += 1
        elif tamper == "source-port":
            ref["edge_id"] = context.input_artifact_refs("right")[0]["edge_id"]
        return {"output": assemble_prompt([inputs["left"]], source_output_refs=[ref])}

    registry.register(NodeDefinition(
        "boundary.assembly", "1", "Assembly", "Test", {}, EMPTY_SCHEMA,
        inputs=(NodePort("left", "PROMPT", data_schema_version=2),
                NodePort("right", "PROMPT", data_schema_version=2)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=2),), input_storage="references",
    ), assemble)
    source = node(registry, "boundary.dual", 1)
    assembly = node(registry, "boundary.assembly", 2)
    output = node(registry, "tools.output", 3, mode="prompt")
    doc = document([source, assembly, output], [
        edge(50, source, assembly, source_port="left", target_port="left"),
        edge(51, source, assembly, source_port="right", target_port="right"),
        edge(52, assembly, output)])
    with closing(GraphWorkflowService(tmp_path / "bindings.sqlite", registry=registry)) as service:
        final = execute(service, create(service, doc))
        records = history(service, final)
        produced = next(row for row in records["node_runs"]
                        if row["node_binding_id"] == assembly["node_binding_id"])
        if tamper == "valid":
            assert final["status"] == "succeeded"
            assert produced["output_refs"]
        else:
            assert final["status"] == "failed"
            assert produced["diagnostic"]["code"] == "graph_artifact_binding_mismatch"
            assert produced["output_refs"] == {}
            assert all(row["node_binding_id"] != assembly["node_binding_id"] for row in records["outputs"])


@pytest.mark.parametrize("failure", ["invalid-output", "raises"])
def test_rejected_node_keeps_prior_accepted_state_but_saves_neither_its_write_nor_output(tmp_path, failure):
    registry = installed()
    register_counter_type(registry)
    register_writer(registry)
    register_writer(registry, component="boundary.reject", failure=failure, input_port=True)
    writer = node(registry, "boundary.write", 1)
    reject = node(registry, "boundary.reject", 2)
    output = node(registry, "tools.output", 3)
    doc = document([writer, reject, output], [
        edge(60, writer, reject), edge(61, reject, output)],
        bindings=[counter_binding(writer, reject)])
    with closing(GraphWorkflowService(tmp_path / "failure.sqlite", registry=registry)) as service:
        initial = create(service, doc)
        final = execute(service, initial)
        assert final["status"] == "failed"
        assert final["objects"][STATE_KEY]["value"] == {"count": 1}
        assert final["objects"][STATE_KEY]["revision"] == initial["objects"][STATE_KEY]["revision"] + 1
        records = history(service, final)
        rejected = next(row for row in records["node_runs"]
                        if row["node_binding_id"] == reject["node_binding_id"])
        assert rejected["status"] == "failed" and rejected["output_refs"] == {}
        assert records["chain"]["completed_nodes"] == [writer["node_binding_id"]]
        assert [row["node_binding_id"] for row in records["outputs"]] == [writer["node_binding_id"]]


@pytest.mark.parametrize("declared", [False, True], ids=["missing-declaration", "outside-declared-key"])
def test_reference_mode_node_cannot_read_bound_objects_outside_its_access_declaration(tmp_path, declared):
    registry = installed()
    register_counter_type(registry)

    def read(config, inputs, context):
        basis = context.object_read("counter/other" if declared else STATE_KEY)
        return {"output": text_content(str(basis["value"]["count"]))}

    registry.register(NodeDefinition(
        "boundary.undeclared", "1", "Undeclared object access", "Test", {"key": STATE_KEY},
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
         "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("objects:read", "objects:write"), input_storage="references",
        object_accesses=counter_access() if declared else (),
    ), read)
    source = node(registry, "boundary.undeclared", 1)
    output = node(registry, "tools.output", 2)
    doc = document([source, output], [edge(70, source, output)], bindings=[
        counter_binding(source), counter_binding(source, key="counter/other")])
    with closing(GraphWorkflowService(tmp_path / "access.sqlite", registry=registry)) as service:
        initial = create(service, doc)
        final = execute(service, initial)
        assert final["status"] == "failed"
        rejected = next(row for row in history(service, final)["node_runs"]
                        if row["node_binding_id"] == source["node_binding_id"])
        assert rejected["diagnostic"]["code"] == "graph_object_access_undeclared"
        assert rejected["output_refs"] == {}
        assert final["objects"] == initial["objects"]


@pytest.mark.parametrize("serialized", [False, True], ids=["json", "json-text-json"])
@pytest.mark.parametrize("with_materials", [False, True], ids=["current-only", "materials-and-current"])
def test_assembled_prompt_roundtrip_preserves_exact_current_input_and_artifact_bindings(
        tmp_path, serialized, with_materials):
    registry = installed()
    current = node(registry, "tools.text", 1, text="current question")
    assembly = node(registry, "prompts.assembly", 2)
    to_json = node(registry, "tools.prompt-to-json", 3)
    to_prompt = node(registry, "tools.json-to-prompt", 6)
    output = node(registry, "tools.output", 7, mode="prompt")
    nodes = [current, assembly, to_json, to_prompt, output]
    edges = [edge(101, current, assembly, target_port="current_input"),
             edge(102, assembly, to_json), edge(105, to_prompt, output)]
    if with_materials:
        source = node(registry, "prompts.source", 8, value=materials(1001, "instructions"))
        nodes.append(source)
        edges.append(edge(100, source, assembly))
    if serialized:
        serialize = node(registry, "tools.json-to-text", 4)
        parse = node(registry, "tools.text-to-json", 5)
        nodes.extend([serialize, parse])
        edges.extend([edge(103, to_json, serialize), edge(104, serialize, parse),
                      edge(106, parse, to_prompt)])
    else:
        edges.append(edge(103, to_json, to_prompt))
    database = tmp_path / "assembled-roundtrip.sqlite"
    with closing(GraphWorkflowService(database, registry=registry)) as service:
        final = execute(service, create(service, document(nodes, edges)))
        assert final["status"] == "succeeded"
        outputs = {entry["node_binding_id"]: entry["outputs"]["output"] for entry in final["nodes"]}
        original = outputs[assembly["node_binding_id"]]
        assert outputs[output["node_binding_id"]] == original
        assert original["assembly"]["messages"] == (
            [{"role": "system", "content": "instructions"}] if with_materials else []
        ) + [{"role": "user", "content": "current question"}]
        accepted = history(service, final)
        assert all(row["input_values"] == {} for row in accepted["node_runs"])
        validate_graph_bundle(bundle(service))
    with closing(GraphWorkflowService(database, registry=registry)) as reopened:
        assert reopened.get_session(final["workflow_session_id"])["nodes"] == final["nodes"]
        assert reopened.get_run(final["workflow_session_id"], final["selected_chain_run_id"]) == accepted


def assembled_graph(registry, transform, *, with_materials, token):
    current = node(registry, "tools.text", 1, text="current " + token)
    assembly = node(registry, "prompts.assembly", 2)
    nodes = [current, assembly, transform]
    edges = [edge(100, current, assembly, target_port="current_input"),
             edge(101, assembly, transform)]
    if with_materials:
        before = materials(1001, "before " + token)
        after = materials(1002, "after " + token)
        after["items"][0]["placement"] = "after"
        source = node(registry, "prompts.source", 8,
                      value=prompt_content(before["items"] + after["items"]))
        nodes.append(source)
        edges.append(edge(102, source, assembly))
    return nodes, edges, assembly


@pytest.mark.parametrize("with_materials", [False, True], ids=["current-only", "before-current-after"])
def test_assembled_prompt_text_projection_uses_actual_message_order_and_current_input(
        tmp_path, with_materials):
    registry = installed()
    project = node(registry, "tools.prompt-to-text", 3, separator="|")
    nodes, edges, assembly = assembled_graph(
        registry, project, with_materials=with_materials, token="question")
    output = node(registry, "tools.output", 4)
    nodes.append(output)
    edges.append(edge(103, project, output))
    with closing(GraphWorkflowService(tmp_path / "projection.sqlite", registry=registry)) as service:
        final = execute(service, create(service, document(nodes, edges)))
        assert final["status"] == "succeeded"
        payloads = {entry["node_binding_id"]: entry["outputs"]["output"] for entry in final["nodes"]}
        assert payloads[output["node_binding_id"]] == text_content(
            "before question|current question|after question" if with_materials else "current question")
        assert payloads[assembly["node_binding_id"]]["assembly"]["current_input"] == {
            "role": "user", "content": "current question"}


@pytest.mark.parametrize("with_materials", [False, True], ids=["current-only", "before-current-after"])
@pytest.mark.parametrize("operation", ["regex", "variable-replace-all", "variable-replace-selected"])
def test_assembled_prompt_transform_rebuilds_current_input_and_retains_source_artifact_bindings(
        tmp_path, with_materials, operation):
    registry = installed()
    variable = operation != "regex"
    config = ({"mode": "prompt", "scope": "all", "object_keys": [VARIABLE_KEY]}
              if variable else {"mode": "prompt", "scope": "body",
                                "pattern": "question", "replacement": "answer"})
    if operation == "variable-replace-selected":
        config["names"] = ["value"]
    transform = node(registry, "tools." + operation, 3, **config)
    nodes, edges, assembly = assembled_graph(
        registry, transform, with_materials=with_materials, token="{{value}}" if variable else "question")
    output = node(registry, "tools.output", 4, mode="prompt")
    nodes.append(output)
    edges.append(edge(103, transform, output))
    bindings, controls = [], []
    if variable:
        register = node(registry, "tools.variable-register", 9, has_initial=True, initial_value="answer")
        nodes.append(register)
        controls.append(control(200, register, transform))
        bindings.append(ObjectBinding(
            VARIABLE_KEY, "workflow.variable", 1, "shared",
            readers=(register["node_binding_id"], transform["node_binding_id"]),
            writers=(register["node_binding_id"],),
        ).to_dict())
    with closing(GraphWorkflowService(tmp_path / "transform.sqlite", registry=registry)) as service:
        final = execute(service, create(service, document(
            nodes, edges, bindings=bindings, controls=controls)))
        assert final["status"] == "succeeded"
        payloads = {entry["node_binding_id"]: entry["outputs"].get("output") for entry in final["nodes"]}
        original = payloads[assembly["node_binding_id"]]
        transformed = payloads[transform["node_binding_id"]]
        assert transformed["stage"] == "assembled"
        assert transformed["assembly"]["messages"] == (
            [{"role": "system", "content": "before answer"}] if with_materials else []
        ) + [{"role": "user", "content": "current answer"}] + (
            [{"role": "system", "content": "after answer"}] if with_materials else []
        )
        assert transformed["assembly"]["current_input"] == {"role": "user", "content": "current answer"}
        assert transformed["assembly"]["manifest"] == original["assembly"]["manifest"]
        assert original["assembly"]["current_input"]["content"] == (
            "current {{value}}" if variable else "current question")
        assert payloads[output["node_binding_id"]] == transformed
        validate_graph_bundle(bundle(service))
