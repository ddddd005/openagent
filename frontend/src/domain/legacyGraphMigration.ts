import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { graphClone, graphObject, graphUuid, nodePorts,
  type GraphDiagnostic, type GraphDocument, type GraphNode, type GraphNodeType } from "./workflowGraph";

export interface LegacyGraphMigration {
  document: GraphDocument;
  mappings: { source_node_id: string; target_node_id: string }[];
  diagnostics: GraphDiagnostic[];
}

// Conversion reads the saved named links; it never changes the legacy document or history.
export function migrateLegacyGraph(source: GraphDocument, catalog: GraphNodeType[],
  sourceSessionId: string | null = null): LegacyGraphMigration {
  const document = graphClone(source);
  document.workflow_definition_id = crypto.randomUUID();
  document.revision = 1;
  document.name = `${source.name}（普通图）`;
  const diagnostics: GraphDiagnostic[] = [];
  const error = (node: GraphNode, message: string, port_id?: string) => diagnostics.push({
    reason_code: "graph_legacy_migration_unsupported", message, node_id: node.node_binding_id,
    ...(port_id ? { port_id } : {}),
  });
  const originals = new Map(source.nodes.map(node => [node.node_binding_id, node]));
  const agents = source.nodes.filter(node => node.component_id === "legacy.agent");
  const stageAgent = (stage: unknown) => agents.find(node => node.config.stage === stage);
  const assemblies = source.nodes.filter(node => node.component_id === "legacy.preparation.assemble");
  const assemblyStage = (node: GraphNode) => node.config.targetStage
    ?? originals.get(source.edges.find(edge => edge.source_node_id === node.node_binding_id
      && edge.source_port_id === "assembled" && edge.target_port_id === "prompt-in")?.target_node_id ?? "")?.config.stage;
  const downstreamStages = (id: string, visited = new Set<string>()): Set<unknown> => {
    if (visited.has(id)) return new Set();
    visited.add(id);
    const node = originals.get(id);
    if (node?.component_id === "legacy.preparation.assemble") return new Set([assemblyStage(node)]);
    return new Set(source.edges.filter(edge => edge.source_node_id === id)
      .flatMap(edge => [...downstreamStages(edge.target_node_id, visited)]));
  };
  const removedRoots = new Map<string, string>();
  const ports = new Map<string, { input: Record<string, string>; output: Record<string, string> }>();
  const alias = (node: GraphNode, component: string, input: Record<string, string>, output: Record<string, string>, version = "1") => {
    node.component_id = `workflow.${component}`;
    node.component_version = version;
    ports.set(node.node_binding_id, { input, output });
  };
  for (const node of document.nodes) {
    const original = originals.get(node.node_binding_id)!;
    const kind = original.component_id.replace("legacy.preparation.", "");
    if (original.component_id === "legacy.agent") {
      alias(node, "agent", { "prompt-in": "prompt", "model-in": "model" }, { out: "output" }, "2");
      node.config = { max_model_requests: 8, max_model_attempts: 32 };
    } else if (original.component_id === "legacy.output") {
      alias(node, "output", { in: "input" }, { out: "output" }); node.config = { mode: "text" };
    } else if (original.component_id === "legacy.model-provider") {
      alias(node, "model-provider", {}, { "model-out": "model" }, "2");
      const reference = original.config.provider_ref;
      node.config = { provider_id: graphObject(reference) ? reference.provider_id : null,
        parameters: graphClone(original.config.parameters) };
    } else if (["prompt-item", "prompt-group", "text", "text-to-prompt", "prompt-to-text", "regex", "variable-replace",
      "variable-register", "variable-assign", "session-data-read", "session-data-write", "json-to-text"].includes(kind)) {
      const mode = String(node.config.mode);
      const input: Record<string, string> = ["text-to-prompt", "regex", "variable-replace"].includes(kind)
        ? { [kind === "text-to-prompt" ? "text" : mode]: "input" }
        : kind === "prompt-to-text" ? { prompt: "input" } : kind === "json-to-text" ? { json: "input" }
        : ["variable-register", "variable-assign", "session-data-write"].includes(kind) ? { text: "text" } : {};
      const output: Record<string, string> = kind.startsWith("variable-") && kind !== "variable-replace" ? { text: "text" }
        : kind.startsWith("session-data-") ? { json: "json" }
        : { [kind === "regex" || kind === "variable-replace" ? mode
          : ["prompt-item", "prompt-group", "text-to-prompt"].includes(kind) ? "prompt" : "text"]: "output" };
      alias(node, kind, input, output);
    } else if (kind === "prompt-collect") {
      alias(node, "prompt-summary", { prompt: "input" }, { prompt: "output" }, "2"); node.config = {};
    } else if (kind === "tool") {
      alias(node, "tool", {}, { "tool-descriptions": "tool-descriptions", "tool-schemas": "tool-schemas" });
    } else if (kind === "tool-collect") {
      alias(node, "tool-summary", { "tool-descriptions": "tool-descriptions", "tool-schemas": "tool-schemas" },
        { "tool-descriptions": "tool-descriptions", "tool-schemas": "tool-schemas" }); node.config = {};
    } else if (kind === "global-content") {
      alias(node, "global-content", {}, { prompt: "output" }); node.config = { resource_id: original.config.resourceId };
    } else if (kind === "context") {
      const reference = original.config.reference;
      const binding = original.config.sourceBindingId ?? (graphObject(reference) ? reference.nodeBindingId : undefined);
      const selected = agents.find(agent => agent.node_binding_id === binding)
        ?? stageAgent(binding === AGENT_BINDINGS.B ? "B" : binding === AGENT_BINDINGS.A ? "A" : undefined);
      alias(node, "context", {}, { context: "output" }); node.config = { source_node_id: selected?.node_binding_id ?? binding };
      if (!selected || !graphUuid(node.config.source_node_id)) error(node, "上下文来源没有明确对应的 Agent 节点");
      if (graphObject(reference) && reference.workflowSessionId !== sourceSessionId)
        error(node, "跨会话上下文选择需要显式重新绑定，原来源仍保留在旧工作流");
      if (graphObject(reference) && reference.selectedChainId !== null && reference.selectedChainId !== undefined)
        error(node, "指定历史链的上下文选择不能改成当前选中历史，请先核对来源");
    } else if (kind === "assemble") {
      alias(node, "prompt-assembly", { prompt: "input", context: "input", "tool-descriptions": "input",
        "tool-schemas": "input", "current-input": "current_input" }, { assembled: "output" });
      node.config = { max_messages: 4096, max_total_chars: 4000000 };
      if (!["A", "B"].includes(String(assemblyStage(original)))) error(node, "装配节点的目标无法由旧具名连线确定");
    } else if (kind === "root-input") {
      const stages = downstreamStages(node.node_binding_id);
      if (stages.size !== 1 || !stages.has("A") && !stages.has("B")) error(node, "当前输入跨多个或未知 Agent，不能猜测数据来源");
      if (original.config.text !== "") error(node, "已编辑的当前输入内容需要显式选择迁移语义");
      if (stages.has("B") && stages.size === 1) {
        const upstream = stageAgent("A");
        if (upstream) removedRoots.set(node.node_binding_id, upstream.node_binding_id);
        else error(node, "B 当前输入没有 A 上游来源");
      } else {
        alias(node, "current-input", {}, { "current-input": "output" }); node.config = { input_name: "text" };
      }
    } else error(node, `节点类型 ${original.component_id} 尚无明确迁移规则`);
    if (node.public_outputs) node.public_outputs = node.public_outputs.map(port => {
      const mapped = ports.get(node.node_binding_id)?.output[port];
      if (!mapped) error(node, `公开输出 ${port} 无法迁移`, port);
      return mapped ?? port;
    });
  }
  document.nodes = document.nodes.filter(node => !removedRoots.has(node.node_binding_id));
  const oldInputGroups = new Map<string, GraphDocument["edges"]>();
  for (const edge of source.edges) {
    const key = `${edge.target_node_id}/${edge.target_port_id}`;
    const group = oldInputGroups.get(key) ?? []; group.push(edge); oldInputGroups.set(key, group);
  }
  const orderedEdges = [...oldInputGroups.values()].flatMap(group => group.sort((a, b) => a.order - b.order));
  document.edges = orderedEdges.flatMap(edge => {
    const from = originals.get(edge.source_node_id); const to = originals.get(edge.target_node_id);
    if (!from || !to) {
      diagnostics.push({ reason_code: "graph_legacy_migration_unsupported", message: "旧连线缺少节点，不能丢弃",
        edge_id: edge.edge_id }); return [];
    }
    if (from.component_id === "legacy.agent" && to.component_id === "legacy.agent" && edge.source_port_id === "out" && edge.target_port_id === "in") {
      const roots = [...removedRoots.values()];
      if (from.config.stage === "A" && to.config.stage === "B" && roots.includes(from.node_binding_id)) return [];
      error(to, "Agent 间旧主连线没有可替换的提示词当前输入路径", edge.target_port_id); return [];
    }
    const removed = removedRoots.get(edge.source_node_id);
    const sourcePort = removed ? "output_json" : ports.get(from.node_binding_id)?.output[edge.source_port_id];
    const targetPort = ports.get(to.node_binding_id)?.input[edge.target_port_id];
    if (!sourcePort || !targetPort || removedRoots.has(edge.target_node_id)) {
      error(!sourcePort ? from : to, "旧具名端口没有明确映射", !sourcePort ? edge.source_port_id : edge.target_port_id); return [];
    }
    return [{ ...graphClone(edge), source_node_id: removed ?? edge.source_node_id,
      source_port_id: sourcePort, target_port_id: targetPort }];
  });
  const orders = new Map<string, number>();
  for (const edge of document.edges) {
    const key = `${edge.target_node_id}/${edge.target_port_id}`;
    edge.order = orders.get(key) ?? 0; orders.set(key, edge.order + 1);
  }
  for (const node of document.nodes) {
    const type = catalog.find(row => row.component_id === node.component_id && row.component_version === node.component_version);
    if (!type || !type.executable) error(node, "迁移需要的节点实现尚未接入当前后端");
    for (const edge of document.edges.filter(edge => edge.source_node_id === node.node_binding_id || edge.target_node_id === node.node_binding_id)) {
      const direction = edge.source_node_id === node.node_binding_id ? "outputs" : "inputs";
      const port = direction === "outputs" ? edge.source_port_id : edge.target_port_id;
      if (type && !nodePorts(type, node, direction).some(row => row.port_id === port)) error(node, "迁移端口不符合当前节点目录", port);
    }
  }
  const reachable = new Set<string>();
  const visit = (id: string) => {
    if (reachable.has(id)) return;
    reachable.add(id);
    document.edges.filter(edge => edge.target_node_id === id).forEach(edge => visit(edge.source_node_id));
  };
  for (const node of document.nodes) if (catalog.find(type => type.component_id === node.component_id
    && type.component_version === node.component_version)?.is_output) visit(node.node_binding_id);
  for (const node of source.nodes) if (["legacy.preparation.variable-register", "legacy.preparation.variable-assign",
    "legacy.preparation.session-data-write"].includes(node.component_id) && !reachable.has(node.node_binding_id))
    error(node, "旧隐式会话写入没有输出依赖，需要补明确数据连线后再迁移");
  return { document, mappings: sourceSessionId === null ? [] : agents.map(node => ({
    source_node_id: node.node_binding_id, target_node_id: node.node_binding_id })), diagnostics };
}
