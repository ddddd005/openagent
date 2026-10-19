"""Backend-owned display snapshots of an exact frontend node's stored item."""

from copy import deepcopy

from .chat_contracts import CHAT_CONTEXT_TYPE, validate_chat_context
from .frontend_node_contracts import FRONTEND_NODE_COMPONENT
from .graph_records import require, uuid_value


class GraphFrontendNodes:
    def _frontend_snapshot_scope(self, data, workflow_definition_id, definition_revision,
                                 node_binding_id, session_id):
        require(uuid_value(workflow_definition_id) and uuid_value(node_binding_id)
                and uuid_value(session_id) and type(definition_revision) is int
                and definition_revision >= 1,
                "frontend_node_invalid_binding", "An exact frontend node and session are required")
        session, document = self._consumer_scope(
            data, session_id, workflow_definition_id, definition_revision)
        self._require_session_active_in(data.records, session)
        node = next((item for item in document["nodes"]
                     if item["node_binding_id"] == node_binding_id), None)
        require(node is not None
                and (node["component_id"], node["component_version"]) == FRONTEND_NODE_COMPONENT,
                "frontend_node_binding_mismatch", "Binding must identify the exact frontend.node@2", 409)
        entry = data.registry.get(node["component_id"], node["component_version"])
        require(entry is not None, "graph_missing_node_type",
                "Frontend node implementation is unavailable", 409)
        data.registry.validate_config(entry, node["config"])
        return node

    def _read_frontend_snapshot(self, data, workflow_definition_id, definition_revision,
                                node_binding_id, session_id):
        node = self._frontend_snapshot_scope(
            data, workflow_definition_id, definition_revision, node_binding_id, session_id)
        key = node["config"]["object_key"]
        record = data.state.read_object(
            session_id, key, node_id=node_binding_id)
        require((record["type_id"], record["schema_version"]) == (CHAT_CONTEXT_TYPE, 1)
                and record["binding"].get("registrar_node_id") == node_binding_id,
                "frontend_node_registration_mismatch",
                "Frontend snapshot must read the node's own registered item", 409)
        owner = {
            "workflow_definition_id": workflow_definition_id,
            "definition_revision": definition_revision, "node_binding_id": node_binding_id,
            "workflow_session_id": session_id, "object_key": key,
        }
        basis = {"revision": record["revision"], "revision_id": record["revision_id"]}
        snapshot = {
            "schema_version": 2, "kind": "workflow.frontend-node-snapshot",
            "owner": owner, "basis": basis, "chat": validate_chat_context(record["value"]),
        }
        return self._publish_frontend_snapshot(snapshot)

    def _publish_frontend_snapshot(self, snapshot):
        owner = snapshot["owner"]
        self._frontend_node_snapshots[
            (owner["workflow_definition_id"], owner["node_binding_id"],
             owner["workflow_session_id"])] = deepcopy(snapshot)
        connections = getattr(self, "_frontend_node_connections", None)
        if connections is not None:
            connections.publish_snapshot(snapshot)
        return deepcopy(snapshot)

    def read_frontend_node_snapshot(self, workflow_definition_id, definition_revision,
                                    node_binding_id, session_id):
        """Read actual stored data, then replace the node's temporary display cache."""
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            self._check_open()
            return self._read_frontend_snapshot(
                data, workflow_definition_id, definition_revision,
                node_binding_id, session_id)

    def get_cached_frontend_node_snapshot(self, workflow_definition_id, definition_revision,
                                          node_binding_id, session_id):
        """Observe the last explicit read without refreshing it from current data."""
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            self._check_open()
            self._frontend_snapshot_scope(
                data, workflow_definition_id, definition_revision,
                node_binding_id, session_id)
            return deepcopy(self._frontend_node_snapshots.get(
                (workflow_definition_id, node_binding_id, session_id)))

    def _accept_frontend_node_output(self, sid, chain_id, event):
        if event["event"] != "succeeded":
            return
        basis = self._execution_registries.get(chain_id)
        registry = basis.registry if basis is not None else self.registry
        with self._lock, self._data_management.read_execution(
                registry=registry) as data:
            chain = data.records.chain(chain_id)
            document = data.configuration.fixed_document(
                chain["workflow_definition_id"], chain["definition_revision"])
            node = next(item for item in document["nodes"]
                        if item["node_binding_id"] == event["node_binding_id"])
            if (node["component_id"], node["component_version"]) != FRONTEND_NODE_COMPONENT:
                return
            run = data.records.node_run(event["node_run_id"])
            require(event.get("settlement_committed") is True
                    and run["status"] == "succeeded"
                    and run["workflow_session_id"] == sid
                    and run["chain_run_id"] == chain_id
                    and run["node_binding_id"] == node["node_binding_id"]
                    and run["output_refs"] == event["output_refs"],
                    "frontend_node_result_unaccepted",
                    "Frontend snapshot refresh requires its actual committed invocation", 409)
            self._read_frontend_snapshot(
                data, chain["workflow_definition_id"], chain["definition_revision"],
                node["node_binding_id"], sid)

    def _frontend_node_state_changed(self, sid):
        connections = getattr(self, "_frontend_node_connections", None)
        if connections is None:
            return
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            session = data.records.session(sid)
            owner = (session["workflow_definition_id"], session["definition_revision"], sid)
        connections.state_changed(*owner)
