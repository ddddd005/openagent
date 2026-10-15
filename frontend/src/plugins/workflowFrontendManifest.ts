import type { FrontendExtension } from "../domain/frontendExtensions";

export const workflowFrontendExtensions: FrontendExtension[] = [
  ["session-object", "renderer", "workbench.session-object", "workbench", "session-object",
    { scope: "session", type_id: "workflow.frontend-state", schema_version: 1 }, null, null],
  ["node-fields", "field-editor", "workbench.node-fields", "workbench", "node-fields",
    { component_id: "frontend.state.append", component_version: "1" }, "frontend.state.append", "1"],
  ["public-output", "consumer", "consumer.public-output", "consumer", "public-output",
    { scope: "content", type_id: "FRONTEND_DISPLAY", schema_version: 1 }, null, null],
].map(([id, kind, entrypoint, surface, slot, target, component_id, component_version]) => ({
  schema_version: slot === "node-fields" ? 3 : 2,
  host_protocol_version: slot === "node-fields" ? 2 : 1,
  extension_id: `workflow.frontend.${id}`,
  kind, entrypoint: `workflow.frontend.${entrypoint}`, component_id, component_version,
  package_id: "workflow.frontend", package_version: "1.0.0", binding: { surface, slot, target },
})) as FrontendExtension[];

export const frontendNodeFieldsExtension: FrontendExtension = {
  schema_version: 3, host_protocol_version: 2,
  extension_id: "workflow.frontend.node-entry-fields",
  kind: "field-editor", entrypoint: "workflow.frontend.workbench.node-entry-fields",
  component_id: "frontend.node", component_version: "1",
  package_id: "workflow.frontend", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "node-fields",
    target: { component_id: "frontend.node", component_version: "1" } },
};
workflowFrontendExtensions.push(frontendNodeFieldsExtension);

export const frontendChatNodeFieldsExtension: FrontendExtension = {
  ...frontendNodeFieldsExtension,
  extension_id: "workflow.frontend.node-entry-fields-v2",
  component_version: "2",
  binding: { surface: "workbench", slot: "node-fields",
    target: { component_id: "frontend.node", component_version: "2" } },
};
export const chatSessionObjectExtension: FrontendExtension = {
  schema_version: 2, host_protocol_version: 1,
  extension_id: "workflow.frontend.chat-session-object",
  kind: "renderer", entrypoint: "workflow.frontend.workbench.chat-session-object",
  component_id: null, component_version: null,
  package_id: "workflow.frontend", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "session-object",
    target: { scope: "session", type_id: "workflow.chat-context", schema_version: 1 } },
};
workflowFrontendExtensions.push(frontendChatNodeFieldsExtension, chatSessionObjectExtension);

export function frontendNodeFieldsExtensionForVersion(version: string) {
  return version === "1" ? frontendNodeFieldsExtension
    : version === "2" ? frontendChatNodeFieldsExtension : undefined;
}
