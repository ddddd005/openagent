import { graphClone, graphObject, graphUuid, isGraphDocument, type GraphEntry } from "../domain/workflowGraph";
import type { DraftStorage } from "./browserStorage";
import type { WorkspaceWorkflow } from "../domain/workspace";

export const WORKBENCH_STORAGE_KEY = "workflow-workbench:fixed-base:v1";
export const WORKBENCH_RECOVERY_STORAGE_KEY = "workflow-workbench:isolated-graph:v7";
const MAX_BYTES = 4_000_000;
// Keep these exact identities aligned with backend storage_retirement._RETIRED_NODES.
const RETIRED_COMPONENTS = [
  ["workflow.text", "1"], ["workflow.current-input", "1"],
  ["workflow.output", "1"], ["workflow.regex", "1"],
  ["workflow.text-to-prompt", "1"], ["workflow.prompt-to-text", "1"],
  ["workflow.json-to-text", "1"], ["workflow.prompt-item", "1"],
  ["workflow.prompt-group", "1"], ["workflow.prompt-source", "1"],
  ["workflow.prompt-summary", "1"], ["workflow.prompt-summary", "2"],
  ["workflow.tool", "1"], ["workflow.tool-summary", "1"],
  ["workflow.global-content", "1"], ["workflow.global-content", "2"],
  ["workflow.variable-register", "1"], ["workflow.variable-assign", "1"],
  ["workflow.variable-replace", "1"], ["workflow.session-data-read", "1"],
  ["workflow.session-data-write", "1"], ["workflow.object-read", "1"],
  ["workflow.object-write", "1"], ["workflow.object-delete", "1"],
  ["workflow.agent", "1"], ["workflow.agent", "2"],
  ["workflow.model-provider", "1"], ["workflow.model-provider", "2"],
  ["workflow.context", "1"], ["workflow.prompt-assembly", "1"],
  ["models.source", "1"], ["models.source", "3"],
  ["models.chat", "1"], ["models.chat", "2"],
  ["agents.execute", "1"], ["agents.execute", "2"], ["agents.execute", "3"],
  ["agents.execute", "5"], ["agents.execute", "6"], ["agents.execute", "7"],
  ["agents.delta", "1"], ["agents.delta", "2"],
  ["context.output", "1"], ["context.output", "2"],
  ["context.assembly", "1"], ["context.assembly", "2"], ["context.assembly", "3"],
  ["context.merge", "1"], ["context.merge", "2"],
  ["context.window", "1"], ["context.window", "2"], ["context.window", "3"],
  ["context.read", "1"], ["context.save", "1"], ["context.advance", "1"],
  ["context.project-text", "1"], ["context.plan", "1"], ["context.summary-prompt", "1"],
  ["context.summary", "1"], ["context.replace", "1"],
  ["prompts.item", "1"], ["prompts.group", "1"], ["prompts.source", "1"],
  ["prompts.summary", "1"], ["prompts.assembly", "1"], ["prompts.global-reference", "1"],
  ["prompts.global-resolve", "1"], ["prompts.tool", "1"], ["prompts.tool-summary", "1"],
] as const;
export interface WorkbenchConfiguration {
  activeWorkflowId: string;
  selectedWorkflowId: string;
  catalog: WorkspaceWorkflow[];
  graph: { schema_version: 1; entries: Record<string, GraphEntry> };
}
export interface SavedWorkbench extends WorkbenchConfiguration {
  schemaVersion: 7;
  kind: "graph-workbench";
  revision: number;
  savedAt: string;
}
export interface WorkbenchRead {
  value: SavedWorkbench | null;
  raw: string | null;
  error: string | null;
  rewrite: boolean;
}
function metadata(value: Record<string, unknown>) {
  return Number.isSafeInteger(value.revision) && Number(value.revision) > 0
    && typeof value.savedAt === "string" && Number.isFinite(Date.parse(value.savedAt));
}
function workflow(value: unknown): value is WorkspaceWorkflow {
  return graphObject(value) && graphUuid(value.id) && typeof value.title === "string"
    && typeof value.description === "string" && Number.isSafeInteger(value.nodeCount) && Number(value.nodeCount) >= 0
    && (value.state === undefined || value.state === "saved" || value.state === "draft")
    && (value.sourceId === undefined || typeof value.sourceId === "string")
    && (value.copyPending === undefined || typeof value.copyPending === "boolean");
}
function entry(value: unknown): value is GraphEntry {
  return graphObject(value) && isGraphDocument(value.document)
    && Number.isSafeInteger(value.saved_revision) && Number(value.saved_revision) >= 0
    && (value.session_id === null || graphUuid(value.session_id))
    && (value.pending === null || graphObject(value.pending));
}
function explicitLegacy(document: unknown, row?: Record<string, unknown>) {
  if (row && Object.hasOwn(row, "compatibility")) return true;
  const pending = row?.pending;
  const body = graphObject(pending) && graphObject(pending.body) ? pending.body : null;
  return [document, row?.saved_document, body?.document].some(candidate =>
    graphObject(candidate) && (Array.isArray(candidate.package_lock) && candidate.package_lock.some(lock =>
      graphObject(lock) && ["workflow.compat", "workflow.context-compression"].includes(String(lock.package_id)))
      || Array.isArray(candidate.nodes) && candidate.nodes.some(node =>
        graphObject(node) && RETIRED_COMPONENTS.some(([id, version]) =>
          node.component_id === id && node.component_version === version))));
}
export function isSavedWorkbench(value: unknown): value is SavedWorkbench {
  if (!graphObject(value) || value.schemaVersion !== 7 || value.kind !== "graph-workbench" || !metadata(value)
    || !Array.isArray(value.catalog) || !value.catalog.length || value.catalog.length > 128
    || !value.catalog.every(workflow) || new Set(value.catalog.map(row => row.id)).size !== value.catalog.length
    || !graphObject(value.graph) || value.graph.schema_version !== 1 || !graphObject(value.graph.entries)) return false;
  const entries = value.graph.entries;
  return Object.keys(entries).length === value.catalog.length
    && value.catalog.every(row => entry(entries[row.id]) && !explicitLegacy(
      (entries[row.id] as GraphEntry).document, entries[row.id] as unknown as Record<string, unknown>))
    && value.catalog.some(row => row.id === value.activeWorkflowId)
    && value.catalog.some(row => row.id === value.selectedWorkflowId);
}
function readMixed(value: Record<string, unknown>): SavedWorkbench {
  if (!metadata(value) || !Array.isArray(value.catalog) || !graphObject(value.documents)
    || !graphObject(value.graph) || value.graph.schema_version !== 1 || !graphObject(value.graph.entries))
    throw new Error("schema");
  const sourceCatalog = value.catalog, documents = value.documents, entries = value.graph.entries;
  if (sourceCatalog.length > 128 || new Set(sourceCatalog.map(row => graphObject(row) && row.id)).size !== sourceCatalog.length)
    throw new Error("catalog");
  const catalog: WorkspaceWorkflow[] = [];
  const currentEntries: Record<string, GraphEntry> = {};
  for (const row of sourceCatalog) {
    if (!graphObject(row) || typeof row.id !== "string" || !graphObject(documents[row.id]))
      throw new Error("catalog");
    const documentRow = documents[row.id] as Record<string, unknown>;
    const entryRow = entries[row.id];
    if (explicitLegacy(documentRow.document, documentRow)
      || graphObject(entryRow) && explicitLegacy(documentRow.document, entryRow)) continue;
    if (!workflow(row) || !isGraphDocument(documentRow.document) || !graphObject(entryRow))
      throw new Error("current-schema");
    const restored = { ...graphClone(entryRow),
      document: graphClone(documentRow.document) };
    if (!entry(restored)) throw new Error("metadata");
    catalog.push(graphClone(row));
    currentEntries[row.id] = restored;
  }
  // Unclassified records must not disappear merely because the catalog omitted them.
  if (Object.keys(documents).some(id => !sourceCatalog.some(row => graphObject(row) && row.id === id))
    || Object.keys(entries).some(id => !sourceCatalog.some(row => graphObject(row) && row.id === id)))
    throw new Error("unclassified");
  const fallback = catalog.find(row => !row.copyPending)?.id ?? catalog[0]?.id ?? "";
  return { schemaVersion: 7, kind: "graph-workbench", revision: Number(value.revision), savedAt: String(value.savedAt),
    activeWorkflowId: catalog.some(row => row.id === value.activeWorkflowId) ? String(value.activeWorkflowId) : fallback,
    selectedWorkflowId: catalog.some(row => row.id === value.selectedWorkflowId) ? String(value.selectedWorkflowId) : fallback,
    catalog, graph: { schema_version: 1, entries: currentEntries } };
}
function readRetiredCurrent(value: Record<string, unknown>): SavedWorkbench {
  if (!Array.isArray(value.catalog) || !value.catalog.length
    || !graphObject(value.graph) || !graphObject(value.graph.entries)
    || !value.catalog.some(row => graphObject(row) && row.id === value.activeWorkflowId)
    || !value.catalog.some(row => graphObject(row) && row.id === value.selectedWorkflowId))
    throw new Error("schema");
  const sourceEntries = value.graph.entries;
  if (!Object.values(sourceEntries).some(row => graphObject(row) && explicitLegacy(row.document, row)))
    throw new Error("schema");
  const documents = Object.fromEntries(Object.entries(sourceEntries).map(([id, row]) =>
    [id, graphObject(row) ? { document: row.document } : null]));
  return readMixed({ ...value, documents });
}
export function readWorkbench(storage: DraftStorage): WorkbenchRead {
  let raw: string | null = null;
  try {
    raw = storage.getItem(WORKBENCH_STORAGE_KEY);
    if (raw === null) return { value: null, raw, error: null, rewrite: false };
    if (raw.length > MAX_BYTES) throw new Error("capacity");
    const value: unknown = JSON.parse(raw);
    if (isSavedWorkbench(value)) return { value, raw, error: null, rewrite: false };
    if (graphObject(value) && value.schemaVersion === 6 && value.kind === "unified-workbench")
      return { value: readMixed(value), raw, error: null, rewrite: true };
    if (graphObject(value) && value.schemaVersion === 7 && value.kind === "graph-workbench")
      return { value: readRetiredCurrent(value), raw, error: null, rewrite: true };
    if (graphObject(value) && [1, 2, 3, 4, 5].includes(Number(value.schemaVersion)) && value.kind === "fixed-workbench")
      return { value: null, raw, error: null, rewrite: true };
    throw new Error("schema");
  } catch {
    return { value: null, raw, error: "保存记录无法读取或版本不兼容，原始记录未覆盖", rewrite: false };
  }
}
export function writeWorkbench(storage: DraftStorage, value: SavedWorkbench, expectedRaw: string | null): string {
  if (!isSavedWorkbench(value)) throw new Error("工作台保存内容格式无效");
  const raw = JSON.stringify(value);
  if (raw.length > MAX_BYTES) throw new Error("工作台保存内容超出容量");
  if (storage.getItem(WORKBENCH_STORAGE_KEY) !== expectedRaw)
    throw new Error("其他页面更新了保存记录，当前修改尚未保存");
  storage.setItem(WORKBENCH_STORAGE_KEY, raw);
  return raw;
}
