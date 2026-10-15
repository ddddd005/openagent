import type { FrontendExtension } from "../domain/frontendExtensions";

export const frontendChatNodeFieldsExtension: FrontendExtension = {
  schema_version: 3, host_protocol_version: 2,
  extension_id: "workflow.frontend.node-entry-fields-v2",
  kind: "field-editor", entrypoint: "workflow.frontend.workbench.node-entry-fields",
  component_id: "frontend.node", component_version: "2",
  package_id: "workflow.frontend", package_version: "1.0.0",
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

export const workflowFrontendExtensions: FrontendExtension[] = [
  frontendChatNodeFieldsExtension,
  chatSessionObjectExtension,
];

export function frontendNodeFieldsExtensionForVersion(version: string) {
  return version === "2" ? frontendChatNodeFieldsExtension : undefined;
}
