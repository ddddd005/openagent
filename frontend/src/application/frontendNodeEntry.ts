import { WorkbenchApiError, workbenchRequest } from "../adapters/workbenchApi";
import { graphClone, graphJsonSignature, graphObject, graphUuid,
  type GraphDocument, type GraphEntry, type GraphNode, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";

export interface FrontendNodeOwner {
  workflow_definition_id: string;
  definition_revision: number;
  node_binding_id: string;
  session_id: string | null;
}
export interface FrontendNodeEntry {
  schema_version: 1;
  kind: "workflow.frontend-node-entry";
  owner: FrontendNodeOwner;
  page_url: string;
  channel_url: string;
  events_url: string;
}
export interface FrontendNodeLaunchTarget { owner: FrontendNodeOwner | null; diagnosis: string }
export interface FrontendNodePopup {
  opener: unknown;
  location: { href: string };
  close(): void;
}
const exactUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
function exactOwner(value: unknown): value is FrontendNodeOwner {
  return graphObject(value) && Object.keys(value).length === 4
    && exactUuid(value.workflow_definition_id) && exactUuid(value.node_binding_id)
    && Number.isSafeInteger(value.definition_revision) && Number(value.definition_revision) >= 1
    && (value.session_id === null || exactUuid(value.session_id));
}
function address(owner: FrontendNodeOwner) {
  if (!exactOwner(owner)) throw new WorkbenchApiError("rejected", "前端节点绑定无效");
  return `${owner.workflow_definition_id}/${owner.definition_revision}/${owner.node_binding_id}/${owner.session_id ?? "none"}`;
}
export function frontendNodeInputs(document: GraphDocument, catalog: GraphNodeType[]) {
  if (!catalog.some(type => type.component_id === "tools.editable-input"
    && type.component_version === "1" && type.executable)) return [];
  return document.nodes.filter(node => node.component_id === "tools.editable-input" && node.component_version === "1");
}
export function frontendNodeLaunchTarget(entry: GraphEntry | undefined,
  target: { node: GraphNode; document: GraphDocument }, session: GraphSession | null,
  unresolved = false): FrontendNodeLaunchTarget {
  const unavailable = (diagnosis: string): FrontendNodeLaunchTarget => ({ owner: null, diagnosis });
  if (!entry || entry.saved_revision < 1) return unavailable("正式保存前端节点后打开");
  if (entry.pending || unresolved) return unavailable("先核实原请求，再打开前端节点");
  const saved = entry.saved_document, node = target.node;
  if (!saved || saved.schema_version !== 3 || saved.revision !== entry.saved_revision
    || saved.workflow_definition_id !== entry.document.workflow_definition_id
    || target.document.workflow_definition_id !== saved.workflow_definition_id
    || target.document.revision !== saved.revision
    || !target.document.nodes.some(item => item.node_binding_id === node.node_binding_id
      && item.component_id === node.component_id && item.component_version === node.component_version)
    || node.component_id !== "frontend.node" || !["1", "2"].includes(node.component_version))
    return unavailable("缺少当前前端节点的准确已保存定义");
  const savedNode = saved.nodes.find(item => item.node_binding_id === node.node_binding_id);
  if (!savedNode || savedNode.component_id !== node.component_id
    || savedNode.component_version !== node.component_version
    || graphJsonSignature(savedNode.config) !== graphJsonSignature(node.config))
    return unavailable("当前前端节点配置尚未正式保存");
  if (entry.session_id !== null && (!session || session.workflow_session_id !== entry.session_id
    || session.workflow_definition_id !== saved.workflow_definition_id
    || session.definition_revision !== saved.revision))
    return unavailable("所选会话与前端节点的已保存修订不一致");
  if (session?.archived || session?.readonly) return unavailable("当前会话不可打开前端节点");
  const owner = {
    workflow_definition_id: saved.workflow_definition_id, definition_revision: saved.revision,
    node_binding_id: node.node_binding_id, session_id: entry.session_id,
  };
  return exactOwner(owner) ? { owner, diagnosis: "打开前端节点" } : unavailable("前端节点绑定身份无效");
}
export function validateFrontendNodeEntry(value: unknown, owner: FrontendNodeOwner): FrontendNodeEntry {
  const invalid = () => new WorkbenchApiError("unavailable", "前端节点入口的归属或地址不匹配");
  const bound = address(owner);
  if (!graphObject(value) || Object.keys(value).length !== 6 || value.schema_version !== 1
    || value.kind !== "workflow.frontend-node-entry" || !exactOwner(value.owner)
    || graphJsonSignature(value.owner) !== graphJsonSignature(owner)) throw invalid();
  const expected = {
    page_url: `/frontend-node/${bound}/`,
    channel_url: `/api/graph/frontend-nodes/${bound}/channel`,
    events_url: `/api/graph/frontend-nodes/${bound}/events`,
  };
  let origin: string | null = null;
  for (const [field, pathname] of Object.entries(expected)) {
    const raw = value[field];
    if (typeof raw !== "string") throw invalid();
    let url: URL;
    try { url = new URL(raw); } catch { throw invalid(); }
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.pathname !== pathname || url.search || url.hash
      || raw !== url.href || origin !== null && url.origin !== origin) throw invalid();
    origin = url.origin;
  }
  return graphClone(value) as unknown as FrontendNodeEntry;
}
export async function readFrontendNodeEntry(owner: FrontendNodeOwner): Promise<FrontendNodeEntry> {
  const original = graphClone(owner), path = `/api/graph/frontend-nodes/${address(original)}/entry`;
  return validateFrontendNodeEntry(await workbenchRequest<unknown>(path), original);
}
export async function openFrontendNodeEntry(owner: FrontendNodeOwner, ports: {
  current(): boolean;
  openBlank(): FrontendNodePopup | null;
  read?(owner: FrontendNodeOwner): Promise<FrontendNodeEntry>;
}): Promise<void> {
  const original = graphClone(owner);
  address(original);
  if (!ports.current()) throw new WorkbenchApiError("rejected", "当前前端节点已变化，请重新打开");
  const popup = ports.openBlank();
  if (!popup) throw new WorkbenchApiError("unavailable", "浏览器阻止了新窗口，请允许后打开前端节点");
  popup.opener = null;
  try {
    const entry = validateFrontendNodeEntry(await (ports.read ?? readFrontendNodeEntry)(original), original);
    if (!ports.current()) throw new WorkbenchApiError("rejected", "当前前端节点已变化，未打开迟到入口");
    popup.location.href = entry.page_url;
  } catch (error) {
    popup.close();
    throw error;
  }
}
