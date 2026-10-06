"""Own separate graph and legacy coordinators without initializing unused Agents."""

from pathlib import Path
from threading import RLock


_RESOURCE_METHODS = frozenset({
    "list_model_providers", "get_model_configuration", "save_model_configuration", "diagnose_model_configuration",
    "list_global_content", "resolve_global_content", "save_global_content", "delete_global_content",
    "register_session_data", "list_session_data_definitions",
})


class WorkflowHost:
    def __init__(self, database_path, mode="offline"):
        self.database = Path(database_path).resolve()
        self.mode = mode
        self._lock = RLock()
        self._graph = None
        self._legacy = None

    @property
    def graph_service(self):
        with self._lock:
            if self._graph is None:
                from .graph_service import GraphWorkflowService
                self._graph = GraphWorkflowService(self.database)
            return self._graph

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _RESOURCE_METHODS:
            return getattr(self.graph_service, name)
        with self._lock:
            if self._legacy is None:
                from .workflow import WorkflowService
                self._legacy = WorkflowService(database_path=self.database, mode=self.mode)
            return getattr(self._legacy, name)

    def close(self):
        with self._lock:
            if self._graph is not None:
                self._graph.close()
            if self._legacy is not None:
                self._legacy.close()
