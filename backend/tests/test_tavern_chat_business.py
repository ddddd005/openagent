"""Tavern-only proofs preserve exact artifacts and never own Agent context."""

from copy import deepcopy

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import create_content_package, text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.tavern.chat_contracts import (
    EMPTY_TAVERN_CHAT_STATE, TAVERN_CHAT_DISPLAY_TYPE, TAVERN_CHAT_STATE_TYPE,
    append_tavern_chat_view, prepare_tavern_chat_adoption, read_tavern_chat_view,
    tavern_chat_view_artifact_bindings, validate_tavern_chat_display,
    validate_tavern_chat_state_write,
)
from phase1_agent.tavern.chat_nodes import register_tavern_chat, settle_tavern_chat_append

from test_frontend_business import binding, ref, uid


def chat_package():
    return CapabilityPackage(PackageManifest(
        "sample.tavern-chat", "1.0.0",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),)), register_tavern_chat)


def registry():
    return CapabilityPackageLoader((create_content_package(), chat_package())).load().registry


def record():
    return {"type_id": TAVERN_CHAT_STATE_TYPE, "schema_version": 1, "deleted": False,
            "revision": 1, "revision_id": uid(11), "value": deepcopy(EMPTY_TAVERN_CHAT_STATE),
            "binding": ObjectBinding("tavern-chat", TAVERN_CHAT_STATE_TYPE, 1, "shared",
                                     readers=(uid(3),), writers=(uid(3),)).to_dict()}


def proof():
    old = record()
    view = read_tavern_chat_view(old, uid(1), "tavern-chat")
    appended = append_tavern_chat_view(
        view, binding(20), [binding(21)], role="assistant", node_run_id=uid(4))
    prepared = prepare_tavern_chat_adoption(
        workflow_session_id=uid(1), object_key="tavern-chat", object_record=old,
        view=appended, view_ref=ref(22))
    artifacts = {
        uid(20): {"value": view, "component_id": "tavern.chat.output", "component_version": "1",
                  "config": {"object_key": "tavern-chat"},
                  "producer": {"workflow_session_id": uid(1)}},
        uid(21): {"value": text_content("accepted tavern reply")},
        uid(22): {"value": appended, "component_id": "tavern.chat.append", "component_version": "1",
                  "config": {"object_key": "tavern-chat", "role": "assistant"},
                  "producer": {"workflow_session_id": uid(1), "node_run_id": uid(4)},
                  "input_refs": {"view": [binding(20)], "content": [binding(21)]}},
    }
    context = {"workflow_session_id": uid(1), "object_key": "tavern-chat", "current_record": old,
               "operation": "put", "resolve_artifact": lambda reference: deepcopy(artifacts[reference["output_id"]])}
    return old, view, appended, prepared, artifacts, context


def test_isolated_registration_has_declared_access_and_reference_only_state():
    host = registry()
    assert host.get("frontend.state.append", "1") is None
    assert host.data_types.get("workflow.frontend-state", 1) is None
    node = host.get("tavern.chat.append", "1").definition
    assert node.input_storage == "references"
    assert node.default_config == {"object_key": "tavern-chat", "role": "assistant"}
    assert node.object_accesses[0]["type_id"] == TAVERN_CHAT_STATE_TYPE
    old, _, appended, prepared, _, context = proof()
    assert host.data_types.default(TAVERN_CHAT_STATE_TYPE, 1) == EMPTY_TAVERN_CHAT_STATE
    desired = prepared["desired_value"]
    assert desired["entries"][0]["source_ref"] == ref(21)
    assert set(desired["entries"][0]) == {"entry_id", "role", "source_ref"}
    assert "accepted tavern reply" not in str(desired)
    assert tavern_chat_view_artifact_bindings(appended) == [binding(20), binding(21)]
    assert prepared["operation_key"].startswith("tavern-chat-append:")
    validate_tavern_chat_state_write(desired, context)
    assert old == record()


@pytest.mark.parametrize("mutate", [
    lambda artifacts: artifacts[uid(22)].update(component_id="frontend.state.append"),
    lambda artifacts: artifacts[uid(20)].update(component_id="frontend.state.output"),
    lambda artifacts: artifacts[uid(22)].update(component_id="thirdparty.fake"),
    lambda artifacts: artifacts[uid(22)].update(component_version="2"),
    lambda artifacts: artifacts[uid(22)]["config"].update(object_key="other"),
    lambda artifacts: artifacts[uid(22)]["config"].update(role="user"),
    lambda artifacts: artifacts[uid(22)]["producer"].update(workflow_session_id=uid(999)),
    lambda artifacts: artifacts[uid(22)]["producer"].update(node_run_id=uid(999)),
    lambda artifacts: artifacts[uid(22)]["input_refs"].update(content=[binding(999)]),
    lambda artifacts: artifacts[uid(22)]["input_refs"].update(view=[binding(999)]),
    lambda artifacts: artifacts[uid(22)]["value"]["entries"][0].update(entry_id=uid(999)),
    lambda artifacts: artifacts[uid(20)]["producer"].update(workflow_session_id=uid(999)),
    lambda artifacts: artifacts[uid(20)]["value"]["basis"].update(head_revision=999),
    lambda artifacts: artifacts[uid(21)].update(value={"schema_version": 2, "kind": "workflow.json", "value": {}}),
    lambda artifacts: artifacts[uid(21)].update(value={"schema_version": 1, "kind": "workflow.text", "text": "legacy"}),
])
def test_proof_rejects_generic_nodes_forged_config_inputs_and_foreign_owner(mutate):
    _, _, _, prepared, artifacts, context = proof()
    mutate(artifacts)
    with pytest.raises(ContractValidationError):
        validate_tavern_chat_state_write(prepared["desired_value"], context)


def test_initialization_reset_delete_stale_basis_and_foreign_owner_are_rejected():
    old, _, appended, prepared, _, context = proof()
    validate_tavern_chat_state_write(
        EMPTY_TAVERN_CHAT_STATE, {**context, "operation": "initialize", "current_record": None})
    for value, operation in ((prepared["desired_value"], "initialize"), (None, "delete")):
        with pytest.raises(ContractValidationError):
            validate_tavern_chat_state_write(value, {**context, "operation": operation})
    with pytest.raises(ContractValidationError):
        validate_tavern_chat_state_write(EMPTY_TAVERN_CHAT_STATE, {
            **context, "current_record": {**old, "value": prepared["desired_value"]}})
    for changes in ({"workflow_session_id": uid(999)}, {"object_record": {**old, "revision": 2}}):
        arguments = dict(workflow_session_id=uid(1), object_key="tavern-chat",
                         object_record=old, view=appended, view_ref=ref(22))
        with pytest.raises(ContractValidationError):
            prepare_tavern_chat_adoption(**{**arguments, **changes})
    with pytest.raises(ContractValidationError):
        read_tavern_chat_view({**old, "type_id": "workflow.frontend-state"}, uid(1), "tavern-chat")


def test_adoption_identity_stays_stable_and_remap_keeps_immutable_sources():
    old, _, appended, prepared, _, _ = proof()
    same = prepare_tavern_chat_adoption(
        workflow_session_id=uid(1), object_key="tavern-chat", object_record=old,
        view=appended, view_ref=ref(22))
    assert prepared == same
    types = registry().data_types
    inherited = types.remap(TAVERN_CHAT_STATE_TYPE, 1, prepared["desired_value"], {
        "workflow_session_id": {uid(1): uid(2)}}, scope="session")
    assert inherited == prepared["desired_value"]
    inherited_view = read_tavern_chat_view({**old, "value": inherited}, uid(2), "tavern-chat")
    assert inherited_view["owner"]["workflow_session_id"] == uid(2)
    assert inherited_view["entries"][0]["source_ref"] == ref(21)
    assert types.references(TAVERN_CHAT_STATE_TYPE, 1, inherited) == [ref(21), ref(22)]


def test_executor_and_settlement_require_actual_inputs_authorized_read_and_receipt():
    host = registry()
    old, view, appended, _, artifacts, _ = proof()
    entry = host.get("tavern.chat.append", "1")
    context = NodeExecutionContext(
        definition=entry.definition, node_binding_id=uid(3), workflow_session_id=uid(1),
        chain_run_id=uid(2), node_run_id=uid(4), state={}, private_states={}, external_inputs={},
        object_states={"tavern-chat": old}, type_registry=host.data_types,
        input_refs={"view": [binding(20)], "content": [binding(21)]},
        host=lambda ctx, capability, operation, payload: artifacts[payload["reference"]["output_id"]],
    )
    outputs = entry.executor(entry.definition.default_config, {
        "view": view, "content": [text_content("accepted tavern reply")]}, context)
    assert outputs == {"view": appended} and context.object_writes == []
    settlement = settle_tavern_chat_append(context, outputs, {"view": uid(22), "commit": uid(23)})
    receipt = {"object_key": "tavern-chat", "revision": 2, "revision_id": uid(24), "deleted": False}
    committed = settlement["finalize"](None, None, [receipt])
    assert committed["commit"]["receipt"] == receipt
    assert committed["commit"]["kind"] == "workflow.tavern-chat-commit"
    for receipts in ([], [{**receipt, "revision": 99}], [{**receipt, "object_key": "other"}]):
        with pytest.raises(ContractValidationError):
            settlement["finalize"](None, None, receipts)
    with pytest.raises(ContractValidationError):
        entry.executor(entry.definition.default_config, {
            "view": view, "content": [text_content("forged reply")]}, context)
    context.node_binding_id = uid(999)
    with pytest.raises(ContractValidationError) as denied:
        entry.executor(entry.definition.default_config, {
            "view": view, "content": [text_content("accepted tavern reply")]}, context)
    assert denied.value.reason_code == "session_object_access_denied"


@pytest.mark.parametrize("sources", [[], {}, [{"output_id": uid(21)}], [binding(21), binding(21)]])
def test_append_rejects_missing_malformed_or_duplicate_exact_sources(sources):
    _, view, _, _, _, _ = proof()
    with pytest.raises(ContractValidationError):
        append_tavern_chat_view(view, binding(20), sources, role="assistant", node_run_id=uid(4))


def test_display_has_strict_envelope_and_only_exports_direct_text_references():
    _, _, appended, _, _, _ = proof()
    display = {"schema_version": 1, "kind": "workflow.tavern-chat-display", "entries": appended["entries"]}
    types = registry().data_types
    assert types.public_artifact_references(TAVERN_CHAT_DISPLAY_TYPE, 1, display) == [ref(21)]
    for changes in ({"kind": "workflow.frontend-display"}, {"context": {}}, {"schema_version": True}):
        with pytest.raises(ContractValidationError):
            validate_tavern_chat_display({**display, **changes})
    session_ref = {"scope": "session", "workflow_session_id": uid(1),
                   "object_key": "context", "revision_id": uid(11)}
    invalid = deepcopy(display)
    invalid["entries"][0]["source_ref"] = session_ref
    with pytest.raises(ContractValidationError) as denied:
        validate_tavern_chat_display(invalid)
    assert denied.value.reason_code == "tavern_chat_exact_artifact_required"


@pytest.mark.parametrize("producer,version", [
    ("tavern.chat.output", "1"), ("tavern.chat.append", "1"),
    ("frontend.state.output", "1"), ("frontend.state.append", "1"),
    ("tavern.chat.append", "2"),
])
def test_presentation_accepts_only_registered_exact_tavern_origins(producer, version):
    host = registry()
    _, view, _, _, _, _ = proof()
    entry = host.get("tavern.chat.presentation", "1")
    context = NodeExecutionContext(
        definition=entry.definition, node_binding_id=uid(3), workflow_session_id=uid(1),
        chain_run_id=uid(2), node_run_id=uid(4), state={}, private_states={}, external_inputs={},
        object_states={}, type_registry=host.data_types, input_refs={"view": [binding(20)]},
        host=lambda ctx, capability, operation, payload: (
            {"value": deepcopy(view)} if operation == "resolve-artifact"
            else {"component_id": producer, "component_version": version}),
    )
    if producer.startswith("tavern.") and version == "1":
        assert entry.executor({}, {"view": view}, context) == {"display": {
            "schema_version": 1, "kind": "workflow.tavern-chat-display", "entries": []}}
    else:
        with pytest.raises(ContractValidationError) as denied:
            entry.executor({}, {"view": view}, context)
        assert denied.value.reason_code == "tavern_chat_unproven_presentation"
