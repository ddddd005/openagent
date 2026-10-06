import { graphClone, nodePorts, upgradeGraph, type GraphDocument, type GraphNode, type GraphNodeType } from "./workflowGraph";

export const frontendStateType = "workflow.frontend-state";
export const frontendPackage = { package_id: "workflow.frontend-business", version: "1.0.0" };
export const frontendComponents = ["frontend.state.output", "frontend.state.append", "frontend.presentation"] as const;
export type FrontendRole = "user" | "assistant" | "system";
export interface FrontendWiringRequest {
  sourceNodeId: string;
  sourcePortId: string;
  objectKey: string;
  role: FrontendRole;
}
export function frontendSources(document: GraphDocument, catalog: GraphNodeType[]) {
  return document.nodes.flatMap(node => {
    const definition = catalog.find(type => type.component_id === node.component_id && type.component_version === node.component_version);
    return nodePorts(definition, node, "outputs").map(port => ({ node, port,
      compatible: !!definition?.executable && port.data_type === "TEXT" && port.data_schema_version === 2 }));
  });
}
export function frontendWiringUnavailable(catalog: GraphNodeType[], packageLock?: GraphDocument["package_lock"]) {
  const missing = frontendComponents.filter(id => !catalog.some(type => type.component_id === id
    && type.component_version === "1" && type.executable));
  return missing.length || packageLock && !packageLock.some(item => item.package_id === frontendPackage.package_id
    && item.version === frontendPackage.version)
    ? `前端展示包未加载${missing.length ? `：${missing.join("、")}` : ""}。请通过平台 packages.configure 显式启用 workflow.frontend-business@1.0.0 后刷新目录。` : null;
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
  const precedes = (from: string, to: string) => from !== to && ancestors([to]).has(from);
  return { links, ancestors, precedes };
}
function activeNodes(document: GraphDocument, catalog: GraphNodeType[], extra: string[] = []) {
  const targets = document.event_bindings?.length ? []
    : document.nodes.filter(node => definitionFor(catalog, node)?.is_output).map(node => node.node_binding_id);
  return dependencies(document).ancestors([...targets, ...(document.execution_roots ?? []), ...extra]);
}
function objectAccesses(document: GraphDocument, catalog: GraphNodeType[], key: string, active: Set<string>) {
  return document.nodes.filter(node => active.has(node.node_binding_id)).flatMap(node => {
    const type = definitionFor(catalog, node);
    if (!type && document.object_bindings?.some(binding => binding.object_key === key
      && [...binding.readers, ...binding.writers].includes(node.node_binding_id)))
      throw new Error("既有对象访问节点的类型缺失，无法证明接线顺序，请先恢复目录。");
    return (type?.object_accesses ?? []).filter(access => {
      const value = node.config[String(access.config_field)];
      return access.multiple ? Array.isArray(value) && value.includes(key) : value === key;
    }).map(access => {
      const binding = document.object_bindings?.find(row => row.object_key === key);
      if (binding && (binding.type_id !== access.type_id || binding.schema_version !== access.schema_version
        || access.access !== "write" && !binding.readers.includes(node.node_binding_id)
        || access.access !== "read" && !binding.writers.includes(node.node_binding_id)))
        throw new Error("既有对象访问契约或权限与绑定不匹配，请先显式修复对象绑定。");
      return { id: node.node_binding_id, write: access.access !== "read" };
    });
  });
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
function assertObjectOrder(document: GraphDocument, catalog: GraphNodeType[], key: string) {
  const active = activeNodes(document, catalog), { precedes } = dependencies(document);
  assertNoCycles(document, active);
  const accesses = objectAccesses(document, catalog, key, active);
  for (const access of accesses) for (const other of accesses) {
    if (access.id !== other.id && (access.write || other.write)
      && !precedes(access.id, other.id) && !precedes(other.id, access.id))
      throw new Error("既有前端对象读写顺序不明确。请先手工确认读写依赖，或选择新对象键；不会自动重排已有节点。");
  }
}

// Mutates only the editing entry's detached document; the caller accepts this entire change once.
export function wireFrontendDisplay(document: GraphDocument, catalog: GraphNodeType[],
  packageLock: NonNullable<GraphDocument["package_lock"]>, request: FrontendWiringRequest) {
  const missing = frontendWiringUnavailable(catalog, packageLock);
  if (missing) throw new Error(missing);
  if (document.package_lock?.some(item => item.package_id === frontendPackage.package_id && item.version !== frontendPackage.version))
    throw new Error("当前图锁定了其他前端展示包版本，请先显式处理包版本；不会自动更新旧图。");
  const source = frontendSources(document, catalog).find(row => row.node.node_binding_id === request.sourceNodeId
    && row.port.port_id === request.sourcePortId);
  if (!source?.compatible) throw new Error("请选择确切的 TEXT@2 输出端口；不会自动转换其他内容类型或版本。");
  if (!request.objectKey || request.objectKey.trim() !== request.objectKey || request.objectKey.length > 128)
    throw new Error("前端对象键须为 1–128 个字符，且首尾不能包含空格。");
  if (!["user", "assistant", "system"].includes(request.role)) throw new Error("请选择已登记的展示角色。");
  const existing = document.object_bindings?.find(binding => binding.object_key === request.objectKey);
  if (existing && (existing.type_id !== frontendStateType || existing.schema_version !== 1 || existing.scope !== "shared"))
    throw new Error("此对象键已被其他类型或私有对象占用，请选择共享的 workflow.frontend-state@1 对象或新键。");
  const activatedBefore = activeNodes(document, catalog, [source.node.node_binding_id]);
  assertNoCycles(document, activatedBefore);
  const { precedes } = dependencies(document);
  let previousWriter: string | undefined;
  if (existing) {
    const accesses = objectAccesses(document, catalog, request.objectKey, activatedBefore);
    if (accesses.length) {
      const tails = [...new Set(accesses.filter(access => access.write && accesses.every(other => other.id === access.id
        || precedes(other.id, access.id))).map(access => access.id))];
      if (tails.length !== 1) throw new Error("既有前端对象访问未形成明确的末次写入。请先在图中确认读写顺序，或选择新对象键；不会自动重排已有节点。");
      previousWriter = tails[0];
    }
  }
  const create = (component: string, x: number, y: number, config: Record<string, unknown>): GraphNode => {
    const type = catalog.find(row => row.component_id === component && row.component_version === "1")!;
    return { node_binding_id: crypto.randomUUID(), component_id: component, component_version: "1",
      title: type.display_name, position: { x, y }, config: { ...graphClone(type.default_config), ...config } };
  };
  const x = Math.max(...document.nodes.map(node => node.position.x), source.node.position.x) + 300;
  const read = create("frontend.state.output", x, source.node.position.y - 170, { object_key: request.objectKey });
  const append = create("frontend.state.append", x + 300, source.node.position.y,
    { object_key: request.objectKey, role: request.role });
  const existingAppendIds = new Set(document.nodes.filter(node => node.component_id === "frontend.state.append"
    && node.component_version === "1" && node.config.object_key === request.objectKey).map(node => node.node_binding_id));
  const existingViewIds = new Set([...existingAppendIds, ...document.nodes.filter(node =>
    node.component_id === "frontend.state.output" && node.component_version === "1"
      && node.config.object_key === request.objectKey).map(node => node.node_binding_id)]);
  const presentations = document.nodes.filter(node => node.component_id === "frontend.presentation" && node.component_version === "1"
    && node.public_outputs?.includes("display")
    && document.edges.some(edge => edge.target_node_id === node.node_binding_id && edge.target_port_id === "view"
      && edge.source_port_id === "view" && existingViewIds.has(edge.source_node_id)));
  if (presentations.length > 1) throw new Error("此对象已有多个公开展示节点，请先手工确认保留的展示来源。");
  const presentation = presentations[0] ?? create("frontend.presentation", x + 600, source.node.position.y, {});
  if (presentations.length && !activatedBefore.has(presentation.node_binding_id))
    throw new Error("此对象的公开展示节点当前未启用，请先显式设置其执行根；不会自动激活旧展示链。");
  if (precedes(presentation.node_binding_id, source.node.node_binding_id))
    throw new Error("所选来源依赖既有展示节点，重接展示会形成循环。");
  upgradeGraph(document, packageLock);
  const predecessors = previousWriter ? [previousWriter] : [];
  document.control_edges!.push(...predecessors.map(id => ({ edge_id: crypto.randomUUID(), source_node_id: id,
    target_node_id: read.node_binding_id })));
  document.nodes.push(read, append);
  if (!document.nodes.includes(presentation)) document.nodes.push(presentation);
  presentation.public_outputs = [...new Set([...(presentation.public_outputs ?? []), "display"])];
  document.edges = document.edges.filter(edge => !(edge.target_node_id === presentation.node_binding_id && edge.target_port_id === "view"));
  const edge = (sourceId: string, sourcePort: string, targetId: string, targetPort: string) => ({ edge_id: crypto.randomUUID(),
    source_node_id: sourceId, source_port_id: sourcePort, target_node_id: targetId, target_port_id: targetPort, order: 0 });
  document.edges.push(edge(read.node_binding_id, "view", append.node_binding_id, "view"),
    edge(source.node.node_binding_id, source.port.port_id, append.node_binding_id, "content"),
    edge(append.node_binding_id, "view", presentation.node_binding_id, "view"));
  document.execution_roots = [...new Set([...document.execution_roots!, presentation.node_binding_id])];
  if (existing) {
    existing.readers.push(read.node_binding_id, append.node_binding_id);
    existing.writers.push(append.node_binding_id);
  } else document.object_bindings!.push({ object_key: request.objectKey, type_id: frontendStateType, schema_version: 1,
    scope: "shared", owner_node_id: null, readers: [read.node_binding_id, append.node_binding_id],
    writers: [append.node_binding_id], default_value: { entries: [], view_ref: null } });
  assertObjectOrder(document, catalog, request.objectKey);
  const allowed = new Set([...activatedBefore, read.node_binding_id, append.node_binding_id, presentation.node_binding_id]);
  if ([...activeNodes(document, catalog)].some(id => !allowed.has(id)))
    throw new Error("接线将激活其他旧节点，请先手工确认执行依赖。");
  return [read.node_binding_id, append.node_binding_id, presentation.node_binding_id];
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
  assertObjectOrder(document, catalog, String(node.config.object_key));
  return true;
}
