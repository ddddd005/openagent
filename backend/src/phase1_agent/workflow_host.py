"""Lazily expose independent management and the graph execution coordinator."""

from pathlib import Path
from threading import RLock


class WorkflowHost:
    def __init__(self, database_path):
        self.database = Path(database_path).resolve()
        self._lock = RLock()
        self._graph = None
        self._providers = None
        self._content = None
        self._evidence = None

    @property
    def provider_configuration(self):
        with self._lock:
            if self._providers is None:
                from .graph_provider_host import ProviderConfigurationService
                self._providers = ProviderConfigurationService(self.database)
            return self._providers

    @property
    def content_configuration(self):
        with self._lock:
            if self._content is None:
                from .content_configuration import ContentConfigurationService
                self._content = ContentConfigurationService(self.database)
            return self._content

    @property
    def request_evidence(self):
        with self._lock:
            if self._evidence is None:
                from .request_evidence import RequestEvidenceService
                self._evidence = RequestEvidenceService(self.database)
            return self._evidence

    def get_global_resource(self, identity):
        from .content_configuration import global_resource_service
        from .host_sdk import ResourceIdentity
        identity = ResourceIdentity.from_dict(identity).to_dict()
        return global_resource_service(self, identity["type_id"]).get_global_resource(identity)

    def list_global_resources(self, *, scope=None, type_id=None):
        from .content_configuration import list_managed_global_resources
        return list_managed_global_resources(self, scope=scope, type_id=type_id)

    def save_global_resource(self, record, *, expected_sequence, idempotency_key):
        from .content_configuration import global_resource_service
        from .contract_errors import require
        require(type(record) is dict, "global_resource_invalid", "Global resource envelope is invalid")
        return global_resource_service(self, record.get("type_id")).save_global_resource(
            record, expected_sequence=expected_sequence, idempotency_key=idempotency_key)

    def delete_global_resource(self, identity, *, expected_sequence, idempotency_key):
        from .content_configuration import global_resource_service
        from .host_sdk import ResourceIdentity
        identity = ResourceIdentity.from_dict(identity).to_dict()
        return global_resource_service(self, identity["type_id"]).delete_global_resource(
            identity, expected_sequence=expected_sequence, idempotency_key=idempotency_key)

    @property
    def graph_service(self):
        with self._lock:
            if self._graph is None:
                from .graph_service import GraphWorkflowService
                self._graph = GraphWorkflowService(self.database)
            return self._graph

    def close(self):
        with self._lock:
            if self._graph is not None:
                self._graph.close()
