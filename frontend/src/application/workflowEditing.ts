import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphSignature, isGraphDocument,
  type GraphDocument, type GraphEntry, type GraphSession } from "../domain/workflowGraph";
import type { WorkflowTarget } from "./workflowManagement";

export interface WorkflowEditingPorts {
  entry(workflowId: string): GraphEntry | undefined;
  canEdit(workflowId: string): boolean;
  observedSession(sessionId: string): GraphSession | undefined;
  acceptDraft(workflowId: string, document: GraphDocument): void;
}
export function createWorkflowEditing(ports: WorkflowEditingPorts) {
  function edit({ workflowId }: WorkflowTarget, change: (document: GraphDocument) => boolean) {
    const entry = ports.entry(workflowId);
    if (!entry || !ports.canEdit(workflowId)) return null;
    const next = graphClone(entry.document);
    if (!change(next) || graphSignature(next) === graphSignature(entry.document)) return null;
    if (next.schema_version === 2 && !isGraphDocument(next))
      throw new WorkbenchApiError("rejected", "编辑结果超出文档范围，或执行根、控制依赖、对象权限不完整");
    const view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (entry.session_id && (!view || !view.can_submit
      || ["prepared", "running", "pausing", "paused"].includes(view.status)))
      throw new WorkbenchApiError("unavailable", "当前会话未稳定，刷新或结束运行后再编辑");
    ports.acceptDraft(workflowId, graphClone(next));
    return workflowId;
  }
  return { edit };
}
