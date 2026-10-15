"""Phase-one object/snapshot acceptance with an independent trusted package."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import json_content, text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, WriteIntent
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from test_graph_service import node, edge, document, create, run
from test_success_checkpoints import checkpoint_history


def task_package():
    def register(host):
        host.register_data_type(DataTypeDefinition(
            "example.task", 1, {"type": "object", "required": ["count"],
                              "properties": {"count": {"type": "integer", "minimum": 0}},
                              "additionalProperties": False}, default_value={"count": 0}))
        def map_links(value, mapping):
            result = deepcopy(value)
            for ref in result.get("refs", []):
                if ref["scope"] == "session":
                    ref["workflow_session_id"] = mapping.get("workflow_session_id", {}).get(
                        ref["workflow_session_id"], ref["workflow_session_id"])
            return result
        host.register_data_type(DataTypeDefinition(
            "example.links", 1, {"type": "object", "properties": {"refs": {"type": "array"}},
                                "additionalProperties": False}, default_value={"refs": []},
            references=lambda value: value["refs"], reference_mapper=map_links))

        def count(config, inputs, context):
            old = context.object_read(config["key"])
            value = {"count": old["value"]["count"] + 1}
            context.object_write(config["key"], value, expected_revision=old["revision"])
            if config["fail"]:
                raise ValueError("intentional post-write failure")
            return {"output": text_content(str(value["count"]))}

        host.register_node(NodeDefinition(
            "example.count", "1", "Task counter", "Example", {"key": "main", "fail": False},
            {"type": "object", "properties": {"key": {"type": "string"}, "fail": {"type": "boolean"}},
             "required": ["key", "fail"], "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read", "objects:write")),
            count)
        host.register_node(NodeDefinition(
            "example.object-read", "1", "Task read", "Example", {"object_key": "main"},
            {"type": "object", "properties": {"object_key": {"type": "string"}},
             "required": ["object_key"], "additionalProperties": False},
            outputs=(NodePort("value", "JSON", data_schema_version=2),),
            capabilities=("objects:read",),
        ), lambda config, inputs, context: {
            "value": json_content(context.object_read(config["object_key"])["value"]),
        })
    return CapabilityPackage(PackageManifest(
        "example.tasks", "1.0.0", (PackageDependency("workflow.content", "1.0.0"),),
    ), register)


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "objects.sqlite", capability_packages=[task_package()],
                                      enabled_packages={"workflow.tools": "1.0.0", "example.tasks": "1.0.0"})) as service:
        yield service


def object_graph(service, *, fail=False):
    count = node(service.registry, "example.count", 1, fail=fail)
    read = node(service.registry, "example.object-read", 2, object_key="main")
    convert = node(service.registry, "json-to-text", 3)
    output = node(service.registry, "output", 4)
    doc = document([count, read, convert, output], [
        edge(count, output, 1), edge(read, convert, 2, source_port="value"),
    ])
    doc["schema_version"] = 2
    doc["object_bindings"] = [
        ObjectBinding("main", "example.task", 1, "shared",
                      readers=(count["node_binding_id"], read["node_binding_id"]),
                      writers=(count["node_binding_id"],)).to_dict(),
        ObjectBinding("other", "example.task", 1, "private",
                      readers=(count["node_binding_id"],), writers=(count["node_binding_id"],),
                      owner_node_id=count["node_binding_id"]).to_dict(),
    ]
    doc["package_lock"] = list(service.registry.package_lock)
    count["public_outputs"] = ["output"]
    return doc


def write(service, view, key, value, *, expected=None, operation_key=None, node_id=None):
    return service.update_session_objects(view["workflow_session_id"], node_id=node_id or view["nodes"][0]["node_binding_id"],
        writes=[WriteIntent(key, expected or view["objects"][key]["revision"],
                            operation_key or str(uuid4()), value).to_dict()],
        expected_revision=view["revision"], idempotency_key=str(uuid4()))["session"]


def test_registered_object_defaults_isolation_and_shared_read_nodes(service):
    doc = object_graph(service)
    # Connect the read node to a second output so it executes after the counter.
    other_output = node(service.registry, "output", 5)
    doc["nodes"].append(other_output)
    doc["edges"].append(edge(doc["nodes"][2], other_output, 3))
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded"
    assert final["objects"]["main"]["value"] == {"count": 1}
    assert final["objects"]["other"]["value"] == {"count": 0}
    assert final["objects"]["main"]["revision"] == 2
    manifest = service.get_run(final["workflow_session_id"], final["chains"][-1]["chain_run_id"])["state_manifests"]
    assert manifest["start"]["objects"]["main"]["revision"] == 1
    assert manifest["end"]["objects"]["main"]["revision"] == 2
    assert manifest["end"]["package_lock"] == list(service.registry.package_lock)


def test_unknown_types_permissions_cas_and_schema_rejected(service):
    doc = object_graph(service)
    bad = deepcopy(doc)
    bad["workflow_definition_id"] = str(uuid4())
    bad["object_bindings"][0]["type_id"] = "missing.type"
    with pytest.raises(ContractValidationError):
        create(service, bad)
    view = create(service, doc)
    with pytest.raises(ContractValidationError) as denied:
        service.read_session_object(view["workflow_session_id"], object_key="other",
                                    node_id=doc["nodes"][1]["node_binding_id"])
    assert denied.value.reason_code == "session_object_access_denied"
    changed = write(service, view, "main", {"count": 3})
    with pytest.raises(ContractValidationError) as stale:
        write(service, changed, "main", {"count": 4}, expected=1)
    assert stale.value.reason_code == "stale_object_revision"
    with pytest.raises(ContractValidationError):
        write(service, changed, "main", {"count": "bad"})
    assert service.get_session(view["workflow_session_id"])["objects"]["main"]["value"] == {"count": 3}


def test_receipts_atomic_multi_write_and_delete_retains_checkpoint(service):
    view = create(service, object_graph(service))
    sid, writer = view["workflow_session_id"], view["nodes"][0]["node_binding_id"]
    first = WriteIntent("main", 1, "first", {"count": 2}).to_dict()
    wrong = WriteIntent("other", 5, "wrong", {"count": 3}).to_dict()
    with pytest.raises(ContractValidationError):
        service.update_session_objects(sid, node_id=writer, writes=[first, wrong],
            expected_revision=view["revision"], idempotency_key="atomic-failure")
    assert service.get_session(sid)["objects"]["main"]["revision"] == 1
    response = service.update_session_objects(sid, node_id=writer, writes=[first],
        expected_revision=view["revision"], idempotency_key="write")
    assert service.update_session_objects(sid, node_id=writer, writes=[first],
        expected_revision=view["revision"], idempotency_key="write") == response
    current = response["session"]
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        objects = SessionObjectStore(store, service.registry.data_types)
        assert objects.apply(sid, node_id=writer, writes=[first])[0]["revision"] == 2
        store._connection.execute("ROLLBACK")
    delete = WriteIntent("main", 2, "delete", operation="delete").to_dict()
    result = service.update_session_objects(sid, node_id=writer, writes=[delete],
        expected_revision=current["revision"], idempotency_key="delete")["session"]
    assert result["objects"]["main"]["deleted"]
    with pytest.raises(ContractValidationError) as deleted:
        service.read_session_object(sid, object_key="main", node_id=writer)
    assert deleted.value.reason_code == "session_object_deleted"
    assert result["head_commit_id"] == view["head_commit_id"]


def test_failed_effect_is_rejected_and_has_no_success_checkpoint(service):
    view = create(service, object_graph(service, fail=True))
    head = view["head_commit_id"]
    final = run(service, view)
    assert final["status"] == "failed" and final["objects"]["main"]["value"] == {"count": 0}
    assert final["head_commit_id"] == head
    assert checkpoint_history(service, final)["latest"] is None
    end = service.get_run(final["workflow_session_id"], final["chains"][-1]["chain_run_id"])["state_manifests"]["end"]
    assert end["status"] == "failed" and end["objects"]["main"]["revision"] == 1


def test_missing_package_keeps_objects_and_frozen_run_but_blocks_export_and_execution(tmp_path):
    path = tmp_path / "missing.sqlite"
    with closing(GraphWorkflowService(path, capability_packages=[task_package()],
                                      enabled_packages={"workflow.tools": "1.0.0", "example.tasks": "1.0.0"})) as service:
        doc = object_graph(service)
        final = run(service, create(service, doc))
        sid = final["workflow_session_id"]
        chain_id = final["selected_chain_run_id"]
        frozen_run = service.get_run(sid, chain_id)
        service.configure_capability_packages({"workflow.tools": "1.0.0"})
        assert service.get_session_objects(sid)["objects"]["main"]["value"] == {"count": 1}
        assert service.get_run(sid, chain_id) == frozen_run
        with pytest.raises(ContractValidationError) as denied:
            service.read_public_output(sid, workflow_definition_id=doc["workflow_definition_id"],
                definition_revision=1, node_id=doc["nodes"][0]["node_binding_id"], port_id="output")
        assert denied.value.reason_code == "graph_missing_node_type"
        with pytest.raises(ContractValidationError):
            service.start(sid, expected_revision=final["revision"], idempotency_key="missing-start")
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.get_session_objects(sid)["objects"]["main"]["value"] == {"count": 1}


def test_foreign_or_private_same_type_revision_reference_is_denied(service):
    # Type-specific references are provided by a trusted package, never guessed.
    registry = service.registry.detached()
    registry.data_types.register(DataTypeDefinition(
        "example.reference", 1, {"type": "object"}, default_value={},
        references=lambda value: [value["ref"]] if "ref" in value else []))
    service.registry = registry
    doc = object_graph(service)
    doc["object_bindings"].append(ObjectBinding(
        "ref", "example.reference", 1, "shared", readers=(doc["nodes"][0]["node_binding_id"],),
        writers=(doc["nodes"][0]["node_binding_id"],)).to_dict())
    first, foreign = create(service, doc), service.create_session(doc["workflow_definition_id"], 1, idempotency_key="foreign")
    for revision_id in (first["objects"]["other"]["revision_id"], foreign["objects"]["main"]["revision_id"]):
        ref = {"scope": "session", "workflow_session_id": first["workflow_session_id"],
               "object_key": "main", "revision_id": revision_id}
        with pytest.raises(ContractValidationError) as denied:
            write(service, first, "ref", {"ref": ref})
        assert denied.value.reason_code == "session_object_reference_denied"


def test_local_object_references_persist_and_foreign_artifacts_are_denied(service):
    doc = object_graph(service)
    writer = doc["nodes"][0]["node_binding_id"]
    doc["object_bindings"].append(ObjectBinding(
        "links", "example.links", 1, "shared", readers=(writer,), writers=(writer,)).to_dict())
    first = run(service, create(service, doc))
    output = next(row for row in service.get_run(first["workflow_session_id"],
        first["chains"][-1]["chain_run_id"])["outputs"] if row["node_binding_id"] == writer)
    refs = [
        {"scope": "artifact", "output_id": output["output_id"]},
        {"scope": "session", "workflow_session_id": first["workflow_session_id"],
         "object_key": "main", "revision_id": first["objects"]["main"]["revision_id"]},
    ]
    edited = write(service, first, "links", {"refs": refs})
    second = run(service, edited)
    assert second["objects"]["links"]["value"]["refs"] == refs
    assert service.read_session_object(first["workflow_session_id"], object_key="main", node_id=writer,
        revision_id=refs[1]["revision_id"])["value"] == {"count": 1}
    foreign = run(service, service.create_session(
        doc["workflow_definition_id"], 1, idempotency_key="foreign-artifact"))
    foreign_output = next(row for row in service.get_run(foreign["workflow_session_id"],
        foreign["chains"][-1]["chain_run_id"])["outputs"] if row["node_binding_id"] == writer)
    with pytest.raises(ContractValidationError) as denied:
        write(service, second, "links", {"refs": [
            {"scope": "artifact", "output_id": foreign_output["output_id"]}]})
    assert denied.value.reason_code == "session_object_reference_denied"
    assert service.get_session(second["workflow_session_id"])["objects"] == second["objects"]


def test_tombstone_reopen_and_unrelated_manifest_access(service):
    final = run(service, create(service, object_graph(service)))
    writer, sid = final["nodes"][0]["node_binding_id"], final["workflow_session_id"]
    deleted = service.update_session_objects(sid, node_id=writer, writes=[
        WriteIntent("main", final["objects"]["main"]["revision"], "logical-delete", operation="delete").to_dict()],
        expected_revision=final["revision"], idempotency_key="logical-delete")["session"]
    assert deleted["objects"]["main"]["deleted"]
    assert deleted["head_commit_id"] == final["head_commit_id"]
    other = service.create_session(final["workflow_definition_id"], 1, idempotency_key="other-manifest")
    with closing(SqliteStore(service.database)) as store:
        commit = store._get("workflow_commit", final["head_commit_id"])
    with pytest.raises(ContractValidationError) as denied:
        service.get_state_manifest(other["workflow_session_id"], snapshot_id=commit["state_snapshot_id"])
    assert denied.value.reason_code == "manifest_scope_mismatch"
    path = service.database
    service.close()
    with closing(GraphWorkflowService(path, capability_packages=[task_package()])) as reopened:
        assert reopened.get_session(sid)["objects"] == deleted["objects"]
        with pytest.raises(ContractValidationError) as denied:
            reopened.read_session_object(sid, object_key="main", node_id=writer)
        assert denied.value.reason_code == "session_object_deleted"


def test_project_selection_survives_restart_and_missing_install_is_diagnosed(tmp_path):
    path = tmp_path / "project.sqlite"
    with closing(GraphWorkflowService(path, capability_packages=[task_package()],
                                      enabled_packages={"workflow.tools": "1.0.0", "example.tasks": "1.0.0"})) as service:
        final = run(service, create(service, object_graph(service)))
    with closing(GraphWorkflowService(path, capability_packages=[task_package()])) as reopened:
        assert any(row["package_id"] == "example.tasks" for row in reopened.platform_capabilities()["package_lock"])
        assert run(reopened, reopened.get_session(final["workflow_session_id"]))["objects"]["main"]["value"] == {"count": 2}
        before = reopened.get_session(final["workflow_session_id"])
        historical = reopened.get_run(final["workflow_session_id"], before["selected_chain_run_id"])
    with closing(GraphWorkflowService(path)) as missing:
        catalog = missing.platform_capabilities()
        assert catalog["package_diagnostics"][0]["reason_code"] == "package_missing_dependency"
        assert catalog["package_diagnostics"][0]["enabled_packages"] == {
            "workflow.tools": "1.0.0", "example.tasks": "1.0.0"}
        assert catalog["package_lock"] == [] and missing.registry.catalog() == []
        assert missing.get_session(final["workflow_session_id"])["objects"]["main"]["value"] == {"count": 2}
        assert missing.get_run(final["workflow_session_id"], before["selected_chain_run_id"]) == historical
        with pytest.raises(ContractValidationError) as denied:
            run(missing, missing.get_session(final["workflow_session_id"]))
        assert denied.value.reason_code == "package_missing_dependency"
        assert not hasattr(missing, "_native_runtime")


def test_long_valid_object_identity_has_a_bounded_automatic_operation_key(service):
    doc = object_graph(service)
    key = "k" * 128
    doc["nodes"][0]["config"]["key"] = key
    doc["nodes"][1]["config"]["object_key"] = key
    doc["object_bindings"][0]["object_key"] = key
    final = run(service, create(service, doc))
    assert final["status"] == "succeeded" and final["objects"][key]["value"] == {"count": 1}


def test_new_workflow_uses_its_own_private_node_permissions_and_empty_data(service):
    doc = object_graph(service)
    first = run(service, create(service, doc))
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    original, renamed = doc["nodes"][0]["node_binding_id"], str(uuid4())
    target["nodes"][0]["node_binding_id"] = renamed
    for binding in target["object_bindings"]:
        for field in ("readers", "writers"):
            binding[field] = [renamed if node_id == original else node_id for node_id in binding[field]]
        if binding.get("owner_node_id") == original:
            binding["owner_node_id"] = renamed
    for link in target["edges"]:
        if link["source_node_id"] == original:
            link["source_node_id"] = renamed
    fresh = create(service, target)
    assert fresh["objects"]["main"]["value"] == {"count": 0}
    assert fresh["objects"]["other"]["binding"]["owner_node_id"] == renamed
    assert service.read_session_object(fresh["workflow_session_id"], object_key="other", node_id=renamed)
    with pytest.raises(ContractValidationError) as denied:
        service.read_session_object(fresh["workflow_session_id"], object_key="other", node_id=original)
    assert denied.value.reason_code == "session_object_access_denied"
    assert service.read_session_object(first["workflow_session_id"], object_key="other", node_id=original)
    with pytest.raises(ContractValidationError) as denied:
        service.read_session_object(first["workflow_session_id"], object_key="other", node_id=renamed)
    assert denied.value.reason_code == "session_object_access_denied"
    assert service.get_session(first["workflow_session_id"]) == first
