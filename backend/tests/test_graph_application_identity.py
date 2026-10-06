"""Pure normalization and synchronous application-origin ownership."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest, loads_strict
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_digest, application_command_identity,
    application_identity_key, bind_application_command, native_command_request,
    persist_application_command_identity,
)


SESSION = str(UUID(int=1, version=4))
DEFINITION = str(UUID(int=2, version=4))
RESOURCE = str(UUID(int=3, version=4))
KEY = "original-request"


def fingerprint(value):
    return "workflow-op-v1:" + content_digest(value).rsplit(":", 1)[1]


@pytest.mark.parametrize("operation,parameters,native_operation,normalized", [
    ("definition.save", {"document": {"revision": 1}, "expected_revision": 0},
     "graph.definition.save", {"document": {"revision": 1}, "expected_revision": 0}),
    ("legacy.migrate", {"document": {}, "source_session_id": None,
                       "expected_source_revision": None, "mappings": []},
     "graph.legacy.migrate", {"document": {}, "source_session_id": None,
                             "expected_source_revision": None, "mappings": []}),
    ("session.create", {"workflow_definition_id": DEFINITION, "definition_revision": 1},
     "graph.session.create", {"workflow_definition_id": DEFINITION, "definition_revision": 1}),
    ("consumer.session.create", {"workflow_definition_id": DEFINITION, "definition_revision": 1},
     "graph.session.create", {"workflow_definition_id": DEFINITION, "definition_revision": 1}),
    ("run.start", {"session_id": SESSION, "expected_revision": 1},
     "graph.run.start", {"session": SESSION, "expected_revision": 1, "inputs": {}}),
    ("consumer.run.start", {"session_id": SESSION, "expected_revision": 1, "inputs": {"x": 2}},
     "graph.run.start", {"session": SESSION, "expected_revision": 1, "inputs": {"x": 2}}),
    ("run.control", {"session_id": SESSION, "action": "pause", "expected_revision": 1},
     "graph.run.control", {"session": SESSION, "action": "pause", "expected_revision": 1,
                           "add_model_requests": 0, "add_model_attempts": 0}),
    ("consumer.run.control", {"session_id": SESSION, "action": "extend_budget",
                              "expected_revision": 1, "add_model_requests": 2, "add_model_attempts": 3},
     "graph.run.control", {"session": SESSION, "action": "extend_budget", "expected_revision": 1,
                           "add_model_requests": 2, "add_model_attempts": 3}),
    ("session.copy", {"session_id": SESSION, "document": {}, "expected_session_revision": 1,
                      "expected_data_revision": 0, "expected_definition_revision": 1,
                      "expected_head_revision": 1},
     "graph.session.copy", {"source": SESSION, "document": {}, "expected_session_revision": 1,
                            "expected_data_revision": 0, "expected_definition_revision": 1,
                            "expected_head_revision": 1, "mappings": []}),
    ("session.rebind", {"session_id": SESSION, "definition_revision": 2, "expected_revision": 1,
                        "expected_data_revision": 0, "expected_head_revision": 1},
     "graph.session.rebind", {"session": SESSION, "definition_revision": 2, "expected_revision": 1,
                              "expected_data_revision": 0, "expected_head_revision": 1, "mappings": []}),
    ("session.data.update", {"session_id": SESSION, "expected_revision": 1, "expected_data_revision": 0},
     "graph.session.data", {"session": SESSION, "expected_revision": 1, "expected_data_revision": 0,
                            "variables": None, "shared": None}),
    ("candidate.select", {"session_id": SESSION, "candidate_id": RESOURCE, "expected_revision": 1,
                          "expected_data_revision": 0, "expected_head_revision": 1},
     "graph.candidate.select", {"session": SESSION, "candidate_id": RESOURCE, "expected_revision": 1,
                                "expected_data_revision": 0, "expected_head_revision": 1}),
    ("candidate.fork", {"session_id": SESSION, "candidate_id": RESOURCE, "expected_revision": 1,
                        "expected_data_revision": 0, "expected_head_revision": 1},
     "graph.candidate.fork", {"session": SESSION, "candidate_id": RESOURCE, "expected_revision": 1,
                              "expected_data_revision": 0, "expected_head_revision": 1}),
    ("object.write", {"session_id": SESSION, "node_id": RESOURCE, "writes": [], "expected_revision": 1},
     "graph.objects.update", {"session": SESSION, "node_id": RESOURCE, "writes": [], "expected_revision": 1}),
    ("context.adopt", {"session_id": SESSION, "node_id": RESOURCE, "view_output_id": DEFINITION,
                       "expected_revision": 1},
     "graph.context.adopt", {"session": SESSION, "node_id": RESOURCE, "view_output_id": DEFINITION,
                             "expected_revision": 1}),
])
def test_normalization_matches_native_caller_fields(operation, parameters, native_operation, normalized):
    parameters = {**parameters, "idempotency_key": KEY}
    original = deepcopy(parameters)
    assert native_command_request(operation, parameters) == {
        "table": "idempotency", "operation": native_operation, "key": KEY,
        "request_digest": fingerprint(normalized),
    }
    assert parameters == original


@pytest.mark.parametrize("operation,native_operation", [
    ("event.submit", "graph.event.submit"),
    ("consumer.event.submit", "graph.consumer.event.submit"),
])
def test_event_request_retains_definition_payload_and_public_native_operation(operation, native_operation):
    parameters = {"session_id": SESSION, "workflow_definition_id": DEFINITION, "definition_revision": 1,
                  "event_id": "inspect", "event_schema_version": 1, "payload": {"count": 2},
                  "expected_revision": 1, "idempotency_key": KEY}
    normalized = {key: value for key, value in parameters.items() if key not in ("session_id", "idempotency_key")}
    normalized["session"] = SESSION
    assert native_command_request(operation, parameters) == {
        "table": "idempotency", "operation": native_operation, "key": KEY,
        "request_digest": fingerprint(normalized),
    }


@pytest.mark.parametrize("operation,parameters,normalized", [
    ("resource.save", {"record": {"envelope_version": 1, "scope": "workspace",
                                  "type_id": "workflow.global-content", "resource_id": RESOURCE,
                                  "data_schema_version": 1, "update_sequence": 1, "value": {}},
                       "expected_sequence": 0},
     {"operation": "write", "record": {"envelope_version": 1, "scope": "workspace",
                                      "type_id": "workflow.global-content", "resource_id": RESOURCE,
                                      "data_schema_version": 1, "update_sequence": 1, "value": {}},
      "expected_sequence": 0}),
    ("resource.delete", {"identity": {"envelope_version": 1, "scope": "workspace",
                                      "type_id": "workflow.global-content", "resource_id": RESOURCE},
                         "expected_sequence": 2},
     {"operation": "delete", "reference": {"envelope_version": 1, "scope": "workspace",
                                          "type_id": "workflow.global-content", "resource_id": RESOURCE},
      "expected_sequence": 2}),
    ("resource.import", {"legacy_id": RESOURCE},
     {"operation": "import-legacy-current", "resource_id": RESOURCE, "scope": "workspace"}),
])
def test_resource_normalization_keeps_its_separate_native_digest_profile(operation, parameters, normalized):
    assert native_command_request(operation, {**parameters, "idempotency_key": KEY}) == {
        "table": "global_resource_receipts", "operation": normalized["operation"],
        "key": KEY, "request_digest": content_digest(normalized),
    }


def test_application_origin_does_not_collapse_optional_shape_or_consumer_scope():
    parameters = {"session_id": SESSION, "expected_revision": 1, "idempotency_key": KEY}
    explicit = {**parameters, "inputs": {}}
    assert native_command_request("run.start", parameters) == native_command_request("run.start", explicit)
    assert application_command_digest("run.start", parameters) != application_command_digest("run.start", explicit)
    management = application_command_identity("run.start", "management", parameters)
    consumer = application_command_identity("consumer.run.start", "consumer", parameters)
    assert management["native_receipt"] == consumer["native_receipt"]
    assert management["request_sha256"] != consumer["request_sha256"]
    assert application_identity_key("run.start", "management", KEY) != application_identity_key(
        "consumer.run.start", "consumer", KEY)


class Connection:
    in_transaction = True

    def __init__(self):
        self.saved = []

    def execute(self, sql, values):
        self.saved.append((sql, values))


def write_origin(connection, *, operation="graph.run.start", key=KEY, request_digest=None):
    parameters = {"session_id": SESSION, "expected_revision": 1, "idempotency_key": KEY}
    native = native_command_request("run.start", parameters)
    persist_application_command_identity(connection, table="idempotency", operation=operation, key=key,
        request_digest=native["request_digest"] if request_digest is None else request_digest)


def test_origin_hook_is_filtered_synchronous_and_resets_after_exception():
    connection = Connection()
    parameters = {"session_id": SESSION, "expected_revision": 1, "idempotency_key": KEY}
    with pytest.raises(RuntimeError):
        with bind_application_command("run.start", "management", parameters):
            write_origin(connection, operation="graph.run.begin")
            write_origin(connection, key="internal-key")
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(write_origin, connection).result()
            assert connection.saved == []
            write_origin(connection)
            raise RuntimeError("leave synchronous command")
    write_origin(connection)
    assert len(connection.saved) == 1
    values = connection.saved[0][1]
    assert values[0] == APPLICATION_IDENTITY_OPERATION
    assert values[1] == application_identity_key("run.start", "management", KEY)
    assert values[2] == values[5] == "workflow-op-v1:" + application_command_digest("run.start", parameters)
    assert values[3] == "[]"
    assert loads_strict(values[4]) == [{"application_identity": application_command_identity(
        "run.start", "management", parameters)}]


def test_origin_hook_rejects_a_matching_native_write_with_another_digest_or_no_transaction():
    connection = Connection()
    parameters = {"session_id": SESSION, "expected_revision": 1, "idempotency_key": KEY}
    with bind_application_command("run.start", "management", parameters):
        with pytest.raises(ContractValidationError):
            write_origin(connection, request_digest=fingerprint({"another": "request"}))
        connection.in_transaction = False
        with pytest.raises(ContractValidationError):
            write_origin(connection)
    assert connection.saved == []


def test_a_nonreceipt_nested_command_cannot_reuse_its_callers_origin():
    connection = Connection()
    parameters = {"session_id": SESSION, "expected_revision": 1, "idempotency_key": KEY}
    with bind_application_command("run.start", "management", parameters):
        with bind_application_command("packages.configure", "management", {"enabled_packages": {}}):
            write_origin(connection)
        assert connection.saved == []
        write_origin(connection)
    assert len(connection.saved) == 1
