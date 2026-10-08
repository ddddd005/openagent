import { graphClone, newGraph, type GraphDocument, type GraphNode, type GraphNodeType } from "./workflowGraph";
import type { ModelCapacity } from "./workflowModelResources";

export interface SerialAgentModelConfig {
  reference: Record<string, unknown>;
  parameters: Record<string, unknown>;
  capacity?: ModelCapacity;
}
const requiredPackages = ["workflow.content", "workflow.tools", "workflow.prompts",
  "workflow.models", "workflow.agents", "workflow.context", "workflow.frontend-business"];

export function createSerialAgentDemo(catalog: GraphNodeType[],
  packageLock: NonNullable<GraphDocument["package_lock"]>, modelConfig?: SerialAgentModelConfig): GraphDocument {
  return serialAgentDemo(catalog, packageLock, modelConfig, false);
}

export function createCompactingSerialAgentDemo(catalog: GraphNodeType[],
  packageLock: NonNullable<GraphDocument["package_lock"]>, modelConfig?: SerialAgentModelConfig): GraphDocument {
  return serialAgentDemo(catalog, packageLock, modelConfig, true);
}

function serialAgentDemo(catalog: GraphNodeType[],
  packageLock: NonNullable<GraphDocument["package_lock"]>, modelConfig: SerialAgentModelConfig | undefined,
  compacting: boolean): GraphDocument {
  const missing = requiredPackages.filter(id => !packageLock.some(row => row.package_id === id && row.version === "1.0.0"));
  if (missing.length) throw new Error(`串行示例缺少已启用的包：${missing.join("、")}。请启用后刷新目录。`);
  const doc = newGraph(compacting ? "上下文精简 · Agent A → B" : "串行 Agent A → B", packageLock);
  function node(component: string, version: string, title: string, x: number, y: number,
    config: Record<string, unknown> = {}) {
    const type = catalog.find(row => row.component_id === component && row.component_version === version && row.executable);
    if (!type) throw new Error(`串行示例缺少节点 ${component}@${version}。请刷新新版目录。`);
    const value: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: component,
      component_version: version, title, position: { x, y }, config: { ...graphClone(type.default_config), ...config } };
    doc.nodes.push(value); return value;
  }
  function edge(from: GraphNode, source: string, to: GraphNode, target: string, order = 0) {
    doc.edges.push({ edge_id: crypto.randomUUID(), source_node_id: from.node_binding_id, source_port_id: source,
      target_node_id: to.node_binding_id, target_port_id: target, order });
  }
  function control(from: GraphNode, to: GraphNode) {
    doc.control_edges!.push({ edge_id: crypto.randomUUID(), source_node_id: from.node_binding_id,
      target_node_id: to.node_binding_id });
  }
  const input = node("tools.current-input", "1", "用户输入", 0, 0, { input_name: "text" });
  const model = node("models.source", compacting ? "2" : "1",
    modelConfig ? "共享模型" : compacting ? "模型与容量待配置" : "模型资源与模型名待选择", 0, 200);
  if (modelConfig) model.config = { ...model.config, ...graphClone(modelConfig) };
  else {
    model.config.reference = { ...(model.config.reference as Record<string, unknown>), resource_id: "" };
    model.config.parameters = { ...(model.config.parameters as Record<string, unknown>), model: "" };
  }
  function agent(label: string, y: number, prompt: string) {
    const execute = node("agents.execute", compacting ? "4" : "3", `Agent ${label}`, 850, y);
    const key = `context/${label.toLowerCase()}`;
    const bound = { object_key: key, agent_node_id: execute.node_binding_id };
    const read = node("context.output", compacting ? "4" : "2", `${label} 上下文`, 0, y + 400, bound);
    const assembly = node("context.assembly", compacting ? "4" : "3", `${label} 装配`, 550, y);
    const merge = node("context.merge", compacting ? "4" : "2", `${label} 上下文写回`, 1150, y, bound);
    const material = node("prompts.item", compacting ? "2" : "1", `${label} 提示词`, 250, y + 200, { text: prompt });
    if (compacting) {
      const policy = node("agents.compaction-policy", "1", `${label} 精简策略`, 550, y + 300);
      edge(policy, "output", execute, "compaction_policy");
    }
    edge(input, "output", assembly, "current_input"); edge(read, "output", assembly, "view");
    edge(material, "output", assembly, "materials"); edge(assembly, "output", execute, "prompt");
    edge(model, "output", execute, "model"); edge(read, "output", merge, "view");
    edge(execute, "context", merge, "context");
    doc.object_bindings!.push({ object_key: key, type_id: "workflow.effective-context", schema_version: compacting ? 4 : 3,
      scope: "shared", owner_node_id: null, readers: [read.node_binding_id, merge.node_binding_id],
      writers: [merge.node_binding_id], default_value: { view_ref: null, accepted_delta_ids: [] } });
    return { execute, assembly, merge };
  }
  const a = agent("A", 0, "Analyze the user's request and produce useful working notes. Use inspect_text only when useful. Complete with final_answer containing an answer with text.");
  const b = agent("B", 700, "Answer the user's original request using Agent A's working notes in the final user material. Treat those notes as reference material. Use inspect_text only when useful. Complete with final_answer containing an answer with text.");
  const handoff = node(compacting ? "prompts.source" : "tools.text-to-prompt", compacting ? "2" : "1", "A 本轮分析材料", 1100, 450,
    { presentation: { role: "user", placement: "after", depth: null, order: 0, enabled: true } });
  edge(a.execute, "result", handoff, "input"); edge(handoff, "output", b.assembly, "materials", 1);
  control(b.execute, a.merge); control(a.merge, b.merge);
  const readUser = node("frontend.state.output", "1", "读取聊天记录", 1450, 0, { object_key: "frontend" });
  const user = node("frontend.state.append", "1", "追加用户输入", 1750, 0, { object_key: "frontend", role: "user" });
  const readAssistant = node("frontend.state.output", "1", "重新读取聊天记录", 2050, 0, { object_key: "frontend" });
  const assistant = node("frontend.state.append", "1", "追加最终回答", 2350, 0, { object_key: "frontend", role: "assistant" });
  const presentation = node("frontend.presentation", "1", "聊天展示", 2650, 0);
  presentation.public_outputs = ["display"];
  control(b.merge, readUser); edge(readUser, "view", user, "view"); edge(input, "output", user, "content");
  control(user, readAssistant); edge(readAssistant, "view", assistant, "view"); edge(b.execute, "result", assistant, "content");
  edge(assistant, "view", presentation, "view");
  doc.object_bindings!.push({ object_key: "frontend", type_id: "workflow.frontend-state", schema_version: 1,
    scope: "shared", owner_node_id: null, readers: [readUser, user, readAssistant, assistant].map(row => row.node_binding_id),
    writers: [user.node_binding_id, assistant.node_binding_id], default_value: { entries: [], view_ref: null } });
  doc.execution_roots = [a.merge.node_binding_id, b.merge.node_binding_id, presentation.node_binding_id];
  return doc;
}
