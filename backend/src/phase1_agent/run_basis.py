"""One published capability load result bound to one workflow invocation.

The value is deliberately small and transient: it carries the frozen registry
that execution uses and the definition query built over that same registry and
the manifests published with it.  It is not persisted and does not become a
second definition authority.
"""

from dataclasses import dataclass

from .graph_contracts import NodeRegistry
from .workflow_definitions import WorkflowDefinitionCatalog


@dataclass(frozen=True)
class RunBasis:
    registry: NodeRegistry
    definitions: WorkflowDefinitionCatalog

    def __post_init__(self):
        if not isinstance(self.registry, NodeRegistry):
            raise TypeError("A run basis requires the trusted node registry")
        if not getattr(self.registry, "_frozen", False):
            raise ValueError("A run basis requires the frozen execution registry")
        if self.definitions.registry is not self.registry:
            raise ValueError("A run basis requires one definition query over its own registry")
