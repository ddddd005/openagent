"""One internal classification source for application commands and HTTP selection."""

from types import SimpleNamespace

from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import (
    COMMANDS, LIFECYCLE_COMMANDS, MANAGEMENT_COMMANDS, MANAGEMENT_OPERATION_PREFIXES,
    QUERIES, application_operation_authority,
)
from phase1_agent.server import create_server, management_request
from phase1_agent.workflow_host import WorkflowHost


LIFECYCLE = {
    "definition.archive", "definition.delete", "session.archive", "session.delete",
}
MANAGEMENT = {
    "request-evidence.write", "request-evidence.resolve", "content.metadata.update",
    "session.metadata.update", "provider.credential.save", "credential.delete",
    "definition.update", "definition.copy", "session.create", "checkpoint.fork", "object.write",
    "resource.save", "resource.delete",
}
EXECUTION = {"run.start", "event.submit", "run.control", "packages.configure"}
PREFIXES = ("provider.", "credential.", "resource.", "content.metadata.", "request-evidence.")


def _command_authorities():
    return {spec.name: application_operation_authority(spec.name) for spec in COMMANDS}


def test_golden_classification_for_every_current_command():
    assert _command_authorities() == {
        "request-evidence.write": "management",
        "request-evidence.resolve": "management",
        "content.metadata.update": "management",
        "session.metadata.update": "management",
        "provider.credential.save": "management",
        "credential.delete": "management",
        "definition.update": "management",
        "definition.copy": "management",
        "definition.archive": "lifecycle",
        "definition.delete": "lifecycle",
        "session.create": "management",
        "session.archive": "lifecycle",
        "session.delete": "lifecycle",
        "run.start": "execution",
        "event.submit": "execution",
        "run.control": "execution",
        "checkpoint.fork": "management",
        "object.write": "management",
        "resource.save": "management",
        "resource.delete": "management",
        "packages.configure": "execution",
    }
    assert {name for name, authority in _command_authorities().items()
            if authority == "execution"} == EXECUTION
    assert {name for name, authority in _command_authorities().items()
            if authority == "management"} == MANAGEMENT
    assert {name for name, authority in _command_authorities().items()
            if authority == "lifecycle"} == LIFECYCLE
    assert LIFECYCLE_COMMANDS == LIFECYCLE
    assert MANAGEMENT_COMMANDS == {
        "definition.update", "definition.copy", "session.create", "object.write",
        "session.metadata.update", "checkpoint.fork",
    }
    assert MANAGEMENT_OPERATION_PREFIXES == PREFIXES


def test_queries_and_unregistered_or_retired_names_stay_management():
    assert {application_operation_authority(spec.name) for spec in QUERIES} == {"management"}
    for name in ("definition.save", "draft.save", "consumer.draft.save", "session.copy",
                 "context.adopt", "candidate.select", "not.registered", "", "definition.read"):
        assert application_operation_authority(name) == "management", name
    for name in (None, 1, True, ["run.start"]):
        assert application_operation_authority(name) == "management", name


def test_application_dispatch_uses_the_same_authority_for_each_service():
    authorities = SimpleNamespace(graph_service=object(), graph_management_service=object())
    app = GraphApplication(authorities)
    for spec in COMMANDS:
        target = app._invocation_service(app._commands[spec.name])
        authority = application_operation_authority(spec.name)
        expected = (authorities if authority == "lifecycle"
                    else authorities.graph_management_service if authority == "management"
                    else authorities.graph_service)
        assert target is expected, spec.name
    for spec in QUERIES:
        assert app._invocation_service(app._queries[spec.name]) is authorities.graph_management_service


def test_http_management_selection_matches_the_same_authority():
    for spec in COMMANDS:
        data = {"operation": spec.name, "parameters": {}}
        expected = application_operation_authority(spec.name) != "execution"
        assert management_request("POST", "/api/graph/commands", data) is expected, spec.name
        # Consumer entry points are management-selected first and then scope-denied,
        # independently of the command authority.
        assert management_request("POST", "/api/graph/consumer/commands", data) is True, spec.name
    for spec in QUERIES:
        data = {"operation": spec.name, "parameters": {}}
        assert management_request("POST", "/api/graph/queries", data) is True
        assert management_request("POST", "/api/graph/consumer/queries", data) is True
    for name in ("definition.save", "draft.save", "context.adopt", "not.registered"):
        assert management_request("POST", "/api/graph/commands",
                                  {"operation": name, "parameters": {}}) is True
    for data in (None, {}, {"operation": "run.start"},
                 {"operation": "run.start", "parameters": {}, "scope": "management"},
                 {"operation": 1, "parameters": {}}, {"operation": ["run.start"], "parameters": {}}):
        assert management_request("POST", "/api/graph/commands", data) is True
    for path in ("/api/graph/resources/read", "/api/graph/resources/list",
                 "/api/graph/resources/save", "/api/graph/resources/delete"):
        assert management_request("POST", path, {}) is True


def test_classification_source_is_not_a_public_discovery_contract():
    app = GraphApplication(SimpleNamespace(graph_service=None, graph_management_service=None))
    described = app.describe()
    assert set(described) == {"schema_version", "kind", "scope", "boundary", "commands", "queries"}
    fields = {"name", "kind", "scope", "required_fields", "optional_fields",
              "idempotency", "idempotency_key_format", "http_status"}
    assert all(set(item) == fields for item in described["commands"] + described["queries"])
    text = repr(described)
    for token in ("LIFECYCLE_COMMANDS", "MANAGEMENT_COMMANDS", "application_operation_authority",
                  "MANAGEMENT_OPERATION_PREFIXES"):
        assert token not in text
    assert "authority" not in {key for item in described["commands"] + described["queries"]
                               for key in item}


class MockHTTPServer:
    def __init__(self, address, handler):
        self.server_address = address
        self.RequestHandlerClass = handler

    def server_close(self):
        pass


def _handler_dispatch(server, responses, path, data):
    handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
    handler.command, handler.path, handler.server = "POST", path, server
    handler._validate_peer = lambda **kwargs: None
    handler._read_json = lambda maximum: data
    handler._json = lambda status, result: responses.append((status, result))
    handler._error = lambda status, code, message, *args, **kwargs: responses.append(
        (status, {"error": code, "message": message}))
    handler._dispatch("POST")
    return responses[-1]


def test_http_management_requests_do_not_initialize_execution_authority(tmp_path, monkeypatch):
    import phase1_agent.server as transport

    monkeypatch.setattr(transport, "LocalHTTPServer", MockHTTPServer)
    host = WorkflowHost(tmp_path / "classification.sqlite")
    server = transport.create_server(host, port=0)
    responses = []
    try:
        for operation in ("definition.save", "not.registered"):
            status, body = _handler_dispatch(server, responses, "/api/graph/commands", {
                "operation": operation, "parameters": {}})
            assert status == 404 and body["error"] == "not_found", operation
            assert host._graph is None
        status, listed = _handler_dispatch(server, responses, "/api/graph/queries", {
            "operation": "draft.list", "parameters": {"kind": "workbench"}})
        assert status == 200 and listed["records"] == []
        assert host._graph is None
        manager = host._management_graph
        assert manager is not None and manager._execution_owner is False
        assert manager._lease is None and manager._executor is None
        for path in ("/api/graph/resources/read", "/api/graph/resources/list",
                     "/api/graph/resources/save", "/api/graph/resources/delete"):
            status, body = _handler_dispatch(server, responses, path, {})
            assert status == 404 and body["error"] == "not_found", path
            assert host._graph is None
    finally:
        server.server_close()
        host.close()
