"""Project base runtime: lazily expose management and the graph execution coordinator."""

from pathlib import Path
from threading import RLock

from .project_support import (
    EXECUTION_ROLE,
    MANAGEMENT_ROLE,
    ProjectDefinitionManagement,
    ProjectExecutionManagement,
    ProjectStorageEnvironment,
    require_matching_database,
)


class WorkflowHost:
    """Assemble, acquire and hold the project base runtime and workflow services.

    The host owns the storage environment shared by the workflow management and
    execution entries, the project definition management and the thin execution
    acquisition organization.  Service construction stays lazily inside this
    class; the host is the lifecycle owner of the services it creates.

    The host owns no workflow-internal construction path: on the first
    acquisition of a role it asks the project definition management for the
    declared trusted binding of the workflow source, re-checks the declared
    environment and then creates the service along that binding.  Direct
    construction of the workflow services by trusted callers is unchanged.

    The provider configuration / model profile and global content domains are
    also acquired through their own declared management bindings: their public
    management views are lazy, share the same project storage environment and
    never activate workflow execution.  The workflow source receives only the
    already-validated trusted factories of the two domains as narrow dependency
    parameters; the graph service keeps building its own domain views at its
    original construction time.

    ``capability_source`` is only a trusted-local composition/test injection
    seam for the workflow source itself; it is never a string entrypoint, module
    path or dynamic import, and an injected source still passes the same
    declaration validation and binding acquisition.
    """

    def __init__(self, database_path, *, environment=None, capability_source=None,
                 workflow_package_root=None):
        self.database = Path(database_path).resolve()
        if environment is None:
            environment = ProjectStorageEnvironment(self.database)
        else:
            require_matching_database(self.database, environment)
        self.environment = environment
        self.workflow_package_root = (Path(workflow_package_root).resolve()
                                      if workflow_package_root is not None
                                      else self.database.parent / "packages" / "workflow")
        self._workflow_packages = None
        self.definitions = ProjectDefinitionManagement(capability_source)
        self.execution_management = ProjectExecutionManagement(
            management_entry=lambda: self._management_dispatch_entry(),
            execution_entry=lambda: self.graph_service,
            lifecycle_entry=lambda: self._lifecycle_service(),
        )
        self._lock = RLock()
        self._graph = None
        self._management_graph = None
        self._providers = None
        self._content = None
        self._evidence = None

    # -- domain capability bindings ---------------------------------------

    def _provider_management_binding(self):
        from .provider_capability import CAPABILITY_ID

        return self.definitions.require_role_binding(
            MANAGEMENT_ROLE, database=self.database, environment=self.environment,
            capability_id=CAPABILITY_ID)

    def _content_management_binding(self):
        from .content_capability import CAPABILITY_ID

        return self.definitions.require_role_binding(
            MANAGEMENT_ROLE, database=self.database, environment=self.environment,
            capability_id=CAPABILITY_ID)

    @property
    def provider_configuration(self):
        with self._lock:
            if self._providers is None:
                binding = self._provider_management_binding()
                service = binding.factory(
                    database=self.database, environment=self.environment)
                if service is None:
                    from .contract_errors import DiagnosticError
                    raise DiagnosticError(
                        "capability_binding_invalid",
                        "Trusted provider access binding produced no service", status=409)
                self._providers = service
            return self._providers

    @property
    def content_configuration(self):
        with self._lock:
            if self._content is None:
                binding = self._content_management_binding()
                service = binding.factory(
                    database=self.database, environment=self.environment)
                if service is None:
                    from .contract_errors import DiagnosticError
                    raise DiagnosticError(
                        "capability_binding_invalid",
                        "Trusted content access binding produced no service", status=409)
                self._content = service
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

    def _acquire_role(self, role):
        """Create one workflow service along its declared trusted local binding.

        The two project domain sources are resolved through their own validated
        management bindings first and only their trusted factories are handed to
        the workflow source as narrow dependency parameters.
        """
        binding = self.definitions.require_role_binding(
            role, database=self.database, environment=self.environment)
        provider_factory = self._provider_management_binding().factory
        content_factory = self._content_management_binding().factory
        service = binding.factory(
            database=self.database, environment=self.environment,
            provider_configuration_factory=provider_factory,
            content_configuration_factory=content_factory,
            workflow_package_source=self._package_source())
        if service is None:
            from .contract_errors import DiagnosticError
            raise DiagnosticError("capability_binding_invalid",
                                  "Trusted workflow access binding produced no service", status=409)
        return service

    def _package_source(self):
        if self._workflow_packages is None:
            from .local_workflow_packages import LocalWorkflowPackageSource
            self._workflow_packages = LocalWorkflowPackageSource(self.workflow_package_root)
        return self._workflow_packages

    @property
    def graph_service(self):
        with self._lock:
            if self._graph is None:
                self._graph = self._acquire_role(EXECUTION_ROLE)
            return self._graph

    @property
    def graph_management_service(self):
        with self._lock:
            if self._graph is not None:
                return self._graph
            if self._management_graph is None:
                self._management_graph = self._acquire_role(MANAGEMENT_ROLE)
            return self._management_graph

    def _management_dispatch_entry(self):
        """Existing management dispatch target: the acquired execution entry or this host."""
        with self._lock:
            return self._graph if self._graph is not None else self

    def _check_application_operation(self, name, parameters):
        with self._lock:
            service = self._graph if self._graph is not None else self._management_graph
            if service is not None:
                service._check_application_operation(name, parameters)

    def _lifecycle_service(self):
        return self._graph if self._graph is not None else self.graph_management_service

    def archive_definition(self, identity, *, expected_revision, idempotency_key):
        with self._lock:
            return self._lifecycle_service().archive_definition(
                identity, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def delete_definition(self, identity, *, expected_revision, idempotency_key):
        with self._lock:
            return self._lifecycle_service().delete_definition(
                identity, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def archive_session(self, sid, *, expected_revision, idempotency_key):
        with self._lock:
            return self._lifecycle_service().archive_session(
                sid, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def delete_session(self, sid, *, expected_revision, idempotency_key):
        with self._lock:
            return self._lifecycle_service().delete_session(
                sid, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def close(self):
        with self._lock:
            if self._graph is not None:
                self._graph.close()
            if self._management_graph is not None and self._management_graph is not self._graph:
                self._management_graph.close()
