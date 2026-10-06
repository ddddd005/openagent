"""Public declarations enforce CAS, revocation, and safe scoped result fields."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.exposure_configuration import ExposureConfigurationStore, validate_exposure_configuration
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService
from test_workflow_context_view import database, persistent_data
from test_workflow_prepared_context import uid
from test_server_prompt_configs import request, running_server


def declaration(stage="A", kind="result", fields=None):
    return {
        "schemaVersion": 1, "id": uid(9510 if kind == "state" else 9500 if stage == "A" else 9501),
        "workflowId": "frontend:main-test", "stage": stage,
        "nodeBindingId": A_BINDING if stage == "A" else B_BINDING,
        "publicName": stage + "." + kind, "kind": kind,
        "type": "public_agent_state" if kind == "state" else "public_node_result" if stage == "A" else "delivered_workflow_result",
        "fields": fields if fields is not None else ["result_text"] if stage == "A" else ["delivered_text"],
    }


def configuration(version=1, registrations=None):
    return {
        "schema_version": 1, "kind": "workflow_exposure_configuration",
        "config_id": uid(9502), "workflow_id": "frontend:main-test", "revision": version,
        "registrations": registrations if registrations is not None else [
            declaration(), declaration("B"), declaration("A", "state", ["status", "run_id", "revision", "budget"]),
        ],
    }


@pytest.mark.parametrize("change", [
    lambda v: v.update(schema_version=True), lambda v: v.update(revision=True),
    lambda v: v.update(workflow_id="another"), lambda v: v.update(private="secret"),
    lambda v: v["registrations"][0].update(fields=["messages"]),
    lambda v: v["registrations"][0].update(nodeBindingId=B_BINDING),
    lambda v: v["registrations"][0].update(stage="Output"),
    lambda v: v["registrations"][0].update(publicName=" "),
    lambda v: v["registrations"][0].update(type="private_context"),
    lambda v: v["registrations"].append(deepcopy(v["registrations"][0])),
    lambda v: v["registrations"][1].update(fields=["result_text"]),
])
def test_strict_declarations_reject_private_or_incompatible_fields(change):
    value = configuration()
    change(value)
    with pytest.raises(ContractValidationError) as failure:
        validate_exposure_configuration(value)
    assert failure.value.reason_code == "invalid_request"


def test_revision_cas_receipts_and_revocation_survive_reopen(database):
    with closing(WorkflowService(database)) as service:
        record = configuration()
        assert service.save_exposure_configuration(record=record, expected_revision=0, idempotency_key="save") == record
        assert service.save_exposure_configuration(record=record, expected_revision=0, idempotency_key="save") == record
        with pytest.raises(ContractValidationError) as conflict:
            service.save_exposure_configuration(record=configuration(2), expected_revision=1, idempotency_key="save")
        assert conflict.value.reason_code == "idempotency_conflict"
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "result", "turn")
        service.wait_for_idle(sid)
        before = persistent_data(database)
        read = service.read_exposures(sid, record["config_id"], 1)
        assert persistent_data(database) == before
        assert read["availability"] == "available"
        assert read["observations"][0]["value"] == {"result_text": "[Offline draft]\n\nresult"}
        assert read["observations"][1]["value"] == {"delivered_text": "[Offline revision]\n\nresult"}
        assert set(read["observations"][2]["value"]) == {"status", "run_id", "revision", "budget"}
        service.save_exposure_configuration(record=configuration(2, []), expected_revision=1, idempotency_key="revoke")
        revoked = service.read_exposures(sid, record["config_id"], 1)
        assert revoked["availability"] == "unavailable"
        assert revoked["reason_code"] == "declaration_stale"
        assert revoked["registrations"] == revoked["observations"] == []
        assert service.read_exposures(sid, record["config_id"], 2)["observations"] == []
    with closing(WorkflowService(database)) as reopened:
        assert reopened.get_exposure_configuration(record["config_id"], 1) == record
        assert reopened.read_exposures(sid, record["config_id"], 1) == revoked


def test_write_rollback_and_empty_migration_leave_no_invented_declarations(database):
    def fail(point):
        if point == "exposure_configuration_before_commit":
            raise RuntimeError("private storage detail")
    with closing(SqliteStore(database, fault_injector=fail)) as store:
        catalog = ExposureConfigurationStore(store)
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        with pytest.raises(RuntimeError):
            catalog.write(configuration(), expected_revision=0, idempotency_key="fault")
        assert catalog.get(uid(9502)) is None
        assert store._connection.execute("SELECT COUNT(*) FROM exposure_configuration_revisions").fetchone()[0] == 0
        assert store._connection.execute("SELECT COUNT(*) FROM exposure_configuration_mutations").fetchone()[0] == 0


def test_http_only_declared_read_fields_are_available_without_dispatch_or_write(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "http", "turn")
        service.wait_for_idle(sid)
        config = configuration()
        with running_server(service) as port:
            body = {"record": config, "expected_revision": 0, "idempotency_key": "save"}
            assert request(port, "POST", "/api/exposure-configurations", body)[0] == 201
            path = f"/api/sessions/{sid}/exposures/{config['config_id']}/revisions/1"
            before = persistent_data(database)
            status, read = request(port, "GET", path)
            assert status == 200 and len(read["observations"]) == 3
            assert request(port, "GET", path + "?field=private")[0] == 404
            assert persistent_data(database) == before
            assert request(port, "POST", "/api/exposure-configurations", body, origin=False)[0] == 403
            invalid = deepcopy(body)
            invalid["record"]["registrations"][0]["fields"] = ["private_context"]
            status, error = request(port, "POST", "/api/exposure-configurations", invalid)
            assert status == 400 and error["error"]["reason_code"] == "invalid_request"
            assert "private_context" not in str(error)
