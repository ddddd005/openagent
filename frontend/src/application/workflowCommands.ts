import { WorkbenchApiError } from "../adapters/workbenchApi";
import {
  graphClone, graphSignature, isGraphDocument, isGraphSession,
  graphObject, graphUuid, type GraphCommand, type GraphDocument, type GraphEntry, type GraphSession, type GraphManagementResult,
} from "../domain/workflowGraph";
import { newerGraphObservation } from "../domain/graphObservation";

export type GraphReceipt =
  | { kind: "definition"; document: GraphDocument }
  | { kind: "session"; session: GraphSession; observation?: GraphSession; historicalDocument?: GraphDocument }
  | { kind: "management"; result: GraphManagementResult };
export interface GraphCommandPorts {
  persist?(): boolean | Promise<boolean>;
  request?(path: string, body: Record<string, unknown>): Promise<unknown>;
  readReceipt(path: string, body: Record<string, unknown>): Promise<unknown>;
  readDefinition(id: string, revision: number): Promise<GraphDocument>;
  observedSession?(sessionId: string): GraphSession | undefined;
  current(): boolean;
  accept?(receipt: GraphReceipt, command: GraphCommand): void;
}
export function createGraphCommand(path: string, body: Record<string, unknown>, action: string): GraphCommand {
  return graphClone({ path, body: { ...body, idempotency_key: crypto.randomUUID() }, action });
}
function unknown(reason: string): never {
  throw new WorkbenchApiError("unknown", `${reason}，保留原请求`);
}

// Receipt identity is compared with the frozen request basis, never with the live editor or a newer observation.
async function readReceipt(value: unknown, request: GraphCommand,
  basis: { definitionId: string; savedRevision: number; sessionId: string | null },
  ports: Pick<GraphCommandPorts, "readDefinition" | "observedSession">): Promise<GraphReceipt> {
  if (request.action === "save") {
    if (!isGraphDocument(value) || value.workflow_definition_id !== basis.definitionId
      || value.revision !== Number(request.body.expected_revision) + 1
      || graphSignature(value) !== graphSignature(request.body.document as GraphDocument))
      unknown("定义保存回执不匹配");
    return { kind: "definition", document: value };
  }
  if (["definition-archive", "definition-delete", "session-archive", "session-delete"].includes(request.action)) {
    const session = request.action.startsWith("session-"), deleted = request.action.endsWith("-delete");
    if (!graphObject(value) || Object.keys(value).length !== (session ? 7 : 6)
      || value.schema_version !== 1 || value.kind !== `workflow.${session ? "session" : "definition"}-management`
      || value.workflow_definition_id !== basis.definitionId || value.revision !== request.body.expected_revision
      || value.archived !== !deleted || value.deleted !== deleted
      || session && (!graphUuid(request.source_session_id) || value.workflow_session_id !== request.source_session_id))
      unknown("归档或删除回执与目标不匹配");
    return { kind: "management", result: value as unknown as GraphManagementResult };
  }
  const candidate = ["candidate-select", "candidate-fork"].includes(request.action);
  const definitionId = candidate ? request.expected_definition_id : basis.definitionId;
  if (!definitionId || !isGraphSession(value, definitionId)
    || !["copy", "create", "candidate-fork"].includes(request.action) && value.workflow_session_id !== basis.sessionId
    || ["create", "rebind"].includes(request.action) && value.definition_revision !== basis.savedRevision
    || request.action === "copy" && value.definition_revision !== (request.body.document as GraphDocument).revision)
    unknown("会话回执不匹配");
  if (request.action === "event" && (value.workflow_definition_id !== request.body.workflow_definition_id
    || value.definition_revision !== request.body.definition_revision)) unknown("事件回执定义依据不匹配");
  if (!candidate) return { kind: "session", session: value };
  if (value.selected_chain_run_id !== request.expected_chain_run_id
    || value.definition_revision !== request.expected_definition_revision
    || request.action === "candidate-fork" && (value.workflow_session_id === request.source_session_id
      || value.source?.kind !== "fork_candidate" || value.source.workflow_session_id !== request.source_session_id
      || value.source.candidate_commit_id !== request.body.candidate_id))
    unknown("候选回执与原请求不匹配");
  const observation = newerGraphObservation(ports.observedSession?.(value.workflow_session_id), value);
  let historicalDocument: GraphDocument;
  try { historicalDocument = await ports.readDefinition(observation.workflow_definition_id, observation.definition_revision); }
  catch (failure) {
    unknown(`后端已返回原操作回执，但确切定义读取失败：${failure instanceof Error ? failure.message : String(failure)}`);
  }
  if (!isGraphDocument(historicalDocument) || historicalDocument.workflow_definition_id !== observation.workflow_definition_id
    || historicalDocument.revision !== observation.definition_revision)
    unknown("历史定义身份或修订不匹配");
  const latest = newerGraphObservation(ports.observedSession?.(value.workflow_session_id), observation);
  if (latest.workflow_definition_id !== historicalDocument.workflow_definition_id
    || latest.definition_revision !== historicalDocument.revision)
    unknown("原候选操作已有回执，但定义读取期间会话已采用另一版本；刷新后核实");
  return { kind: "session", session: value, observation: latest, historicalDocument };
}

export async function dispatchGraphCommand(entry: GraphEntry, command: GraphCommand, ports: GraphCommandPorts,
  reconcile = false) {
  const request = graphClone(command);
  if (!reconcile && ["rebind", "candidate-select", "candidate-fork"].includes(request.action))
    throw new WorkbenchApiError("rejected", request.action === "rebind"
      ? "会话修订重绑入口已退役；请新建会话"
      : "候选历史恢复与分叉入口已退役；未提交新请求");
  if (!reconcile) {
    const message = request.action === "save"
      ? "旧整图发布入口已退役；正式工作流保存暂不可用，未提交新请求"
      : "当前工作流命令写入暂不可用；未提交新请求";
    throw new WorkbenchApiError("unavailable", message, undefined, "graph_command_write_unavailable");
  }
  const samePending = () => entry.pending !== null && JSON.stringify(entry.pending) === JSON.stringify(request);
  const current = () => ports.current() && samePending();
  if (entry.pending === null) unknown("未保留原工作流请求");
  if (!samePending()) unknown("待核实请求已变化");
  if (!current()) unknown("工作流已关闭或原请求已变化");
  const receipt = await readGraphCommandReceipt(request, {
    definitionId: entry.document.workflow_definition_id,
    savedRevision: entry.saved_revision,
    sessionId: entry.session_id,
  }, ports);
  if (!current()) unknown("工作流已关闭或原请求已变化；回执仅供核实");
  return receipt;
}

export async function readGraphCommandReceipt(command: GraphCommand,
  basis: { definitionId: string; savedRevision: number; sessionId: string | null },
  ports: Pick<GraphCommandPorts, "readReceipt" | "readDefinition" | "observedSession">) {
  const request = graphClone(command);
  const value = await ports.readReceipt(request.path, graphClone(request.body));
  return readReceipt(value, request, basis, ports);
}

export async function validateGraphCommandResult(value: unknown, command: GraphCommand,
  basis: { definitionId: string; savedRevision: number; sessionId: string | null },
  ports: Pick<GraphCommandPorts, "readDefinition" | "observedSession">) {
  return readReceipt(value, graphClone(command), basis, ports);
}
