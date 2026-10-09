import { nodePorts, type GraphDocument, type GraphEdge, type GraphNodeType } from "./workflowGraph";

export type ConnectionRequest = Omit<GraphEdge, "edge_id" | "order">;
export function graphConnectionIssue(document: GraphDocument, catalog: GraphNodeType[], edge: ConnectionRequest): string | null {
  const source = document.nodes.find(node => node.node_binding_id === edge.source_node_id);
  const target = document.nodes.find(node => node.node_binding_id === edge.target_node_id);
  if (!source || !target) return "连线节点已不存在";
  const typeFor = (node: typeof source) => catalog.find(type =>
    type.component_id === node.component_id && type.component_version === node.component_version);
  const sourceType = typeFor(source), targetType = typeFor(target);
  if (!sourceType || !targetType) return "节点版本缺失，不能确认端口";
  if ([sourceType, targetType].some((type, index) =>
    type.port_modes && !type.port_modes[String((index ? target : source).config.mode)])) return "节点模式无效";
  const output = nodePorts(sourceType, source, "outputs").find(port => port.port_id === edge.source_port_id);
  const input = nodePorts(targetType, target, "inputs").find(port => port.port_id === edge.target_port_id);
  if (!output || !input) return "端口缺失或模式已变化，请核对连线";
  if (output.data_type !== input.data_type || (output.data_schema_version ?? 1) !== (input.data_schema_version ?? 1))
    return `端口不兼容：${output.data_type}@${output.data_schema_version ?? 1} → ${input.data_type}@${input.data_schema_version ?? 1}`;
  if (document.edges.some(row => Object.entries(edge).every(([key, value]) => row[key as keyof GraphEdge] === value)))
    return "这条连线已存在";
  if (!input.multiple && document.edges.some(row => row.target_node_id === edge.target_node_id
    && row.target_port_id === edge.target_port_id)) return "此输入只接受一条连线";
  if (graphDependencyCycle(document, edge.source_node_id, edge.target_node_id)) return "连线会形成循环（包含前序依赖）";
  return null;
}

export function graphDependencyCycle(document: GraphDocument, source: string, target: string): boolean {
  const visited = new Set<string>(), pending = [target];
  const edges = [...document.edges, ...(document.control_edges ?? [])];
  while (pending.length) {
    const id = pending.pop()!;
    if (id === source) return true;
    if (visited.has(id)) continue;
    visited.add(id);
    pending.push(...edges.filter(edge => edge.source_node_id === id).map(edge => edge.target_node_id));
  }
  return false;
}
