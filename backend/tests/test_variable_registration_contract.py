"""Current explicit variable registration and shared Lorebook read acceptance."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.content_contracts import json_content, text_content
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.context_v4 import read_native_context_v5
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.graph_contracts import GraphCompiler
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, ResourceIdentity, TypeRegistry
from phase1_agent.lorebook_engine import default_lorebook_entry, evaluate_lorebook
from phase1_agent.variable_contract import (
    VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE, empty_variable_value, read_variable,
    scalar_text, validate_variable, variable_data_type,
)

def uid(number):
    return str(UUID(int=number, version=4))


PROVIDERS = (uid(351), uid(352))
ACTOR = uid(353)
KEYS = ("item-a", "item-b")
SESSIONS = (uid(354), uid(355))


def scan(current="seed"):
    root = {"type_id": "workflow.effective-context", "schema_version": 4,
            "deleted": False, "revision_id": uid(395), "revision": 1,
            "value": {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0}}
    view = read_native_context_v5(root, SESSIONS[0], "context-item")
    return assemble_native_context_prompt(
        [], view, text_content(current),
        context_ref={"scope": "artifact", "output_id": uid(396)},
        current_input_ref={"scope": "artifact", "output_id": uid(397)},
    )


@pytest.fixture(scope="module")
def registry():
    return create_package_registry().registry


def objects(*, values=None, readers=(ACTOR,), writers=(ACTOR,)):
    values = values or [empty_variable_value(), empty_variable_value()]
    result = {}
    for key, provider, value in zip(KEYS, PROVIDERS, values):
        binding = ObjectBinding(
            key, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
            readers=(provider, *readers), writers=(provider, *writers),
            registrar_node_id=provider,
        ).to_dict()
        result[key] = {
            "type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
            "value": deepcopy(value), "binding": binding, "revision": 1,
            "revision_id": str(uuid4()), "deleted": False,
        }
    return result


def variable(name="key", value_type="string", value="seed", *, assigned=True):
    return {"registered": True, "name": name, "value_type": value_type,
            "assigned": assigned, "value": value if assigned else None}


def context(registry, component, state, *, actor=ACTOR, session=SESSIONS[0],
            config=None, host=None, input_refs=None):
    version = "2" if component == "tools.variable-register" else "1"
    entry = registry.get(component, version)
    config = {**deepcopy(entry.definition.default_config), **(config or {})}
    registry.validate_config(entry, config)
    accesses = {}
    for access in entry.definition.object_accesses:
        keys = config[access["config_field"]]
        for key in keys if access["multiple"] else [keys]:
            accesses[key] = access["access"]
    runtime = NodeExecutionContext(
        definition=entry.definition, node_binding_id=actor, workflow_session_id=session,
        chain_run_id=str(uuid4()), node_run_id=str(uuid4()), state={}, private_states={},
        external_inputs={}, object_states=state, type_registry=registry.data_types,
        object_accesses=accesses, host=host, input_refs=input_refs,
    )
    return entry, config, runtime


def invoke(registry, component, state, *, inputs=None, **options):
    entry, config, runtime = context(registry, component, state, **options)
    return entry.executor(config, inputs or {}, runtime), runtime


def settle(state, runtime):
    for intent in runtime.object_writes:
        current = state[intent["object_key"]]
        assert current["revision"] == intent["expected_revision"]
        current.update(value=deepcopy(intent["value"]), revision=current["revision"] + 1,
                       revision_id=str(uuid4()))


def test_admission_is_exact_default_deny_and_survives_registry_copy(registry):
    admitted = {(entry.type_id, entry.schema_version) for entry in registry.data_types.definitions()
                if entry.checkpoint_admission}
    assert admitted == {
        ("workflow.effective-context", 4), (CHAT_CONTEXT_TYPE, 1), (VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION),
    }
    detached = registry.data_types.detached(frozen=True)
    assert detached.get(VARIABLE_TYPE, 1, scope="session").checkpoint_admission is True
    assert detached.get(CHAT_CONTEXT_TYPE, 1, scope="session").checkpoint_admission is True
    assert detached.get("workflow.frontend-state", 1, scope="session") is None
    assert detached.get(VARIABLE_TYPE, 1, scope="session").to_dict()["checkpoint_admission"] is True
    assert DataTypeDefinition("other", 1, {}).checkpoint_admission is False
    assert variable_data_type().default_value == empty_variable_value()


@pytest.mark.parametrize("scope,admission", [("global", True), ("content", True), ("session", 1)])
def test_admission_rejects_non_session_or_non_boolean_declarations(scope, admission):
    with pytest.raises(ContractValidationError) as error:
        TypeRegistry().register(DataTypeDefinition(
            "other", 1, {}, scope=scope, checkpoint_admission=admission))
    assert error.value.reason_code == "host_invalid_type"


def test_registration_is_only_explicit_non_working_provider_version_two(registry):
    assert registry.get("tools.variable-register", "1") is None
    provider = registry.get("tools.variable-register", "2").definition
    assert provider.execution_class == "non_working"
    assert provider.provided_session_item_types == (
        {"type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION},)
    assert provider.default_config["object_key"] == ""
    assert provider.outputs == ()
    assert registry.get("tools.variable-assign", "1").definition.execution_class == "working"


def test_two_items_two_sessions_registration_updates_and_read_snapshots(registry):
    sessions = {sid: objects() for sid in SESSIONS}
    initial = deepcopy(sessions)
    for provider, key, name in zip(PROVIDERS, KEYS, ("first", "second")):
        _, runtime = invoke(registry, "tools.variable-register", sessions[SESSIONS[0]],
                            actor=provider, config={"object_key": key, "name": name,
                                "value_type": "integer", "has_initial": True, "initial_value": 0})
        settle(sessions[SESSIONS[0]], runtime)
    assert sessions[SESSIONS[1]] == initial[SESSIONS[1]]
    _, _, reader = context(registry, "tools.variable-to-content", sessions[SESSIONS[0]],
                            config={"object_key": KEYS[0]})
    snapshot = read_variable(reader, KEYS[0])
    for operation, incoming, expected in (("set", 5, 5), ("add", 3, 8), ("subtract", 2, 6)):
        _, runtime = invoke(registry, "tools.variable-assign", sessions[SESSIONS[0]],
                            config={"object_key": KEYS[0], "operation": operation, "value": incoming})
        settle(sessions[SESSIONS[0]], runtime)
        assert sessions[SESSIONS[0]][KEYS[0]]["value"]["value"] == expected
    assert snapshot["value"]["value"] == 0
    assert read_variable(reader, KEYS[0])["value"]["value"] == 6
    assert sessions[SESSIONS[0]][KEYS[1]]["value"]["value"] == 0
    assert sessions[SESSIONS[1]] == initial[SESSIONS[1]]
    _, repeated = invoke(registry, "tools.variable-register", sessions[SESSIONS[0]],
        actor=PROVIDERS[0], config={"object_key": KEYS[0], "name": "first",
            "value_type": "integer", "has_initial": True, "initial_value": 99})
    assert repeated.object_writes == []
    assert sessions[SESSIONS[0]][KEYS[0]]["value"]["value"] == 6
    assert set(snapshot["value"]) == {"registered", "name", "value_type", "assigned", "value"}
    assert all(sid not in key for sid in SESSIONS for key in KEYS)


def test_provider_rejects_another_provided_item_even_with_explicit_acl(registry):
    state = objects(readers=(ACTOR, PROVIDERS[0]), writers=(ACTOR, PROVIDERS[0]))
    # Avoid duplicate ACL on the first provider; the second item explicitly authorizes both.
    for item in state.values():
        for access in ("readers", "writers"):
            item["binding"][access] = list(dict.fromkeys(item["binding"][access]))
    before = deepcopy(state)
    entry, config, runtime = context(registry, "tools.variable-register", state,
        actor=PROVIDERS[0], config={"object_key": KEYS[1], "name": "wrong"})
    with pytest.raises(ContractValidationError) as error:
        entry.executor(config, {}, runtime)
    assert error.value.reason_code == "variable_registration_target_mismatch"
    assert runtime.object_writes == []
    assert state == before


@pytest.mark.parametrize("value_type,value,text", [
    ("string", "", ""), ("integer", 0, "0"), ("number", 0.5, "0.5"), ("boolean", False, "false"),
])
def test_shared_scalar_contract_explicit_assign_and_content_reads(registry, value_type, value, text):
    state = objects(values=[variable("key", value_type, assigned=False), empty_variable_value()])
    _, writer = invoke(registry, "tools.variable-assign", state,
        config={"object_key": KEYS[0]}, inputs={"value": json_content(value)})
    settle(state, writer)
    result, reader = invoke(registry, "tools.variable-to-content", state,
        config={"object_key": KEYS[0], "mode": "text"})
    assert result["output"] == text_content(text)
    assert reader.object_writes == []
    assert scalar_text(value_type, value) == text
    validate_variable(state[KEYS[0]]["value"])


@pytest.mark.parametrize("value_type,incoming", [
    ("integer", True), ("boolean", 0), ("number", "1"), ("string", False),
])
def test_type_errors_do_not_submit_writes(registry, value_type, incoming):
    state = objects(values=[variable("key", value_type, assigned=False), empty_variable_value()])
    entry, config, runtime = context(registry, "tools.variable-assign", state,
        config={"object_key": KEYS[0], "value": incoming})
    with pytest.raises(ContractValidationError) as error:
        entry.executor(config, {}, runtime)
    assert error.value.reason_code == "variable_type_mismatch"
    assert runtime.object_writes == []


@pytest.mark.parametrize("state_value,reason", [
    (empty_variable_value(), "variable_not_registered"),
    (variable(assigned=False), "variable_unassigned"),
])
def test_reads_and_assignments_do_not_implicitly_register(registry, state_value, reason):
    state = objects(values=[state_value, empty_variable_value()])
    entry, config, runtime = context(registry, "tools.variable-to-content", state,
        config={"object_key": KEYS[0]})
    with pytest.raises(ContractValidationError) as error:
        entry.executor(config, {}, runtime)
    assert error.value.reason_code == reason
    assert runtime.object_writes == []
    if not state_value["registered"]:
        with pytest.raises(ContractValidationError) as error:
            invoke(registry, "tools.variable-assign", state, config={"object_key": KEYS[0]})
        assert error.value.reason_code == "variable_not_registered"


def graph(registry):
    nodes = []
    for number, component, version, updates in (
        (351, "tools.variable-register", "2", {"object_key": KEYS[0]}),
        (352, "tools.variable-register", "2", {"object_key": KEYS[1], "name": "other"}),
        (353, "tools.variable-assign", "1", {"object_key": KEYS[0], "value": "updated"}),
        (357, "tools.text", "1", {"text": "visible"}),
        (358, "tools.output", "1", {}),
    ):
        nodes.append({
            "node_binding_id": uid(number), "component_id": component, "component_version": version,
            "title": component, "position": {"x": 0, "y": 0},
            "config": {**deepcopy(registry.get(component, version).definition.default_config), **updates},
        })
    event_id = "tools.variable-register/" + PROVIDERS[0]
    doc = {
        "schema_version": 2, "workflow_definition_id": uid(359), "revision": 1, "name": "Variables",
        "nodes": nodes, "object_bindings": [item["binding"] for item in objects().values()],
        "edges": [{"edge_id": uid(360), "source_node_id": uid(357), "source_port_id": "output",
                   "target_node_id": uid(358), "target_port_id": "input", "order": 0}],
        "control_edges": [
            {"edge_id": uid(361), "source_node_id": uid(357), "target_node_id": ACTOR},
            {"edge_id": uid(362), "source_node_id": ACTOR, "target_node_id": uid(358)},
        ],
        "event_bindings": [{"event_id": event_id, "schema_version": 1,
            "display_name": "Register variable", "audience": "management",
            "target_node_ids": [PROVIDERS[0]],
            "payload_schema": {"type": "object", "properties": {}, "additionalProperties": False}}],
    }
    return doc, event_id


def test_ordinary_plan_filters_providers_but_keeps_connected_assignment_and_exact_event(registry):
    doc, event_id = graph(registry)
    compiler = GraphCompiler(registry)
    ordinary = compiler.compile(doc)
    assert ordinary.ordered_node_ids == (uid(357), ACTOR, uid(358))
    assert compiler.compile_event(doc, event_id, 1, {}).ordered_node_ids == (PROVIDERS[0],)
    with pytest.raises(ContractValidationError) as error:
        compiler.compile_event(doc, event_id, 1, {"name": "override"})
    assert error.value.reason_code == "graph_event_payload_invalid"
    doc["control_edges"].append({
        "edge_id": uid(363), "source_node_id": PROVIDERS[0], "target_node_id": ACTOR})
    with pytest.raises(ContractValidationError) as error:
        compiler.compile(doc)
    assert error.value.reason_code == "graph_non_working_dependency"


def test_compiler_rejects_provider_config_targeting_another_registrar(registry):
    doc, _ = graph(registry)
    doc["nodes"][0]["config"]["object_key"] = KEYS[1]
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(registry).compile(doc)
    assert error.value.reason_code == "variable_registration_target_mismatch"


def test_compiler_rejects_business_values_in_provider_binding_defaults(registry):
    doc, _ = graph(registry)
    doc["object_bindings"][0]["default_value"] = variable()
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(registry).compile(doc)
    assert error.value.reason_code == "variable_registration_default_invalid"
    doc["object_bindings"][0]["default_value"] = empty_variable_value()
    GraphCompiler(registry).compile(doc)


@pytest.mark.parametrize("value_type,value,current,expected", [
    ("integer", 0, "0", True), ("boolean", False, "false", True),
    ("string", "", "seed", False), ("string", None, "seed", False),
])
def test_lorebook_shared_values_preserve_zero_false_and_empty_semantics(
        value_type, value, current, expected):
    member = default_lorebook_entry(uid(371))
    member.update(primary_keywords=["{{key}}"], text="Body {{key}} stays literal")
    values = [variable("key", value_type, value, assigned=value is not None)]
    before = deepcopy(values)
    result = evaluate_lorebook([member], scan(current), node_id=ACTOR, variables=values)
    assert bool(result["materials"]["items"]) is expected
    if expected:
        assert result["materials"]["items"][0]["text"] == "Body {{key}} stays literal"
    assert values == before


@pytest.mark.parametrize("component", ["tools.variable-replace-all", "lorebook.group"])
def test_duplicate_names_rejected_within_explicit_authorized_set(registry, component):
    state = objects(values=[variable(), variable(value="other")])
    if component.startswith("tools."):
        with pytest.raises(ContractValidationError) as error:
            invoke(registry, component, state, config={"object_keys": list(KEYS)},
                   inputs={"input": text_content("{{key}}")})
    else:
        with pytest.raises(ContractValidationError) as error:
            evaluate_lorebook([], scan(), node_id=ACTOR,
                              variables=[item["value"] for item in state.values()])
    assert error.value.reason_code == "variable_name_conflict"


@pytest.mark.parametrize("component", ["lorebook.item", "lorebook.group", "lorebook.global-activate"])
def test_inline_and_global_lorebook_read_live_exact_session_values_without_mutation(registry, component):
    member = default_lorebook_entry(uid(380))
    member.update(primary_keywords=["{{key}}"], text="Activated")
    prompt = scan("seed")
    artifact_id = uid(381)
    identity = ResourceIdentity("workspace", "workflow.tavern.lorebook", uid(382)).to_dict()
    resource = {**identity, "data_schema_version": 1, "update_sequence": 1,
                "value": {"name": "Shared global content", "enabled": True, "entries": [member]}}
    original_resource = deepcopy(resource)

    def host(runtime, capability, operation, payload):
        if operation == "resolve-artifact":
            assert payload["reference"]["output_id"] == artifact_id
            return {"value": deepcopy(prompt)}
        assert operation == "current-global-resource"
        assert payload == identity
        return deepcopy(resource)

    config = {"object_keys": [KEYS[0]]}
    if component == "lorebook.item":
        config["entry"] = member
    if component == "lorebook.group":
        config["entries"] = [member]
    inputs = {"input": prompt}
    if component == "lorebook.global-activate":
        inputs["resource"] = global_resource_reference(identity)
    session_states = {
        SESSIONS[0]: objects(values=[variable(), variable("other", value="unselected")]),
        SESSIONS[1]: objects(values=[variable(value="different"), empty_variable_value()]),
    }
    for sid, expected in ((SESSIONS[0], True), (SESSIONS[1], False)):
        state = session_states[sid]
        before = deepcopy(state)
        result, runtime = invoke(registry, component, state, config=config, inputs=inputs, session=sid,
            host=host, input_refs={"input": [{"output_id": artifact_id}]})
        assert bool(result["output"]["items"]) is expected
        assert runtime.object_writes == []
        assert [read["object_key"] for read in runtime.reads if read["kind"] == "object_read"] == [KEYS[0]]
        assert state == before
    _, writer = invoke(registry, "tools.variable-assign", session_states[SESSIONS[0]],
        config={"object_key": KEYS[0], "value": "changed"})
    settle(session_states[SESSIONS[0]], writer)
    result, runtime = invoke(registry, component, session_states[SESSIONS[0]], config=config,
        inputs=inputs, host=host, input_refs={"input": [{"output_id": artifact_id}]})
    assert result["output"]["items"] == []
    assert runtime.object_writes == []
    assert resource == original_resource


def test_lorebook_authorization_does_not_follow_variable_name(registry):
    state = objects(values=[variable(), empty_variable_value()], readers=())
    member = default_lorebook_entry(uid(390))
    member["primary_keywords"] = ["{{key}}"]
    entry, config, runtime = context(registry, "lorebook.item", state,
        config={"entry": member, "object_keys": [KEYS[0]]},
        host=lambda *args: {"value": scan()}, input_refs={"input": [{"output_id": uid(391)}]})
    with pytest.raises(ContractValidationError) as error:
        entry.executor(config, {"input": scan()}, runtime)
    assert error.value.reason_code == "session_object_access_denied"
    assert runtime.object_writes == []
