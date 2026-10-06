"""Save fault rollback, explicit retained-view adoption and immutable copies.

Failed chains do not resume. After closing, the platform adopts the proven
exact V3 without executing its upstream producer again.
"""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from phase1_agent.host_sdk import ObjectBinding, WriteIntent
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.capability_packages import CapabilityPackage
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort

from test_context_integration import synthetic_package, context_graph, _output
from test_graph_service import create, run, node, edge


def _assert_no_replay_command(service, final):
    assert final["available_actions"] == ["close"]
    for action in ("resume", "retry_archive"):
        with pytest.raises(ContractValidationError) as rejected:
            service.control(final["workflow_session_id"], action=action,
                            expected_revision=final["revision"], idempotency_key=str(uuid4()))
        assert rejected.value.reason_code == "recovery_unavailable"


def _candidate_id(service, final):
    history = service.get_run(final["workflow_session_id"], final["chains"][-1]["chain_run_id"])
    advance_id = next(entry["node_binding_id"] for entry in final["nodes"] if entry["label"] == "context.advance")
    return next(entry["output_refs"]["output"] for entry in history["node_runs"]
                if entry["node_binding_id"] == advance_id)


def _close_and_adopt(service, final):
    output_id = _candidate_id(service, final)
    writer = next(entry["node_binding_id"] for entry in final["nodes"] if entry["label"] == "context.save")
    with pytest.raises(ContractValidationError):
        service.adopt_context_view(final["workflow_session_id"], node_id=writer,
                                   view_output_id=output_id, expected_revision=final["revision"],
                                   idempotency_key=str(uuid4()))
    closed = service.control(final["workflow_session_id"], action="close",
                             expected_revision=final["revision"], idempotency_key=str(uuid4()))
    key = str(uuid4())
    adopted = service.adopt_context_view(closed["workflow_session_id"], node_id=writer,
                                        view_output_id=output_id, expected_revision=closed["revision"],
                                        idempotency_key=key)
    replay = service.adopt_context_view(closed["workflow_session_id"], node_id=writer,
                                       view_output_id=output_id, expected_revision=closed["revision"],
                                       idempotency_key=key)
    assert replay == adopted
    value = adopted["session"]["objects"]["context"]["value"]
    assert value["view_ref"] == {"scope": "artifact", "output_id": output_id}
    assert value["accepted_delta_ids"] == _output(final, "context.advance")["applied_delta_ids"]
    assert adopted["session"]["objects"]["context"]["revision"] == 2
    return adopted


def test_save_business_fault_retains_accepted_v3_but_has_no_platform_replay_entry(tmp_path, monkeypatch):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "business-fault.sqlite", capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        original = service._host_call

        def fail_save(context, capability, operation, payload):
            if capability == "context:validate" and payload.get("operation") == "save":
                raise OSError("injected save authorization read failure")
            return original(context, capability, operation, payload)

        monkeypatch.setattr(service, "_host_call", fail_save)
        final = run(service, create(service, context_graph(service)))
        assert final["status"] == "failed"
        candidate = _output(final, "context.advance")
        assert len(candidate["units"]) == 1 and len(candidate["applied_delta_ids"]) == 1
        assert final["objects"]["context"]["value"] == {"view_ref": None, "accepted_delta_ids": []}
        assert final["objects"]["context"]["revision"] == 1
        assert len(calls) == 1
        _assert_no_replay_command(service, final)
        monkeypatch.setattr(service, "_host_call", original)
        # Fixing the temporary cause does not create a failed-chain replay API.
        _assert_no_replay_command(service, service.get_session(final["workflow_session_id"]))
        assert _output(service.get_session(final["workflow_session_id"]), "context.advance") == candidate
        _close_and_adopt(service, final)
        assert len(calls) == 1


def test_save_transaction_fault_rolls_back_pointer_and_receipt_retaining_exact_v3(tmp_path, monkeypatch):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "transaction-fault.sqlite", capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        doc = context_graph(service)
        save_id = doc["nodes"][-1]["node_binding_id"]
        original = service._save_effects
        observed_writes = []

        def fail_after_write(repo, sid, document, event):
            original(repo, sid, document, event)
            if event["node_binding_id"] == save_id:
                observed_writes.extend(event["object_writes"])
                raise OSError("injected failure after context write before node transaction commit")

        monkeypatch.setattr(service, "_save_effects", fail_after_write)
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        final = service.get_session(initial["workflow_session_id"])
        assert final["status"] == "archive_failed"
        assert final["available_actions"] == ["retry_acceptance", "close"]
        assert len(observed_writes) == 1
        candidate = _output(final, "context.advance")
        assert candidate["applied_delta_ids"]
        assert final["objects"]["context"]["revision"] == 1
        assert final["objects"]["context"]["value"] == {"view_ref": None, "accepted_delta_ids": []}
        with closing(SqliteStore(service.database)) as store:
            assert store._connection.execute(
                "SELECT result FROM session_object_receipts WHERE session_id=? AND operation_key=?",
                (final["workflow_session_id"], observed_writes[0]["operation_key"])).fetchone() is None
        # The successful candidate can now retry only settlement. Explicit
        # close still permits adopting the separately accepted legacy V3.
        _close_and_adopt(service, final)
        assert len(calls) == 1
        assert not service._service_runs
        assert not service._runtime_hosts


def test_explicit_adoption_rejects_late_cas_wrong_writer_and_foreign_session(tmp_path, monkeypatch):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "adoption-fences.sqlite", capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        original = service._host_call

        def fail_save(context, capability, operation, payload):
            if capability == "context:validate" and payload.get("operation") == "save":
                raise OSError("retain a candidate without committing context")
            return original(context, capability, operation, payload)

        monkeypatch.setattr(service, "_host_call", fail_save)
        final = run(service, create(service, context_graph(service)))
        output_id = _candidate_id(service, final)
        writer = final["nodes"][-1]["node_binding_id"]
        monkeypatch.setattr(service, "_host_call", original)
        closed = service.control(final["workflow_session_id"], action="close",
                                 expected_revision=final["revision"], idempotency_key=str(uuid4()))
        for bad_writer in (final["nodes"][2]["node_binding_id"], str(uuid4())):
            with pytest.raises(ContractValidationError):
                service.adopt_context_view(closed["workflow_session_id"], node_id=bad_writer,
                                           view_output_id=output_id, expected_revision=closed["revision"],
                                           idempotency_key=str(uuid4()))
        foreign = create(service, context_graph(service))
        with pytest.raises(ContractValidationError):
            service.adopt_context_view(foreign["workflow_session_id"], node_id=foreign["nodes"][-1]["node_binding_id"],
                                       view_output_id=output_id, expected_revision=foreign["revision"],
                                       idempotency_key=str(uuid4()))
        edited = service.update_session_objects(
            closed["workflow_session_id"], node_id=writer,
            writes=[WriteIntent("context", 1, str(uuid4()),
                                {"view_ref": None, "accepted_delta_ids": []}).to_dict()],
            expected_revision=closed["revision"], idempotency_key=str(uuid4()))["session"]
        with pytest.raises(ContractValidationError) as late:
            service.adopt_context_view(edited["workflow_session_id"], node_id=writer,
                                       view_output_id=output_id, expected_revision=edited["revision"],
                                       idempotency_key=str(uuid4()))
        assert late.value.reason_code == "context_stale_basis"
        assert service.get_session(edited["workflow_session_id"])["objects"]["context"]["value"] == {
            "view_ref": None, "accepted_delta_ids": []}
        assert len(calls) == 1


def test_inline_units_are_consistent_immutable_copies_but_not_body_deduplication(tmp_path):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "copies.sqlite", capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        first = run(service, create(service, context_graph(service)))
        original_unit = _output(first, "context-test.unit")
        assert _output(first, "context-test.delta")["unit"] == original_unit
        assert _output(first, "context.advance")["units"][0]["unit"] == original_unit
        second = run(service, first)
        assert second["status"] == "succeeded"
        assert _output(second, "context.read")["units"][0]["unit"] == original_unit
        assert _output(second, "context.window")["units"][0]["unit"] == original_unit
        ready = _output(second, "context.assembly")
        assert ready["context"]["units"][0]["unit"] == original_unit
        assert ready["messages"][0] == original_unit["root"]
        assert ready["messages"][1]["content"] == original_unit["messages"][0]["content"]
        # One mutable authority is a pointer; immutable material copies still exist.
        assert set(second["objects"]["context"]["value"]) == {"view_ref", "accepted_delta_ids"}
        assert len(calls) == 2


def test_generic_object_edit_cannot_rebind_another_context_owner_through_artifact_pointer(tmp_path):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "owner-reference.sqlite", capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        doc = context_graph(service)
        other_read = node(service.registry, "context.read", 10, object_key="other")
        other_read["title"] = "Other context read"
        other_project = node(service.registry, "context.project-text", 11)
        other_project["title"] = "Other context projection"
        doc["nodes"] += [other_read, other_project]
        doc["edges"].append(edge(other_read, other_project, 50, target_port="view"))
        doc["execution_roots"].append(other_project["node_binding_id"])
        doc["object_bindings"].append(ObjectBinding(
            "other", EFFECTIVE_CONTEXT_TYPE, 1, "private",
            readers=(other_read["node_binding_id"],), writers=(other_read["node_binding_id"],),
            owner_node_id=other_read["node_binding_id"],
        ).to_dict())
        final = run(service, create(service, doc))
        assert final["status"] == "succeeded"
        assert _output(final, "Other context projection") == {"schema_version": 2, "kind": "workflow.text", "text": ""}
        with pytest.raises(ContractValidationError):
            service.read_session_object(final["workflow_session_id"], object_key="context",
                                        node_id=other_read["node_binding_id"])
        original = final["objects"]["context"]["value"]
        # Artifact existence in the same session must not change its object owner.
        with pytest.raises(ContractValidationError):
            service.update_session_objects(
                final["workflow_session_id"], node_id=other_read["node_binding_id"],
                writes=[WriteIntent("other", 1, str(uuid4()), original).to_dict()],
                expected_revision=final["revision"], idempotency_key=str(uuid4()))


def test_generic_graph_object_write_also_cannot_launder_context_pointer(tmp_path):
    calls, attempted = [], []
    package = synthetic_package(calls)

    def register(host):
        package.register(host)

        def write(config, inputs, context):
            current = context.object_read(config["object_key"])
            if attempted:
                context.object_write(config["object_key"], attempted[0], expected_revision=current["revision"])
            return {"output": text_content("generic object writer")}

        host.register_node(NodeDefinition(
            "context-test.object-writer", "1", "Generic object writer", "Test", {"object_key": "other"},
            {"type": "object", "additionalProperties": False,
             "properties": {"object_key": {"type": "string"}}, "required": ["object_key"]},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read", "objects:write"), input_storage="references",
            object_accesses=({"config_field": "object_key", "multiple": False, "access": "read_write",
                              "type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 1},),
        ), write)

    with closing(GraphWorkflowService(
        tmp_path / "graph-write-owner.sqlite", capability_packages=[CapabilityPackage(package.manifest, register)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        doc = context_graph(service)
        writer = node(service.registry, "context-test.object-writer", 12)
        doc["nodes"].append(writer)
        doc["execution_roots"].append(writer["node_binding_id"])
        doc["object_bindings"].append(ObjectBinding(
            "other", EFFECTIVE_CONTEXT_TYPE, 1, "private",
            readers=(writer["node_binding_id"],), writers=(writer["node_binding_id"],),
            owner_node_id=writer["node_binding_id"],
        ).to_dict())
        first = run(service, create(service, doc))
        assert first["status"] == "succeeded"
        with pytest.raises(ContractValidationError):
            service.read_session_object(first["workflow_session_id"], object_key="context",
                                        node_id=writer["node_binding_id"])
        attempted.append(deepcopy(first["objects"]["context"]["value"]))
        started = service.start(first["workflow_session_id"], expected_revision=first["revision"],
                                idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError) as denied:
            service.wait(started["active_chain_run_id"])
        assert denied.value.reason_code == "context_save_owner_mismatch"
        final = service.get_session(first["workflow_session_id"])
        assert final["objects"]["other"]["revision"] == 1
        assert final["objects"]["other"]["value"] == {"view_ref": None, "accepted_delta_ids": []}
