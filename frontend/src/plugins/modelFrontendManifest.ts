import type { FrontendExtension } from "../domain/frontendExtensions";
export const modelFrontendExtensions: FrontendExtension[] = [
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.models.workbench-panel",
    kind: "workbench-panel", entrypoint: "workflow.models.workbench.panel",
    component_id: null, component_version: null, package_id: "workflow.models", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "panel", target: {} } },
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.models.node-fields",
    kind: "field-editor", entrypoint: "workflow.models.workbench.node-fields",
    component_id: "models.source", component_version: "1", package_id: "workflow.models", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "node-fields", target: { component_id: "models.source", component_version: "1" } } },
];
