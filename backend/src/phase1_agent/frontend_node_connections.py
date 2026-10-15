"""Only frontend-node display connections, without business dispatch or replay."""

from copy import deepcopy
from threading import Condition, RLock


def _key(owner):
    return tuple(owner[field] for field in (
        "workflow_definition_id", "definition_revision", "node_binding_id", "session_id",
    ))


class FrontendNodeConnection:
    def __init__(self, owner):
        self.owner = deepcopy(owner)
        self._condition = Condition()
        self._pending = {}
        self._snapshot_revision_id = None
        self._closed = False

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def snapshot(self, value):
        with self._condition:
            revision = value["basis"]["revision_id"]
            if self._closed or revision == self._snapshot_revision_id:
                return
            self._snapshot_revision_id = revision
            self._pending["snapshot"] = deepcopy(value)
            self._condition.notify()

    def state_changed(self):
        with self._condition:
            if self._closed:
                return
            self._pending["state_changed"] = {
                "schema_version": 1, "kind": "workflow.frontend-node-state-changed",
                "owner": deepcopy(self.owner),
            }
            self._condition.notify()

    def next(self, timeout=1):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or bool(self._pending), timeout)
            if self._closed or not self._pending:
                return None
            name = next(iter(self._pending))
            return name, self._pending.pop(name)

    def close(self):
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()


class FrontendNodeConnections:
    def __init__(self):
        self._lock = RLock()
        self._connections = {}
        self._closed = False

    def attach(self, owner):
        connection = FrontendNodeConnection(owner)
        with self._lock:
            if self._closed:
                connection.close()
            else:
                self._connections.setdefault(_key(owner), set()).add(connection)
        return connection

    def detach(self, connection):
        with self._lock:
            key = _key(connection.owner)
            connections = self._connections.get(key)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    self._connections.pop(key)
        connection.close()

    def publish_snapshot(self, snapshot):
        address = snapshot["owner"]
        owner = {
            "workflow_definition_id": address["workflow_definition_id"],
            "definition_revision": address["definition_revision"], "node_binding_id": address["node_binding_id"],
            "session_id": address["workflow_session_id"],
        }
        with self._lock:
            connections = tuple(self._connections.get(_key(owner), ()))
        for connection in connections:
            connection.snapshot(snapshot)

    def state_changed(self, workflow_definition_id, definition_revision, session_id):
        with self._lock:
            connections = tuple(connection for owner, group in self._connections.items()
                                if (owner[0], owner[1], owner[3]) == (
                                    workflow_definition_id, definition_revision, session_id)
                                for connection in group)
        for connection in connections:
            connection.state_changed()

    def close(self):
        with self._lock:
            self._closed = True
            connections = tuple(connection for group in self._connections.values() for connection in group)
            self._connections.clear()
        for connection in connections:
            connection.close()

    @property
    def connection_count(self):
        with self._lock:
            return sum(len(group) for group in self._connections.values())
