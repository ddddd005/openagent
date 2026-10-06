"""Intercept the existing private archive query before any coordinator starts."""

import re

from .graph_application_contracts import QUERIES, validate_parameters
from .graph_archives import read_graph_archive
from .graph_records import require, uuid_value


_ARCHIVE_QUERY = next(spec for spec in QUERIES if spec.name == "archive.read")


def dispatch_archive_read(database_for_read, method, path, data=None):
    route = re.fullmatch(r"/api/graph/sessions/([^/]+)/archives/([^/]+)", path)
    if route is not None:
        require(method == "GET", "not_found", "Archive route not found", 404)
        require(uuid_value(route[1]) and uuid_value(route[2]), "not_found", "Archive not found", 404)
        parameters = {"session_id": route[1], "archive_id": route[2]}
    elif method == "POST" and path in ("/api/graph/queries", "/api/graph/consumer/queries") \
            and type(data) is dict and data.get("operation") == "archive.read":
        require(set(data) == {"operation", "parameters"} and type(data["parameters"]) is dict,
                "invalid_request", "Application request fields differ")
        require(path == "/api/graph/queries",
                "application_scope_denied", "Operation requires trusted management scope", 403)
        parameters = validate_parameters(_ARCHIVE_QUERY, data["parameters"])
    else:
        return None
    return 200, read_graph_archive(database_for_read(), parameters["session_id"], parameters["archive_id"])
