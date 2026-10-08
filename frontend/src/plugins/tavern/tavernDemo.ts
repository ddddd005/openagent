import { graphClone, type GraphDocument, type GraphNode, type GraphNodeType } from "../../domain/workflowGraph";
import type { SerialAgentModelConfig } from "../../domain/serialAgentDemo";

const requiredPackages = ["workflow.content", "workflow.tools", "workflow.prompts",
  "workflow.models", "workflow.agents", "workflow.context"];

export function createTavernDemo(catalog: GraphNodeType[],
  packageLock: NonNullable<GraphDocument["package_lock"]>, modelConfig?: SerialAgentModelConfig): GraphDocument {
  const missing = requiredPackages.filter(id => !packageLock.some(row => row.package_id === id && row.version === "1.0.0"));
  if (!packageLock.some(row => row.package_id === "workflow.tavern" && row.version === "1.2.0"))
    missing.push("workflow.tavern@1.2.0");
  if (missing.length) throw new Error(`酒馆示例缺少精确包：${missing.join("、")}`);
  const doc: GraphDocument = { schema_version: 2, workflow_definition_id: crypto.randomUUID(),
    revision: 1, name: "酒馆聊天", nodes: [], edges: [], control_edges: [], execution_roots: [],
    object_bindings: [], package_lock: graphClone(packageLock) };
  function node(component: string, version: string, title: string, x: number, y: number,
    config: Record<string, unknown> = {}) {
    const type = catalog.find(row => row.component_id === component && row.component_version === version && row.executable);
    if (!type) throw new Error(`酒馆示例缺少节点 ${component}@${version}`);
    const value: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: component,
      component_version: version, title, position: { x, y }, config: { ...graphClone(type.default_config), ...config } };
    doc.nodes.push(value); return value;
  }
  function edge(from: GraphNode, source: string, to: GraphNode, target: string) {
    doc.edges.push({ edge_id: crypto.randomUUID(), source_node_id: from.node_binding_id, source_port_id: source,
      target_node_id: to.node_binding_id, target_port_id: target, order: 0 });
  }
  function control(from: GraphNode, to: GraphNode) {
    doc.control_edges!.push({ edge_id: crypto.randomUUID(), source_node_id: from.node_binding_id,
      target_node_id: to.node_binding_id });
  }
  const input = node("tools.current-input", "1", "用户输入", 0, 0, { input_name: "text" });
  const model = node("models.source", "2", modelConfig ? "模型" : "模型与容量待配置", 0, 200);
  if (modelConfig) model.config = { ...model.config, ...graphClone(modelConfig) };
  else {
    model.config.reference = { ...(model.config.reference as Record<string, unknown>), resource_id: "" };
    model.config.parameters = { ...(model.config.parameters as Record<string, unknown>), model: "" };
  }
  const agent = node("agents.execute", "4", "酒馆 Agent", 850, 0);
  const binding = { object_key: "context/tavern", agent_node_id: agent.node_binding_id };
  const read = node("context.output", "4", "Agent 上下文", 0, 400, binding);
  const assembly = node("context.assembly", "4", "提示词装配", 550, 0);
  const merge = node("context.merge", "4", "正常上下文写回", 1150, 0, binding);
  const prompt = node("prompts.item", "2", "提示词", 250, 200, {
    text: "Respond to the user's message. Complete with final_answer containing an answer with text.",
  });
  edge(input, "output", assembly, "current_input"); edge(read, "output", assembly, "view");
  edge(prompt, "output", assembly, "materials"); edge(assembly, "output", agent, "prompt");
  edge(model, "output", agent, "model"); edge(read, "output", merge, "view"); edge(agent, "context", merge, "context");
  doc.object_bindings!.push({ object_key: binding.object_key, type_id: "workflow.effective-context", schema_version: 4,
    scope: "shared", owner_node_id: null, readers: [read.node_binding_id, merge.node_binding_id],
    writers: [merge.node_binding_id], default_value: { view_ref: null, accepted_delta_ids: [] } });
  const chatBinding = { object_key: "tavern-chat" };
  const readUser = node("tavern.chat.output", "1", "读取酒馆记录", 1450, 0, chatBinding);
  const user = node("tavern.chat.append", "1", "追加用户消息", 1750, 0, { ...chatBinding, role: "user" });
  const readAssistant = node("tavern.chat.output", "1", "重新读取酒馆记录", 2050, 0, chatBinding);
  const assistant = node("tavern.chat.append", "1", "追加 Agent 消息", 2350, 0, { ...chatBinding, role: "assistant" });
  const display = node("tavern.chat.presentation", "1", "酒馆展示", 2650, 0);
  display.public_outputs = ["display"];
  control(merge, readUser); edge(readUser, "view", user, "view"); edge(input, "output", user, "content");
  control(user, readAssistant); edge(readAssistant, "view", assistant, "view"); edge(agent, "result", assistant, "content");
  edge(assistant, "view", display, "view");
  doc.object_bindings!.push({ object_key: "tavern-chat", type_id: "workflow.tavern.chat-state", schema_version: 1,
    scope: "shared", owner_node_id: null, readers: [readUser, user, readAssistant, assistant].map(row => row.node_binding_id),
    writers: [user.node_binding_id, assistant.node_binding_id], default_value: { entries: [], view_ref: null } });
  doc.execution_roots = [merge.node_binding_id, display.node_binding_id];
  return doc;
}
