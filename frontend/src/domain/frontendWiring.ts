import { nodePorts, type GraphDocument, type GraphNode, type GraphNodeType } from "./workflowGraph";

export const frontendStateType = "workflow.frontend-state";
export type FrontendRole = "user" | "assistant" | "system";
export function frontendSources(document: GraphDocument, catalog: GraphNodeType[]) {
  return document.nodes.flatMap(node => {
    const definition = catalog.find(type => type.component_id === node.component_id && type.component_version === node.component_version);
    return nodePorts(definition, node, "outputs").map(port => ({ node, port,
      compatible: !!definition?.executable && port.data_type === "TEXT" && port.data_schema_version === 2 }));
  });
}

const definitionFor = (catalog: GraphNodeType[], node: GraphNode) => catalog.find(type =>
  type.component_id === node.component_id && type.component_version === node.component_version);
function dependencies(document: GraphDocument) {
  const links = [...document.edges, ...(document.control_edges ?? [])];
  const ancestors = (targets: string[]) => {
    const pending = [...targets], reached = new Set<string>();
    while (pending.length) {
      const id = pending.pop()!;
      if (reached.has(id)) continue;
      reached.add(id);
      pending.push(...links.filter(link => link.target_node_id === id).map(link => link.source_node_id));
    }
    return reached;
  };
  return { links, ancestors };
}
function activeNodes(document: GraphDocument, catalog: GraphNodeType[], extra: string[] = []) {
  const targets = document.event_bindings?.length ? []
    : document.nodes.filter(node => definitionFor(catalog, node)?.is_output).map(node => node.node_binding_id);
  return dependencies(document).ancestors([...targets, ...(document.execution_roots ?? []), ...extra]);
}
function objectAccesses(document: GraphDocument, catalog: GraphNodeType[], key: string, active: Set<string>) {
  for (const node of document.nodes) {
    if (!active.has(node.node_binding_id)) continue;
    const type = definitionFor(catalog, node);
    if (!type && document.object_bindings?.some(binding => binding.object_key === key
      && [...binding.readers, ...binding.writers].includes(node.node_binding_id)))
      throw new Error("既有对象访问节点的类型缺失，无法验证访问契约，请先恢复目录。");
    for (const access of type?.object_accesses ?? []) {
      const value = node.config[String(access.config_field)];
      const matches = access.multiple ? Array.isArray(value) && value.includes(key) : value === key;
      if (!matches) continue;
      const binding = document.object_bindings?.find(row => row.object_key === key);
      if (binding && (binding.type_id !== access.type_id || binding.schema_version !== access.schema_version
        || access.access !== "write" && !binding.readers.includes(node.node_binding_id)
        || access.access !== "read" && !binding.writers.includes(node.node_binding_id)))
        throw new Error("既有对象访问契约或权限与绑定不匹配，请先显式修复对象绑定。");
    }
  }
}
function assertNoCycles(document: GraphDocument, active: Set<string>) {
  const { links } = dependencies(document), visiting = new Set<string>(), visited = new Set<string>();
  const visit = (id: string) => {
    if (visiting.has(id)) throw new Error("此来源或展示接线会形成依赖循环，请先手工调整图。");
    if (visited.has(id)) return;
    visiting.add(id);
    for (const link of links.filter(link => link.target_node_id === id)) visit(link.source_node_id);
    visiting.delete(id); visited.add(id);
  };
  for (const id of active) visit(id);
}
function assertObjectAccess(document: GraphDocument, catalog: GraphNodeType[], key: string) {
  const active = activeNodes(document, catalog);
  assertNoCycles(document, active);
  objectAccesses(document, catalog, key, active);
}

export function editFrontendSource(document: GraphDocument, catalog: GraphNodeType[], nodeId: string,
  edgeId: string | null, source: { nodeId: string; portId: string } | null) {
  const node = document.nodes.find(row => row.node_binding_id === nodeId);
  if (!node || node.component_id !== "frontend.state.append" || node.component_version !== "1") return false;
  const incoming = document.edges.filter(edge => edge.target_node_id === nodeId && edge.target_port_id === "content");
  const previous = edgeId ? incoming.find(edge => edge.edge_id === edgeId) : null;
  if (edgeId && !previous) return false;
  if (source && !frontendSources(document, catalog).some(row => row.compatible
    && row.node.node_binding_id === source.nodeId && row.port.port_id === source.portId))
    throw new Error("展示来源须为确切的 TEXT@2 输出端口。");
  if (source && incoming.some(edge => edge.edge_id !== edgeId
    && edge.source_node_id === source.nodeId && edge.source_port_id === source.portId))
    throw new Error("此文本来源已接入当前追加节点。");
  if (!source) {
    if (!previous) return false;
    document.edges = document.edges.filter(edge => edge.edge_id !== edgeId);
  } else if (previous) {
    previous.source_node_id = source.nodeId; previous.source_port_id = source.portId;
  } else document.edges.push({ edge_id: crypto.randomUUID(), source_node_id: source.nodeId,
    source_port_id: source.portId, target_node_id: nodeId, target_port_id: "content",
    order: incoming.length ? Math.max(...incoming.map(edge => edge.order)) + 1 : 0 });
  assertObjectAccess(document, catalog, String(node.config.object_key));
  return true;
}
