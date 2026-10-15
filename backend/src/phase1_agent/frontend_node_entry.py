"""Exact addresses declared by the installed frontend node implementation."""

import re

from .contract_errors import require
from .frontend_node_channel import _owner
from .frontend_node_operations import FrontendNodeOperations


FRONTEND_NODE_ENTRY = {
    "schema_version": 1, "component_id": "frontend.node", "component_version": "2",
    "page_prefix": "/frontend-node/", "api_prefix": "/api/graph/frontend-nodes/",
    "channel_suffix": "channel", "events_suffix": "events", "entry_suffix": "entry",
}
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_ADDRESS = rf"({_UUID})/([1-9][0-9]{{0,15}})/({_UUID})/({_UUID}|none)"


def frontend_node_route(path, *, page=False):
    prefix = FRONTEND_NODE_ENTRY["page_prefix" if page else "api_prefix"]
    if not path.startswith(prefix):
        return None
    tail = "/" if page else r"/(entry|channel|events)"
    match = re.fullmatch(re.escape(prefix) + _ADDRESS + tail, path)
    require(match is not None, "not_found", "Frontend node address was not found", 404)
    owner = _owner({
        "workflow_definition_id": match[1], "definition_revision": int(match[2]),
        "node_binding_id": match[3], "session_id": None if match[4] == "none" else match[4],
    })
    return owner, "page" if page else match[5]


def frontend_node_entry(service, owner, origin):
    owner = _owner(owner)
    require(type(origin) is str
            and re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[1-9][0-9]{0,4}", origin),
            "frontend_node_invalid_origin", "Frontend entry requires the actual local HTTP origin")
    FrontendNodeOperations(service).bind(**owner)
    coordinates = "/".join(str(owner[field]) for field in (
        "workflow_definition_id", "definition_revision", "node_binding_id",
    )) + "/" + (owner["session_id"] or "none")
    api = origin + FRONTEND_NODE_ENTRY["api_prefix"] + coordinates + "/"
    return {
        "schema_version": 1, "kind": "workflow.frontend-node-entry", "owner": owner,
        "page_url": origin + FRONTEND_NODE_ENTRY["page_prefix"] + coordinates + "/",
        "channel_url": api + FRONTEND_NODE_ENTRY["channel_suffix"],
        "events_url": api + FRONTEND_NODE_ENTRY["events_suffix"],
    }
