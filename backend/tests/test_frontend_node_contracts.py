"""Owned frontend append evidence preserves exact reads and real input bindings."""

from copy import deepcopy
from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_contracts import (
    EMPTY_FRONTEND_STATE, FRONTEND_STATE_TYPE, frontend_state_data_type,
    validate_frontend_state_write, validate_frontend_view,
)
from phase1_agent.frontend_node_contracts import (
    FRONTEND_NODE_APPEND_TYPE, build_frontend_node_append, frontend_node_append_artifact_bindings,
    frontend_node_append_data_type, frontend_node_append_references, prepare_frontend_node_adoption,
    validate_frontend_node_append,
)
from phase1_agent.host_sdk import ObjectBinding, TypeRegistry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.session_objects import SessionObjectStore
from test_frontend_business import binding, ref, uid


def record():
    return {
        "type_id": FRONTEND_STATE_TYPE, "schema_version": 1, "deleted": False,
        "revision": 2, "revision_id": uid(11),
        "binding": ObjectBinding(
            "frontend-owned", FRONTEND_STATE_TYPE, 1, "shared",
            readers=(uid(3),), writers=(uid(3), uid(7)), registrar_node_id=uid(3)).to_dict(),
        "value": {
            "entries": [{"entry_id": uid(49), "role": "user", "source_ref": ref(50)}],
            "view_ref": ref(51),
        },
    }


def proof_fixture():
    current = record()
    proof = build_frontend_node_append(
        current, workflow_session_id=uid(1), object_key="frontend-owned",
        source_output_refs=[binding(21), binding(22)], role="assistant", node_run_id=uid(4))
    prepared = prepare_frontend_node_adoption(
        workflow_session_id=uid(1), object_key="frontend-owned",
        object_record=current, proof=proof, proof_ref=ref(30))
    artifacts = {
        uid(21): {"value": text_content("first")},
        uid(22): {"value": text_content("second")},
        uid(30): {
            "value": proof, "component_id": "frontend.node", "component_version": "1",
            "config": {"object_key": "old-fixed-seed", "role": "system"},
            "consumed_config": {"object_key": "frontend-owned", "role": "assistant"},
            "producer": {"workflow_session_id": uid(1), "chain_run_id": uid(2),
                         "node_binding_id": uid(3), "node_run_id": uid(4)},
            "input_refs": {"content": [binding(21), binding(22)]},
            "reads": [{"kind": "object_read", "object_key": "frontend-owned",
                       "revision": 2, "revision_id": uid(11)}],
        },
    }
    context = {
        "workflow_session_id": uid(1), "object_key": "frontend-owned",
        "writer_node_id": uid(3), "current_record": current, "operation": "put",
        "resolve_artifact": lambda reference: deepcopy(artifacts[reference["output_id"]]),
    }
    return current, proof, prepared, artifacts, context


def test_owned_append_uses_independent_proof_and_exact_immutable_sources():
    current, proof, prepared, _, context = proof_fixture()
    assert proof["entries"][0] == current["value"]["entries"][0]
    assert [entry["source_ref"] for entry in proof["entries"][1:]] == [ref(21), ref(22)]
    assert all(set(entry) == {"entry_id", "role", "source_ref"} for entry in proof["entries"])
    assert "first" not in str(proof)
    assert set(proof) == {
        "schema_version", "kind", "owner", "basis", "entries", "source_output_refs", "role", "node_run_id"}
    assert "view_binding" not in proof and "derivation" not in proof
    assert frontend_node_append_artifact_bindings(proof) == [binding(21), binding(22)]
    assert frontend_node_append_references(proof) == [ref(21), ref(22), ref(50)]
    assert prepared["desired_value"] == {"entries": proof["entries"], "view_ref": ref(30)}
    assert prepared["write_intent"]["expected_revision"] == 2
    assert prepared == prepare_frontend_node_adoption(
        workflow_session_id=uid(1), object_key="frontend-owned",
        object_record=current, proof=proof, proof_ref=ref(30))
    validate_frontend_state_write(prepared["desired_value"], context)
    assert current == record()
    with pytest.raises(ContractValidationError):
        validate_frontend_view(proof)


def test_owned_proof_type_uses_existing_session_state_without_checkpoint_admission():
    _, proof, prepared, _, context = proof_fixture()
    types = TypeRegistry()
    types.register(frontend_state_data_type())
    types.register(frontend_node_append_data_type())
    assert types.validate(FRONTEND_NODE_APPEND_TYPE, 1, proof, scope="content") == proof
    assert types.content_artifact_bindings(FRONTEND_NODE_APPEND_TYPE, 1, proof) == [binding(21), binding(22)]
    assert types.references(FRONTEND_NODE_APPEND_TYPE, 1, proof, scope="content") == [ref(21), ref(22), ref(50)]
    assert types.remap(FRONTEND_NODE_APPEND_TYPE, 1, proof, {
        "workflow_session_id": {uid(1): uid(9)}}, scope="content") == proof
    assert types.get(FRONTEND_STATE_TYPE, 1, scope="session").checkpoint_admission is False
    assert types.default(FRONTEND_STATE_TYPE, 1) == EMPTY_FRONTEND_STATE
    assert types.validate_write(FRONTEND_STATE_TYPE, 1, prepared["desired_value"], context) == prepared["desired_value"]


@pytest.mark.parametrize("mutate", [
    lambda source: source.update(component_id="thirdparty.fake"),
    lambda source: source.update(component_version="2"),
    lambda source: source["consumed_config"].update(object_key="other"),
    lambda source: source["consumed_config"].update(role="user"),
    lambda source: source["producer"].update(workflow_session_id=uid(99)),
    lambda source: source["producer"].update(node_binding_id=uid(99)),
    lambda source: source["producer"].update(node_run_id=uid(99)),
    lambda source: source["input_refs"].update(content=[binding(99)]),
    lambda source: source.update(reads=[]),
    lambda source: source.pop("reads"),
    lambda source: source["reads"][0].update(object_key="other"),
    lambda source: source["reads"][0].update(revision=1),
    lambda source: source["reads"][0].update(revision_id=uid(99)),
    lambda source: source["value"]["owner"].update(workflow_session_id=uid(99)),
    lambda source: source["value"]["owner"].update(object_key="other"),
    lambda source: source["value"]["basis"].update(head_revision=1),
    lambda source: source["value"]["basis"].update(revision_id=uid(99)),
    lambda source: source["value"]["entries"][0].update(role="system"),
    lambda source: source["value"]["entries"][1].update(entry_id=uid(99)),
    lambda source: source["value"]["entries"][1].update(source_ref=ref(99)),
    lambda source: source["value"]["entries"].reverse(),
])
def test_owned_proof_rejects_forged_producer_frozen_config_inputs_read_and_state(mutate):
    _, _, prepared, artifacts, context = proof_fixture()
    mutate(artifacts[uid(30)])
    with pytest.raises(ContractValidationError):
        validate_frontend_state_write(prepared["desired_value"], context)


@pytest.mark.parametrize("mutation", ["registrar", "writer", "foreign_key", "state", "content_type", "content_version"])
def test_owned_proof_rejects_other_writer_registrar_owner_and_non_text_sources(mutation):
    _, _, prepared, artifacts, context = proof_fixture()
    if mutation == "registrar":
        context["current_record"]["binding"]["registrar_node_id"] = uid(7)
    elif mutation == "writer":
        context["writer_node_id"] = uid(7)
    elif mutation == "foreign_key":
        context["object_key"] = "other"
    elif mutation == "state":
        prepared["desired_value"]["entries"][0]["role"] = "system"
    elif mutation == "content_type":
        artifacts[uid(21)]["value"] = {"schema_version": 2, "kind": "workflow.json", "value": {}}
    else:
        artifacts[uid(21)]["value"]["schema_version"] = 1
    with pytest.raises(ContractValidationError):
        validate_frontend_state_write(prepared["desired_value"], context)


@pytest.mark.parametrize("mutate", [
    lambda proof: proof.update(extra=True),
    lambda proof: proof.update(schema_version=True),
    lambda proof: proof.update(kind="workflow.frontend-view"),
    lambda proof: proof["owner"].update(node_binding_id=uid(3)),
    lambda proof: proof["basis"].update(head_revision=True),
    lambda proof: proof["basis"].update(head_revision=0),
    lambda proof: proof.update(source_output_refs=[]),
    lambda proof: proof.update(source_output_refs={}),
    lambda proof: proof["source_output_refs"].append(binding(21)),
    lambda proof: proof["source_output_refs"][0].update(order=True),
    lambda proof: proof["source_output_refs"][0].update(order=-1),
    lambda proof: proof["source_output_refs"][0].update(other="unbound"),
    lambda proof: proof.update(role="tool"),
    lambda proof: proof.update(node_run_id="not-a-uuid"),
])
def test_owned_proof_format_rejects_unknown_fields_and_malformed_bindings(mutate):
    _, proof, _, _, _ = proof_fixture()
    mutate(proof)
    with pytest.raises(ContractValidationError):
        validate_frontend_node_append(proof)


def test_owned_prepare_requires_exact_artifact_and_current_basis():
    current, proof, _, _, _ = proof_fixture()
    arguments = {
        "workflow_session_id": uid(1), "object_key": "frontend-owned",
        "object_record": current, "proof": proof, "proof_ref": ref(30),
    }
    for overrides in (
        {"workflow_session_id": uid(99)}, {"object_key": "other"},
        {"object_record": {**current, "revision": 3}},
        {"proof_ref": {"scope": "session", "workflow_session_id": uid(1),
                       "object_key": "frontend-owned", "revision_id": uid(11)}},
    ):
        with pytest.raises(ContractValidationError):
            prepare_frontend_node_adoption(**{**arguments, **overrides})


def _apply_real_write(service, sid, writer, intent):
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            result = SessionObjectStore(store, service.registry.data_types).apply(
                sid, node_id=writer, writes=[intent])
            store._connection.execute("COMMIT")
            return result
        except BaseException:
            store._connection.execute("ROLLBACK")
            raise


@pytest.mark.parametrize("writer", ["owner", "other"])
def test_real_apply_replays_original_receipt_but_rejects_equal_state_with_another_key(tmp_path, writer):
    from test_frontend_node_runtime import _create, _graph, _output, _save
    from test_graph_service import run

    with closing(GraphWorkflowService(tmp_path / "owned-replay.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        owner, other = (entry["node_binding_id"] for entry in frontends)
        graph["object_bindings"][0]["writers"].append(other)
        initial = _create(service, _save(service, graph))
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        sid, key = final["workflow_session_id"], frontends[0]["config"]["object_key"]
        commit = _output(final, frontends[0], "commit")
        original_intent = {
            "object_key": key, "expected_revision": initial["objects"][key]["revision"],
            "operation_key": commit["operation_key"], "operation": "put",
            "value": deepcopy(final["objects"][key]["value"]),
        }
        with closing(service._store()) as store:
            before = tuple(tuple(row) for row in store._connection.execute(
                "SELECT * FROM session_object_receipts ORDER BY session_id,operation_key"))
        assert _apply_real_write(service, sid, owner, original_intent) == [commit["receipt"]]
        assert service.get_session(sid)["objects"] == final["objects"]
        fresh_key_intent = {
            **original_intent, "expected_revision": final["objects"][key]["revision"],
            "operation_key": str(uuid4()),
        }
        with pytest.raises(ContractValidationError):
            _apply_real_write(service, sid, owner if writer == "owner" else other, fresh_key_intent)
        assert service.get_session(sid)["objects"] == final["objects"]
        with closing(service._store()) as store:
            after = tuple(tuple(row) for row in store._connection.execute(
                "SELECT * FROM session_object_receipts ORDER BY session_id,operation_key"))
        assert after == before
