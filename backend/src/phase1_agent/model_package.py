"""Model source and ordinary one-response Chat nodes; no Agent dependency."""

from __future__ import annotations

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .graph_contracts import NodeDefinition, NodePort
from .model_configuration import DEFAULT_PROVIDER_ID
from .model_contract import (
    CHAT_PROVIDER_TYPE, MODEL_PACKAGE_ID, model_type_definitions, object_schema,
    validate_model_source_config, validate_provider_record, validate_public_model_binding,
)
from .prompt_contract import validate_ready_prompt
from .model_host_service import (
    MODEL_SERVICE_DEFINITION, MODEL_SERVICE_REF, create_model_host_service, model_service_requirement,
)

MODEL_FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.models.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.models.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    {"extension_id": "workflow.models.node-fields", "kind": "field-editor",
     "entrypoint": "workflow.models.workbench.node-fields",
     "component_id": "models.source", "component_version": "1",
     "binding": {"surface": "workbench", "slot": "node-fields",
                 "target": {"component_id": "models.source", "component_version": "1"}}},
)


def create_model_package() -> CapabilityPackage:
    def register(host):
        for extension in MODEL_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)
        host.register_service(MODEL_SERVICE_DEFINITION, create_model_host_service)
        for definition in model_type_definitions():
            host.register_data_type(definition)
        config = {
            "reference": {"envelope_version": 1, "scope": "workspace",
                          "type_id": CHAT_PROVIDER_TYPE, "resource_id": DEFAULT_PROVIDER_ID},
            "parameters": {"model": "deepseek-flash", "thinking": "disabled", "stream": False},
        }

        def preflight(config, records):
            reference = config["reference"]
            record = next((record for record in records if all(
                record.get(key) == reference[key] for key in reference)), None)
            validate_provider_record(record, reference)

        host.register_node(NodeDefinition(
            "models.source", "1", "Model source", "Models", config,
            object_schema({"reference": {"type": "object"}, "parameters": {"type": "object"}}),
            outputs=(NodePort("output", "MODEL_BINDING"),),
            capabilities=("models:resolve", "resources:read"), input_storage="references",
            service_requirements=(model_service_requirement("models:resolve", "bind-model"),),
        ), lambda config, inputs, context: {
            "output": context.host_call("models:resolve", "bind-model", config)},
            config_validator=validate_model_source_config,
            resource_dependencies_declaration=lambda config: [
                {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
            resource_preflight_validator=preflight)

        def chat(config, inputs, context):
            validate_ready_prompt(inputs["prompt"])
            binding = validate_public_model_binding(inputs["model"])
            return {"output": context.host_call("models:call", "chat",
                                                {"binding_id": binding["binding_id"]})}

        host.register_node(NodeDefinition(
            "models.chat", "1", "Chat", "Models", {}, object_schema({}),
            inputs=(NodePort("model", "MODEL_BINDING"),
                    NodePort("prompt", "PROMPT", data_schema_version=2)),
            outputs=(NodePort("output", "MODEL_RESULT"),),
            capabilities=("models:call",), input_storage="references", is_output=True,
            service_requirements=(model_service_requirement("models:call", "chat"),),
        ), chat)

    exports = {
        "data_types": [{"scope": definition.scope, "type_id": definition.type_id,
                        "schema_version": definition.schema_version}
                       for definition in model_type_definitions()],
        "nodes": [{"component_id": component, "component_version": "1"}
                  for component in ("models.source", "models.chat")],
        "services": [MODEL_SERVICE_REF.to_dict()],
        "frontend_extensions": [{"extension_id": row["extension_id"]} for row in MODEL_FRONTEND_EXTENSIONS],
    }
    return CapabilityPackage(PackageManifest(
        MODEL_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),),
        exports=exports, schema_version=3,
    ), register)
