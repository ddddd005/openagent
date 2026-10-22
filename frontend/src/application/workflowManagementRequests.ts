import { computed, shallowRef } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { isRequestEvidenceRecord, requestEvidencePorts, confirmCurrentPendingEvidence, confirmEvidenceForResolution,
  evidenceCoordinate, type PendingRequestEvidence, type RequestEvidencePorts, type RequestEvidenceRecord } from "../adapters/requestEvidenceApi";
import { graphCommandOperation, readGraphReceipt, sendGraphCommand } from "../adapters/workflowApplicationApi";
import { readGraphDefinition } from "../adapters/workflowGraphApi";
import { graphClone, graphJsonSignature, graphObject, graphUuid,
  type GraphCommand } from "../domain/workflowGraph";
import { createResourceOutbox } from "./resourceOutbox";
import { validateGraphCommandResult, type GraphCommandPorts, type GraphReceipt } from "./workflowCommands";

interface ManagementRequest {
  operation: string;
  parameters: Record<string, unknown>;
}
export interface WorkflowManagementRequestPorts {
  evidence?: RequestEvidencePorts;
  send?: GraphCommandPorts["request"];
  readReceipt?: GraphCommandPorts["readReceipt"];
  current?(workflowId: string): boolean;
  accept(workflowId: string, receipt: GraphReceipt, command: GraphCommand): void | Promise<void>;
  // Called only after the original request evidence has been resolved. The
  // receipt may already say deleted, but local removal waits for this point.
  completed?(workflowId: string, command: GraphCommand, receipt: GraphReceipt): void | Promise<void>;
}
const ownerKind = "workflow-management";
const operations = new Set(["definition.archive", "definition.delete", "session.create",
  "session.archive", "session.delete", "session.data.update", "run.start", "run.control", "event.submit"]);
const controlActions = new Set(["pause", "resume", "close", "retry_archive", "retry_acceptance", "retry_failed_node"]);
const same = (left: unknown, right: unknown) => graphJsonSignature(left) === graphJsonSignature(right);
function unavailable(message: string) {
  return new WorkbenchApiError("unavailable", message, undefined, "graph_management_request_invalid");
}
function originalCommand(request: ManagementRequest): GraphCommand {
  const body = graphClone(request.parameters);
  if (request.operation === "session.create")
    return { path: "/api/graph/sessions", body, action: "create" };
  if (request.operation.startsWith("definition.")) {
    const identity = body.identity, action = request.operation.split(".")[1];
    delete body.identity;
    return { path: `/api/graph/definitions/${identity}/${action}`, body, action: `definition-${action}` };
  }
  const sessionId = body.session_id as string;
  delete body.session_id;
  const suffixes: Record<string, string> = { "session.archive": "archive", "session.delete": "delete",
    "session.data.update": "data", "run.start": "runs", "run.control": "control",
    "event.submit": "events/submit" };
  const suffix = suffixes[request.operation];
  const action = request.operation === "run.start" ? "start"
    : request.operation === "run.control" ? String(body.action)
      : request.operation === "event.submit" ? "event"
        : request.operation === "session.data.update" ? "data" : request.operation.replace(".", "-");
  return { path: `/api/graph/sessions/${sessionId}/${suffix}`, body, action,
    ...(["session.archive", "session.delete"].includes(request.operation) ? { source_session_id: sessionId } : {}) };
}

// Only original business parameters are durable. Editor state never enters this channel.
export function createWorkflowManagementRequests(ports: WorkflowManagementRequestPorts) {
  const evidence = ports.evidence ?? requestEvidencePorts;
  const send = ports.send ?? sendGraphCommand, readReceipt = ports.readReceipt ?? readGraphReceipt;
  const entries = new Map<string, ReturnType<typeof createEntry>>();
  function createEntry(workflowId: string) {
    let record: RequestEvidenceRecord | null = null, failure: unknown = null;
    let acceptRecoveredResult = true;
    const lastReceipt = shallowRef<GraphReceipt | null>(null);
    function valid(value: unknown): value is ManagementRequest {
      if (!graphObject(value) || Object.keys(value).length !== 2 || !operations.has(String(value.operation))
        || !graphObject(value.parameters) || typeof value.parameters.idempotency_key !== "string") return false;
      const parameters = value.parameters;
      if (value.operation === "session.create" && parameters.workflow_definition_id !== workflowId) return false;
      if (String(value.operation).startsWith("definition.") && parameters.identity !== workflowId) return false;
      if (!["session.create", "definition.archive", "definition.delete"].includes(String(value.operation))
        && !graphUuid(parameters.session_id)) return false;
      try {
        const command = originalCommand(value as unknown as ManagementRequest);
        const mapped = graphCommandOperation(command.path, command.body);
        return same(mapped, value);
      } catch { return false; }
    }
    function checked(value: unknown): RequestEvidenceRecord {
      if (!isRequestEvidenceRecord(value) || value.owner_kind !== ownerKind || value.owner_id !== workflowId
        || !valid({ operation: value.operation, parameters: value.request }))
        throw new WorkbenchApiError("unknown", "工作流管理原请求身份或正文不匹配");
      return graphClone(value);
    }
    async function readPending() {
      const records = await evidence.read(ownerKind, workflowId);
      if (!Array.isArray(records) || !records.every(value => isRequestEvidenceRecord(value)
        && value.owner_kind === ownerKind && value.owner_id === workflowId)
        || records.filter(value => value.resolution === "pending").length > 1)
        throw new WorkbenchApiError("unknown", "工作流管理原请求无法核对");
      const pending = records.find(value => value.resolution === "pending");
      if (!pending) return null;
      record = checked(pending);
      return JSON.stringify({ operation: record.operation, parameters: record.request });
    }
    async function writePending(raw: string | null) {
      if (raw !== null) {
        const value: unknown = JSON.parse(raw);
        if (!valid(value)) throw unavailable("工作流管理原请求无效，未提交");
        record = checked(await evidence.write({ operation: value.operation,
          idempotency_key: String(value.parameters.idempotency_key),
          owner_kind: ownerKind, owner_id: workflowId, request: graphClone(value.parameters) }));
        if (record.resolution !== "pending" || !same(record.request, value.parameters))
          throw new WorkbenchApiError("unknown", "保存的原请求不同，业务未提交");
        await confirmCurrentPendingEvidence(evidence, record);
        return;
      }
      if (!record) throw new WorkbenchApiError("unknown", "管理原请求证据缺失，未结案");
      const original = record;
      await confirmEvidenceForResolution(evidence, original);
      const resolved = checked(await evidence.resolve({ operation: original.operation,
        idempotency_key: original.idempotency_key, request_digest: original.request_digest, resolution: "verified" }));
      if (resolved.resolution !== "verified" || resolved.request_digest !== original.request_digest
        || !same(resolved.request, original.request))
        throw new WorkbenchApiError("unknown", "管理原请求结案回执不同，原请求仍待核实");
      record = null;
    }
    async function invoke(request: ManagementRequest, reconcile = false) {
      const command = originalCommand(request);
      try {
        if (!reconcile && ports.current?.(workflowId) === false)
          throw new WorkbenchApiError("rejected", "原工作流已关闭，业务未提交");
        const value = await (reconcile ? readReceipt : send)(command.path, graphClone(command.body));
        const receipt = await validateGraphCommandResult(value, command, {
          definitionId: workflowId,
          savedRevision: request.operation === "session.create" ? Number(request.parameters.definition_revision) : 0,
          sessionId: typeof request.parameters.session_id === "string" ? request.parameters.session_id : null,
        }, { readDefinition: readGraphDefinition });
        lastReceipt.value = graphClone(receipt);
        outbox.progress.value = "business-confirmed";
        try { if (acceptRecoveredResult) await ports.accept(workflowId, receipt, graphClone(command)); }
        catch (cause) {
          outbox.progress.value = "acceptance-failed";
          throw new WorkbenchApiError("unknown",
            `管理正式结果已确认，当前视图接纳失败：${cause instanceof Error ? cause.message : String(cause)}`);
        }
        return receipt;
      } catch (cause) { failure = cause; throw cause; }
    }
    const outbox = createResourceOutbox<ManagementRequest>({
      readPending, writePending, save: request => invoke(request),
      readReceipt: request => invoke(request, true),
    }, valid, async request => {
      if (!request || (request.operation !== "definition.delete" && request.operation !== "session.delete")) return;
      const receipt = lastReceipt.value;
      if (!receipt || receipt.kind !== "management" || !receipt.result.deleted) return;
      if (acceptRecoveredResult) await ports.completed?.(workflowId, originalCommand(request), graphClone(receipt));
    }, "工作流管理");
    async function dispatch(command: GraphCommand): Promise<GraphReceipt> {
      const request = graphCommandOperation(command.path, command.body);
      if (!valid(request)) throw unavailable("此写操作尚未接入管理原请求保护，未提交");
      if (request.operation === "session.data.update")
        throw new WorkbenchApiError("rejected", "旧变量会话数据写入入口已退役；未提交新请求");
      if (request.operation === "run.control"
        && (!controlActions.has(String(request.parameters.action))
          || ["add_model_requests", "add_model_attempts"].some(key => Object.hasOwn(request.parameters, key))))
        throw new WorkbenchApiError("rejected", "此工作流运行操作已退役；未提交新请求");
      if (ports.current?.(workflowId) === false) throw unavailable("原工作流已关闭，未提交");
      failure = null; lastReceipt.value = null;
      if (!await outbox.send(request) || !lastReceipt.value) {
        if (failure instanceof Error) throw failure;
        throw new WorkbenchApiError(outbox.pending.value ? "unknown" : "unavailable",
          outbox.error.value || "原管理请求尚未完成，未提交");
      }
      return graphClone(lastReceipt.value);
    }
    async function reconcile(options: { acceptResult?: boolean } = {}) {
      if (outbox.busy.value) return false;
      acceptRecoveredResult = options.acceptResult !== false;
      try { return await outbox.reconcile(); }
      finally { acceptRecoveredResult = true; }
    }
    return { ...outbox, reconcile, dispatch, lastReceipt,
      matchesEvidence: (value: PendingRequestEvidence) => !!record
        && evidenceCoordinate(record) === evidenceCoordinate(value) && record.request_digest === value.request_digest,
      pendingCommand: computed(() => outbox.pending.value ? originalCommand(outbox.pending.value) : null) };
  }
  function forWorkflow(workflowId: string) {
    if (!graphUuid(workflowId)) throw unavailable("工作流管理目标身份无效");
    let entry = entries.get(workflowId);
    if (!entry) { entry = createEntry(workflowId); entries.set(workflowId, entry); }
    return entry;
  }
  return { forWorkflow };
}
