import type { FrontendExtension } from "../domain/frontendExtensions";

export const tavernFrontendExtensions: FrontendExtension[] = [
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.tavern.item-fields",
    kind: "field-editor", entrypoint: "workflow.tavern.workbench.item-fields",
    component_id: "lorebook.item", component_version: "1", package_id: "workflow.tavern", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "lorebook.item", component_version: "1" } } },
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.tavern.group-fields",
    kind: "field-editor", entrypoint: "workflow.tavern.workbench.group-fields",
    component_id: "lorebook.group", component_version: "1", package_id: "workflow.tavern", package_version: "1.0.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "lorebook.group", component_version: "1" } } },
];
