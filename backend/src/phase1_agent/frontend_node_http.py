"""HTTP ownership adapter for the frontend node's private channel."""

from .contract_errors import require
from .frontend_node_channel import FrontendNodeChannel, _owner
from .frontend_node_entry import frontend_node_entry
from .frontend_node_operations import FrontendNodeOperations


class FrontendNodeHTTP:
    def __init__(self, service, connections):
        self._service = service
        self.connections = connections

    def entry(self, owner, origin):
        return frontend_node_entry(self._service(False), owner, origin)

    def channel(self, owner, request):
        owner = _owner(owner)
        require(type(request) is dict and _owner(request.get("owner")) == owner,
                "frontend_node_owner_mismatch", "Request owner differs from its node address", 409)
        service = self._service(request.get("operation") in ("start", "pause", "resume", "close", "reroll"))
        return FrontendNodeChannel(service, owner).handle(request)

    def subscribe(self, owner):
        owner = _owner(owner)
        service = self._service(False)
        FrontendNodeOperations(service).bind(**owner)
        connection = self.connections.attach(owner)
        try:
            if owner["session_id"] is not None:
                service.read_frontend_node_snapshot(
                    owner["workflow_definition_id"], owner["definition_revision"],
                    owner["node_binding_id"], owner["session_id"])
        except BaseException:
            self.connections.detach(connection)
            raise
        return connection
