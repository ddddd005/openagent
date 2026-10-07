import type { FrontendExtension } from "../domain/frontendExtensions";

export const promptFrontendExtensions: FrontendExtension[] = [
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.prompts.workbench-panel",
    kind: "workbench-panel", entrypoint: "workflow.prompts.workbench.panel",
    component_id: null, component_version: null, package_id: "workflow.prompts", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "panel", target: {} } },
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.prompts.node-fields",
    kind: "field-editor", entrypoint: "workflow.prompts.workbench.node-fields",
    component_id: "prompts.global-reference", component_version: "1", package_id: "workflow.prompts", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "prompts.global-reference", component_version: "1" } } },
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.prompts.node-fields-v2",
    kind: "field-editor", entrypoint: "workflow.prompts.workbench.node-fields-v2",
    component_id: "prompts.global-reference", component_version: "2", package_id: "workflow.prompts", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "prompts.global-reference", component_version: "2" } } },
];
