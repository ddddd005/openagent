import { frontendStateType } from "../domain/frontendWiring";
import { graphClone, newGraph, type GraphNodeType, type GraphPort, type GraphSession, type GraphSessionObject } from "../domain/workflowGraph";
import type { FrontendDisplayEntry } from "../domain/frontendDisplay";

const port = (port_id: string, data_type: string, version = 1, multiple = false): GraphPort =>
  ({ port_id, data_type, data_schema_version: version, required: true, multiple });
const base = { component_version: "1", category: "Frontend", config_schema: {}, default_config: {},
  inputs: [], outputs: [], is_output: false, executable: true, input_storage: "references" as const };
const access = { config_field: "object_key", multiple: false, type_id: frontendStateType, schema_version: 1 };
export const frontendCatalog: GraphNodeType[] = [
  { ...base, component_id: "tools.current-input", display_name: "Input", outputs: [port("output", "TEXT", 2)] },
  { ...base, component_id: "tools.regex", display_name: "Processed", inputs: [port("input", "TEXT", 2)],
    outputs: [port("output", "TEXT", 2)] },
  { ...base, component_id: "tools.output", display_name: "Legacy Output", default_config: { mode: "text" },
    config_schema: { properties: { mode: { enum: ["text", "prompt", "json"] } } }, is_output: true },
  { ...base, component_id: "tools.output", component_version: "2", display_name: "Output",
    config_schema: { properties: { mode: { enum: ["text", "prompt", "json"] } } },
    default_config: { mode: "text" }, is_output: true,
    provided_session_item_types: [{ type_id: frontendStateType, schema_version: 1 }] },
  { ...base, component_id: "frontend.state.output", display_name: "Read", default_config: { object_key: "frontend" },
    config_schema: { properties: { object_key: { type: "string" } } },
    outputs: [port("view", "FRONTEND_VIEW")], object_accesses: [{ ...access, access: "read" }] },
  { ...base, component_id: "frontend.state.append", display_name: "Append", default_config: { object_key: "frontend", role: "assistant" },
    config_schema: { properties: { object_key: { type: "string" }, role: { enum: ["user", "assistant", "system"] } } },
    inputs: [port("view", "FRONTEND_VIEW"), port("content", "TEXT", 2, true)],
    outputs: [port("view", "FRONTEND_VIEW"), port("commit", "FRONTEND_COMMIT")],
    object_accesses: [{ ...access, access: "read_write" }] },
  { ...base, component_id: "frontend.presentation", display_name: "Display",
    inputs: [port("view", "FRONTEND_VIEW")], outputs: [port("display", "FRONTEND_DISPLAY")] },
];
export const frontendLock = [{ package_id: "workflow.content", version: "1.0.0" },
  { package_id: "workflow.tools", version: "1.0.0" },
  { package_id: "workflow.frontend-business", version: "1.0.0" }];
export const frontendExecutionLock = frontendLock;
export const frontendProjectLock = [...frontendExecutionLock, { package_id: "workflow.frontend", version: "1.0.0" }];
export function explicitFrontendGraph(definitionId?: string) {
  const document = newGraph("Existing explicit frontend", frontendExecutionLock);
  if (definitionId) document.workflow_definition_id = definitionId;
  const sourceId = crypto.randomUUID(), registrarId = crypto.randomUUID();
  const readId = crypto.randomUUID(), appendId = crypto.randomUUID(), presentationId = crypto.randomUUID();
  const objectKey = "item/output-test";
  document.nodes = [
    { node_binding_id: sourceId, component_id: "tools.current-input", component_version: "1",
      title: "Input", position: { x: 0, y: 0 }, config: { input_name: "text" } },
    { node_binding_id: registrarId, component_id: "tools.output", component_version: "2",
      title: "Output", position: { x: 0, y: 100 }, config: { mode: "text" } },
    { node_binding_id: readId, component_id: "frontend.state.output", component_version: "1",
      title: "Read", position: { x: 300, y: 0 }, config: { object_key: objectKey } },
    { node_binding_id: appendId, component_id: "frontend.state.append", component_version: "1",
      title: "Append", position: { x: 600, y: 0 }, config: { object_key: objectKey, role: "user" } },
    { node_binding_id: presentationId, component_id: "frontend.presentation", component_version: "1",
      title: "Display", position: { x: 900, y: 0 }, config: {}, public_outputs: ["display"] },
  ];
  document.edges = [
    { edge_id: crypto.randomUUID(), source_node_id: readId, source_port_id: "view",
      target_node_id: appendId, target_port_id: "view", order: 0 },
    { edge_id: crypto.randomUUID(), source_node_id: sourceId, source_port_id: "output",
      target_node_id: appendId, target_port_id: "content", order: 0 },
    { edge_id: crypto.randomUUID(), source_node_id: appendId, source_port_id: "view",
      target_node_id: presentationId, target_port_id: "view", order: 0 },
  ];
  document.execution_roots = [presentationId];
  document.object_bindings = [{ object_key: objectKey, type_id: frontendStateType, schema_version: 1,
    scope: "shared", owner_node_id: null, readers: [readId, appendId], writers: [appendId],
    registrar_node_id: registrarId, default_value: { entries: [], view_ref: null } }];
  return { document, sourceId, registrarId, readId, appendId, presentationId, objectKey };
}
export function frontendEntry(): FrontendDisplayEntry {
  return { entry_id: crypto.randomUUID(), role: "assistant",
    source_ref: { scope: "artifact", output_id: crypto.randomUUID() } };
}
export function frontendObject(entries = [frontendEntry()]): GraphSessionObject {
  const reader = crypto.randomUUID();
  return { type_id: frontendStateType, schema_version: 1, revision_id: crypto.randomUUID(), revision: 1,
    deleted: false, value: { entries, view_ref: { scope: "artifact", output_id: crypto.randomUUID() } },
    binding: { object_key: "frontend", type_id: frontendStateType, schema_version: 1, scope: "shared",
      owner_node_id: null, readers: [reader], writers: [reader] } };
}
export function frontendSession(definitionId: string): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_definition_id: definitionId,
    workflow_session_id: crypto.randomUUID(), definition_revision: 1, revision: 1, data_revision: 0,
    head_revision: 1, status: "succeeded", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} }, objects: { frontend: graphClone(frontendObject()) } };
}
