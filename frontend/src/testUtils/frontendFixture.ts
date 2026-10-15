import { graphClone, type GraphNodeType, type GraphPort, type GraphSession } from "../domain/workflowGraph";
import { frontendChatNodeType } from "./frontendNodeFixture";

const port = (port_id: string, data_type: string, version = 1, multiple = false): GraphPort =>
  ({ port_id, data_type, data_schema_version: version, required: true, multiple });
const base = { component_version: "1", category: "Frontend", config_schema: {}, default_config: {},
  inputs: [], outputs: [], is_output: false, executable: true, input_storage: "references" as const };

export const frontendCatalog: GraphNodeType[] = [
  { ...base, component_id: "tools.current-input", display_name: "Input", outputs: [port("output", "TEXT", 2)] },
  { ...base, component_id: "tools.regex", display_name: "Processed", inputs: [port("input", "TEXT", 2)],
    outputs: [port("output", "TEXT", 2)] },
  { ...base, component_id: "tools.output", display_name: "Output", default_config: { mode: "text" },
    config_schema: { properties: { mode: { enum: ["text", "prompt", "json"] } } }, is_output: true },
  graphClone(frontendChatNodeType),
];

export const frontendExecutionLock = [
  { package_id: "workflow.content", version: "1.0.0" },
  { package_id: "workflow.tools", version: "1.0.0" },
  { package_id: "workflow.frontend-business", version: "1.0.0" },
];
export const frontendProjectLock = [...frontendExecutionLock,
  { package_id: "workflow.frontend", version: "1.0.0" }];

export function frontendSession(definitionId: string): GraphSession {
  const sessionId = crypto.randomUUID(), nodeId = crypto.randomUUID();
  const object = { type_id: "workflow.chat-context", schema_version: 1, revision_id: crypto.randomUUID(),
    revision: 1, deleted: false, value: { rounds: [] }, binding: {
      object_key: "chat", type_id: "workflow.chat-context", schema_version: 1,
      scope: "shared" as const, owner_node_id: null, readers: [nodeId], writers: [nodeId],
    } };
  return { schema_version: 2, execution_model: "graph", workflow_definition_id: definitionId,
    workflow_session_id: sessionId, definition_revision: 1, revision: 1, data_revision: 0,
    head_revision: 1, status: "succeeded", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} }, objects: { chat: object } };
}
