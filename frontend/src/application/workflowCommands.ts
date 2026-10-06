import { WorkbenchApiError } from "../adapters/workbenchApi";
import {
  graphClone, graphObject, graphSignature, isGraphDocument, isGraphSession,
  type GraphCommand, type GraphDocument, type GraphEntry, type GraphSession,
} from "../domain/workflowGraph";
import { newerGraphObservation } from "../domain/graphObservation";

export type GraphReceipt =
  | { kind: "definition"; document: GraphDocument }
  | { kind: "migration"; document: GraphDocument; session: GraphSession; provenance: Record<string, unknown> }
  | { kind: "session"; session: GraphSession; observation?: GraphSession; historicalDocument?: GraphDocument };
export interface GraphCommandPorts {
  persist(): boolean;
  request(path: string, body: Record<string, unknown>): Promise<unknown>;
  readDefinition(id: string, revision: number): Promise<GraphDocument>;
  observedSession?(sessionId: string): GraphSession | undefined;
  current(): boolean;
  accept(receipt: GraphReceipt, command: GraphCommand): void;
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
  ports: GraphCommandPorts): Promise<GraphReceipt> {
  if (request.action === "save") {
    if (!isGraphDocument(value) || value.workflow_definition_id !== basis.definitionId
      || value.revision !== Number(request.body.expected_revision) + 1
      || graphSignature(value) !== graphSignature(request.body.document as GraphDocument))
      unknown("定义保存回执不匹配");
    return { kind: "definition", document: value };
  }
  if (request.action === "migrate") {
    if (!graphObject(value) || !isGraphDocument(value.document) || value.document.revision !== 1
      || graphSignature(value.document) !== graphSignature(request.body.document as GraphDocument)
      || !isGraphSession(value.session, basis.definitionId)
      || value.session.definition_revision !== value.document.revision || !graphObject(value.provenance))
      unknown("迁移回执身份或定义不匹配");
    return { kind: "migration", document: value.document, session: value.session, provenance: value.provenance };
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

export async function dispatchGraphCommand(entry: GraphEntry, command: GraphCommand, ports: GraphCommandPorts) {
  const request = graphClone(command);
  const basis = { definitionId: entry.document.workflow_definition_id,
    savedRevision: entry.saved_revision, sessionId: entry.session_id };
  entry.pending = graphClone(request);
  if (!ports.persist()) {
    entry.pending = null;
    throw new WorkbenchApiError("unavailable", "原工作流请求无法保存，未提交");
  }
  let value: unknown;
  try { value = await ports.request(request.path, graphClone(request.body)); }
  catch (failure) {
    if (ports.current() && failure instanceof WorkbenchApiError && failure.kind === "rejected") {
      entry.diagnostics = failure.diagnostics?.length ? graphClone(failure.diagnostics)
        : [{ reason_code: failure.code ?? "graph_request_rejected", message: failure.reason }];
      entry.pending = null;
      if (!ports.persist()) entry.pending = graphClone(request);
    }
    throw failure;
  }
  const receipt = await readReceipt(value, request, basis, ports);
  if (!ports.current()) unknown("工作流已关闭或恢复，迟到回执未覆盖当前文档");
  ports.accept(receipt, request);
  entry.pending = null;
  entry.diagnostics = [];
  if (["copy", "rebind"].includes(request.action)) entry.state_mappings = [];
  if (!ports.persist()) {
    entry.pending = graphClone(request);
    unknown("后端已处理请求，本机回执保存失败");
  }
  return value;
}
