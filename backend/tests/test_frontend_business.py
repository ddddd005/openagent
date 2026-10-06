"""Frontend contracts prove exact inputs, CAS state and reference-only display."""

from copy import deepcopy
from uuid import UUID

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import create_content_package, text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_business import register_frontend_business, settle_frontend_append
from phase1_agent.frontend_contracts import (
    EMPTY_FRONTEND_STATE, FRONTEND_STATE_TYPE, append_frontend_view,
    frontend_view_artifact_bindings, prepare_frontend_adoption, read_frontend_view,
    validate_frontend_state_write,
)
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import ObjectBinding


def uid(number):
    return str(UUID(int=number, version=4))


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def binding(number):
    return {"edge_id": uid(number + 100), "output_id": uid(number), "order": 0}


def registry():
    package = CapabilityPackage(PackageManifest(
        "sample.frontend-business", "1.0.0", dependencies=(PackageDependency("workflow.content", "1.0.0"),)),
        register_frontend_business)
    return CapabilityPackageLoader((create_content_package(), package)).load().registry


def record():
    return {"type_id": FRONTEND_STATE_TYPE, "schema_version": 1, "deleted": False,
            "revision": 1, "revision_id": uid(11), "value": deepcopy(EMPTY_FRONTEND_STATE),
            "binding": ObjectBinding("frontend", FRONTEND_STATE_TYPE, 1, "shared",
                                     readers=(uid(3),), writers=(uid(3),)).to_dict()}


def proof():
    old = record()
    view = read_frontend_view(old, uid(1), "frontend")
    appended = append_frontend_view(view, binding(20), [binding(21)], role="assistant", node_run_id=uid(4))
    prepared = prepare_frontend_adoption(
        workflow_session_id=uid(1), object_key="frontend", object_record=old,
        view=appended, view_ref=ref(22))
    artifacts = {
        uid(20): {"value": view, "component_id": "frontend.state.output", "component_version": "1",
                  "config": {"object_key": "frontend"}, "producer": {"workflow_session_id": uid(1)}},
        uid(21): {"value": text_content("accepted reply")},
        uid(22): {"value": appended, "component_id": "frontend.state.append", "component_version": "1",
                  "config": {"object_key": "frontend", "role": "assistant"},
                  "producer": {"workflow_session_id": uid(1), "node_run_id": uid(4)},
                  "input_refs": {"view": [binding(20)], "content": [binding(21)]}},
    }
    context = {"workflow_session_id": uid(1), "object_key": "frontend", "current_record": old,
               "operation": "put", "resolve_artifact": lambda reference: deepcopy(artifacts[reference["output_id"]])}
    return old, view, appended, prepared, artifacts, context


def test_opt_in_registration_has_declared_access_and_no_text_or_kernel_copy():
    host = registry()
    assert host.get("frontend.state.append", "1").definition.input_storage == "references"
    old, _, appended, prepared, _, context = proof()
    assert host.data_types.default(FRONTEND_STATE_TYPE, 1) == EMPTY_FRONTEND_STATE
    desired = prepared["desired_value"]
    assert desired["entries"][0]["source_ref"] == ref(21)
    assert set(desired["entries"][0]) == {"entry_id", "role", "source_ref"}
    assert "accepted reply" not in str(desired)
    assert frontend_view_artifact_bindings(appended) == [binding(20), binding(21)]
    validate_frontend_state_write(desired, context)
    assert old == record()


@pytest.mark.parametrize("mutate", [
    lambda artifacts: artifacts[uid(22)].update(component_id="thirdparty.fake"),
    lambda artifacts: artifacts[uid(22)]["config"].update(object_key="other"),
    lambda artifacts: artifacts[uid(22)]["config"].update(role="user"),
    lambda artifacts: artifacts[uid(22)]["producer"].update(workflow_session_id=uid(999)),
    lambda artifacts: artifacts[uid(22)]["producer"].update(node_run_id=uid(999)),
    lambda artifacts: artifacts[uid(22)]["input_refs"].update(content=[binding(999)]),
    lambda artifacts: artifacts[uid(22)]["value"]["entries"][0].update(entry_id=uid(999)),
    lambda artifacts: artifacts[uid(20)].update(component_id="thirdparty.read"),
    lambda artifacts: artifacts[uid(20)]["value"]["basis"].update(head_revision=999),
    lambda artifacts: artifacts[uid(21)].update(value={"schema_version": 2, "kind": "workflow.json", "value": {}}),
])
def test_provenance_rejects_spoofed_producer_config_input_and_retained_state(mutate):
    _, _, _, prepared, artifacts, context = proof()
    mutate(artifacts)
    with pytest.raises(ContractValidationError):
        validate_frontend_state_write(prepared["desired_value"], context)


def test_initialization_reset_delete_stale_basis_and_foreign_owner_are_rejected():
    old, _, appended, prepared, _, context = proof()
    validate_frontend_state_write(EMPTY_FRONTEND_STATE, {**context, "operation": "initialize", "current_record": None})
    for value, operation in ((prepared["desired_value"], "initialize"), (None, "delete")):
        with pytest.raises(ContractValidationError):
            validate_frontend_state_write(value, {**context, "operation": operation})
    with pytest.raises(ContractValidationError):
        validate_frontend_state_write(EMPTY_FRONTEND_STATE, {
            **context, "current_record": {**old, "value": prepared["desired_value"]}})
    for changes in ({"workflow_session_id": uid(999)}, {"object_record": {**old, "revision": 2}}):
        arguments = dict(workflow_session_id=uid(1), object_key="frontend",
                         object_record=old, view=appended, view_ref=ref(22))
        with pytest.raises(ContractValidationError):
            prepare_frontend_adoption(**{**arguments, **changes})


def test_candidate_identity_is_stable_and_fork_keeps_immutable_sources():
    old, _, appended, prepared, _, _ = proof()
    same = prepare_frontend_adoption(
        workflow_session_id=uid(1), object_key="frontend", object_record=old,
        view=appended, view_ref=ref(22))
    assert prepared == same
    types = registry().data_types
    inherited = types.remap(FRONTEND_STATE_TYPE, 1, prepared["desired_value"], {
        "workflow_session_id": {uid(1): uid(2)}}, scope="session")
    assert inherited == prepared["desired_value"]
    fork_view = read_frontend_view({**old, "value": inherited}, uid(2), "frontend")
    assert fork_view["owner"]["workflow_session_id"] == uid(2)
    assert fork_view["entries"][0]["source_ref"] == ref(21)
    assert types.references(FRONTEND_STATE_TYPE, 1, inherited) == [ref(21), ref(22)]


def test_executor_and_settlement_use_authorized_read_actual_inputs_and_receipt():
    host = registry()
    old, view, appended, _, artifacts, _ = proof()
    entry = host.get("frontend.state.append", "1")
    context = NodeExecutionContext(
        definition=entry.definition, node_binding_id=uid(3), workflow_session_id=uid(1),
        chain_run_id=uid(2), node_run_id=uid(4), state={}, private_states={}, external_inputs={},
        object_states={"frontend": old}, type_registry=host.data_types,
        input_refs={"view": [binding(20)], "content": [binding(21)]},
        host=lambda ctx, capability, operation, payload: artifacts[payload["reference"]["output_id"]],
    )
    outputs = entry.executor(entry.definition.default_config, {
        "view": view, "content": [text_content("accepted reply")]}, context)
    assert outputs == {"view": appended} and context.object_writes == []
    settlement = settle_frontend_append(context, outputs, {"view": uid(22), "commit": uid(23)})
    receipt = {"object_key": "frontend", "revision": 2, "revision_id": uid(24), "deleted": False}
    committed = settlement["finalize"](None, None, [receipt])
    assert committed["commit"]["receipt"] == receipt
    with pytest.raises(ContractValidationError):
        settlement["finalize"](None, None, [])
    with pytest.raises(ContractValidationError):
        settlement["finalize"](None, None, [{**receipt, "revision": 99}])
    with pytest.raises(ContractValidationError):
        entry.executor(entry.definition.default_config, {
            "view": view, "content": [text_content("forged reply")]}, context)
    context.node_binding_id = uid(999)
    with pytest.raises(ContractValidationError) as denied:
        entry.executor(entry.definition.default_config, {
            "view": view, "content": [text_content("accepted reply")]}, context)
    assert denied.value.reason_code == "session_object_access_denied"


@pytest.mark.parametrize("sources", [[], {}, [{"output_id": uid(21)}], [binding(21), binding(21)]])
def test_append_rejects_missing_malformed_or_duplicate_exact_sources(sources):
    _, view, _, _, _, _ = proof()
    with pytest.raises(ContractValidationError):
        append_frontend_view(view, binding(20), sources, role="assistant", node_run_id=uid(4))
