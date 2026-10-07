"""Read-only projections and dynamic object validators share the record boundary."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.global_resources import GlobalResourceStore
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import graph_error
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, TypeRegistry, WriteIntent
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore


def current_document(service):
    source, output = str(uuid4()), str(uuid4())
    return {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Read-only boundary", "object_bindings": [],
        "package_lock": deepcopy(list(service.registry.execution_package_lock)),
        "nodes": [
            {"node_binding_id": source, "component_id": "tools.text", "component_version": "1",
             "title": "Text", "position": {"x": 0, "y": 0}, "config": {"text": "Retained"}},
            {"node_binding_id": output, "component_id": "tools.output", "component_version": "1",
             "title": "Output", "position": {"x": 200, "y": 0}, "config": {"mode": "text"},
             "public_outputs": ["output"]},
        ],
        "edges": [
            {"edge_id": str(uuid4()), "source_node_id": source, "source_port_id": "output",
             "target_node_id": output, "target_port_id": "input", "order": 0},
        ],
    }


def completed(service, document):
    service.save_definition(document, expected_revision=0, idempotency_key="definition")
    view = service.create_session(document["workflow_definition_id"], 1, idempotency_key="session")
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key="start")
    service.wait(started["active_chain_run_id"])
    view = service.get_session(view["workflow_session_id"])
    assert view["status"] == "succeeded"
    return view


def test_readonly_consumer_preserves_outputs_and_candidates_refuse_missing_exact_component(
    tmp_path, monkeypatch,
):
    path = tmp_path / "readonly.sqlite"
    with closing(GraphWorkflowService(path, enabled_packages={"workflow.tools": "1.0.0"})) as service:
        document = current_document(service)
        final = completed(service, document)
        sid = final["workflow_session_id"]
        available = service.registry.get
        monkeypatch.setattr(service.registry, "get", lambda component_id, component_version: (
            None if (component_id, component_version) == ("tools.text", "1")
            else available(component_id, component_version)
        ))

        def forbidden(*args, **kwargs):
            pytest.fail("Read-only consumer compiled an unavailable current definition")

        with monkeypatch.context() as reading:
            reading.setattr(service, "_consumer_inputs", forbidden)
            reading.setattr("phase1_agent.graph_contracts.GraphCompiler.compile", forbidden)
            before = service.get_session(sid)
            assert before["readonly"] is True
            assert before["status"] == "succeeded"
            assert before["can_submit"] is False and before["available_actions"] == []
            consumer = service.get_consumer(sid)
            assert consumer["status"] == "succeeded"
            assert consumer["can_submit"] is False and consumer["available_actions"] == []
            assert consumer["inputs"] == []
            assert consumer["outputs"][0]["payload"]["text"] == "Retained"
        candidates = service.list_graph_candidates(sid)["candidates"]
        assert candidates and all(not item["can_select"] for item in candidates)
        assert candidates[0]["diagnostic"]["reason_code"] == "historical_definition_unavailable"
        assert service.get_session(sid) == before


def links_package(seen):
    def validate_write(value, context):
        if value is None:
            return
        references = value["refs"] + ([value["proof"]] if value["proof"] is not None else [])
        for reference in references:
            artifact = context["resolve_artifact"](reference)
            seen.append(reference["output_id"])
            if artifact["value"]["text"] != "Accepted proof":
                raise graph_error("boundary_invalid_proof", "Artifact does not prove this object transition", 409)

    def register(host):
        host.register_data_type(DataTypeDefinition(
            "boundary.links", 1,
            {"type": "object", "required": ["refs", "proof"],
             "properties": {"refs": {"type": "array"}, "proof": {"type": ["object", "null"]}},
             "additionalProperties": False},
            {"refs": [], "proof": None}, references=lambda value: value["refs"],
            write_validator=validate_write,
        ))
        host.register_node(NodeDefinition(
            "boundary.writer", "1", "Writer", "Boundary", {},
            {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read", "objects:write"),
        ), lambda config, inputs, context: {"output": text_content("Original artifact")})

    return CapabilityPackage(PackageManifest(
        "boundary.objects", "1",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),),
    ), register)


@pytest.mark.parametrize("field", ["refs", "proof"], ids=["declared-reference", "dynamic-validator"])
def test_object_write_validation_preserves_artifact_authorization_and_rolls_back_writes(
    tmp_path, field,
):
    path = tmp_path / "object-boundary.sqlite"
    seen = []
    with closing(GraphWorkflowService(
        path, enabled_packages={"workflow.tools": "1.0.0", "boundary.objects": "1"},
        capability_packages=(links_package(seen),),
    )) as service:
        document = current_document(service)
        writer = document["nodes"][0]
        writer.update(component_id="boundary.writer", config={})
        document["object_bindings"] = [ObjectBinding(
            key, "boundary.links", 1, "shared",
            readers=(writer["node_binding_id"],), writers=(writer["node_binding_id"],),
        ).to_dict() for key in ("notes", "links")]
        final = completed(service, document)
        sid = final["workflow_session_id"]
        foreign = service.create_session(document["workflow_definition_id"], 1, idempotency_key="other-session")
        output_id = next(row["output_id"] for row in final["outputs"]
                         if row["node_binding_id"] == writer["node_binding_id"])
        reference = {"scope": "artifact", "output_id": output_id}

        with closing(SqliteStore(path)) as store:
            objects = SessionObjectStore(store, service.registry.data_types)
            original = objects.current(sid)
            assert objects._resolve_write_artifact(sid, reference)["value"]["text"] == "Original artifact"
            with pytest.raises(ContractValidationError) as denied:
                objects._resolve_write_artifact(foreign["workflow_session_id"], reference)
            assert denied.value.reason_code == "session_object_reference_denied"
            assert seen == []
            value = {"refs": [reference] if field == "refs" else [],
                     "proof": reference if field == "proof" else None}
            store._connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(ContractValidationError) as denied:
                objects.apply(sid, node_id=document["nodes"][1]["node_binding_id"], writes=[
                    WriteIntent("links", original["links"]["revision"], "unauthorized-write", value).to_dict(),
                ])
            assert denied.value.reason_code == "session_object_access_denied"
            store._connection.execute("ROLLBACK")
            assert seen == []
            values_before = [tuple(row) for row in store._connection.execute(
                "SELECT * FROM session_object_values ORDER BY revision_id",
            )]
            store._connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(ContractValidationError) as error:
                objects.apply(sid, node_id=writer["node_binding_id"], writes=[
                    WriteIntent("notes", original["notes"]["revision"], "preceding-write",
                                {"refs": [], "proof": None}).to_dict(),
                    WriteIntent("links", original["links"]["revision"], "blocked-write", value).to_dict(),
                ])
            assert error.value.reason_code == "boundary_invalid_proof"
            store._connection.execute("ROLLBACK")
            assert seen == [output_id]
            assert objects.current(sid) == original
            assert [tuple(row) for row in store._connection.execute(
                "SELECT * FROM session_object_values ORDER BY revision_id",
            )] == values_before
            assert store._connection.execute(
                "SELECT 1 FROM session_object_receipts WHERE session_id=? "
                "AND operation_key IN ('preceding-write','blocked-write','unauthorized-write')",
                (sid,),
            ).fetchone() is None


def test_resource_receipt_replay_precedes_missing_current_type_and_new_writes_fail(tmp_path):
    path = tmp_path / "resource-replay.sqlite"
    types = TypeRegistry()
    types.register(DataTypeDefinition("boundary.global", 1, {"type": "string"}, "", scope="global"))
    record = {
        "envelope_version": 1, "scope": "workspace", "type_id": "boundary.global",
        "resource_id": str(uuid4()), "data_schema_version": 1, "update_sequence": 1,
        "value": "original",
    }
    with closing(SqliteStore(path)) as store:
        resources = GlobalResourceStore(store, types)
        write_key, delete_key = str(uuid4()), str(uuid4())
        accepted_write = resources.write(record, expected_sequence=0, idempotency_key=write_key)
        reference = accepted_write["reference"]
        accepted_delete = resources.delete(reference, expected_sequence=1, idempotency_key=delete_key)
        unavailable = GlobalResourceStore(store, TypeRegistry())
        assert unavailable.write(record, expected_sequence=0, idempotency_key=write_key) == accepted_write
        assert unavailable.delete(reference, expected_sequence=1, idempotency_key=delete_key) == accepted_delete
        new_key = str(uuid4())
        with pytest.raises(ContractValidationError) as error:
            unavailable.write(record, expected_sequence=0, idempotency_key=new_key)
        assert error.value.reason_code == "host_unknown_type"
        assert resources.head(reference) == accepted_delete
        assert store._connection.execute(
            "SELECT 1 FROM global_resource_receipts WHERE idempotency_key=?", (new_key,),
        ).fetchone() is None
