"""HTTP transport adapter for the transport-independent graph application."""

import re

from .graph_application import GraphApplication
from .graph_records import require, uuid_value


def _application(service, *, consumer=False):
    if isinstance(service, GraphApplication):
        return service.for_consumer() if consumer else service
    return GraphApplication(service, scope="consumer" if consumer else "management")


def _command(application, name, data, session_id=None):
    require(type(data) is dict, "invalid_request", "Graph request must be an object")
    require(session_id is None or "session_id" not in data,
            "invalid_request", "Session identity is supplied by the route")
    parameters = {**data, **({"session_id": session_id} if session_id is not None else {})}
    return application.command_status(name), application.command(name, parameters)["result"]


def _query(application, name, data=None, session_id=None):
    require(data is None or type(data) is dict, "invalid_request", "Graph request must be an object")
    require(session_id is None or data is None or "session_id" not in data,
            "invalid_request", "Session identity is supplied by the route")
    parameters = {**(data or {}), **({"session_id": session_id} if session_id is not None else {})}
    return 200, application.query(name, parameters)


def dispatch_platform(service, method, path, data):
    application = _application(service)
    prefix = "/api/graph"
    if method == "GET" and path == prefix + "/platform":
        return _query(application, "platform")
    global_routes = {
        "/resources/read": ("query", "resource.read"),
        "/resources/list": ("query", "resource.list"),
        "/resources/save": ("command", "resource.save"),
        "/resources/delete": ("command", "resource.delete"),
        "/packages/configure": ("command", "packages.configure"),
        "/registrations/list": ("query", "registration.list"),
    }
    suffix = path[len(prefix):] if path.startswith(prefix) else None
    if suffix in global_routes:
        require(method == "POST" and type(data) is dict, "invalid_request", "Platform request fields differ")
        kind, name = global_routes[suffix]
        return (_query if kind == "query" else _command)(application, name, data)
    match = re.fullmatch(prefix + r"/sessions/([^/]+)/(objects|objects/read|objects/write|objects/artifacts/read|information/read|manifests/read|context/adopt)", path)
    if match is None:
        return None
    require(uuid_value(match[1]), "not_found", "Session not found", 404)
    if method == "GET" and match[2] == "objects":
        return _query(application, "object.list", session_id=match[1])
    routes = {"objects/read": ("query", "object.read"),
              "objects/write": ("command", "object.write"),
              "objects/artifacts/read": ("query", "object.artifact.read"),
              "information/read": ("query", "information.read"),
              "manifests/read": ("query", "manifest.read"),
              "context/adopt": ("command", "context.adopt")}
    require(method == "POST" and match[2] in routes, "not_found", "Platform route not found", 404)
    require(type(data) is dict, "invalid_request", "Platform request fields differ")
    kind, name = routes[match[2]]
    return (_query if kind == "query" else _command)(application, name, data, match[1])


def dispatch_graph(service, method, path, data=None):
    application = _application(service)
    prefix = "/api/graph"
    entry = re.fullmatch(prefix + r"/(?:(consumer)/)?(application|commands|queries)", path)
    if entry:
        scoped = _application(application, consumer=bool(entry[1]))
        if method == "GET" and entry[2] == "application":
            return 200, scoped.describe()
        require(method == "POST" and entry[2] in ("commands", "queries"),
                "not_found", "Application route not found", 404)
        require(type(data) is dict and set(data) == {"operation", "parameters"}
                and type(data["parameters"]) is dict, "invalid_request", "Application request fields differ")
        if entry[2] == "queries":
            return 200, scoped.query(data["operation"], data["parameters"])
        return scoped.command_status(data["operation"]), scoped.command(data["operation"], data["parameters"])
    platform = dispatch_platform(application, method, path, data)
    if platform is not None:
        return platform
    if method == "GET" and path in (prefix + "/node-types", prefix + "/node-types/v2"):
        return _query(application, "catalog.node-types", {"protocol_version": 2 if path.endswith("/v2") else 1})
    history = re.fullmatch(prefix + r"/sessions/([^/]+)/runs/([^/]+)", path)
    if method == "GET" and history:
        require(uuid_value(history[1]) and uuid_value(history[2]), "not_found", "Execution not found", 404)
        return _query(application, "run.read", {"chain_id": history[2]}, history[1])
    match = re.fullmatch(prefix + r"/definitions/([^/]+)(?:/revisions/([1-9][0-9]{0,15})|/(sessions|consumer|consumer-sessions))?", path)
    if method == "GET" and match:
        require(uuid_value(match[1]), "not_found", "Definition not found", 404)
        if match[3] == "consumer":
            return _query(application, "consumer.definition", {"identity": match[1]})
        if match[3] == "consumer-sessions":
            return _query(application, "consumer.sessions", {"identity": match[1]})
        if match[3] == "sessions":
            return _query(application, "definition.sessions", {"workflow_definition_id": match[1]})
        return _query(application, "definition.read",
                      {"identity": match[1], "revision": int(match[2]) if match[2] else None})
    match = re.fullmatch(prefix + r"/sessions/([^/]+)(?:/(runs|control|copy|rebind|data|consumer|consumer/runs|consumer/control|actions|consumer/actions|outputs/public|outputs/read|outputs/history|candidates|candidates/select|candidates/fork|events/submit|events/bindings|events/read|consumer/events/submit|consumer/events/bindings|consumer/events/read))?", path)
    if match:
        require(uuid_value(match[1]), "not_found", "Session not found", 404)
        reads = {None: "session.read", "consumer": "consumer.read", "outputs/public": "output.list",
                 "candidates": "candidate.list", "actions": "session.actions", "consumer/actions": "consumer.actions"}
        if method == "GET" and match[2] in reads:
            return _query(application, reads[match[2]], session_id=match[1])
    require(method == "POST", "not_found", "Graph route not found", 404)
    global_commands = {"/definitions": "definition.save", "/sessions": "session.create",
                       "/consumer/sessions": "consumer.session.create"}
    route = path[len(prefix):]
    if route in global_commands:
        return _command(application, global_commands[route], data)
    commands = {"runs": "run.start", "control": "run.control", "copy": "session.copy",
                "rebind": "session.rebind", "data": "session.data.update",
                "consumer/runs": "consumer.run.start", "consumer/control": "consumer.run.control",
                "events/submit": "event.submit", "consumer/events/submit": "consumer.event.submit",
                "candidates/select": "candidate.select", "candidates/fork": "candidate.fork"}
    queries = {"outputs/read": "output.read", "outputs/history": "output.history",
               "events/bindings": "event.bindings", "consumer/events/bindings": "consumer.event.bindings",
               "events/read": "event.read", "consumer/events/read": "consumer.event.read"}
    route = match[2] if match else None
    require(route in commands or route in queries, "not_found", "Graph route not found", 404)
    if route in queries:
        require(type(data) is dict, "invalid_request", "Graph request must be an object")
        return _query(application, queries[route], data, match[1])
    return _command(application, commands[route], data, match[1])
