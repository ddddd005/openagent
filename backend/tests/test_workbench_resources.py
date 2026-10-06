"""Unified workbench resources, independent session copies and exact run outputs."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4
from threading import Event

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.preparation_program import execute_preparation_program, validate_program_result
from phase1_agent.prepared_context import validate_frozen_preparation
from phase1_agent.program_variable_store import ProgramVariableStore
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService
from phase1_agent.workbench_resources import CORE_SESSION_NOTE

from resource_fixtures import resource
from test_preparation_program import node, program, root, view
from test_server_prompt_configs import request, running_server
from test_workflow_preparation_program import draft, fence, publish, current_state
from test_workflow_prepared_context import factory, snapshots
from test_workflow_prompt_selection import choice


def test_migration_11_keeps_existing_session_and_program_state(tmp_path):
    path = tmp_path / "migration.sqlite"
    with closing(WorkflowService(path)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.write_session_data(sid, definition_id=CORE_SESSION_NOTE["definition_id"], revision=1,
            value="preserved", expected_revision=0, expected_session_revision=1, idempotency_key=str(uuid4()))
    with closing(SqliteStore(path)) as store:
        original = store.read_bundle()
        state = ProgramVariableStore(store).current(sid)
        for table in ("workbench_resource_receipts", "workbench_resource_heads",
                      "workbench_resource_revisions", "workbench_session_owners"):
            store._connection.execute(f"DROP TABLE {table}")
        store._connection.execute("PRAGMA user_version = 11")
    with closing(WorkflowService(path)) as service, closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert store.read_bundle() == original
        assert ProgramVariableStore(store).current(sid) == state
        assert service.get_session(sid)["revision"] == 1
        assert service.read_session_data(sid, definition_id=CORE_SESSION_NOTE["definition_id"], revision=1)["value"] == "preserved"

def test_public_agent_declaration_must_match_the_session_workflow(tmp_path):
    from test_exposure_configuration import configuration
    with closing(WorkflowService(tmp_path / "owners.sqlite")) as service:
        source = service.create_session()["workflow_session_id"]
        owner = str(uuid4())
        child = service.create_session(workflow_id=owner)["workflow_session_id"]
        declaration = configuration()
        service.save_exposure_configuration(record=declaration, expected_revision=0, idempotency_key="source")
        with pytest.raises(ContractValidationError) as denied:
            service.read_exposures(child, declaration["config_id"], 1)
        assert denied.value.reason_code == "ownership_mismatch"
        with pytest.raises(ContractValidationError):
            service.read_node_outputs(child, node_id=A_BINDING,
                exposure_configuration={"config_id": declaration["config_id"], "revision": 1})
        copied = deepcopy(declaration)
        copied["config_id"] = str(uuid4())
        copied["workflow_id"] = owner
        for row in copied["registrations"]:
            row["workflowId"] = owner
        service.save_exposure_configuration(record=copied, expected_revision=0, idempotency_key="child")
        assert service.read_exposures(child, copied["config_id"], 1)["workflow_session_id"] == child
        assert service.read_exposures(source, declaration["config_id"], 1)["workflow_session_id"] == source


def test_resource_http_cas_revision_receipt_delete_and_missing(tmp_path):
    with closing(WorkflowService(tmp_path / "resources.sqlite")) as service, running_server(service) as port:
        record = resource()
        command = {"record": record, "expected_revision": 0, "idempotency_key": str(uuid4())}
        assert request(port, "POST", "/api/global-content", command) == (201, record)
        assert request(port, "POST", "/api/global-content", command) == (201, record)
        assert request(port, "GET", "/api/global-content")[1] == [record]
        changed = deepcopy(command)
        changed["record"]["name"] = "Different"
        assert request(port, "POST", "/api/global-content", changed)[0] == 409
        assert request(port, "POST", "/api/global-content/resolve", {"resource_ids": [{}]})[0] == 400
        assert request(port, "POST", f"/api/global-content/{record['resource_id']}/delete",
                       {"expected_revision": 1})[0] == 200
        status, failed = request(port, "POST", "/api/global-content/resolve",
                                 {"resource_ids": [record["resource_id"]]})
        assert status == 404 and failed["error"]["reason_code"] == "global_content_missing"


def test_session_data_defaults_schema_permissions_isolation_and_copy_empty(tmp_path):
    with closing(WorkflowService(tmp_path / "data.sqlite")) as service:
        sid = service.create_session()["workflow_session_id"]
        note = CORE_SESSION_NOTE
        assert service.read_session_data(sid, definition_id=note["definition_id"], revision=1)["value"] == ""
        command = dict(definition_id=note["definition_id"], revision=1, value="live data",
                       expected_revision=0, expected_session_revision=1, idempotency_key=str(uuid4()))
        written = service.write_session_data(sid, **command)
        assert service.write_session_data(sid, **command) == written
        copy_command = dict(expected_source_revision=1, expected_data_revision=1, idempotency_key=str(uuid4()))
        copied = service.copy_workbench_session(sid, **copy_command)
        child = copied["workflow_session_id"]
        assert service.copy_workbench_session(sid, **copy_command)["workflow_session_id"] == child
        assert service.read_session_data(child, definition_id=note["definition_id"], revision=1)["value"] == "live data"
        service.write_session_data(child, **{**command, "value": "child",
            "expected_revision": 1, "idempotency_key": str(uuid4())})
        assert service.read_session_data(sid, definition_id=note["definition_id"], revision=1)["value"] == "live data"
        with pytest.raises(ContractValidationError):
            service.write_session_data(sid, **{**command, "value": 5,
                "expected_revision": 1, "idempotency_key": str(uuid4())})
        definition = {**deepcopy(note), "definition_id": str(uuid4()), "key": "test:readonly",
                      "public": False, "writable": False}
        service.register_session_data(definition)
        with pytest.raises(ContractValidationError) as denied:
            service.read_session_data(sid, definition_id=definition["definition_id"], revision=1, public=True)
        assert denied.value.status_code == 403


def test_output_declarations_current_chain_and_explicit_historical_run(tmp_path):
    path = tmp_path / "outputs.sqlite"
    with closing(WorkflowService(path, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        graph = draft()
        source = node("public-text", "text", {"text": "public"})
        source["public_outputs"] = ["text"]
        graph["config"]["preparation"] = program([source, node("private", "text", {"text": "internal"})])
        publish(service, graph)
        first = service.submit(sid, "first", "first", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        reading = service.read_node_outputs(sid, node_id="public-text")
        assert reading["outputs"] == {"text": {"kind": "text", "text": "public"}}
        with pytest.raises(ContractValidationError) as denied:
            service.read_node_outputs(sid, node_id="private")
        assert denied.value.status_code == 403
        assert service.read_node_outputs(sid, node_id="private", public=False)["outputs"]["text"]["text"] == "internal"
        with running_server(service) as port:
            status, public = request(port, "GET", f"/api/sessions/{sid}/outputs/public")
            assert status == 200
            assert public["session_revision"] == service.get_session(sid)["revision"]
            assert [entry["node_id"] for entry in public["nodes"]] == ["public-text"]
        old_run = reading["run_id"]
        newer = deepcopy(graph["config"])
        newer["revision"] = 2
        newer["preparation"] = program([node("other", "text", {"text": "new"})])
        service.save_prompt_config("config", newer, expected_revision=1, idempotency_key="other")
        selected = choice("A", "B")
        for reference in selected["nodes"].values():
            reference["revision"] = 2
        service.submit(sid, "second", "second", prompt_selection=selected)
        service.wait_for_idle(sid)
        assert service.read_node_outputs(sid, node_id="public-text")["status"] == "unproduced"
        assert service.list_public_node_outputs(sid)["nodes"] == []
        assert service.read_node_outputs(sid, node_id="public-text", run_id=old_run)["outputs"] == reading["outputs"]


def test_global_three_rounds_latest_new_run_frozen_history_reopen(tmp_path):
    path = tmp_path / "global-runs.sqlite"
    calls = []
    record = resource()
    sid = None
    for round_number in range(1, 4):
        with closing(WorkflowService(path, model_factory=factory(calls))) as service:
            if sid is None:
                sid = service.create_session()["workflow_session_id"]
                graph = draft()
                graph["config"]["preparation"] = program([
                    node("global", "global-source", {"resource_id": record["resource_id"]}),
                ], prompt="global")
                publish(service, graph)
            record["revision"] = round_number
            record["members"][0]["text"] = f"global v{round_number}"
            service.save_global_content(record, expected_revision=round_number - 1, idempotency_key=str(uuid4()))
            service.submit(sid, f"root {round_number}", f"global:{round_number}", prompt_selection=choice("A", "B"))
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
            evidence = [validate_frozen_preparation(snapshot) for snapshot in snapshots(path)]
            assert [item["collection"]["items"][0]["text"] for item in evidence] == [
                f"global v{number}" for number in range(1, round_number + 1) for _ in range(2)
            ]
            assert all(item["program"]["program"]["nodes"][0]["config"]["record"]["revision"]
                       == (index // 2) + 1 for index, item in enumerate(evidence))
    assert len(calls) == 6


def test_copy_completed_inherits_live_data_not_commit_and_stays_isolated(tmp_path):
    path = tmp_path / "copy-completed.sqlite"
    with closing(WorkflowService(path, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        service.submit(sid, "first", "first", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        note = CORE_SESSION_NOTE
        state = current_state(path, sid)
        service.write_session_data(sid, definition_id=note["definition_id"], revision=1, value="after commit",
            expected_revision=state["revision"], expected_session_revision=service.get_session(sid)["revision"],
            idempotency_key=str(uuid4()))
        copy_command = dict(expected_source_revision=service.get_session(sid)["revision"],
                            expected_data_revision=current_state(path, sid)["revision"], idempotency_key=str(uuid4()))
        child = service.copy_workbench_session(sid, **copy_command)["workflow_session_id"]
        assert current_state(path, child)["data"][note["key"]]["value"] == "after commit"
        assert current_state(path, child)["values"] == current_state(path, sid)["values"]
        service.submit(sid, "parent later", "later", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        assert len(service.get_session(child)["messages"]) == 2
        assert service.copy_workbench_session(sid, **copy_command)["workflow_session_id"] == child


def test_cross_agent_source_actual_execution_protection_and_preview(tmp_path):
    path = tmp_path / "context-sources.sqlite"
    with closing(WorkflowService(path, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        graph = draft()
        graph["config"]["preparation"] = program([
            node("source", "context-source", {"binding_id": A_BINDING}),
            node("regex", "regex", {"mode": "prompt", "rule": {
                "pattern": "first", "replacement": "changed", "flags": "", "mode": "all",
            }}, input="source"),
        ], context="regex")
        publish(service, graph)
        for number in (1, 2):
            service.submit(sid, "first" if number == 1 else "second", f"context:{number}", prompt_selection=choice("A", "B"))
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
        latest = snapshots(path)[-1]
        evidence = validate_frozen_preparation(latest)
        assert latest["node_binding_id"] == B_BINDING
        source = evidence["config"]["context_sources"][A_BINDING]
        assert evidence["canonical_messages"][:-1] == source["messages"]
        assert evidence["protected_blocks"] == source["protected_blocks"]
        assert source["scope"]["node_binding_id"] == A_BINDING
        assert any("changed" in override["text"] for override in evidence["send_view"]["overrides"])
        preview = service.preview_node_context(sid, A_BINDING, **fence(service, sid), prompt_config=graph, text="preview")
        assert preview["status"] == "ready"


def test_registered_json_read_write_conversion_and_frozen_validation():
    from phase1_agent.prompt_regex import RegexLimits
    from test_workflow_prepared_context import collection
    processing = program([
        node("write", "session-data-write", {"definition": CORE_SESSION_NOTE, "value": "hello"}),
        node("read", "session-data-read", {"definition": CORE_SESSION_NOTE}),
        node("convert", "json-to-text", {}, input="read"),
    ])
    basis = {"revision": 0, "values": {}}
    result = execute_preparation_program(processing, collection(), view(), basis,
                                        limits=RegexLimits(), node_input=root())
    assert result["stages"][-1]["output"] == {"kind": "text", "text": "hello"}
    assert validate_program_result(result, collection(), view(), basis, processing, node_input=root()) == result


def test_canvas_output_ports_and_tool_projection_do_not_depend_on_enabled_item_offsets():
    from phase1_agent.preparation_program import project_node_outputs, validate_preparation_program
    converted = node("convert", "text-to-prompt", {
        "item_instance_id": str(uuid4()), "role": "system", "placement": "before",
        "depth": None, "order": 0,
    }, input="text")
    converted.update(output_ports=["prompt"], public_outputs=["prompt"])
    assert validate_preparation_program(program([node("text", "text", {"text": "hello"}), converted]))
    converted["public_outputs"] = ["text"]
    with pytest.raises(ContractValidationError):
        validate_preparation_program(program([node("text", "text", {"text": "hello"}), converted]))
    description, schema = str(uuid4()), str(uuid4())
    tool = node("tool", "prompt-source", {"instances": [
        {"group_instance_id": None, "item_instance_id": identity} for identity in (description, schema)
    ]})
    tool["output_ports"] = ["tool-descriptions", "tool-schemas"]
    material = {"kind": "prompt", "items": [{"item_instance_id": schema, "text": "{}"}], "view": None}
    outputs = project_node_outputs(tool, material)
    assert outputs["tool-descriptions"]["items"] == []
    assert outputs["tool-schemas"]["items"] == material["items"]


def test_active_global_update_is_frozen_copy_rejected_and_owner_isolation(tmp_path):
    entered, release = Event(), Event()
    path = tmp_path / "active-global.sqlite"
    with closing(WorkflowService(path, model_factory=factory([], entered=entered, release=release))) as service:
        source = service.create_session()["workflow_session_id"]
        record = resource()
        graph = draft()
        graph["config"]["preparation"] = program([
            node("global", "global-source", {"resource_id": record["resource_id"]}),
        ], prompt="global")
        publish(service, graph)
        service.save_global_content(record, expected_revision=0, idempotency_key=str(uuid4()))
        copy_key = dict(expected_source_revision=1, expected_data_revision=0,
                        idempotency_key=str(uuid4()), target_workflow_id=str(uuid4()))
        child = service.copy_workbench_session(source, **copy_key)["workflow_session_id"]
        service.submit(source, "blocked", "active", prompt_selection=choice("A", "B"))
        assert entered.wait(5)
        try:
            assert service.copy_workbench_session(source, **copy_key)["workflow_session_id"] == child
            with pytest.raises(ContractValidationError):
                service.copy_workbench_session(source,
                    expected_source_revision=service.get_session(source)["revision"],
                    expected_data_revision=current_state(path, source)["revision"],
                    idempotency_key=str(uuid4()))
            latest = deepcopy(record)
            latest["revision"] = 2
            latest["members"][0]["text"] = "global v2"
            service.save_global_content(latest, expected_revision=1, idempotency_key=str(uuid4()))
        finally:
            release.set()
        service.wait_for_idle(source)
        assert service.get_session(source)["error"] is None
        assert all(validate_frozen_preparation(snapshot)["collection"]["items"][0]["text"] == "global v1"
                   for snapshot in snapshots(path))
        owners = {row["workflow_session_id"]: row.get("workflow_id", "frontend:main-test")
                  for row in service.list_sessions()}
        assert owners[child] == copy_key["target_workflow_id"]
        assert owners[source] == "frontend:main-test"


def test_private_session_data_publication_and_missing_global_input_diagnostics(tmp_path):
    with closing(WorkflowService(tmp_path / "diagnostics.sqlite")) as service:
        sid = service.create_session()["workflow_session_id"]
        private = {**deepcopy(CORE_SESSION_NOTE), "definition_id": str(uuid4()),
                   "key": "test:private", "public": False}
        service.register_session_data(private)
        graph = draft()
        reading = node("private", "session-data-read", {"definition": private})
        reading["public_outputs"] = ["json"]
        graph["config"]["preparation"] = program([reading])
        publish(service, graph)
        with pytest.raises(ContractValidationError):
            service.submit(sid, "private", "private", prompt_selection=choice("A", "B"))
        changed = deepcopy(graph["config"])
        changed["revision"] = 2
        changed["preparation"] = program([node("global", "global-source", {"resource_id": str(uuid4())})], prompt="global")
        service.save_prompt_config("config", changed, expected_revision=1, idempotency_key="missing")
        selected = choice("A", "B")
        for ref in selected["nodes"].values():
            ref["revision"] = 2
        with running_server(service) as port:
            status, failure = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "missing", "idempotency_key": "missing", "prompt_selection": selected,
            })
        assert status == 404
        assert failure["error"]["reason_code"] == "global_content_missing"
        assert service.get_session(sid)["messages"] == []
