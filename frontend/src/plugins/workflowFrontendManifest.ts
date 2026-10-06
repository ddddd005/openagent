import type { FrontendExtension } from "../domain/frontendExtensions";

export const workflowFrontendExtensions: FrontendExtension[] = [
  ["workbench-panel", "workbench-panel", "workbench.panel", "workbench", "panel", {}, null, null],
  ["session-object", "renderer", "workbench.session-object", "workbench", "session-object",
    { scope: "session", type_id: "workflow.frontend-state", schema_version: 1 }, null, null],
  ["node-fields", "field-editor", "workbench.node-fields", "workbench", "node-fields",
    { component_id: "frontend.state.append", component_version: "1" }, "frontend.state.append", "1"],
  ["public-output", "consumer", "consumer.public-output", "consumer", "public-output",
    { scope: "content", type_id: "FRONTEND_DISPLAY", schema_version: 1 }, null, null],
].map(([id, kind, entrypoint, surface, slot, target, component_id, component_version]) => ({
  schema_version: 2, host_protocol_version: 1, extension_id: `workflow.frontend.${id}`,
  kind, entrypoint: `workflow.frontend.${entrypoint}`, component_id, component_version,
  package_id: "workflow.frontend", package_version: "1.0.0", binding: { surface, slot, target },
})) as FrontendExtension[];
