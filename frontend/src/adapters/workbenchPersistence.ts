import { graphObject, graphUuid, isGraphDocument, type GraphEntry } from "../domain/workflowGraph";
import type { WorkspaceWorkflow } from "../domain/workspace";

export const WORKBENCH_STORAGE_KEY = "workflow-workbench:fixed-base:v1";
export const WORKBENCH_RECOVERY_STORAGE_KEY = "workflow-workbench:isolated-graph:v7";
// Reject retired product node identities; this list never migrates stored content.
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
  ["models.source", "1"], ["models.source", "2"], ["models.source", "3"], ["models.source", "4"],
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
function metadata(value: Record<string, unknown>) {
  return Number.isSafeInteger(value.revision) && Number(value.revision) > 0
    && typeof value.savedAt === "string" && Number.isFinite(Date.parse(value.savedAt));
}
function workflow(value: unknown): value is WorkspaceWorkflow {
  return graphObject(value) && graphUuid(value.id) && typeof value.title === "string"
    && typeof value.description === "string" && Number.isSafeInteger(value.nodeCount) && Number(value.nodeCount) >= 0
    && (value.state === undefined || value.state === "saved" || value.state === "draft")
    && (value.sourceId === undefined || typeof value.sourceId === "string")
    && (value.copyPending === undefined || typeof value.copyPending === "boolean")
    && (value.archived === undefined || typeof value.archived === "boolean")
    && (value.deleted === undefined || typeof value.deleted === "boolean");
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
    || !Array.isArray(value.catalog) || value.catalog.length > 128
    || !value.catalog.every(workflow) || new Set(value.catalog.map(row => row.id)).size !== value.catalog.length
    || !graphObject(value.graph) || value.graph.schema_version !== 1 || !graphObject(value.graph.entries)) return false;
  const entries = value.graph.entries;
  return Object.keys(entries).length === value.catalog.length
    && value.catalog.every(row => entry(entries[row.id]) && !explicitLegacy(
      (entries[row.id] as GraphEntry).document, entries[row.id] as unknown as Record<string, unknown>))
    && (value.catalog.length === 0 ? value.activeWorkflowId === "" && value.selectedWorkflowId === ""
      : value.catalog.some(row => row.id === value.activeWorkflowId)
        && value.catalog.some(row => row.id === value.selectedWorkflowId));
}
