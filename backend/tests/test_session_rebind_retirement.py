"""Retired session-rebind writes remain available only as exact receipt reads."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_identity, application_origin_key,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from test_graph_service import create, service, text_graph


def test_rebind_write_entrypoints_are_retired_without_database_changes(service):
    view = create(service, text_graph(service.registry))
    session_id = view["workflow_session_id"]
    parameters = {
        "session_id": session_id,
        "definition_revision": 1,
        "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"],
        "expected_head_revision": view["head_revision"],
        "idempotency_key": str(uuid4()),
    }
    before = service.database.read_bytes()

    assert "session.rebind" not in {command.name for command in COMMANDS}
    assert "session.rebind" not in {
        command["name"] for command in GraphApplication(service).describe()["commands"]
    }
    assert not hasattr(service, "rebind_session")
    with pytest.raises(ContractValidationError) as application_error:
        GraphApplication(service).command("session.rebind", parameters)
    assert application_error.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as http_error:
        dispatch_graph(service, "POST", f"/api/graph/sessions/{session_id}/rebind", {
            key: value for key, value in parameters.items() if key != "session_id"
        })
    assert http_error.value.reason_code == "not_found"
    assert service.database.read_bytes() == before


def test_exact_historical_rebind_receipt_is_read_only_and_preserved(service):
    view = create(service, text_graph(service.registry))
    parameters = {
        "session_id": view["workflow_session_id"],
        "definition_revision": 2,
        "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"],
        "expected_head_revision": view["head_revision"],
        "idempotency_key": "historical-rebind-receipt",
    }
    frozen = deepcopy(view)
    frozen.update(
        definition_revision=2,
        revision=view["revision"] + 1,
        head_revision=view["head_revision"] + 1,
        head_commit_id=str(uuid4()),
    )
    identity = application_command_identity("session.rebind", "management", parameters)
    native = identity["native_receipt"]
    command_digest = native["request_digest"]
    origin_digest = "workflow-op-v1:" + identity["request_sha256"]
    encoded_result = canonical_bytes([{"graph_result": frozen}]).decode("utf-8")
    encoded_identity = canonical_bytes([{"application_identity": identity}]).decode("utf-8")
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO idempotency(operation,key,digest,result_refs,result_payload,request_digest) "
            "VALUES(?,?,?,?,?,?)",
            (native["operation"], native["key"], command_digest, "[]", encoded_result, command_digest),
        )
        connection.execute(
            "INSERT INTO idempotency(operation,key,digest,result_refs,result_payload,request_digest) "
            "VALUES(?,?,?,?,?,?)",
            (APPLICATION_IDENTITY_OPERATION, application_origin_key(identity),
             origin_digest, "[]", encoded_identity, origin_digest),
        )

    before = service.database.read_bytes()
    receipt = read_graph_application_receipt(
        service.database, "session.rebind", deepcopy(parameters), scope="management",
    )
    assert receipt["outcome"] == "matched"
    assert receipt["result"] == frozen
    assert receipt["receipt"]["operation"] == "session.rebind"
    assert service.database.read_bytes() == before
    changed_request = {**parameters, "expected_revision": parameters["expected_revision"] + 1}
    unmatched = read_graph_application_receipt(
        service.database, "session.rebind", changed_request, scope="management",
    )
    assert unmatched["outcome"] == "unresolved"
    assert unmatched["reason_code"] == "request_mismatch"
    assert service.database.read_bytes() == before
