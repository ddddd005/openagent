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

export const globalTavernFrontendExtensions: FrontendExtension[] = [
  ...tavernFrontendExtensions.map(row => ({ ...row, package_version: "1.1.0",
    extension_id: row.extension_id + "-v1-1", entrypoint: row.entrypoint + "-v1-1" })),
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.tavern.lorebook-resources",
    kind: "workbench-panel", entrypoint: "workflow.tavern.workbench.lorebook-resources",
    component_id: null, component_version: null, package_id: "workflow.tavern", package_version: "1.1.0",
    binding: { surface: "workbench", slot: "panel", target: {} } },
  ...(["global-reference", "global-activate"] as const).map(name => ({
    schema_version: 2 as const, host_protocol_version: 1 as const, extension_id: `workflow.tavern.${name}-fields`,
    kind: "field-editor" as const, entrypoint: `workflow.tavern.workbench.${name}-fields`,
    component_id: `lorebook.${name}`, component_version: "1", package_id: "workflow.tavern", package_version: "1.1.0",
    binding: { surface: "workbench" as const, slot: "node-fields" as const,
      target: { component_id: `lorebook.${name}`, component_version: "1" } },
  })),
];

export const chatTavernFrontendExtensions: FrontendExtension[] = [
  ...globalTavernFrontendExtensions.map(row => ({ ...row, package_version: "1.2.0",
    extension_id: row.extension_id + "-v1-2", entrypoint: row.entrypoint + "-v1-2" })),
  { schema_version: 2, host_protocol_version: 1, extension_id: "workflow.tavern.chat-launch",
    kind: "workbench-panel", entrypoint: "workflow.tavern.workbench.chat-launch",
    component_id: null, component_version: null, package_id: "workflow.tavern", package_version: "1.2.0",
    binding: { surface: "workbench", slot: "panel", target: {} } },
];

export function tavernExtensionsFor(packages: readonly { package_id: string; version: string }[]) {
  const version = packages.find(row => row.package_id === "workflow.tavern")?.version;
  return version === "1.2.0" ? chatTavernFrontendExtensions
    : version === "1.1.0" ? globalTavernFrontendExtensions : version === "1.0.0" ? tavernFrontendExtensions : [];
}
