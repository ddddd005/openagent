"""Generic transition hooks protect object owners without domain imports."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, ObjectBinding, TypeRegistry, WriteIntent
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from test_graph_service import create, document, edge, node, run
from test_model_package import uid


TYPE = "test.guarded-object"
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["count", "artifact_ref"],
          "properties": {"count": {"type": "integer"}, "artifact_ref": {"type": ["object", "null"]}}}
DEFAULT = {"count": 0, "artifact_ref": None}


def write_context(*, operation="put"):
    return {"workflow_session_id": uid(1), "object_key": "main",
            "current_record": None if operation == "initialize" else {"value": deepcopy(DEFAULT)},
            "operation": operation, "resolve_artifact": lambda ref: {"value": None}}


def test_optional_hook_keeps_old_metadata_and_positional_fields_compatible():
    plain = DataTypeDefinition(TYPE, 1, SCHEMA, DEFAULT)
    assert "has_write_validator" not in plain.to_dict()
    calls = []

    def validate(value, context):
        calls.append(context["operation"])
        value["count"] = 999
        context["current_record"]["value"]["count"] = 999
        return {"count": 999}

    definition = DataTypeDefinition(TYPE, 1, SCHEMA, DEFAULT, write_validator=validate)
    registry = TypeRegistry()
    registry.register(definition)
    detached = registry.detached(frozen=True)
    assert detached.get(TYPE, 1, scope="session").write_validator is validate
    assert detached.catalog()[0]["has_write_validator"] is True
    value, context = deepcopy(DEFAULT), write_context()
    assert detached.validate_write(TYPE, 1, value, context) == DEFAULT
    assert value == DEFAULT and context["current_record"]["value"] == DEFAULT
    assert calls == ["put"]
    with pytest.raises(HostContractError):
        TypeRegistry().register(DataTypeDefinition(TYPE, 1, SCHEMA, DEFAULT, write_validator=True))


def test_delete_and_initialize_hook_have_explicit_context_and_bad_shapes_are_rejected():
    seen = []
    registry = TypeRegistry()
    registry.register(DataTypeDefinition(TYPE, 1, SCHEMA, DEFAULT,
                                         write_validator=lambda value, context: seen.append((value, context))))
    assert registry.validate_write(TYPE, 1, None, write_context(operation="delete")) is None
    assert registry.validate_write(TYPE, 1, DEFAULT, write_context(operation="initialize")) == DEFAULT
    assert seen[0][0] is None and seen[0][1]["operation"] == "delete"
    assert seen[1][1]["current_record"] is None
    with pytest.raises(HostContractError):
        registry.validate_write(TYPE, 1, DEFAULT, {**write_context(), "current_record": None})
    with pytest.raises(HostContractError):
        registry.validate_write(TYPE, 1, DEFAULT, write_context(operation="delete"))


def guarded_package(calls):
    def guard(value, context):
        calls.append((deepcopy(value), {key: deepcopy(item) for key, item in context.items()
                                       if key != "resolve_artifact"}))
        if context["operation"] == "initialize":
            if value != DEFAULT:
                raise HostContractError("guard_bad_default", "New objects require an empty default")
        elif context["operation"] == "delete":
            if context["current_record"]["value"]["count"] > 0:
                raise HostContractError("guard_delete_denied", "Populated objects cannot be deleted")
        elif value["artifact_ref"] is not None:
            artifact = context["resolve_artifact"](value["artifact_ref"])
            assert set(artifact) == {"value", "component_id", "component_version", "config", "producer", "input_refs"}
            assert artifact["component_id"] == "tools.text"
            assert artifact["component_version"] == "1"
            assert artifact["config"]["text"] == artifact["value"]["text"]
            if artifact["value"]["text"] != context["object_key"]:
                raise HostContractError("guard_owner_mismatch", "Artifact belongs to another object")

    def register(host):
        host.register_data_type(DataTypeDefinition(
            TYPE, 1, SCHEMA, DEFAULT, references=lambda value: (
                [deepcopy(value["artifact_ref"])] if value["artifact_ref"] is not None else []),
            reference_mapper=lambda value, mapping: deepcopy(value), write_validator=guard,
        ))
        host.register_node(NodeDefinition(
            "test.guarded-writer", "1", "Guarded writer", "Test", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), lambda config, inputs, context: {"output": text_content("writer")})

    return CapabilityPackage(PackageManifest(
        "test.write-guard", "1.0.0", (PackageDependency("workflow.content", "1.0.0"),)), register)


def object_graph(service, *, default=None):
    source = node(service.registry, "tools.text", 301, text="main")
    output = node(service.registry, "tools.output", 302)
    writer = node(service.registry, "test.guarded-writer", 303)
    doc = document([source, output, writer], [edge(source, output, 304)])
    doc["schema_version"] = 2
    doc["object_bindings"] = [ObjectBinding(
        key, TYPE, 1, "shared", readers=(writer["node_binding_id"],), writers=(writer["node_binding_id"],),
        **({"default_value": default} if default is not None else {}),
    ).to_dict() for key in ("main", "other")]
    doc["package_lock"] = list(service.registry.package_lock)
    return doc


def update(service, view, key, value, *, expected=None, operation="put", operation_key=None):
    intent = WriteIntent(key, expected or view["objects"][key]["revision"], operation_key or str(uuid4()),
                         value, operation).to_dict()
    return service.update_session_objects(
        view["workflow_session_id"], node_id=view["nodes"][2]["node_binding_id"], writes=[intent],
        expected_revision=view["revision"], idempotency_key=str(uuid4()))["session"]


@pytest.fixture
def guarded_service(tmp_path):
    calls = []
    with closing(GraphWorkflowService(
        tmp_path / "guarded.sqlite", capability_packages=[guarded_package(calls)],
        enabled_packages={"test.write-guard": "1.0.0", "workflow.tools": "1.0.0"},
    )) as service:
        yield service, calls


def test_generic_management_write_cannot_launder_same_session_artifact_owner(guarded_service):
    service, calls = guarded_service
    final = run(service, create(service, object_graph(service)))
    history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
    source = next(item for item in history["node_runs"] if item["node_binding_id"] == final["nodes"][0]["node_binding_id"])
    reference = {"scope": "artifact", "output_id": source["output_refs"]["output"]}
    value = {"count": 1, "artifact_ref": reference}
    accepted = update(service, final, "main", value)
    assert accepted["objects"]["main"]["value"] == value
    assert calls[-1][1]["current_record"]["revision"] == 1
    assert calls[-1][1]["current_record"]["revision_id"] == final["objects"]["main"]["revision_id"]
    with pytest.raises(HostContractError) as denied:
        update(service, accepted, "other", value)
    assert denied.value.reason_code == "guard_owner_mismatch"
    assert service.get_session(final["workflow_session_id"]) == accepted
    assert accepted["objects"]["other"]["revision"] == 1
    with pytest.raises(HostContractError):
        update(service, accepted, "main", None, operation="delete")
    assert service.get_session(final["workflow_session_id"]) == accepted


def test_cas_and_reference_checks_precede_hook_and_rejected_defaults_roll_back(guarded_service):
    service, calls = guarded_service
    initial = create(service, object_graph(service))
    assert [context["operation"] for _, context in calls] == ["initialize", "initialize"]
    changed = update(service, initial, "main", {"count": 1, "artifact_ref": None})
    calls.clear()
    with pytest.raises(ContractValidationError) as stale:
        update(service, changed, "main", {"count": 2, "artifact_ref": None}, expected=1)
    assert stale.value.reason_code == "stale_object_revision" and not calls
    with pytest.raises(ContractValidationError):
        update(service, changed, "main", {"count": 2, "artifact_ref": {"scope": "artifact", "output_id": uid(999)}})
    assert not calls
    bad = object_graph(service, default={"count": 1, "artifact_ref": None})
    bad["workflow_definition_id"] = str(uuid4())
    with pytest.raises(HostContractError) as denied:
        create(service, bad)
    assert denied.value.reason_code == "guard_bad_default"


def test_inherited_copy_and_checkpoint_restore_bypass_transition_hook(guarded_service):
    service, calls = guarded_service
    doc = object_graph(service)
    initial = create(service, doc)
    changed = update(service, initial, "main", {"count": 1, "artifact_ref": None})
    calls.clear()
    sid = str(uuid4())
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        objects = SessionObjectStore(store, service.registry.data_types)
        objects.initialize(sid, doc, inherited=changed["objects"], source_session_id=changed["workflow_session_id"])
        assert objects.current(sid)["main"]["value"] == {"count": 1, "artifact_ref": None}
        objects.restore(sid, doc, objects.references(sid))
        store._connection.execute("ROLLBACK")
    assert not calls


def test_write_resolver_rejects_foreign_session_and_unaccepted_producer(guarded_service):
    service, calls = guarded_service
    doc = object_graph(service)
    first = run(service, create(service, doc))
    second = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    history = service.get_run(first["workflow_session_id"], first["selected_chain_run_id"])
    producer = history["node_runs"][0]
    reference = {"scope": "artifact", "output_id": producer["output_refs"]["output"]}
    with closing(SqliteStore(service.database)) as store:
        objects = SessionObjectStore(store, service.registry.data_types)
        detail = objects._resolve_write_artifact(first["workflow_session_id"], reference)
        detail["config"]["text"] = "mutated"
        assert objects._resolve_write_artifact(first["workflow_session_id"], reference)["config"]["text"] == "main"
        with pytest.raises(ContractValidationError):
            objects._resolve_write_artifact(second["workflow_session_id"], reference)
        original = store._get
        store._get = lambda kind, identity: ({**original(kind, identity), "status": "failed"}
                                            if kind == "node_run" else original(kind, identity))
        with pytest.raises(ContractValidationError):
            objects._resolve_write_artifact(first["workflow_session_id"], reference)


def test_rebind_initializes_only_new_bindings_preserving_populated_and_deleted_heads(guarded_service):
    service, calls = guarded_service
    doc = object_graph(service)
    view = create(service, doc)
    view = update(service, view, "main", {"count": 7, "artifact_ref": None})
    view = update(service, view, "other", None, operation="delete")
    before = deepcopy(view["objects"])
    writer = doc["nodes"][2]["node_binding_id"]
    added = deepcopy(doc)
    added["object_bindings"].append(ObjectBinding(
        "new", TYPE, 1, "shared", readers=(writer,), writers=(writer,)).to_dict())
    calls.clear()
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        objects = SessionObjectStore(store, service.registry.data_types)
        objects.rebind(view["workflow_session_id"], added)
        current = objects.current(view["workflow_session_id"])
        assert current["main"] == before["main"]
        assert current["other"] == before["other"] and current["other"]["deleted"]
        assert current["new"]["value"] == DEFAULT and current["new"]["revision"] == 1
        assert [(context["object_key"], context["operation"]) for _, context in calls] == [("new", "initialize")]
        store._connection.execute("ROLLBACK")
    calls.clear()
    added["object_bindings"][-1]["default_value"] = {"count": 99, "artifact_ref": None}
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        objects = SessionObjectStore(store, service.registry.data_types)
        with pytest.raises(HostContractError) as rejected:
            objects.rebind(view["workflow_session_id"], added)
        assert rejected.value.reason_code == "guard_bad_default"
        store._connection.execute("ROLLBACK")
        assert objects.current(view["workflow_session_id"]) == before
