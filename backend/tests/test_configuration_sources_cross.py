"""Workbench and frontend-node edits share one current configuration and CAS basis."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService

from frontend_node_test_support import graph as _graph


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "configuration-sources-cross.sqlite")) as instance:
        yield instance


def submit_workbench(app, document, sequence, *, key=None):
    """Submit a complete document from the workbench source and prove its original receipt."""
    request = {
        "workflow_definition_id": document["workflow_definition_id"],
        "document": deepcopy(document),
        "expected_edit_sequence": sequence,
        "idempotency_key": key or str(uuid4()),
    }
    accepted = app.command("definition.update", request)
    receipt = read_graph_application_receipt(
        app._service.database, "definition.update", request)
    assert receipt["outcome"] == "matched", receipt
    assert receipt["receipt"] == accepted["receipt"]
    assert receipt["result"] == accepted["result"]
    assert app.query("receipt.read", {
        "operation": "definition.update", "parameters": request}) == receipt
    return accepted["result"], request, accepted


def current_rows(service, identity):
    with closing(service._store()) as store:
        return [dict(row) for row in store._connection.execute(
            "SELECT * FROM workflow_current_configuration WHERE workflow_definition_id=?",
            (identity,)).fetchall()]


def current_row(service, identity):
    rows = current_rows(service, identity)
    assert len(rows) == 1
    return rows[0]


def storage_snapshot(service):
    with closing(service._store()) as store:
        return tuple(
            tuple(tuple(row) for row in store._connection.execute(query))
            for query in (
                "SELECT * FROM records ORDER BY record_type,record_id",
                "SELECT * FROM workflow_current_configuration ORDER BY workflow_definition_id",
                "SELECT * FROM idempotency ORDER BY operation,key",
                "SELECT * FROM pending_commands ORDER BY operation,idempotency_key",
            ))


def test_both_sources_share_edit_sequence_current_row_and_frozen_receipts(service, monkeypatch):
    app = GraphApplication(service)
    graph, messages, frontends = _graph(service, bound=True)
    definition_id = graph["workflow_definition_id"]
    input_id = messages[0]["node_binding_id"]
    frontend_id = frontends[0]["node_binding_id"]
    original_text = graph["nodes"][0]["config"]["text"]
    assert original_text == "message 0"

    # 1. The workbench saves the whole S2 document first: current A, sequence 1.
    initial_key = "workbench-initial-" + str(uuid4())
    initial, _, _ = submit_workbench(app, graph, 0, key=initial_key)
    assert initial["outcome"] == "saved"
    assert initial["edit_sequence"] == 1
    assert initial["definition_revision"] == 1
    assert initial["changed_categories"] == ["fixed"]
    assert initial["source_has_effective_session"] is False
    assert initial["document"]["nodes"][0]["config"]["text"] == original_text
    assert initial["node_id_map"] == {input_id: input_id, frontend_id: frontend_id}
    with closing(service._store()) as store:
        assert store.read_receipt("graph.definition.update", initial_key) == [
            {"graph_result": initial}]

    # 2. The workbench changes only the adjustable text to B, keeping revision 1.
    changed = deepcopy(initial["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    b_key = "workbench-b-" + str(uuid4())
    b_saved, b_request, b_accepted = submit_workbench(app, changed, 1, key=b_key)
    assert b_saved["outcome"] == "saved"
    assert b_saved["edit_sequence"] == 2
    assert b_saved["definition_revision"] == 1
    assert b_saved["changed_categories"] == ["adjustable"]
    assert b_saved["document"]["nodes"][0]["config"]["text"] == "B"
    expected_input = deepcopy(initial["document"]["nodes"][0]["config"])
    expected_input["text"] = "B"
    assert b_saved["document"]["nodes"][0]["config"] == expected_input
    assert b_saved["document"]["nodes"][1] == initial["document"]["nodes"][1]
    assert b_saved["document"]["object_bindings"] == initial["document"]["object_bindings"]
    assert b_saved["document"]["package_lock"] == initial["document"]["package_lock"]
    assert b_saved["document"]["edges"] == initial["document"]["edges"]
    assert service.get_definition(definition_id, 1) == graph

    # 3. The node page binds at fixed revision 1 and a stale C is refused by CAS.
    bound = FrontendNodeOperations(service).bind(
        workflow_definition_id=definition_id, definition_revision=1,
        node_binding_id=frontend_id, session_id=None)
    current_before = service.get_definition(definition_id)
    assert current_before["edit_sequence"] == 2
    assert current_before["nodes"][0]["config"]["text"] == "B"
    row_before = current_row(service, definition_id)
    snapshot_before = storage_snapshot(service)
    stale_node_key = "node-stale-c-" + str(uuid4())
    with pytest.raises(ContractValidationError) as refused:
        bound.patch_input("C", expected_edit_sequence=1, idempotency_key=stale_node_key)
    assert refused.value.reason_code == "stale_edit_sequence"
    assert service.get_definition(definition_id) == current_before
    assert current_row(service, definition_id) == row_before
    assert storage_snapshot(service) == snapshot_before
    with closing(service._store()) as store:
        assert store.read_receipt("graph.frontend-node.patch-input", stale_node_key) is None

    # 4. The node page reads the real current state, then saves C on that new basis.
    owner = {
        "workflow_definition_id": definition_id, "definition_revision": 1,
        "node_binding_id": frontend_id, "session_id": None,
    }
    channel = FrontendNodeChannel(service, owner)
    state = channel.handle({"owner": deepcopy(owner), "operation": "state", "parameters": {}})
    assert state["owner"] == owner
    assert state["input"] == {"text": "B", "edit_sequence": 2, "policy": "adjustable"}
    assert state["run"] is None and state["snapshot"] is None and state["pending"] == []

    node_c_key = "node-save-c-" + str(uuid4())
    c_saved = bound.patch_input("C", expected_edit_sequence=2, idempotency_key=node_c_key)
    assert c_saved["outcome"] == "saved"
    assert c_saved["edit_sequence"] == 3
    assert c_saved["definition_revision"] == 1
    assert c_saved["changed_categories"] == ["adjustable"]
    assert c_saved["document"]["nodes"][0]["config"]["text"] == "C"
    assert c_saved["document"]["nodes"][1]["config"] == b_saved["document"]["nodes"][1]["config"]
    assert service.get_definition(definition_id) == c_saved["document"]
    with closing(service._store()) as store:
        assert store.read_receipt("graph.frontend-node.patch-input", node_c_key) == [
            {"graph_result": c_saved}]
        assert store.read_receipt("graph.configuration.patch-node", node_c_key) is None
        assert store.read_receipt("graph.definition.update", node_c_key) is None

    # 5. A new management object reads current C / fixed A from storage, not from a save result.
    with closing(GraphManagementService(service.database)) as reader:
        fresh_current = reader.get_definition(definition_id)
        fresh_fixed = reader.get_definition(definition_id, 1)
    assert fresh_current["nodes"][0]["config"]["text"] == "C"
    assert fresh_current["edit_sequence"] == 3
    assert fresh_current["revision"] == 1
    assert fresh_fixed["nodes"][0]["config"]["text"] == original_text
    assert fresh_fixed["revision"] == 1
    assert "edit_sequence" not in fresh_fixed
    assert fresh_current == c_saved["document"]
    assert fresh_current is not c_saved["document"]
    assert fresh_fixed == graph
    assert service.get_definition(definition_id) == fresh_current
    assert service.get_definition(definition_id, 1) == fresh_fixed

    # 6. The workbench's original B request and receipt still describe B, not current C.
    historical = read_graph_application_receipt(
        service.database, "definition.update", b_request)
    assert historical["outcome"] == "matched"
    assert historical["receipt"] == b_accepted["receipt"]
    assert historical["result"] == b_saved
    assert historical["result"]["document"]["nodes"][0]["config"]["text"] == "B"
    assert historical["result"]["edit_sequence"] == 2
    assert service.get_definition(definition_id)["nodes"][0]["config"]["text"] == "C"
    assert app.command("definition.update", b_request) == b_accepted

    # 7. Reverse order: node page saves D, the stale workbench C draft is refused,
    #    then the workbench reads the new basis and saves C.
    node_d_key = "node-save-d-" + str(uuid4())
    d_saved = bound.patch_input("D", expected_edit_sequence=3, idempotency_key=node_d_key)
    assert d_saved["outcome"] == "saved" and d_saved["edit_sequence"] == 4
    assert d_saved["definition_revision"] == 1
    assert service.get_definition(definition_id)["nodes"][0]["config"]["text"] == "D"

    # The full C document was captured on the B/sequence-2 basis and is now stale.
    stale_c_document = deepcopy(b_saved["document"])
    stale_c_document["nodes"][0]["config"]["text"] = "C"
    stale_workbench_key = "workbench-stale-c-" + str(uuid4())
    stale_request = {
        "workflow_definition_id": definition_id, "document": stale_c_document,
        "expected_edit_sequence": 2, "idempotency_key": stale_workbench_key,
    }
    stale_row_before = current_row(service, definition_id)
    stale_snapshot_before = storage_snapshot(service)
    with pytest.raises(ContractValidationError) as stale:
        app.command("definition.update", stale_request)
    assert stale.value.reason_code == "stale_edit_sequence"
    assert service.get_definition(definition_id)["nodes"][0]["config"]["text"] == "D"
    assert current_row(service, definition_id) == stale_row_before
    assert storage_snapshot(service) == stale_snapshot_before
    assert read_graph_application_receipt(
        service.database, "definition.update", stale_request)["outcome"] == "unresolved"

    # A workbench draft captured at basis 3 is also stale: the node page's D save
    # consumed that basis, so even a document declaring edit_sequence 3 is refused.
    stale_three_document = deepcopy(stale_c_document)
    stale_three_document["edit_sequence"] = 3
    with pytest.raises(ContractValidationError) as stale_three:
        app.command("definition.update", {
            "workflow_definition_id": definition_id, "document": stale_three_document,
            "expected_edit_sequence": 3, "idempotency_key": "workbench-stale-3-" + str(uuid4()),
        })
    assert stale_three.value.reason_code == "stale_edit_sequence"
    assert current_row(service, definition_id) == stale_row_before
    assert storage_snapshot(service) == stale_snapshot_before

    # The fresh basis the workbench must submit on is 4.
    fresh = app.query("definition.read", {"identity": definition_id})
    assert fresh["edit_sequence"] == 4
    assert fresh["revision"] == 1
    assert fresh["nodes"][0]["config"]["text"] == "D"
    fresh_c = deepcopy(fresh)
    fresh_c["nodes"][0]["config"]["text"] = "C"
    workbench_c_key = "workbench-fresh-c-" + str(uuid4())
    workbench_c, _, _ = submit_workbench(
        app, fresh_c, fresh["edit_sequence"], key=workbench_c_key)
    assert workbench_c["outcome"] == "saved"
    assert workbench_c["edit_sequence"] == 5
    assert workbench_c["definition_revision"] == 1
    assert workbench_c["changed_categories"] == ["adjustable"]
    assert service.get_definition(definition_id) == workbench_c["document"]
    assert current_row(service, definition_id)["edit_sequence"] == 5
    assert current_row(service, definition_id)["fixed_definition_revision"] == 1

    # 8. A node-page patch enters no second configuration transaction or business entry.
    transaction_entries = []
    native_change = service._configuration_change

    def counted_change(operation, key, request, callback):
        transaction_entries.append(operation)
        return native_change(operation, key, request, callback)

    def forbidden_public_patch(**arguments):
        pytest.fail("frontend-node patch used the public narrow-patch entry")

    monkeypatch.setattr(service, "_configuration_change", counted_change)
    monkeypatch.setattr(service, "patch_node_configuration", forbidden_public_patch)
    node_e_key = "node-save-e-" + str(uuid4())
    e_saved = bound.patch_input("E", expected_edit_sequence=5, idempotency_key=node_e_key)
    assert e_saved["outcome"] == "saved" and e_saved["edit_sequence"] == 6
    assert e_saved["definition_revision"] == 1
    assert transaction_entries == ["graph.frontend-node.patch-input"]
    assert service.get_definition(definition_id)["nodes"][0]["config"]["text"] == "E"

    # Each source keeps its own operation/receipt identity: neither can read the other's.
    with closing(service._store()) as store:
        assert store.read_receipt("graph.frontend-node.patch-input", node_e_key) == [
            {"graph_result": e_saved}]
        assert store.read_receipt("graph.configuration.patch-node", node_e_key) is None
        assert store.read_receipt("graph.definition.update", node_e_key) is None
        assert store.read_receipt("graph.frontend-node.patch-input", b_key) is None
        assert store.read_receipt("graph.definition.update", node_c_key) is None
        assert store.read_receipt("graph.definition.update", b_key) == [{"graph_result": b_saved}]
        assert store.read_receipt("graph.frontend-node.patch-input", node_c_key) == [
            {"graph_result": c_saved}]
    assert bound.read_receipt("patch_input", {
        "text": "C", "expected_edit_sequence": 2, "idempotency_key": node_c_key}) == c_saved
    assert read_graph_application_receipt(
        service.database, "definition.update", b_request)["result"][
            "document"]["nodes"][0]["config"]["text"] == "B"
    assert len(current_rows(service, definition_id)) == 1
    assert current_row(service, definition_id)["edit_sequence"] == 6

    keys = [
        initial_key, b_key, stale_node_key, node_c_key, node_d_key,
        stale_workbench_key, workbench_c_key, node_e_key,
    ]
    assert len(set(keys)) == len(keys)
