"""Configuration functional interface: accept, shape-check and dispatch.

Both production sources enter through this module: the workbench command and
the frontend-node narrow patch.  The interface keeps each source's original
request, outer transaction and receipt identity; it does not own configuration
rules or storage details.
"""

from __future__ import annotations

from copy import deepcopy

from .contract_errors import require


class WorkflowConfigurationService:
    """Functional entry for configuration commands and independent reads."""

    def __init__(self, service):
        self._service = service

    def update_definition(self, document, *, workflow_definition_id, expected_edit_sequence,
                          idempotency_key, confirm_derivation=None):
        service = self._service
        # The receipt hashes the original full request before any current graph
        # is read, so replay keeps the original result after later edits.
        request = {"workflow_definition_id": workflow_definition_id,
                   "expected_edit_sequence": expected_edit_sequence, "document": document}
        if confirm_derivation is not None:
            request["confirm_derivation"] = confirm_derivation
        return service._configuration_change(
            "graph.definition.update", idempotency_key, request,
            lambda data: service._save_configuration_update(
                data, document, workflow_definition_id=workflow_definition_id,
                expected_edit_sequence=expected_edit_sequence,
                confirm_derivation=confirm_derivation))

    def patch_node_configuration(self, *, workflow_definition_id, node_binding_id, component_id,
                                 component_version, changes, expected_edit_sequence,
                                 idempotency_key, confirm_derivation=None):
        service = self._service
        changes = deepcopy(changes)
        request = {
            "workflow_definition_id": workflow_definition_id, "node_binding_id": node_binding_id,
            "component_id": component_id, "component_version": component_version, "changes": changes,
            "expected_edit_sequence": expected_edit_sequence,
        }
        if confirm_derivation is not None:
            request["confirm_derivation"] = confirm_derivation

        def change(data):
            return service._patch_node_configuration(
                data, workflow_definition_id=workflow_definition_id,
                node_binding_id=node_binding_id, component_id=component_id,
                component_version=component_version, changes=changes,
                expected_edit_sequence=expected_edit_sequence,
                confirm_derivation=confirm_derivation)

        # The public narrow patch keeps its existing receipt operation and has
        # no production caller outside tests in this batch.
        return service._configuration_change(
            "graph.configuration.patch-node", idempotency_key, request, change)

    def patch_exact_configuration(self, data, *, workflow_definition_id, node_binding_id,
                                  component_id, component_version, changes,
                                  expected_edit_sequence, confirm_derivation=None):
        """Forward a narrow patch already inside the source's outer transaction."""
        return self._service._patch_node_configuration(
            data, workflow_definition_id=workflow_definition_id,
            node_binding_id=node_binding_id, component_id=component_id,
            component_version=component_version, changes=changes,
            expected_edit_sequence=expected_edit_sequence,
            confirm_derivation=confirm_derivation)

    def read_configuration(self, identity, revision=None):
        service = self._service
        with service._lock, service._configuration_data.read() as data:
            if revision is None:
                current, fixed = data.configuration_view(identity)
                if current is not None:
                    return service._compose_configuration(current, fixed)
            return data.fixed_document(identity, revision)

    def list_configuration_definitions(self, *, include_archived=False):
        require(type(include_archived) is bool, "invalid_request",
                "Archive filter must be boolean")
        service = self._service
        with service._lock, service._configuration_data.read() as data:
            latest = {}
            for row in data.all_definition_revisions():
                identity = row["workflow_definition_id"]
                if identity not in latest or row["revision"] > latest[identity]["revision"]:
                    latest[identity] = row
            result = []
            for identity, row in latest.items():
                archived = data.archived("definition", identity)
                if not include_archived and archived:
                    continue
                current, fixed = data.configuration_view(identity)
                result.append({
                    "document": (service._compose_configuration(current, fixed)
                                 if current is not None else row["document"]),
                    "archived": archived,
                })
            return result
