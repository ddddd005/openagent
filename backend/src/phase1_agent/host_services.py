"""Run-scoped service instances, with frozen operation grants and JSON boundaries."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError

from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import HostContractError, ServiceReference, ensure


@dataclass(frozen=True)
class HostServiceEnvironment:
    """Controlled host primitives; no graph service, mutable store or kernel memory."""

    resolve_input: Callable
    resolve_artifact: Callable
    accept_fact: Callable
    read_resource: Callable
    options: dict


class HostServiceRun:
    def __init__(self, registry, *, workflow_session_id, chain_run_id, nodes, definitions):
        self.registry = registry.detached(frozen=True)
        self.workflow_session_id, self.chain_run_id = workflow_session_id, chain_run_id
        self.nodes = deepcopy(nodes)
        self.definitions = deepcopy(definitions)
        self.instances = {}
        self.routes = {node_id: self.registry.grants(definition.service_requirements, definition.capabilities)
                       for node_id, definition in self.definitions.items()}
        self.cleanup_errors = {}
        self.released = False

    def prepare(self, environment_factory, resources):
        references = {reference for routes in self.routes.values() for reference, _ in routes.values()}
        try:
            for reference in sorted(references, key=lambda ref: (ref.service_id, ref.exact_version)):
                entry = self.registry.get(reference)
                node_ids = {node_id for node_id, routes in self.routes.items()
                            if any(ref == reference for ref, _ in routes.values())}
                nodes = [{**deepcopy(self.nodes[node_id]),
                          "service_requirements": deepcopy(list(self.definitions[node_id].service_requirements))}
                         for node_id in self.nodes if node_id in node_ids]
                records = {node_id: deepcopy(resources.get(node_id, [])) for node_id in node_ids}
                instance = entry.factory(environment_factory(reference, records))
                self.instances[reference] = instance
                ensure(callable(instance), "host_invalid_service_instance", "Service instance must be callable")
                hook = getattr(instance, "prepare_run", None)
                if hook is not None:
                    ensure(callable(hook), "host_invalid_service_instance", "Service prepare hook must be callable")
                    hook(workflow_session_id=self.workflow_session_id, chain_run_id=self.chain_run_id,
                         nodes=nodes, resources=records)
        except BaseException:
            self.release()
            raise

    def require_context(self, context):
        ensure(context.workflow_session_id == self.workflow_session_id
               and context.chain_run_id == self.chain_run_id
               and context.node_binding_id in self.definitions,
               "host_service_owner_mismatch", "Service context is outside this frozen run")
        return self.routes[context.node_binding_id]

    @staticmethod
    def _check(value, schema, code):
        validate_json_value(value)
        ensure(len(canonical_bytes(value)) <= 4_000_000, code, "Service value exceeds the host limit")
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            raise HostContractError(code, "Service value differs from its declared schema") from exc
        return deepcopy(value)

    def call(self, context, capability, operation, payload):
        ensure(not self.released, "host_service_unavailable", "Run service has been released")
        routes = self.require_context(context)
        ensure(capability in self.definitions[context.node_binding_id].capabilities,
               "graph_capability_denied", "Node did not declare this capability")
        ensure((capability, operation) in routes, "host_service_operation_denied",
               "Node did not declare this exact service operation")
        reference, declaration = routes[capability, operation]
        ensure(reference in self.instances, "host_service_unavailable", "Run service instance is unavailable")
        request = self._check(payload, declaration.request_schema, "host_service_request_invalid")
        response = self.instances[reference](context, capability, operation, request)
        return self._check(response, declaration.response_schema, "host_service_response_invalid")

    def owns_capability(self, context, capability):
        routes = self.require_context(context)
        return any(key[0] == capability for key in routes)

    def accept_outputs(self, event):
        for reference, instance in self.instances.items():
            routes = self.routes.get(event["node_binding_id"], {})
            if not any(ref == reference for ref, _ in routes.values()):
                continue
            hook = getattr(instance, "accept_outputs", None)
            if hook is not None:
                ensure(callable(hook), "host_invalid_service_instance", "Service output hook must be callable")
                hook(deepcopy(event))

    def release(self):
        """Attempt every release; retain failures for an explicit cleanup retry."""
        self.released = True
        for reference, instance in list(self.instances.items()):
            try:
                hook = getattr(instance, "release_run", None)
                if hook is not None:
                    ensure(callable(hook), "host_invalid_service_instance", "Service release hook must be callable")
                    hook(self.workflow_session_id, self.chain_run_id)
                dispose = getattr(instance, "dispose", None)
                if dispose is not None:
                    ensure(callable(dispose), "host_invalid_service_instance", "Service dispose hook must be callable")
                    dispose()
            except Exception:
                self.cleanup_errors[reference] = "host_service_release_failed"
            else:
                self.cleanup_errors.pop(reference, None)
                self.instances.pop(reference)
        return not self.instances
