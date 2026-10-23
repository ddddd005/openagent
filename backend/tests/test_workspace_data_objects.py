"""Workspace data objects: declaration freezing, open handoff, finish saves.

The tests use a local package with a real session data type, an isolated SQLite
store, the real Agent lifecycle and a deterministic offline model transport.
They never start the product service, call a real model or touch a default
database.
"""

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.execution_data import ExecutionState
from phase1_agent.graph_application import GraphApplication
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.workflow_host import WorkflowHost

from test_generic_tool_bindings import replace_entry
from test_graph_service import node
from test_local_package_integration import save_configuration
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import VariableToolTransport
from workflow_test_support import graph


PACKAGE = "integration.workspace-data"
PACKAGE_VERSION = "1.0.0"
PACKAGE_MODULE = "workspace_data"
LOADER = "integration.workspace-data-tools"
WORLD_TYPE = "integration.workspace-data.world"
WORLD_KEY = "workspace/world"
WORLD_INITIAL = {"location": "city_gate", "visits": 0}
MODEL_TEXT = "workspace data model text"
FINISH_TEXT = "exported world"


PACKAGE_CODE = '''
"""Test-only workspace data package: type export and host data handoff."""

from copy import deepcopy

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import object_schema
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.host_sdk import DataTypeDefinition
from phase1_agent.tool_binding_contract import WorkspaceToolBindingAdapter


PACKAGE_ID = "integration.workspace-data"
VERSION = "1.0.0"
LOADER = "integration.workspace-data-tools"
WORLD_TYPE = "integration.workspace-data.world"

RECORDS = {"writes": []}


def world_schema():
    return {"type": "object",
            "properties": {"location": {"type": "string", "maxLength": 4096},
                           "visits": {"type": "integer"}},
            "required": ["location", "visits"],
            "additionalProperties": False}


def record_write(value, context):
    RECORDS["writes"].append({
        "object_key": context.get("object_key"),
        "operation": context.get("operation"),
        "writer_node_id": context.get("writer_node_id"),
        "write_evidence": deepcopy(context.get("write_evidence")),
    })


def normalize(config):
    return [{"name": "world_status", "description": "Report the world state.",
             "parameters": object_schema({}), "config": {}}]


def open(binding, invocation):
    return {"binding": deepcopy(binding)}


def invoke(handle, tool, arguments, invocation):
    return {"ok": True}


def finish(handle, invocation):
    return {"text": "exported world"}


def close(handle):
    return None


def abort(handle):
    return None


def register(host):
    host.register_data_type(DataTypeDefinition(
        WORLD_TYPE, 1, world_schema(), scope="session", checkpoint_admission=True,
        max_bytes=4096, write_validator=record_write))
    host.register_node(NodeDefinition(
        LOADER, "1", "Workspace data tools", "Integration",
        {"agent_node_id": "", "data_objects": []},
        object_schema({
            "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "data_objects": {"type": "array", "maxItems": 64, "items": object_schema({
                "object_key": {"type": "string", "minLength": 1, "maxLength": 128}})},
        }),
        execution_class="non_working",
        config_node_references=({
            "config_field": "agent_node_id", "multiple": False,
            "target_types": [{"component_id": "agents.execute", "component_version": "10"}],
        },),
    ), None, tool_binding=WorkspaceToolBindingAdapter(
        normalize=normalize, open=open, invoke=invoke, finish=finish,
        close=close, abort=abort, data_config_field="data_objects"))


def create_package():
    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, VERSION,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.agents", "1.0.0")),
        exports={
            "nodes": [{"component_id": LOADER, "component_version": "1"}],
            "data_types": [{"scope": "session", "type_id": WORLD_TYPE,
                            "schema_version": 1}],
        },
    ), register)
'''


def write_package(root):
    directory = root / (PACKAGE + "-" + PACKAGE_VERSION)
    module = directory / PACKAGE_MODULE
    module.mkdir(parents=True)
    manifest = {
        "schema_version": 1, "package_id": PACKAGE, "version": PACKAGE_VERSION,
        "host_protocol_version": 1,
        "dependencies": [
            {"package_id": "workflow.content", "version": "1.0.0"},
            {"package_id": "workflow.agents", "version": "1.0.0"},
        ],
        "exports": {
            "nodes": [{"component_id": LOADER, "component_version": "1"}],
            "data_types": [{"scope": "session", "type_id": WORLD_TYPE, "schema_version": 1}],
        },
    }
    (directory / "openagent-package.json").write_text(json.dumps({
        "schema_version": 1,
        "entrypoint": PACKAGE_MODULE + ".package:create_package",
        "manifest": manifest,
    }), encoding="utf-8")
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "package.py").write_text(PACKAGE_CODE, encoding="utf-8")


@contextmanager
def workspace_data_case(tmp_path, *, data_objects=None, include_binding=True,
                        agent_reads=True, agent_writes=True):
    """Real Host, local package copy, one Agent and its declared object keys."""
    root = tmp_path / "packages/workflow"
    write_package(root)
    database = tmp_path / "workspace-data.sqlite"
    transport = VariableToolTransport([
        ("final_answer", {"answer": {"text": MODEL_TEXT}})])
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        application.command("packages.configure", {
            "enabled_packages": {**DEFAULT_PACKAGES, PACKAGE: PACKAGE_VERSION}})
        service._service_options[MODEL_SERVICE_REF]["transport_factory"] = transport.factory
        ModelDatabaseFixture.write(service, 1)
        document = graph(service)
        agent = next(row for row in document["nodes"]
                     if row["component_id"] == "agents.execute")
        other = next(row for row in document["nodes"]
                     if row["node_binding_id"] != agent["node_binding_id"])
        loader = node(
            service.registry, LOADER, 901, component_version="1",
            agent_node_id=agent["node_binding_id"],
            data_objects=(deepcopy(data_objects) if data_objects is not None
                          else [{"object_key": WORLD_KEY}]))
        document["nodes"].append(loader)
        if include_binding:
            document["object_bindings"].append(ObjectBinding(
                WORLD_KEY, WORLD_TYPE, 1, "shared",
                readers=((agent if agent_reads else other)["node_binding_id"],),
                writers=((agent if agent_writes else other)["node_binding_id"],),
                default_value=deepcopy(WORLD_INITIAL)).to_dict())
        document.update(schema_version=3, edit_sequence=0, control_edges=[],
                        package_lock=deepcopy(list(service.registry.execution_package_lock)))
        for row in document["nodes"]:
            row["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        _, saved = save_configuration(application, document)
        revision = saved["result"]["definition_revision"]
        session = application.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": revision,
            "idempotency_key": str(uuid4())})["result"]
        yield SimpleNamespace(
            host=host, service=service, application=application, transport=transport,
            document=document, session=session, agent=agent, loader=loader,
            revision=revision, database=database)


_UNSET = object()


def instrument(case, *, finish_export=_UNSET, finish_error=None,
               data_capability=_UNSET):
    """Count lifecycle stages and optionally craft the finish export."""
    entry = case.service.registry.get(LOADER, "1")
    adapter = entry.tool_binding
    observed = SimpleNamespace(opened=[], finish_calls=0, close_calls=0, abort_calls=0)

    def counted_open(binding, invocation):
        observed.opened.append(deepcopy(binding))
        return adapter.open(binding, invocation)

    def counted_finish(handle, invocation):
        observed.finish_calls += 1
        if finish_error is not None:
            raise finish_error
        if finish_export is _UNSET:
            return adapter.finish(handle, invocation)
        return deepcopy(finish_export)

    def counted_close(handle):
        observed.close_calls += 1
        return adapter.close(handle)

    def counted_abort(handle):
        observed.abort_calls += 1
        return adapter.abort(handle)

    changes = {"open": counted_open, "finish": counted_finish,
               "close": counted_close, "abort": counted_abort}
    if data_capability is not _UNSET:
        changes["data_config_field"] = data_capability
    replace_entry(case, LOADER, tool_binding=replace(adapter, **changes))
    return observed


def start(case):
    current = case.service.get_session(case.session["workflow_session_id"])
    return case.application.command("run.start", {
        "session_id": current["workflow_session_id"],
        "expected_revision": current["revision"],
        "idempotency_key": str(uuid4()),
        "inputs": {"text": "workspace data question"},
    })["result"]["active_chain_run_id"]


def finish(case):
    chain_id = start(case)
    case.service.wait(chain_id)
    return chain_id, case.service.get_session(case.session["workflow_session_id"])


def chain_of(session, chain_id):
    return next(row for row in session["chains"] if row["chain_run_id"] == chain_id)


def node_of(session, node_id):
    return next(row for row in session["nodes"] if row["node_binding_id"] == node_id)


def agent_result_text(session, agent):
    return node_of(session, agent["node_binding_id"])["outputs"]["result"]["text"]


def write_records(case):
    return case.service.registry.get(LOADER, "1").tool_binding.normalize.__globals__[
        "RECORDS"]["writes"]


def tool_object_write_rows(case):
    with closing(case.service._store()) as store:
        return [dict(row) for row in store._connection.execute(
            "SELECT operation,key FROM idempotency "
            "WHERE operation='graph.tool.object-write'").fetchall()]


def object_receipt_keys(case, session_id):
    with closing(case.service._store()) as store:
        return [row["operation_key"] for row in store._connection.execute(
            "SELECT operation_key FROM session_object_receipts "
            "WHERE session_id=? AND operation_key LIKE 'workspace:%' "
            "ORDER BY operation_key", (session_id,)).fetchall()]


def submitted_save(key=WORLD_KEY, value=None, expected_revision=1):
    return {"object_key": key,
            "value": deepcopy(WORLD_INITIAL if value is None else value),
            "expected_revision": expected_revision}


def test_open_receives_declared_data_snapshot_and_revision(tmp_path):
    """(b) open carries the exact value, revision and type of each declared object."""
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert observed.finish_calls == 1 and observed.close_calls == 1
        assert len(observed.opened) == 1
        binding = observed.opened[0]
        assert set(binding) == {"binding_id", "agent_node_id", "config", "tools", "data"}
        data = binding["data"]
        assert set(data) == {WORLD_KEY}
        item = data[WORLD_KEY]
        assert set(item) == {"value", "revision", "revision_id", "type_id", "schema_version"}
        current = session["objects"][WORLD_KEY]
        assert item["value"] == WORLD_INITIAL == current["value"]
        assert item["revision"] == current["revision"] == 1
        assert item["revision_id"] == current["revision_id"]
        assert (item["type_id"], item["schema_version"]) == (WORLD_TYPE, 1)
        assert agent_result_text(session, case.agent) == MODEL_TEXT + "\n\n" + FINISH_TEXT
        assert len(case.transport.calls) == 1


def test_open_data_read_failure_reports_the_object_key(tmp_path, monkeypatch):
    """(b') A failed declared-object read fails open before the model loop."""
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case)
        original = ExecutionState.read_object

        def failing(self, sid, object_key, *, node_id, revision_id=None):
            if object_key == WORLD_KEY:
                raise RuntimeError("injected data read failure")
            return original(self, sid, object_key, node_id=node_id,
                            revision_id=revision_id)

        monkeypatch.setattr(ExecutionState, "read_object", failing)
        chain_id = start(case)
        case.service.wait(chain_id)
        session = case.service.get_session(case.session["workflow_session_id"])
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_open_failed", diagnostic
        assert WORLD_KEY in diagnostic["message"]
        assert observed.opened == []
        assert observed.abort_calls == 0
        assert case.transport.calls == []


def test_finish_saves_commit_reread_and_never_leak_into_outputs(tmp_path):
    """(c) saves apply in one observed transaction, are reread, and stay internal."""
    saved = {"location": "market_square", "visits": 2}
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case, finish_export={
            "text": FINISH_TEXT,
            "saves": [{"object_key": WORLD_KEY, "value": saved, "expected_revision": 1}]})
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert observed.finish_calls == 1 and observed.close_calls == 1
        assert observed.abort_calls == 0
        current = session["objects"][WORLD_KEY]
        assert current["value"] == saved and current["revision"] == 2
        assert agent_result_text(session, case.agent) == MODEL_TEXT + "\n\n" + FINISH_TEXT
        outputs = node_of(session, case.agent["node_binding_id"])["outputs"]
        assert set(outputs) == {"result", "receipts"}
        assert outputs["result"]["kind"] == "workflow.text"
        assert outputs["result"]["schema_version"] == 2
        # The write landed through the exact independent finish transaction.
        rows = tool_object_write_rows(case)
        assert len(rows) == 1
        assert rows[0]["key"].startswith("workspace:") and rows[0]["key"].endswith(":finish")
        receipts = object_receipt_keys(case, session["workflow_session_id"])
        assert len(receipts) == 1
        assert receipts[0].startswith("workspace:") and ":object:" in receipts[0]
        writes = [row for row in write_records(case) if row["operation"] == "put"]
        assert len(writes) == 1
        evidence = writes[0]["write_evidence"]
        assert set(evidence) == {"tool_binding_id", "agent_node_id", "chain_run_id",
                                 "node_run_id", "stage"}
        assert evidence["agent_node_id"] == case.agent["node_binding_id"]
        assert evidence["stage"] == "workspace-finish"
        assert evidence["chain_run_id"] == chain_id
        assert writes[0]["writer_node_id"] == case.agent["node_binding_id"]
        assert len(case.transport.calls) == 1


def test_finish_with_only_saves_composes_the_model_text(tmp_path):
    """(c') A save-only finish keeps the export at null and saves the object."""
    saved = {"location": "market_square", "visits": 1}
    with workspace_data_case(tmp_path) as case:
        instrument(case, finish_export={"saves": [
            {"object_key": WORLD_KEY, "value": saved, "expected_revision": 1}]})
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert session["objects"][WORLD_KEY]["value"] == saved
        assert session["objects"][WORLD_KEY]["revision"] == 2
        assert agent_result_text(session, case.agent) == MODEL_TEXT


@pytest.mark.parametrize("label,export", [
    ("unbound", {"saves": [submitted_save(key="workspace/absent")]}),
    ("duplicate", {"saves": [submitted_save(), submitted_save()]}),
    ("non-json", {"saves": [submitted_save(value={"location": object(), "visits": 1})]}),
    ("stale-basis", {"saves": [submitted_save(expected_revision=2)]}),
    ("too-large", {"saves": [submitted_save(value={"location": "x" * 5000, "visits": 1})]}),
    ("bad-shape", {"saves": [{"object_key": WORLD_KEY, "value": WORLD_INITIAL}]}),
], ids=["unbound", "duplicate", "non-json", "stale-basis", "too-large", "bad-shape"])
def test_finish_rejects_invalid_saves_before_any_write(tmp_path, label, export):
    """(d) Invalid save intent fails explicitly with no save transaction or retry."""
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case, finish_export=export)
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_save_invalid", diagnostic
        assert observed.finish_calls == 1 and observed.close_calls == 0
        assert observed.abort_calls == 1
        current = session["objects"][WORLD_KEY]
        assert current["value"] == WORLD_INITIAL and current["revision"] == 1
        assert tool_object_write_rows(case) == []
        assert object_receipt_keys(case, session["workflow_session_id"]) == []
        assert len(case.transport.calls) == 1


def test_finish_save_requires_the_agent_to_be_a_writer(tmp_path):
    """(d) A declared object that omits the Agent from writers cannot be saved."""
    with workspace_data_case(tmp_path, agent_writes=False) as case:
        observed = instrument(case, finish_export={"saves": [submitted_save()]})
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        assert chain_of(session, chain_id)["diagnostic"]["code"] == "workspace_save_invalid"
        assert observed.finish_calls == 1
        assert session["objects"][WORLD_KEY]["revision"] == 1
        assert tool_object_write_rows(case) == []


def test_post_commit_reread_mismatch_reports_save_mismatch_and_keeps_save(
        tmp_path, monkeypatch):
    """(e) A changed reread fails the run but the committed save stays a fact."""
    saved = {"location": "market_square", "visits": 1}
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case, finish_export={"saves": [
            {"object_key": WORLD_KEY, "value": saved, "expected_revision": 1}]})
        original = ExecutionState.read_object
        fired = []

        def tampered(self, sid, object_key, *, node_id, revision_id=None):
            record = original(self, sid, object_key, node_id=node_id,
                              revision_id=revision_id)
            if object_key == WORLD_KEY:
                count = self._store._connection.execute(
                    "SELECT COUNT(*) FROM idempotency "
                    "WHERE operation='graph.tool.object-write'").fetchone()[0]
                if count > 0:
                    fired.append(True)
                    changed = deepcopy(record)
                    changed["value"]["location"] = "tampered"
                    return changed
            return record

        monkeypatch.setattr(ExecutionState, "read_object", tampered)
        chain_id, session = finish(case)
        assert fired == [True]
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_save_mismatch", diagnostic
        assert observed.finish_calls == 1 and observed.close_calls == 0
        current = session["objects"][WORLD_KEY]
        assert current["value"] == saved and current["revision"] == 2
        rows = tool_object_write_rows(case)
        assert len(rows) == 1 and rows[0]["key"].endswith(":finish")
        assert len(object_receipt_keys(case, session["workflow_session_id"])) == 1
        assert len(case.transport.calls) == 1


def test_preparation_rejects_an_unbound_object_key(tmp_path):
    """(a) A declared object key must exist in the document's object bindings."""
    with workspace_data_case(tmp_path, data_objects=[
            {"object_key": "workspace/absent"}]) as case:
        with pytest.raises(ContractValidationError) as caught:
            start(case)
        assert caught.value.reason_code == "tool_binding_object_missing"
        assert case.transport.calls == []


def test_preparation_rejects_an_agent_that_is_not_a_reader(tmp_path):
    """(a) The Agent must be an explicit reader; otherwise preparation fails 403."""
    with workspace_data_case(tmp_path, agent_reads=False) as case:
        with pytest.raises(ContractValidationError) as caught:
            start(case)
        assert caught.value.reason_code == "tool_binding_access_denied"
        assert caught.value.status_code == 403
        assert case.transport.calls == []


def test_preparation_requires_a_registered_object_type():
    """(a) Declaration freezing requires the object type in the session registry."""
    from phase1_agent import tool_bindings

    adapter = SimpleNamespace(data_config_field="data_objects")
    types = SimpleNamespace(get=lambda type_id, schema_version, *, scope=None: None)
    registry = SimpleNamespace(data_types=types)
    bindings = {WORLD_KEY: {"type_id": WORLD_TYPE, "schema_version": 1,
                            "readers": [str(uuid4())], "writers": []}}
    with pytest.raises(ContractValidationError) as caught:
        tool_bindings._prepare_workspace_data_objects(
            adapter, {"data_objects": [{"object_key": WORLD_KEY}]},
            agent_node_id=bindings[WORLD_KEY]["readers"][0],
            document_bindings=bindings, registry=registry)
    assert caught.value.reason_code == "tool_binding_object_type_unavailable"
    assert caught.value.status_code == 409


def test_preparation_rejects_a_repeated_object_key(tmp_path):
    """(a) One binding may declare each object at most once."""
    with workspace_data_case(tmp_path, data_objects=[
            {"object_key": WORLD_KEY}, {"object_key": WORLD_KEY}]) as case:
        with pytest.raises(ContractValidationError) as caught:
            start(case)
        assert caught.value.reason_code == "tool_binding_invalid"
        assert case.transport.calls == []


def test_preparation_rejects_more_than_eight_objects(tmp_path):
    """(a) One workspace binding is bounded to eight declared objects."""
    with workspace_data_case(tmp_path, data_objects=[
            {"object_key": WORLD_KEY} for _ in range(9)]) as case:
        with pytest.raises(ContractValidationError) as caught:
            start(case)
        assert caught.value.reason_code == "tool_binding_invalid"
        assert case.transport.calls == []


def test_legacy_adapter_without_data_capability_keeps_its_exact_behavior(tmp_path):
    """(f) An adapter with data_config_field None keeps the pre-S4 open/finish shape."""
    with workspace_data_case(tmp_path) as case:
        observed = instrument(case, data_capability=None)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert len(observed.opened) == 1
        assert set(observed.opened[0]) == {"binding_id", "agent_node_id", "config", "tools"}
        assert agent_result_text(session, case.agent) == MODEL_TEXT + "\n\n" + FINISH_TEXT
        assert session["objects"][WORLD_KEY]["revision"] == 1

    # The old export shape still rejects saves for a non-data adapter.
    with workspace_data_case(tmp_path / "saves") as case:
        observed = instrument(case, data_capability=None,
                              finish_export={"text": FINISH_TEXT, "saves": []})
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_export_invalid", diagnostic
        assert observed.finish_calls == 1
        assert session["objects"][WORLD_KEY]["revision"] == 1
        assert tool_object_write_rows(case) == []
