"""Capacity-aware model sources and ordinary Chat for current protocols."""

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .graph_contracts import NodeDefinition, NodePort, require
from .model_contract import (
    DEFAULT_PROFILE_ID, MODEL_PROFILE_TYPE, MODEL_PACKAGE_ID, model_type_definitions, object_schema,
    resolve_model_source, validate_model_source_config, validate_public_model_binding,
)
from .model_host_service import (
    MODEL_SERVICE_DEFINITION, MODEL_SERVICE_REF, create_model_host_service, model_service_requirement,
)
from .prompt_contract import validate_ready_prompt

MODEL_FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.models.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.models.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    *tuple({"extension_id": "workflow.models.node-fields-v" + version, "kind": "field-editor",
            "entrypoint": "workflow.models.workbench.node-fields-v" + version,
            "component_id": "models.source", "component_version": version,
            "binding": {"surface": "workbench", "slot": "node-fields",
                        "target": {"component_id": "models.source", "component_version": version}}}
           for version in ("5", "6")),
)


def create_model_package() -> CapabilityPackage:
    def register(host):
        for extension in MODEL_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)
        host.register_service(MODEL_SERVICE_DEFINITION, create_model_host_service)
        for definition in model_type_definitions():
            host.register_data_type(definition)

        def validate(value):
            return validate_model_source_config(value)

        def preflight(config, records, protocol):
            resolved = resolve_model_source(config, records)
            require(resolved["provider"]["protocol"] == protocol, "model_source_protocol_mismatch",
                    "Model source does not match the selected provider protocol")

        for source_version, binding_version, chat_version, protocol in (
            ("5", 2, "3", "chat"), ("6", 4, "4", "gemini"),
        ):
            config = {
                "reference": {"envelope_version": 1, "scope": "workspace",
                              "type_id": MODEL_PROFILE_TYPE, "resource_id": DEFAULT_PROFILE_ID},
            }
            host.register_node(NodeDefinition(
                "models.source", source_version, "Model source", "Models", config,
                object_schema({"reference": {"type": "object"}}),
                outputs=(NodePort("output", "MODEL_BINDING", data_schema_version=binding_version),),
                capabilities=("models:resolve", "resources:read"), input_storage="references",
                service_requirements=(model_service_requirement("models:resolve", "bind-native-model"),),
            ), lambda config, inputs, context: {
                "output": context.host_call("models:resolve", "bind-native-model", config)},
                config_validator=validate,
                resource_dependencies_declaration=lambda config: [
                    {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
                resource_preflight_validator=lambda config, records, protocol=protocol:
                    preflight(config, records, protocol))

            def chat(config, inputs, context):
                validate_ready_prompt(inputs["prompt"])
                binding = validate_public_model_binding(inputs["model"])
                return {"output": context.host_call("models:call", "chat",
                                                    {"binding_id": binding["binding_id"]})}

            host.register_node(NodeDefinition(
                "models.chat", chat_version, "Chat", "Models", {}, object_schema({}),
                inputs=(NodePort("model", "MODEL_BINDING", data_schema_version=binding_version),
                        NodePort("prompt", "PROMPT", data_schema_version=2)),
                outputs=(NodePort("output", "MODEL_RESULT", data_schema_version=2 if protocol == "gemini" else 1),),
                capabilities=("models:call",), input_storage="references", is_output=True,
                service_requirements=(model_service_requirement("models:call", "chat"),)), chat)

    return CapabilityPackage(PackageManifest(
        MODEL_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),),
        exports={
            "data_types": [{"scope": definition.scope, "type_id": definition.type_id,
                            "schema_version": definition.schema_version}
                           for definition in model_type_definitions()],
            "nodes": [{"component_id": component, "component_version": version}
                      for component, versions in (("models.source", ("5", "6")), ("models.chat", ("3", "4")))
                      for version in versions],
            "services": [MODEL_SERVICE_REF.to_dict()],
            "frontend_extensions": [{"extension_id": row["extension_id"]} for row in MODEL_FRONTEND_EXTENSIONS],
        }, schema_version=3), register)
