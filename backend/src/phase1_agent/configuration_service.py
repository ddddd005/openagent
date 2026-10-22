"""Configuration functional interface: accept, shape-check and dispatch.

Both production sources enter through this module: the workbench command and
the frontend-node narrow patch.  The interface keeps each source's original
request, outer transaction and receipt identity; it does not own configuration
rules or storage details.
"""

from __future__ import annotations

from .contract_errors import require, uuid_value


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
            service._check_open()
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
            service._check_open()
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

    def definition_summaries(self, *, include_archived=False):
        """Narrow formal-definition directory rows in one consistent read."""
        require(type(include_archived) is bool, "invalid_request",
                "Archive filter must be boolean")
        service = self._service
        with service._lock:
            service._check_open()
            with service._data_management.read_directory() as directory:
                return directory.definition_summaries(
                    include_archived=include_archived)

    def session_summaries(self, workflow_definition_id, *, include_archived=False):
        """Narrow formal-session directory rows of one definition."""
        require(uuid_value(workflow_definition_id), "invalid_request",
                "Graph identity must be canonical UUID4")
        require(type(include_archived) is bool, "invalid_request",
                "Archive filter must be boolean")
        service = self._service
        with service._lock:
            service._check_open()
            with service._data_management.read_directory() as directory:
                return directory.session_summaries(
                    workflow_definition_id, include_archived=include_archived)

    def copy_definition(self, *, source_workflow_definition_id,
                        expected_source_definition_revision,
                        expected_source_edit_sequence,
                        target_workflow_definition_id, target_name,
                        idempotency_key):
        """Dispatch one formal configuration copy through the shared transaction.

        The original request is exactly the five caller-owned fields; the native
        receipt hashes them before the source is read, so a replay returns the
        persisted result without re-reading source or target.
        """
        service = self._service
        request = {
            "source_workflow_definition_id": source_workflow_definition_id,
            "expected_source_definition_revision": expected_source_definition_revision,
            "expected_source_edit_sequence": expected_source_edit_sequence,
            "target_workflow_definition_id": target_workflow_definition_id,
            "target_name": target_name,
        }
        return service._configuration_change(
            "graph.definition.copy", idempotency_key, request,
            lambda data: service.copy_configuration(
                data,
                source_workflow_definition_id=source_workflow_definition_id,
                expected_source_definition_revision=expected_source_definition_revision,
                expected_source_edit_sequence=expected_source_edit_sequence,
                target_workflow_definition_id=target_workflow_definition_id,
                target_name=target_name))
