import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphSignature, isGraphDocument,
  type GraphCommand, type GraphDocument, type GraphEntry, type GraphSession } from "../domain/workflowGraph";
import type { WorkflowTarget } from "./workflowManagement";
import { createGraphCommand } from "./workflowCommands";

export interface WorkflowEditingPorts {
  entry(workflowId: string): GraphEntry | undefined;
  isDraft(workflowId: string): boolean;
  canEdit(workflowId: string): boolean;
  observedSession(sessionId: string): GraphSession | undefined;
  acceptDraft(workflowId: string, document: GraphDocument): void;
  stageCopy(sourceId: string, targetId: string, document: GraphDocument, baseline: GraphDocument, copying: boolean): void;
  open(workflowId: string): void;
  persist(): boolean;
  dispatch(workflowId: string, command: GraphCommand): Promise<unknown>;
  copying(active: boolean): void;
  report(failure: unknown): void;
}
export function createWorkflowEditing(ports: WorkflowEditingPorts) {
  function edit({ workflowId }: WorkflowTarget, change: (document: GraphDocument) => boolean) {
    const entry = ports.entry(workflowId);
    if (!entry || !ports.canEdit(workflowId)) return null;
    const next = graphClone(entry.document);
    if (!change(next) || graphSignature(next) === graphSignature(entry.document)) return null;
    if (next.schema_version === 2 && !isGraphDocument(next))
      throw new WorkbenchApiError("rejected", "编辑结果超出文档范围，或执行根、控制依赖、对象权限不完整");
    if (ports.isDraft(workflowId)) {
      ports.acceptDraft(workflowId, graphClone(next));
      ports.persist();
      return workflowId;
    }
    const view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (entry.session_id && (!view || !view.can_submit))
      throw new WorkbenchApiError("unavailable", "当前会话未稳定，刷新或结束运行后再编辑");
    const targetId = crypto.randomUUID();
    next.workflow_definition_id = targetId;
    next.revision = 1;
    next.name = `${next.name}（副本）`;
    const baseline = graphClone({ ...entry.document, workflow_definition_id: targetId, revision: 1, name: next.name });
    ports.stageCopy(workflowId, targetId, graphClone(next), baseline, !!view);
    if (!view) {
      ports.open(targetId);
      ports.persist();
    } else {
      ports.copying(true);
      const command = createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/copy`, {
        document: next, expected_session_revision: view.revision, expected_data_revision: view.data_revision,
        expected_definition_revision: view.definition_revision, expected_head_revision: view.head_revision,
        mappings: ports.entry(targetId)?.state_mappings ?? [],
      }, "copy");
      void ports.dispatch(targetId, command).catch(ports.report).finally(() => ports.copying(false));
    }
    return targetId;
  }
  return { edit };
}
