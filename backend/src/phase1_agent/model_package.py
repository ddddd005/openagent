"""Capacity-aware model sources and ordinary Chat for current protocols."""

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .graph_contracts import NodeDefinition, NodePort, require
from .model_contract import (
    DEFAULT_PROFILE_ID, MODEL_PROFILE_TYPE, MODEL_PACKAGE_ID, model_type_definitions, object_schema,
    validate_model_source_config,
)
from .model_host_service import (
    MODEL_SERVICE_DEFINITION, MODEL_SERVICE_REF, create_model_host_service, model_service_requirement,
)
from .model_request_contracts import validate_model_binding, validate_model_result
from .prompt_contract import validate_ready_prompt

MODEL_FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.models.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.models.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    {"extension_id": "workflow.models.node-fields-v7", "kind": "field-editor",
     "entrypoint": "workflow.models.workbench.node-fields-v7",
     "component_id": "models.source", "component_version": "7",
     "binding": {"surface": "workbench", "slot": "node-fields",
                 "target": {"component_id": "models.source", "component_version": "7"}}},
)


def create_model_package() -> CapabilityPackage:
    def register(host):
        for extension in MODEL_FRONTEND_EXTENSIONS:
            protocol = 2 if extension["binding"]["slot"] == "node-fields" else 1
            host.register_frontend_extension(**extension, host_protocol_version=protocol)
        host.register_service(MODEL_SERVICE_DEFINITION, create_model_host_service)
        for definition in model_type_definitions():
            host.register_data_type(definition)

        def validate(value):
            return validate_model_source_config(value)

        config = {
            "reference": {"envelope_version": 1, "scope": "workspace",
                          "type_id": MODEL_PROFILE_TYPE, "resource_id": DEFAULT_PROFILE_ID},
        }
        host.register_node(NodeDefinition(
            "models.source", "7", "Model source", "Models", config,
            object_schema({"reference": {"type": "object"}}),
            outputs=(NodePort("output", "MODEL_BINDING", data_schema_version=5),),
            capabilities=("models:resolve", "resources:read"), input_storage="references",
            service_requirements=(model_service_requirement("models:resolve", "bind-native-model"),),
        ), lambda config, inputs, context: {
            "output": context.host_call("models:resolve", "bind-native-model", config)},
            config_validator=validate,
            resource_dependencies_declaration=lambda config: [
                {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
            resource_preflight_validator=lambda config, records: require(
                any(record.get("type_id") == MODEL_PROFILE_TYPE
                    and record.get("resource_id") == config["reference"]["resource_id"]
                    and type(record.get("data_schema_version")) is int
                    and record["data_schema_version"] == 2 for record in records),
                "model_profile_unavailable", "Referenced model configuration is missing or unsupported"))

        def chat(config, inputs, context):
            prompt = validate_ready_prompt(inputs["prompt"])
            binding = validate_model_binding(inputs["model"])
            messages = [{"kind": "text", **deepcopy(message)}
                        for message in prompt["assembly"]["messages"]]
            prepared = context.host_call("models:call", "prepare-request", {
                "binding_id": binding["binding_id"], "request_key": "chat",
                "purpose": "normal", "messages": messages, "tools": [],
            })
            result = context.host_call("models:call", "dispatch-request", {
                "binding_id": binding["binding_id"], "request_key": "chat",
                "preparation_id": prepared["preparation_id"],
            })
            return {"output": validate_model_result(result, settings=prepared["settings"])}

        host.register_node(NodeDefinition(
            "models.chat", "5", "Chat", "Models", {}, object_schema({}),
            inputs=(NodePort("model", "MODEL_BINDING", data_schema_version=5),
                    NodePort("prompt", "PROMPT", data_schema_version=2)),
            outputs=(NodePort("output", "MODEL_RESULT", data_schema_version=3),),
            capabilities=("models:call",), input_storage="references", is_output=True,
            service_requirements=(model_service_requirement(
                "models:call", "prepare-request", "dispatch-request"),)), chat)

    return CapabilityPackage(PackageManifest(
        MODEL_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),),
        exports={
            "data_types": [{"scope": definition.scope, "type_id": definition.type_id,
                            "schema_version": definition.schema_version}
                           for definition in model_type_definitions()],
            "nodes": [{"component_id": "models.source", "component_version": "7"},
                      {"component_id": "models.chat", "component_version": "5"}],
            "services": [MODEL_SERVICE_REF.to_dict()],
            "frontend_extensions": [{"extension_id": row["extension_id"]} for row in MODEL_FRONTEND_EXTENSIONS],
        }, schema_version=3), register)
