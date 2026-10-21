"""Retired frontend-node receipts remain exact without restoring node authority."""

from contextlib import closing
from copy import deepcopy
from threading import Event, Lock
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.frontend_node_connections import FrontendNodeConnections
from phase1_agent.frontend_node_evidence import (
    FRONTEND_NODE_EVIDENCE_OPERATIONS, frontend_node_evidence_owner,
)
from phase1_agent.frontend_node_http import FrontendNodeHTTP
from phase1_agent.frontend_node_operations import BoundFrontendNodeOperations
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from phase1_agent.request_evidence import RequestEvidenceService
from frontend_node_test_support import (
    create as create_chat_session, graph as chat_graph, save as save_chat_graph,
)
from test_graph_service import document, edge, node, run as graph_run
from test_success_checkpoints import checkpoint_history


def uid():
    return str(uuid4())


def owner_for(graph, frontend, session_id):
    return {
        "workflow_definition_id": graph["workflow_definition_id"],
        "definition_revision": graph["revision"],
        "node_binding_id": frontend["node_binding_id"],
        "session_id": session_id,
    }


def packet(owner, operation, parameters=None):
    return {"owner": deepcopy(owner), "operation": operation,
            "parameters": deepcopy(parameters or {})}


def receipt_packet(owner, operation, original_owner, parameters, *, accept=False):
    return packet(owner, "accept_receipt" if accept else "receipt", {
        "operation": operation,
        "original_request": {
            "owner": deepcopy(original_owner),
            "parameters": deepcopy(parameters),
        },
    })


def database_snapshot(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        schema = tuple(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name",
        ))
        tables = {}
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute("SELECT * FROM " + quoted)
            tables[name] = (
                tuple(column[0] for column in cursor.description),
                tuple(sorted((tuple(row) for row in cursor.fetchall()), key=repr)),
            )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        return version, schema, tables


def evidence_for(path, operation, request):
    private = FRONTEND_NODE_EVIDENCE_OPERATIONS[operation]
    owner_kind, owner_id = frontend_node_evidence_owner(private, request)
    return RequestEvidenceService(path).read_evidence(owner_kind, owner_id)


def assert_only_resolution_changed(before, after, operation, key):
    before_tables, after_tables = before[2], after[2]
    assert before[:2] == after[:2]
    assert before_tables.keys() == after_tables.keys()

    for name in before_tables:
        old_columns, old_rows = before_tables[name]
        new_columns, new_rows = after_tables[name]
        assert old_columns == new_columns
        if name == "pending_commands":
            operation_index = old_columns.index("operation")
            key_index = old_columns.index("idempotency_key")
            resolution_index = old_columns.index("resolution")
            expected = []
            matches = 0
            for row in old_rows:
                row = list(row)
                if row[operation_index] == "frontend-node." + operation.replace("_", "-") \
                        and row[key_index] == key:
                    row[resolution_index] = "verified"
                    matches += 1
                expected.append(tuple(row))
            assert matches == 1
            assert tuple(sorted(expected, key=repr)) == new_rows
        elif name == "idempotency":
            operation_index = old_columns.index("operation")
            evidence_writes = {"graph.request-evidence.resolve", "graph.application.command-identity.v1"}
            old_native = tuple(row for row in old_rows if row[operation_index] not in evidence_writes)
            new_native = tuple(row for row in new_rows if row[operation_index] not in evidence_writes)
            assert old_native == new_native
            assert any(row[operation_index] == "graph.request-evidence.resolve" for row in new_rows)
        else:
            assert old_rows == new_rows


def install_frozen_v1_fixture(service):
    """Install the 037be78 @1 declaration only in this test's live registry."""
    key = ("frontend.node", "1")
    service.registry._nodes.pop(key, None)
    if service.registry.data_types.get("workflow.frontend-state", 1, scope="session") is None:
        service.registry.data_types.register(DataTypeDefinition(
            "workflow.frontend-state", 1, {"type": "object"}, {}))
    if service.registry.data_types.get("FRONTEND_NODE_APPEND", 1, scope="content") is None:
        service.registry.data_types.register(DataTypeDefinition(
            "FRONTEND_NODE_APPEND", 1, {"type": "object"}, scope="content"))
    definition = NodeDefinition(
        "frontend.node", "1", "前端节点", "Frontend",
        {"object_key": "frontend", "role": "assistant", "input_node_id": ""},
        {
            "type": "object",
            "properties": {
                "object_key": {"type": "string", "minLength": 1, "maxLength": 128},
                "role": {"enum": ["user", "assistant", "system"]},
                "input_node_id": {
                    "type": "string", "maxLength": 36,
                    "pattern": "^(?:|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
                },
            },
            "required": ["object_key", "role", "input_node_id"],
            "additionalProperties": False,
        },
        inputs=(NodePort("content", "TEXT", data_schema_version=2, multiple=True),),
        outputs=(NodePort("proof", "FRONTEND_NODE_APPEND"),
                 NodePort("commit", "FRONTEND_COMMIT")),
        is_output=True, capabilities=("objects:read", "objects:write", "artifacts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "multiple": False, "access": "read_write",
                          "type_id": "workflow.frontend-state", "schema_version": 1},),
        config_node_references=({
            "config_field": "input_node_id", "multiple": False, "allow_empty": True,
            "target_types": [{"component_id": "tools.editable-input", "component_version": "1"}],
        },),
        provided_session_item_types=({"type_id": "workflow.frontend-state", "schema_version": 1},),
    )

    def execute(config, inputs, context):
        return {"proof": {
            "schema_version": 1, "kind": "frozen-v1-test-output",
            "object_key": config["object_key"],
        }}

    def settle(context, outputs, output_refs):
        proof = outputs["proof"]
        key = proof["object_key"]
        current = context.object_read(key)
        operation_key = "frozen-v1-test-write:" + key
        context.object_write(
            key, {"fixture": True}, expected_revision=current["revision"],
            operation_key=operation_key,
        )

        def finalize(staged, refs, receipts):
            assert len(receipts) == 1
            return {
                "proof": deepcopy(proof),
                "commit": {
                    "schema_version": 1, "kind": "workflow.frontend-commit",
                    "owner": {"workflow_session_id": context.workflow_session_id,
                              "object_key": key},
                    "view_ref": {"scope": "artifact", "output_id": refs["proof"]},
                    "operation_key": operation_key,
                    "receipt": deepcopy(receipts[0]),
                },
            }

        return {
            "outputs": {"proof": deepcopy(proof)},
            "object_writes": deepcopy(context.object_writes),
            "finalize": finalize,
        }

    service.registry.register(
        definition,
        execute,
        settlement_builder=settle,
    )


def frozen_v1_graph(service, *, count=1, bound=True, readers=True, writers=True):
    messages = [
        node(service.registry, "tools.editable-input", 19001 + index, text="message " + str(index))
        for index in range(count)
    ]
    frontends = [
        node(service.registry, "frontend.node", 19011 + index, component_version="1",
             object_key="legacy-" + str(index),
             input_node_id=messages[index]["node_binding_id"] if bound else "")
        for index in range(count)
    ]
    graph = document([*messages, *frontends], [
        edge(message, frontend, 19001 + index, target_port="content")
        for index, (message, frontend) in enumerate(zip(messages, frontends))
    ])
    graph.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        object_bindings=[
            ObjectBinding(
                entry["config"]["object_key"], "workflow.frontend-state", 1, "shared",
                readers=(entry["node_binding_id"],) if readers else (),
                writers=(entry["node_binding_id"],) if writers else (),
                registrar_node_id=entry["node_binding_id"],
            ).to_dict()
            for entry in frontends
        ],
    )
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph, messages, frontends


def save_graph(service, graph):
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=uid(),
    )
    assert saved["outcome"] == "saved"
    return saved["document"]


def allow_test_fixture_binding(monkeypatch, fixture_service):
    original = BoundFrontendNodeOperations._require_binding

    def require_binding(self, access, *, current=False):
        if self._service is not fixture_service:
            return original(self, access, current=current)
        data = self._binding_data(access)
        document = data.fixed_document(
            self.workflow_definition_id, self.definition_revision)
        node = next((item for item in document["nodes"]
                     if item["node_binding_id"] == self.node_binding_id), None)
        assert node is not None
        if (node["component_id"], node["component_version"]) != ("frontend.node", "1"):
            return original(self, access, current=current)
        entry = self._service.registry.get("frontend.node", "1")
        assert entry is not None
        self._service.registry.validate_config(entry, node["config"])
        binding = next(item for item in document["object_bindings"]
                       if item["object_key"] == node["config"]["object_key"])
        assert binding["type_id"] == "workflow.frontend-state"
        assert binding["schema_version"] == 1
        assert binding["registrar_node_id"] == self.node_binding_id
        assert self.node_binding_id in binding["readers"]
        assert self.node_binding_id in binding["writers"]
        if current:
            configuration = data.current_configuration(self.workflow_definition_id)
            assert configuration is not None
            assert configuration["fixed_definition_revision"] == self.definition_revision
        if self.session_id is not None:
            session = data.session(self.session_id)
            assert session["workflow_definition_id"] == self.workflow_definition_id
            assert session["definition_revision"] == self.definition_revision
        return node

    monkeypatch.setattr(BoundFrontendNodeOperations, "_require_binding", require_binding)


def test_cold_v1_receipts_survive_registry_retirement_without_dispatch(tmp_path, monkeypatch):
    database = tmp_path / "retired-v1-receipts.sqlite"
    service = GraphWorkflowService(database, public_model_factory=lambda *args, **kwargs: pytest.fail(
        "The receipt fixture must not create a model client"))
    install_frozen_v1_fixture(service)
    allow_test_fixture_binding(monkeypatch, service)
    manager = None
    connections = None
    release = {}
    chains = []
    try:
        gate_entered = {}
        gate_calls = 0
        gate_lock = Lock()

        def execute_gate(config, inputs, context):
            nonlocal gate_calls
            with gate_lock:
                gate_calls += 1
            sid = context.workflow_session_id
            if sid in release:
                gate_entered[sid].set()
                assert release[sid].wait(5), "test gate did not release"
            return {}

        service.registry.register(NodeDefinition(
            "test.retired-receipt-gate", "1", "Receipt gate", "Test",
            {}, {"type": "object"}, is_output=True,
        ), execute_gate)
        service.registry.register(NodeDefinition(
            "test.retired-receipt-tail", "1", "Receipt tail", "Test",
            {}, {"type": "object"}, is_output=True,
        ), lambda config, inputs, context: {})
        graph, _, frontends = frozen_v1_graph(service, bound=True, count=3)
        gate = node(service.registry, "test.retired-receipt-gate", 20301)
        gate["configuration_policy"] = deepcopy(service.registry.get(
            "test.retired-receipt-gate", "1",
        ).definition.to_dict()["default_configuration_policy"])
        tail = node(service.registry, "test.retired-receipt-tail", 20302)
        tail["configuration_policy"] = deepcopy(service.registry.get(
            "test.retired-receipt-tail", "1",
        ).definition.to_dict()["default_configuration_policy"])
        graph["nodes"].extend((gate, tail))
        graph["control_edges"] = [
            {"edge_id": uid(), "source_node_id": frontends[1]["node_binding_id"],
             "target_node_id": gate["node_binding_id"]},
            {"edge_id": uid(), "source_node_id": gate["node_binding_id"],
             "target_node_id": tail["node_binding_id"]},
        ]
        graph = save_graph(service, graph)

        session_a = service.create_session(
            graph["workflow_definition_id"], graph["revision"], idempotency_key="receipt-session-a")
        session_b = service.create_session(
            graph["workflow_definition_id"], graph["revision"], idempotency_key="receipt-session-b")
        patch_owner = owner_for(graph, frontends[0], session_a["workflow_session_id"])
        missing_native_owner = owner_for(graph, frontends[2], session_a["workflow_session_id"])
        run_owner = owner_for(graph, frontends[1], session_a["workflow_session_id"])
        close_owner = owner_for(graph, frontends[1], session_b["workflow_session_id"])

        patch_parameters = {
            "text": "accepted but response lost", "expected_edit_sequence": 1,
            "idempotency_key": "receipt-patch-pending",
        }
        missing_native_parameters = {
            "text": "native receipt removed", "expected_edit_sequence": 2,
            "idempotency_key": "receipt-patch-missing-native",
        }
        original_patch = BoundFrontendNodeOperations.patch_input
        accepted_patches = {}

        def save_then_lose(self, **parameters):
            result = original_patch(self, **parameters)
            accepted_patches[self.node_binding_id] = result
            if self.node_binding_id in {
                patch_owner["node_binding_id"], missing_native_owner["node_binding_id"],
            }:
                raise OSError("frontend response lost after native acceptance")
            return result

        monkeypatch.setattr(BoundFrontendNodeOperations, "patch_input", save_then_lose)
        patch_channel = FrontendNodeChannel(service, patch_owner)
        with pytest.raises(OSError, match="response lost"):
            patch_channel.handle(packet(patch_owner, "patch_input", patch_parameters))
        with pytest.raises(OSError, match="response lost"):
            FrontendNodeChannel(service, missing_native_owner).handle(
                packet(missing_native_owner, "patch_input", missing_native_parameters))
        monkeypatch.setattr(BoundFrontendNodeOperations, "patch_input", original_patch)
        assert accepted_patches[patch_owner["node_binding_id"]]["outcome"] == "saved"
        assert accepted_patches[missing_native_owner["node_binding_id"]]["outcome"] == "saved"

        missing_native_request = {
            "owner": missing_native_owner, "parameters": missing_native_parameters,
        }
        run_channel = FrontendNodeChannel(service, run_owner)
        start_parameters = {
            "expected_session_revision": session_a["revision"], "idempotency_key": "receipt-start-a",
        }
        release[session_a["workflow_session_id"]] = Event()
        gate_entered[session_a["workflow_session_id"]] = Event()
        started_a = run_channel.handle(packet(run_owner, "start", start_parameters))
        chain_a = started_a["result"]["start_result"]["active_chain_run_id"]
        chains.append(chain_a)
        assert gate_entered[session_a["workflow_session_id"]].wait(5)

        current_a = service.get_session(session_a["workflow_session_id"])
        pause_parameters = {
            "expected_session_revision": current_a["revision"], "idempotency_key": "receipt-pause-a",
        }
        paused_a = run_channel.handle(packet(run_owner, "pause", pause_parameters))
        assert paused_a["outcome"] == "succeeded"
        release[session_a["workflow_session_id"]].set()
        service.wait(chain_a)
        assert service.get_session(session_a["workflow_session_id"])["status"] == "paused"

        current_a = service.get_session(session_a["workflow_session_id"])
        resume_parameters = {
            "expected_session_revision": current_a["revision"], "idempotency_key": "receipt-resume-a",
        }
        resumed_a = run_channel.handle(packet(run_owner, "resume", resume_parameters))
        assert resumed_a["outcome"] == "succeeded"
        service.wait(chain_a)
        succeeded_a = service.get_session(session_a["workflow_session_id"])
        assert succeeded_a["status"] == "succeeded"

        history = checkpoint_history(service, succeeded_a)
        assert history["latest"] is not None
        fork_parameters = {
            "checkpoint_id": history["latest"]["checkpoint_id"],
            "expected_session_revision": succeeded_a["revision"],
            "expected_data_revision": succeeded_a["data_revision"],
            "expected_head_revision": succeeded_a["head_revision"],
            "idempotency_key": "receipt-fork-a",
        }
        forked_a = run_channel.handle(packet(run_owner, "fork_checkpoint", fork_parameters))
        assert forked_a["outcome"] == "succeeded"

        release[session_b["workflow_session_id"]] = Event()
        gate_entered[session_b["workflow_session_id"]] = Event()
        close_channel = FrontendNodeChannel(service, close_owner)
        start_b_parameters = {
            "expected_session_revision": session_b["revision"], "idempotency_key": "receipt-start-b",
        }
        started_b = close_channel.handle(packet(close_owner, "start", start_b_parameters))
        chain_b = started_b["result"]["start_result"]["active_chain_run_id"]
        chains.append(chain_b)
        assert gate_entered[session_b["workflow_session_id"]].wait(5)
        current_b = service.get_session(session_b["workflow_session_id"])
        pause_b_parameters = {
            "expected_session_revision": current_b["revision"], "idempotency_key": "receipt-pause-b",
        }
        paused_b = close_channel.handle(packet(close_owner, "pause", pause_b_parameters))
        assert paused_b["outcome"] == "succeeded"
        release[session_b["workflow_session_id"]].set()
        service.wait(chain_b)
        assert service.get_session(session_b["workflow_session_id"])["status"] == "paused"
        current_b = service.get_session(session_b["workflow_session_id"])
        close_parameters = {
            "expected_session_revision": current_b["revision"], "idempotency_key": "receipt-close-b",
        }
        closed_b = close_channel.handle(packet(close_owner, "close", close_parameters))
        assert closed_b["outcome"] == "succeeded"

        originals = [
            ("patch_input", patch_owner, patch_parameters, patch_channel),
            ("patch_input", missing_native_owner, missing_native_parameters,
             FrontendNodeChannel(service, missing_native_owner)),
            ("start", run_owner, start_parameters, run_channel),
            ("pause", run_owner, pause_parameters, run_channel),
            ("resume", run_owner, resume_parameters, run_channel),
            ("fork_checkpoint", run_owner, fork_parameters, run_channel),
            ("start", close_owner, start_b_parameters, close_channel),
            ("pause", close_owner, pause_b_parameters, close_channel),
            ("close", close_owner, close_parameters, close_channel),
        ]
        expected = {}
        for operation, original_owner, parameters, channel in originals:
            if original_owner == missing_native_owner:
                continue
            result = channel.handle(receipt_packet(original_owner, operation, original_owner, parameters))
            expected[(operation, original_owner["node_binding_id"], original_owner["session_id"],
                      parameters["idempotency_key"])] = result
            assert result["operation"] == operation
            assert result["idempotency_key"] == parameters["idempotency_key"]
            if operation != "patch_input":
                assert result["outcome"] == "succeeded"

        private_records = []
        for operation, original_owner, parameters, _ in originals:
            if original_owner == missing_native_owner:
                continue
            request = {"owner": original_owner, "parameters": parameters}
            rows = evidence_for(database, operation, request)
            assert len(rows) == 1 and rows[0]["request"] == request
            expected_resolution = "pending" if operation == "patch_input" \
                and original_owner == patch_owner else "verified"
            assert rows[0]["resolution"] == expected_resolution
            private_records.append(rows[0])

        native_operations = {
            "patch_input": "graph.frontend-node.patch-input",
            "start": "graph.frontend-node.start",
            "pause": "graph.frontend-node.pause",
            "resume": "graph.frontend-node.resume",
            "fork_checkpoint": "graph.frontend-node.fork-checkpoint",
            "close": "graph.frontend-node.close",
        }
        with closing(sqlite3.connect(database)) as connection:
            for record in private_records:
                if record["idempotency_key"] == missing_native_parameters["idempotency_key"]:
                    continue
                row = connection.execute(
                    "SELECT 1 FROM idempotency WHERE operation=? AND key=?",
                    (native_operations[next(
                        name for name, private in FRONTEND_NODE_EVIDENCE_OPERATIONS.items()
                        if private == record["operation"])], record["idempotency_key"]),
                ).fetchone()
                assert row is not None

        # Exercise v2 registration before retiring the older declaration.
        v2_graph, _, v2_frontends = chat_graph(service, bound=True)
        v2_graph = save_chat_graph(service, v2_graph)
        v2_initial = create_chat_session(service, v2_graph)
        v2_owner = {
            "workflow_definition_id": v2_graph["workflow_definition_id"],
            "definition_revision": v2_graph["revision"],
            "node_binding_id": v2_frontends[0]["node_binding_id"],
            "session_id": v2_initial["workflow_session_id"],
        }
        v2_channel = FrontendNodeChannel(service, v2_owner)
        v2_final = graph_run(service, v2_initial)
        assert v2_final["status"] == "succeeded"
        v2_expected_chat = v2_final["objects"]["chat-0"]["value"]
        v2_state_before = v2_channel.handle(packet(v2_owner, "state"))
        assert v2_state_before["snapshot"]["chat"] == v2_expected_chat

        service.close()
        manager = GraphManagementService(database)
        # Model a newly installed package registry that no longer exports @1.
        manager.registry._nodes.pop(("frontend.node", "1"), None)
        assert manager.registry.get("frontend.node", "1") is None
        assert manager.registry.get("frontend.node", "2") is not None
        assert manager._execution_owner is False and manager._executor is None
        dispatches = []

        def forbid_dispatch(*args, **kwargs):
            dispatches.append((args, kwargs))
            pytest.fail("A receipt-only request entered frontend or workflow business dispatch")

        for method in ("start", "control", "fork_checkpoint", "_execute"):
            monkeypatch.setattr(manager, method, forbid_dispatch)
        monkeypatch.setattr(
            "phase1_agent.frontend_node_evidence.FrontendNodeEvidenceAdapter.execute", forbid_dispatch)

        connections = FrontendNodeConnections()
        factory_flags = []

        def management_only(execution):
            factory_flags.append(execution)
            assert execution is False
            assert manager._execution_owner is False and manager._executor is None
            return manager

        http = FrontendNodeHTTP(management_only, connections)
        before_reads = database_snapshot(database)
        for operation, original_owner, parameters, _ in originals:
            if original_owner == missing_native_owner:
                continue
            actual = http.channel(
                original_owner, receipt_packet(original_owner, operation, original_owner, parameters))
            identity = (operation, original_owner["node_binding_id"], original_owner["session_id"],
                       parameters["idempotency_key"])
            assert actual == expected[identity]
            assert database_snapshot(database) == before_reads
        assert factory_flags == [False] * len(expected)

        # A genuinely stored native receipt can conclude pending evidence, but only
        # that evidence row and its evidence-resolution receipt may change.
        before_accept = database_snapshot(database)
        accepted = http.channel(
            patch_owner, receipt_packet(patch_owner, "patch_input", patch_owner, patch_parameters, accept=True))
        patch_identity = ("patch_input", patch_owner["node_binding_id"], patch_owner["session_id"],
                          patch_parameters["idempotency_key"])
        assert accepted == expected[patch_identity]
        after_accept = database_snapshot(database)
        assert_only_resolution_changed(before_accept, after_accept, "patch_input",
                                       patch_parameters["idempotency_key"])
        assert evidence_for(database, "patch_input", {
            "owner": patch_owner, "parameters": patch_parameters,
        })[0]["resolution"] == "verified"
        before_replay = database_snapshot(database)
        assert http.channel(
            patch_owner, receipt_packet(patch_owner, "patch_input", patch_owner, patch_parameters, accept=True)
        ) == accepted
        assert database_snapshot(database) == before_replay

        # Different owner coordinates and altered original requests cannot turn a
        # frozen receipt into a new lookup or a write.
        stable = database_snapshot(database)
        for field in ("workflow_definition_id", "definition_revision", "node_binding_id", "session_id"):
            forged_owner = deepcopy(patch_owner)
            forged_owner[field] = forged_owner[field] + 1 if field == "definition_revision" else uid()
            forged = receipt_packet(patch_owner, "patch_input", forged_owner, patch_parameters)
            with pytest.raises((ContractValidationError, DiagnosticError)):
                http.channel(patch_owner, forged)
            assert database_snapshot(database) == stable

        for field in ("workflow_definition_id", "definition_revision", "node_binding_id", "session_id"):
            wrong_route = deepcopy(patch_owner)
            wrong_route[field] = wrong_route[field] + 1 if field == "definition_revision" else uid()
            unresolved = http.channel(wrong_route, receipt_packet(
                wrong_route, "patch_input", wrong_route, patch_parameters))
            assert unresolved["outcome"] == "unknown"
            assert database_snapshot(database) == stable

        changed_body = {**patch_parameters, "text": "changed body"}
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.channel(patch_owner, receipt_packet(
                patch_owner, "patch_input", patch_owner, changed_body))
        changed_key = {**patch_parameters, "idempotency_key": "different-original-key"}
        missing = http.channel(patch_owner, receipt_packet(
            patch_owner, "patch_input", patch_owner, changed_key))
        assert missing["outcome"] == "unknown"
        absent = {**patch_parameters, "idempotency_key": "never-written"}
        missing = http.channel(patch_owner, receipt_packet(
            patch_owner, "patch_input", patch_owner, absent))
        assert missing["outcome"] == "unknown"

        other_node = deepcopy(run_owner)
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.channel(other_node, receipt_packet(
                other_node, "patch_input", patch_owner, patch_parameters))

        chat_only = {
            "round_id": uid(), "expected_session_revision": v2_final["revision"],
            "expected_data_revision": v2_final["data_revision"],
            "expected_head_revision": v2_final["head_revision"],
            "expected_object_revision": v2_final["objects"]["chat-0"]["revision"],
            "idempotency_key": "v1-must-not-receive-reroll",
        }
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.channel(patch_owner, receipt_packet(
                patch_owner, "reroll", patch_owner, chat_only))
        assert database_snapshot(database) == stable
        assert factory_flags == [False] * (len(expected) + 15)

        # Retired business operations remain unavailable on a cold @1 channel,
        # while the same cold registry still serves a logical-chat @2 snapshot.
        cold_v1 = FrontendNodeChannel(manager, patch_owner)
        for business in (
            packet(patch_owner, "state"),
            packet(patch_owner, "start", {
                "expected_session_revision": session_a["revision"], "idempotency_key": "v1-start-denied",
            }),
            packet(patch_owner, "patch_input", {
                "text": "must not dispatch", "expected_edit_sequence": 3,
                "idempotency_key": "v1-patch-denied",
            }),
        ):
            with pytest.raises((ContractValidationError, DiagnosticError)):
                cold_v1.handle(business)
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.subscribe(patch_owner)
        assert factory_flags[-1] is False
        assert database_snapshot(database) == stable

        cold_v2 = FrontendNodeChannel(manager, v2_owner)
        v2_state_after = cold_v2.handle(packet(v2_owner, "state"))
        assert v2_state_after["snapshot"]["chat"] == v2_expected_chat
        assert manager.registry.get("frontend.node", "2") is not None
        assert database_snapshot(database) == stable
        assert dispatches == []
        assert gate_calls >= 2

        # The second pending request has no native result. It must stay unknown
        # and cannot be accepted merely because the original request was saved.
        missing_key = missing_native_parameters["idempotency_key"]
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            deleted = connection.execute(
                "DELETE FROM idempotency WHERE operation='graph.frontend-node.patch-input' AND key=?",
                (missing_key,),
            )
            assert deleted.rowcount == 1
        before_missing_native_read = database_snapshot(database)
        unknown = http.channel(missing_native_owner, receipt_packet(
            missing_native_owner, "patch_input", missing_native_owner, missing_native_parameters))
        assert unknown["outcome"] == "unknown"
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.channel(missing_native_owner, receipt_packet(
                missing_native_owner, "patch_input", missing_native_owner,
                missing_native_parameters, accept=True))
        assert database_snapshot(database) == before_missing_native_read
        assert factory_flags[-2:] == [False, False]
    finally:
        for event in release.values():
            event.set()
        if connections is not None:
            connections.close()
        if manager is not None:
            manager.close()
        if not getattr(service, "_closed", False):
            service.close()


def test_cold_v1_business_surface_rejects_without_consumer_or_execution_factory(tmp_path):
    database = tmp_path / "retired-v1-business.sqlite"
    service = GraphWorkflowService(database)
    install_frozen_v1_fixture(service)
    manager = None
    connections = FrontendNodeConnections()
    try:
        graph, _, frontends = frozen_v1_graph(service, bound=True)
        graph = save_graph(service, graph)
        session = service.create_session(
            graph["workflow_definition_id"], graph["revision"], idempotency_key="business-session")
        owner = owner_for(graph, frontends[0], session["workflow_session_id"])
        service.close()

        manager = GraphManagementService(database)
        manager.registry._nodes.pop(("frontend.node", "1"), None)
        calls = []

        def no_execution(execution):
            calls.append(execution)
            assert execution is False
            return manager

        http = FrontendNodeHTTP(no_execution, connections)
        before = database_snapshot(database)
        channel = FrontendNodeChannel(manager, owner)
        for request in (
            packet(owner, "state"),
            packet(owner, "start", {
                "expected_session_revision": session["revision"], "idempotency_key": "business-start",
            }),
            packet(owner, "patch_input", {
                "text": "not accepted", "expected_edit_sequence": 1, "idempotency_key": "business-patch",
            }),
        ):
            with pytest.raises((ContractValidationError, DiagnosticError)):
                channel.handle(request)
        with pytest.raises((ContractValidationError, DiagnosticError)):
            http.subscribe(owner)
        assert calls == [False]
        assert manager._execution_owner is False and manager._executor is None
        assert database_snapshot(database) == before
    finally:
        connections.close()
        if manager is not None:
            manager.close()
        if not getattr(service, "_closed", False):
            service.close()
