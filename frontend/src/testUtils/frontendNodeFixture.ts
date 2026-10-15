import { graphClone, newGraph, type GraphEntry, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { editableInputType } from "./editableInputFixture";

export const frontendNodeType: GraphNodeType = {
  component_id: "frontend.node", component_version: "1", display_name: "前端节点", category: "Frontend",
  default_config: { object_key: "frontend", role: "assistant", input_node_id: "" },
  config_schema: { type: "object", properties: {
    object_key: { type: "string" }, role: { enum: ["user", "assistant", "system"] },
    input_node_id: { type: "string" },
  }, required: ["object_key", "role", "input_node_id"], additionalProperties: false },
  inputs: [{ port_id: "content", data_type: "TEXT", data_schema_version: 2, required: true, multiple: true }],
  outputs: [{ port_id: "proof", data_type: "FRONTEND_NODE_APPEND", required: true, multiple: false },
    { port_id: "commit", data_type: "FRONTEND_COMMIT", required: true, multiple: false }],
  executable: true, is_output: true, execution_class: "working", input_storage: "references",
  default_configuration_policy: { schema_version: 1, prompt_mode: "fixed" },
  provided_session_item_types: [{ type_id: "workflow.frontend-state", schema_version: 1 }],
  object_accesses: [{ config_field: "object_key", multiple: false, access: "read_write",
    type_id: "workflow.frontend-state", schema_version: 1 }],
  config_node_references: [{ config_field: "input_node_id", multiple: false, allow_empty: true,
    target_types: [{ component_id: "tools.editable-input", component_version: "1" }] }],
};
export const frontendChatNodeType: GraphNodeType = {
  ...graphClone(frontendNodeType), component_version: "2",
  outputs: frontendNodeType.outputs.map(port => ({ ...port,
    ...(port.port_id === "proof" ? { data_schema_version: 2 } : {}) })),
  provided_session_item_types: [{ type_id: "workflow.chat-context", schema_version: 1 }],
  object_accesses: [{ config_field: "object_key", multiple: false, access: "read_write",
    type_id: "workflow.chat-context", schema_version: 1 }],
};
export function frontendNodeFixture(version = "1") {
  const type = version === "2" ? frontendChatNodeType : frontendNodeType;
  const document = newGraph("Frontend nodes", [{ package_id: "workflow.frontend", version: "1.0.0" }]);
  document.schema_version = 3; document.edit_sequence = 1; document.object_bindings = [];
  const make = (type: GraphNodeType, title: string): GraphNode => ({
    node_binding_id: crypto.randomUUID(), component_id: type.component_id, component_version: type.component_version,
    title, config: graphClone(type.default_config), position: { x: 0, y: 0 },
    configuration_policy: graphClone(type.default_configuration_policy!),
  });
  const inputs = [make(editableInputType, "Input"), make(editableInputType, "Input")];
  const nodes = [make(type, "Frontend"), make(type, "Frontend")];
  document.nodes = [...inputs, ...nodes];
  for (const node of nodes) {
    const key = `item/${node.node_binding_id}`; node.config.object_key = key;
    document.object_bindings.push({ object_key: key, type_id: type.provided_session_item_types![0]!.type_id, schema_version: 1,
      scope: "shared", owner_node_id: null, registrar_node_id: node.node_binding_id,
      readers: [node.node_binding_id], writers: [node.node_binding_id] });
  }
  const entry: GraphEntry = { document, saved_document: graphClone(document), saved_revision: 1,
    session_id: null, pending: null };
  return { document, inputs, nodes, entry, catalog: [graphClone(editableInputType), graphClone(type)] };
}
