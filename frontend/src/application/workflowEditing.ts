import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphSignature, isGraphDocument,
  type GraphDocument, type GraphEntry, type GraphSession } from "../domain/workflowGraph";
import type { GraphConfigurationResult } from "../domain/workflowConfiguration";
import type { WorkflowTarget } from "./workflowManagement";

/** The formal result of one document edit, or the fact that nothing had to be submitted. */
export type WorkflowEditOutcome =
  | { outcome: "accepted"; workflowId: string; result: GraphConfigurationResult | null }
  | { outcome: "confirmation-required"; workflowId: string }
  | { outcome: "no-change"; workflowId: string }
  | { outcome: "declined"; workflowId: string };

export interface WorkflowEditAcceptance {
  /** Formal configuration result; null only for the local in-memory fallback without an application. */
  result: GraphConfigurationResult | null;
}

export interface WorkflowEditingPorts {
  entry(workflowId: string): GraphEntry | undefined;
  /** Why this workflow cannot be edited right now (busy, pending request, archived, ...); null when editable. */
  refusal(workflowId: string): string | null;
  observedSession(sessionId: string): GraphSession | undefined;
  /** Submit the proposed document and await the formal acceptance. Null means the confirmation ask was declined. */
  acceptDraft(workflowId: string, document: GraphDocument): Promise<WorkflowEditAcceptance | null>;
}

// One edit is one formal submission: the caller gets a result only after the accepted
// document was applied. No-change edits submit nothing and never claim a save.
export function createWorkflowEditing(ports: WorkflowEditingPorts) {
  async function edit({ workflowId }: WorkflowTarget,
    change: (document: GraphDocument) => boolean): Promise<WorkflowEditOutcome> {
    const entry = ports.entry(workflowId);
    if (!entry) throw new WorkbenchApiError("rejected", "工作流不存在或已关闭", undefined, "graph_edit_unavailable");
    const refusal = ports.refusal(workflowId);
    if (refusal) throw new WorkbenchApiError("rejected", refusal, undefined, "graph_edit_unavailable");
    const next = graphClone(entry.document);
    if (!change(next) || graphSignature(next) === graphSignature(entry.document))
      return { outcome: "no-change", workflowId };
    if (next.schema_version === 2 && !isGraphDocument(next))
      throw new WorkbenchApiError("rejected", "编辑结果超出文档范围，或执行根、控制依赖、对象权限不完整");
    const view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (entry.session_id && (!view || !view.can_submit
      || ["prepared", "running", "pausing", "paused"].includes(view.status)))
      throw new WorkbenchApiError("unavailable", "当前会话未稳定，刷新或结束运行后再编辑");
    const acceptance = await ports.acceptDraft(workflowId, graphClone(next));
    if (!acceptance) return { outcome: "declined", workflowId };
    // The backend answered with a derivation question and wrote nothing: the ask is
    // retained per workflow and confirmed later from the original workflow view.
    if (acceptance.result?.outcome === "confirmation_required")
      return { outcome: "confirmation-required", workflowId };
    return { outcome: "accepted", workflowId: acceptance.result?.workflow_definition_id ?? workflowId,
      result: acceptance.result };
  }
  return { edit };
}
