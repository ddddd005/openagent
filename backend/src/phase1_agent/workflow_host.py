"""Lazily own the current graph coordinator."""

from pathlib import Path
from threading import RLock


class WorkflowHost:
    def __init__(self, database_path):
        self.database = Path(database_path).resolve()
        self._lock = RLock()
        self._graph = None

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
