import { graphClone, graphObject, graphUuid, isGraphDocument,
  type GraphCommand, type GraphEntry } from "../domain/workflowGraph";
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
export interface LegacyPendingCommand {
  key: string;
  workflowId: string;
  command: GraphCommand;
  legacy_action: "extend_budget" | null;
  basis: { definitionId: string; savedRevision: number; sessionId: string | null };
  status: "pending" | "checking" | "verified" | "unknown" | "retired";
  result?: string;
  error?: string;
}
interface LegacyBudgetExtensionCommand extends GraphCommand {
  action: "extend_budget";
}
export interface LegacyWorkbenchEvidence {
  activeWorkflowId: string | null;
  selectedWorkflowId: string | null;
  pending: LegacyPendingCommand[];
  unreadablePending: number;
  unreadableRecord: boolean;
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

const readablePendingActions = new Set([
  "save", "create", "copy", "start", "pause", "resume", "close",
  "retry_archive", "retry_acceptance", "retry_failed_node", "definition-archive",
  "definition-delete", "session-archive", "session-delete", "data", "event",
  "rebind", "candidate-select", "candidate-fork",
]);
function hasLegacyCommandEnvelope(value: unknown): value is GraphCommand {
  return graphObject(value) && typeof value.path === "string"
    && /^\/api\/graph\/(definitions|sessions(?:\/|$))/.test(value.path)
    && graphObject(value.body) && graphUuid(value.body.idempotency_key)
    && typeof value.action === "string";
}
function isLegacyBudgetExtensionCommand(value: unknown): value is LegacyBudgetExtensionCommand {
  if (!hasLegacyCommandEnvelope(value) || value.action !== "extend_budget"
    || !/^\/api\/graph\/sessions\/[0-9a-f-]{36}\/control$/.test(value.path)
    || !graphUuid(value.path.split("/")[4]) || value.body.action !== "extend_budget"
    || !Number.isSafeInteger(value.body.expected_revision) || Number(value.body.expected_revision) < 1
    || !Number.isSafeInteger(value.body.add_model_requests) || Number(value.body.add_model_requests) < 0
    || !Number.isSafeInteger(value.body.add_model_attempts) || Number(value.body.add_model_attempts) < 0) return false;
  return Number(value.body.add_model_requests) + Number(value.body.add_model_attempts) > 0;
}
function legacyCommand(value: unknown): value is GraphCommand | LegacyBudgetExtensionCommand {
  return hasLegacyCommandEnvelope(value)
    && (readablePendingActions.has(value.action) || isLegacyBudgetExtensionCommand(value));
}
export function readLegacyWorkbenchEvidence(value: unknown): LegacyWorkbenchEvidence {
  const result: LegacyWorkbenchEvidence = {
    activeWorkflowId: null, selectedWorkflowId: null, pending: [], unreadablePending: 0,
    unreadableRecord: false,
  };
  if (!graphObject(value) || value.schemaVersion !== 7 || value.kind !== "graph-workbench"
    || !graphObject(value.graph) || value.graph.schema_version !== 1 || !graphObject(value.graph.entries)) {
    result.unreadableRecord = true;
    return result;
  }

  result.activeWorkflowId = graphUuid(value.activeWorkflowId) ? value.activeWorkflowId : null;
  result.selectedWorkflowId = graphUuid(value.selectedWorkflowId) ? value.selectedWorkflowId : null;
  for (const [workflowId, rawEntry] of Object.entries(value.graph.entries)) {
    if (!graphObject(rawEntry) || rawEntry.pending === null || rawEntry.pending === undefined) continue;
    const document = rawEntry.document;
    const sessionId = rawEntry.session_id;
    if (!graphUuid(workflowId) || !isGraphDocument(document)
      || document.workflow_definition_id !== workflowId
      || !Number.isSafeInteger(rawEntry.saved_revision) || Number(rawEntry.saved_revision) < 0
      || sessionId !== null && !graphUuid(sessionId) || !legacyCommand(rawEntry.pending)) {
      result.unreadablePending++;
      continue;
    }
    const command = graphClone(rawEntry.pending);
    // The whole-graph save receipt contract is retired: keep the frozen original
    // request in place but never present it as a verifiable request again.
    const retiredSave = command.action === "save";
    result.pending.push({
      key: `${workflowId}:${command.body.idempotency_key}`,
      workflowId,
      command,
      legacy_action: command.action === "extend_budget" ? "extend_budget" : null,
      basis: { definitionId: document.workflow_definition_id,
        savedRevision: Number(rawEntry.saved_revision), sessionId: sessionId as string | null },
      status: retiredSave ? "retired" : "pending",
      ...(retiredSave
        ? { result: "旧整图保存入口已退役；原保存回执不可再核实，原记录保留" }
        : {}),
    });
  }
  return result;
}
