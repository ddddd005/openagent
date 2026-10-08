"""Lorebook package contracts, exact inputs and read-only object permissions."""

from copy import deepcopy
from uuid import UUID

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package, object_schema, text_content
from phase1_agent.context_package import create_context_package
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.context_v4 import validate_native_context_view
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from phase1_agent.lorebook_engine import default_lorebook_entry
from phase1_agent.prompt_package import create_prompt_package
from phase1_agent.tavern_package import (
    TAVERN_COMPONENTS, TAVERN_FRONTEND_EXTENSIONS, create_tavern_package,
)
from phase1_agent.tool_package import create_tool_package


NODE_ID = str(UUID(int=1, version=4))
KEY = "variable/keyword"


def uid(number):
    return str(UUID(int=number, version=4))


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def entry(number=11, **changes):
    return {**default_lorebook_entry(uid(number)), "text": "activated content", **changes}


def scan_prompt(text="match"):
    view = validate_native_context_view({
        "schema_version": 4, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": uid(800), "object_key": "context", "agent_node_id": uid(801)},
        "basis": {"revision_id": uid(802), "head_revision": 1},
        "messages": [], "layout": [], "accepted_delta_ids": [], "applied_delta_ids": [],
        "once_injected_item_ids": [], "generation": 0,
        "derivation": {"operation": "read", "input_view_ref": None, "update_ref": None},
    })
    return assemble_native_context_prompt(
        [], view, text_content(text), context_ref=ref(900), current_input_ref=ref(901))


@pytest.fixture
def capabilities():
    return CapabilityPackageLoader((
        create_content_package(), create_prompt_package(), create_context_package(),
        create_tool_package(), create_tavern_package(),
    )).load({"workflow.tavern": "1.0.0"})


@pytest.fixture
def registry(capabilities):
    return capabilities.registry


def variable(value="match", *, assigned=True):
    return {"registered": True, "name": "keyword", "value_type": "string",
            "assigned": assigned, "value": value if assigned else None}


def object_state(value, *, readers=(NODE_ID,), type_id="workflow.variable"):
    binding = ObjectBinding(KEY, type_id, 1, "shared", readers=readers).to_dict()
    return {KEY: {
        "type_id": type_id, "schema_version": 1, "binding": binding,
        "value": deepcopy(value), "revision": 7, "revision_id": uid(820), "deleted": False,
    }}


def invoke(registry, *, grouped=False, entries=None, objects=None, object_keys=None,
           prompt=None, accepted=None, references=None, node_id=NODE_ID):
    implementation = registry.get("lorebook.group" if grouped else "lorebook.item", "1")
    members = [entry(primary_keywords=["match"])] if entries is None else entries
    config = {"entries": members, "object_keys": object_keys or []} if grouped else {
        "entry": members[0], "object_keys": object_keys or []}
    registry.validate_config(implementation, config)
    prompt = scan_prompt() if prompt is None else deepcopy(prompt)
    calls = []

    def host(context, capability, operation, payload):
        calls.append((capability, operation, deepcopy(payload)))
        assert (capability, operation) == ("artifacts:read", "resolve-artifact")
        assert payload == {"reference": ref(930)}
        return {"value": deepcopy(prompt if accepted is None else accepted)}

    context = NodeExecutionContext(
        definition=implementation.definition, node_binding_id=node_id,
        workflow_session_id=uid(800), chain_run_id=uid(821), node_run_id=uid(822),
        state={"revision": 0, "values": {}}, private_states={}, external_inputs={},
        type_registry=registry.data_types, object_states=objects or {},
        input_refs={"input": [{"edge_id": uid(931), "output_id": uid(930), "order": 0}]
                    if references is None else references},
        object_accesses={key: "read" for key in config["object_keys"]}, host=host,
    )
    original = deepcopy([config, prompt, objects])
    outputs = implementation.executor(config, {"input": prompt}, context)
    assert [config, prompt, objects] == original
    assert context.object_writes == [] and context.effects == []
    assert set(outputs) == {"output"}
    outputs["output"] = registry.validate_content(outputs["output"], "PROMPT_MATERIALS", 1)
    return outputs["output"], context, calls


def test_independent_tavern_package_loads_without_agent_model_or_network_packages(capabilities):
    assert capabilities.package_lock == tuple(
        {"package_id": identity, "version": "1.0.0"} for identity in (
            "workflow.content", "workflow.context", "workflow.prompts", "workflow.tavern", "workflow.tools"))
    assert capabilities.registry.get("agents.execute", "4") is None
    assert capabilities.registry.get("models.source", "1") is None
    assert create_tavern_package().manifest.to_dict()["exports"]["nodes"] == [
        {"component_id": identity, "component_version": "1"} for identity in TAVERN_COMPONENTS]


def test_exact_frontend_extensions_match_manifest_and_component_targets(capabilities):
    extensions = [row for row in capabilities.frontend_extensions if row["package_id"] == "workflow.tavern"]
    assert extensions == sorted([{
        **deepcopy(row), "schema_version": 2, "host_protocol_version": 1,
        "package_id": "workflow.tavern", "package_version": "1.0.0",
    } for row in TAVERN_FRONTEND_EXTENSIONS], key=lambda row: row["extension_id"])
    for row in extensions:
        assert row["binding"]["target"] == {
            "component_id": row["component_id"], "component_version": row["component_version"]}


def test_builtins_install_tavern_but_exact_selection_does_not_silently_add_it():
    assert DEFAULT_PACKAGES["workflow.tavern"] == "1.2.0"
    loader = CapabilityPackageLoader(builtin_capability_packages())
    old_selection = {key: value for key, value in DEFAULT_PACKAGES.items() if key != "workflow.tavern"}
    selected = loader.load(old_selection)
    assert selected.registry.get("lorebook.item", "1") is None
    assert all(row["package_id"] != "workflow.tavern" for row in selected.package_lock)
    installed = loader.load(DEFAULT_PACKAGES)
    assert installed.registry.get("lorebook.item", "1") is not None
    assert installed.registry.get("lorebook.global-activate", "1") is not None
    legacy = loader.load({**DEFAULT_PACKAGES, "workflow.tavern": "1.0.0"})
    assert legacy.registry.get("lorebook.item", "1") is not None
    assert legacy.registry.get("lorebook.global-activate", "1") is None


@pytest.mark.parametrize("component", TAVERN_COMPONENTS)
def test_ports_defaults_and_capabilities_have_no_history_or_write_access(registry, component):
    definition = registry.get(component, "1").definition
    assert definition.inputs == (NodePort("input", "PROMPT", data_schema_version=6),)
    assert definition.outputs == (NodePort("output", "PROMPT_MATERIALS"),)
    assert definition.input_storage == "references"
    assert set(definition.capabilities) == {"artifacts:read", "objects:read"}
    assert definition.object_accesses == ({
        "config_field": "object_keys", "multiple": True, "access": "read",
        "type_id": "workflow.variable", "schema_version": 1,
    },)
    config = deepcopy(definition.default_config)
    registry.validate_config(registry.get(component, "1"), config)
    member = config["entry"] if component == "lorebook.item" else config["entries"][0]
    assert member["mode"] == "keyword" and member["scan_depth"] == 20
    assert member["recursive"] is False and member["probability_enabled"] is False


def test_single_entry_and_one_member_group_share_semantics_and_fixed_lifecycle(registry):
    member = entry(primary_keywords=["MATCH"])
    single, context, calls = invoke(registry, entries=[member])
    grouped, _, _ = invoke(registry, grouped=True, entries=[member])
    assert single == grouped and len(single["items"]) == 1
    material = single["items"][0]
    assert material["text"] == member["text"]
    assert material["lifecycle"] == "per_request" and material["compaction"] == "never"
    assert material["source"] == {
        "kind": "lorebook", "node_id": NODE_ID, "member_id": member["id"],
        "origin_item_id": material["item_instance_id"],
    }
    assert material["origin_item_ids"] == [material["item_instance_id"]]
    assert calls == [("artifacts:read", "resolve-artifact", {"reference": ref(930)})]
    diagnostic = next(read for read in context.reads if read["kind"] == "lorebook_evaluation")
    assert diagnostic["input_ref"] == ref(930) and diagnostic["recursive_rounds"] == 0
    assert len(diagnostic["entries"]) == 1


def test_stable_item_identity_is_local_to_node_and_does_not_echo_input(registry):
    member = entry(primary_keywords=["match"])
    first, _, _ = invoke(registry, entries=[member])
    changed, _, _ = invoke(registry, entries=[{**member, "text": "changed output"}])
    other, _, _ = invoke(registry, entries=[member], node_id=uid(2))
    assert first["items"][0]["item_instance_id"] == changed["items"][0]["item_instance_id"]
    assert other["items"][0]["item_instance_id"] != first["items"][0]["item_instance_id"]
    assert [item["text"] for item in first["items"]] == ["activated content"]


@pytest.mark.parametrize("members", [[], [entry(primary_keywords=["absent"])]])
def test_empty_group_and_untriggered_entries_produce_valid_empty_materials(registry, members):
    result, context, _ = invoke(registry, grouped=True, entries=members)
    assert result["items"] == []
    assert context.reads[-1]["kind"] == "lorebook_evaluation"


@pytest.mark.parametrize("value", [variable(), variable("", assigned=True),
                                  variable(assigned=False)])
def test_authorized_variable_keyword_reads_current_value_without_mutation(registry, value):
    objects = object_state(value)
    original = deepcopy(objects)
    output, context, _ = invoke(
        registry, entries=[entry(primary_keywords=["{{keyword}}"])],
        object_keys=[KEY], objects=objects)
    assert bool(output["items"]) == (value["assigned"] and bool(value["value"]))
    assert objects == original and objects[KEY]["revision"] == 7
    reads = [read for read in context.reads if read["kind"] == "object_read"]
    assert reads == [{"kind": "object_read", "object_key": KEY, "revision": 7, "revision_id": uid(820)}]
    assert all("value" not in read for read in context.reads)


def test_missing_variable_does_not_read_undeclared_objects_or_suppress_other_keywords(registry):
    output, context, _ = invoke(
        registry, entries=[entry(primary_keywords=["{{missing}}", "match"])],
        objects=object_state(variable()))
    assert len(output["items"]) == 1
    assert not any(read["kind"] == "object_read" for read in context.reads)


@pytest.mark.parametrize("references", [[], [
    {"edge_id": uid(931), "output_id": uid(930), "order": 0},
    {"edge_id": uid(932), "output_id": uid(930), "order": 1},
]])
def test_execution_requires_one_explicit_accepted_input_reference(registry, references):
    with pytest.raises(ContractValidationError) as caught:
        invoke(registry, references=references)
    assert caught.value.reason_code == "lorebook_exact_artifact_required"


def test_accepted_scan_value_cannot_be_replaced_by_a_different_valid_prompt(registry):
    with pytest.raises(ContractValidationError) as caught:
        invoke(registry, prompt=scan_prompt("match"), accepted=scan_prompt("different"))
    assert caught.value.reason_code == "lorebook_artifact_mismatch"


def test_forged_prompt_is_rejected_before_artifact_or_variable_use(registry):
    prompt = scan_prompt()
    prompt["messages"][-1]["blocks"][0]["text"] = "forged"
    with pytest.raises(ContractValidationError) as caught:
        invoke(registry, prompt=prompt, object_keys=[KEY], objects=object_state(variable()))
    assert caught.value.reason_code == "context_prompt_not_ready"


def test_variable_permission_errors_remain_errors_even_when_keywords_are_absent(registry):
    with pytest.raises(ContractValidationError) as caught:
        invoke(registry, object_keys=[KEY], objects=object_state(variable(), readers=()))
    assert caught.value.reason_code == "session_object_access_denied"


def test_wrong_object_type_is_not_treated_as_a_missing_keyword(registry):
    registry = registry.detached()
    registry.data_types.register(DataTypeDefinition(
        "example.other", 1, object_schema({"anything": {"type": "string"}}),
        default_value={"anything": "match"}))
    with pytest.raises(ContractValidationError) as caught:
        invoke(registry, object_keys=[KEY],
               objects=object_state({"anything": "match"}, type_id="example.other"))
    assert caught.value.reason_code == "variable_binding_type_mismatch"


@pytest.mark.parametrize("config", [
    {"entries": [entry(11), entry(11)], "object_keys": []},
    {"entries": [entry()], "object_keys": [KEY, KEY]},
    {"entries": [entry()], "object_keys": [" variable/keyword"]},
    {"entries": [entry()], "object_keys": [], "lifecycle": "context_once"},
    {"entries": [{**entry(), "enabled": False}], "object_keys": []},
])
def test_group_config_is_strict_and_disallows_duplicate_ids_or_lifecycle_escape(registry, config):
    with pytest.raises(ContractValidationError):
        registry.validate_config(registry.get("lorebook.group", "1"), config)


def graph_node(registry, component, number, **config):
    definition = registry.get(component, "1").definition
    return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
            "title": component, "position": {"x": 0, "y": 0},
            "config": {**deepcopy(definition.default_config), **config}}


def compile_fixture(registry, *, binding=None, component_version="1", source_version=6):
    registry = registry.detached()
    registry.register(NodeDefinition(
        "test.scan-source", "1", "Scan source", "Test", {}, object_schema({}),
        outputs=(NodePort("output", "PROMPT", data_schema_version=source_version),)),
        lambda config, inputs, context: {"output": scan_prompt()})
    registry.register(NodeDefinition(
        "test.material-sink", "1", "Material sink", "Test", {}, object_schema({}),
        inputs=(NodePort("input", "PROMPT_MATERIALS"),), is_output=True),
        lambda config, inputs, context: {})
    source = graph_node(registry, "test.scan-source", 2)
    node = graph_node(registry, "lorebook.item", 1, object_keys=[KEY] if binding is not None else [])
    node["component_version"] = component_version
    sink = graph_node(registry, "test.material-sink", 3)
    document = {
        "schema_version": 2, "workflow_definition_id": uid(840), "revision": 1,
        "name": "Read-only lorebook", "nodes": [source, node, sink],
        "edges": [
            {"edge_id": uid(850), "source_node_id": source["node_binding_id"], "source_port_id": "output",
             "target_node_id": NODE_ID, "target_port_id": "input", "order": 0},
            {"edge_id": uid(851), "source_node_id": NODE_ID, "source_port_id": "output",
             "target_node_id": sink["node_binding_id"], "target_port_id": "input", "order": 0},
        ], "object_bindings": [] if binding is None else [binding],
    }
    return GraphCompiler(registry).compile(document)


def test_graph_compile_accepts_explicit_read_only_variable_binding(registry):
    binding = ObjectBinding(KEY, "workflow.variable", 1, "shared", readers=(NODE_ID,)).to_dict()
    plan = compile_fixture(registry, binding=binding)
    assert NODE_ID in plan.ordered_node_ids
    assert plan.definitions[NODE_ID].object_accesses[0]["access"] == "read"


@pytest.mark.parametrize(("type_id", "readers", "reason"), [
    ("workflow.variable", (), "session_object_access_denied"),
    ("workflow.effective-context", (NODE_ID,), "session_object_type_mismatch"),
])
def test_graph_compile_rejects_unauthorized_or_wrong_variable_binding(registry, type_id, readers, reason):
    binding = ObjectBinding(KEY, type_id, 1, "shared", readers=readers).to_dict()
    with pytest.raises(ContractValidationError) as caught:
        compile_fixture(registry, binding=binding)
    assert caught.value.reason_code == reason


def test_graph_compile_does_not_coerce_old_prompt_contract_or_unknown_node_version(registry):
    with pytest.raises(ContractValidationError):
        compile_fixture(registry, source_version=2)
    with pytest.raises(ContractValidationError):
        compile_fixture(registry, component_version="2")
