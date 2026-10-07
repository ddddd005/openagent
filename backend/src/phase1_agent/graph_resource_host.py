"""Declared resource preflight and current-value execution frames."""

from copy import deepcopy

from .graph_records import graph_error, require


class GraphResourceHost:
    """Freeze authorized resources before accepting any graph effects."""

    def _content_dependencies(self, node, *, allow_inputs=False):
        entry = self.registry.get(node["component_id"], node["component_version"])
        callback = entry.resource_dependencies_declaration
        require(callback is not None or allow_inputs, "graph_resource_dependencies_undeclared",
                "Resource reads require a trusted dependency declaration", 409, node_id=node["node_binding_id"])
        if callback is None:
            return []
        try:
            dependencies = callback(deepcopy(node["config"]))
        except Exception as error:
            raise graph_error("graph_resource_dependency_invalid", "Resource dependency declaration failed", 409,
                              node_id=node["node_binding_id"]) from error
        require(type(dependencies) is list and len(dependencies) <= 128,
                "graph_resource_dependency_invalid", "Resource dependencies must be a bounded array", 409,
                node_id=node["node_binding_id"])
        identities = []
        for dependency in dependencies:
            require(type(dependency) is dict, "graph_resource_dependency_invalid",
                    "Resource dependency must be an identity", 409, node_id=node["node_binding_id"])
            if dependency.get("kind") == "global-resource":
                from .host_sdk import ResourceIdentity
                require(set(dependency) == {"kind", "reference"}, "graph_resource_dependency_invalid",
                        "Current resources require a reference", 409, node_id=node["node_binding_id"])
                ResourceIdentity.from_dict(dependency["reference"])
            else:
                raise graph_error("graph_resource_dependency_invalid", "Unknown resource dependency", 409,
                                  node_id=node["node_binding_id"])
            if dependency not in identities:
                identities.append(deepcopy(dependency))
        return identities

    def _preflight_capabilities(self, repo, sid, plan):
        frozen = {"global_resource_ids": {}, "current_global_resources": {}}
        nodes = {node["node_binding_id"]: node for node in plan.document["nodes"]}
        declared_by_node = {}
        for node_id in plan.ordered_node_ids:
            node, definition = nodes[node_id], plan.definitions[node_id]
            if "resources:read" in definition.capabilities:
                entry = self.registry.get(node["component_id"], node["component_version"])
                dependencies = self._content_dependencies(node, allow_inputs=bool(entry.resource_input_ports))
                for port_id in entry.resource_input_ports:
                    input_ports = {port.port_id: port for port in definition.ports(node["config"])[0]}
                    require(port_id in input_ports and input_ports[port_id].data_type == "GLOBAL_RESOURCE_REF",
                            "graph_resource_dependency_invalid", "Reference inheritance requires a declared identity port",
                            409, node_id=node_id)
                    for edge in plan.input_edges[node_id].get(port_id, []):
                        inherited = declared_by_node.get(edge["source_node_id"], [])
                        require(len(inherited) == 1 and inherited[0]["kind"] == "global-resource",
                                "graph_resource_dependency_undeclared",
                                "Reference inheritance requires a single declared source identity",
                                409, node_id=node_id)
                        for dependency in inherited:
                            if dependency not in dependencies:
                                dependencies.append(deepcopy(dependency))
                declared_by_node[node_id] = deepcopy(dependencies)
                current_ids = [dependency["reference"] for dependency in dependencies
                               if dependency["kind"] == "global-resource"]
                try:
                    current = self._global_store(repo.store).read_many(current_ids)
                    entry = self.registry.get(node["component_id"], node["component_version"])
                    if entry.resource_preflight_validator is not None:
                        entry.resource_preflight_validator(deepcopy(node["config"]), deepcopy(current))
                except Exception as error:
                    raise graph_error(getattr(error, "reason_code", "graph_resource_dependency_invalid"), str(error),
                                      getattr(error, "status_code", 409), node_id=node_id) from error
                frozen["global_resource_ids"][node_id] = deepcopy(current_ids)
                frozen["current_global_resources"][node_id] = [deepcopy(record) for record in current]
        return frozen

    def _host_call(self, context, capability, operation, payload):
        if capability == "resources:read" and operation == "current-global-resource":
            return self._read_current_resource(context, payload)
        raise graph_error("graph_capability_unknown", "Host operation is not registered")

    def _read_current_resource(self, context, payload):
        from .host_sdk import ResourceIdentity
        reference = ResourceIdentity.from_dict(payload).to_dict()
        declared = context._external_inputs.get("_workflow_frozen_resources", {}).get(
            "global_resource_ids", {}).get(context.node_binding_id, [])
        require(reference in declared, "graph_resource_dependency_undeclared",
                "Resource is not declared for this node", 409, node_id=context.node_binding_id)
        records = self._resource_frames.get(context.chain_run_id, {}).get(context.node_binding_id, [])
        record = next((row for row in records if all(row[key] == reference[key]
                      for key in ("scope", "type_id", "resource_id"))), None)
        require(record is not None, "recovery_unavailable",
                "Current resource execution frame is unavailable", 409, node_id=context.node_binding_id)
        return deepcopy(record)
