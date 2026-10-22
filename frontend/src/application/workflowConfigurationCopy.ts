import { shallowRef } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { readConfigurationCopyReceipt, submitConfigurationCopy } from "../adapters/workflowConfigurationCopyApi";
import { isRequestEvidenceRecord, requestEvidencePorts, confirmCurrentPendingEvidence, confirmEvidenceForResolution,
  evidenceCoordinate, type PendingRequestEvidence, type RequestEvidencePorts, type RequestEvidenceRecord } from "../adapters/requestEvidenceApi";
import { graphClone, graphJsonSignature, graphUuid } from "../domain/workflowGraph";
import { isWorkflowConfigurationCopyRequest, isWorkflowConfigurationCopyResult,
  workflowConfigurationCopyOperation, workflowConfigurationCopyOwnerKind,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import { createResourceOutbox } from "./resourceOutbox";

export interface WorkflowConfigurationCopyPorts {
  evidence?: RequestEvidencePorts;
  send?: (request: WorkflowConfigurationCopyRequest) => Promise<WorkflowConfigurationCopyResult>;
  readReceipt?: (request: WorkflowConfigurationCopyRequest) => Promise<WorkflowConfigurationCopyResult>;
  /** False when the source workflow is no longer open; prevents a new submission. */
  current?(sourceWorkflowDefinitionId: string): boolean;
  /** Called after the formal result was validated, before the evidence is resolved. */
  accept?(request: WorkflowConfigurationCopyRequest, result: WorkflowConfigurationCopyResult,
    reconcile: boolean): void | Promise<void>;
  /** Called once the durable original request was closed successfully. */
  completed?(request: WorkflowConfigurationCopyRequest,
    result: WorkflowConfigurationCopyResult): void | Promise<void>;
}
export type WorkflowConfigurationCopyApplication = ReturnType<typeof createWorkflowConfigurationCopy>;

function unavailable(message: string) {
  return new WorkbenchApiError("rejected", message, undefined, "graph_configuration_copy_invalid");
}
function unknown(message: string) {
  return new WorkbenchApiError("unknown", message, undefined, "graph_configuration_copy_evidence_invalid");
}

/** One durable original-request channel per source workflow.
 *
 * The channel persists the exact six-field copy request before sending, sends at
 * most once per pending request, resolves the evidence only after the formal
 * result was accepted, and verifies unknown results through the read-only
 * receipt route without resending.
 */
export function createWorkflowConfigurationCopy(ports: WorkflowConfigurationCopyPorts = {}) {
  const evidence = ports.evidence ?? requestEvidencePorts;
  const send = ports.send ?? submitConfigurationCopy;
  const readReceipt = ports.readReceipt ?? readConfigurationCopyReceipt;
  const entries = new Map<string, ReturnType<typeof createEntry>>();

  function createEntry(sourceId: string) {
    let record: RequestEvidenceRecord | null = null;
    const lastResult = shallowRef<WorkflowConfigurationCopyResult | null>(null);
    function valid(value: unknown): value is WorkflowConfigurationCopyRequest {
      return isWorkflowConfigurationCopyRequest(value) && value.source_workflow_definition_id === sourceId;
    }
    function checked(value: unknown): RequestEvidenceRecord {
      if (!isRequestEvidenceRecord(value) || value.owner_kind !== workflowConfigurationCopyOwnerKind
        || value.owner_id !== sourceId || value.operation !== workflowConfigurationCopyOperation
        || !valid(value.request))
        throw unknown("配置复制原请求证据身份或正文无效");
      return graphClone(value);
    }
    async function readPending(): Promise<string | null> {
      const records = await evidence.read(workflowConfigurationCopyOwnerKind, sourceId);
      if (!Array.isArray(records) || !records.every(value => isRequestEvidenceRecord(value)
        && value.owner_kind === workflowConfigurationCopyOwnerKind && value.owner_id === sourceId
        && value.operation === workflowConfigurationCopyOperation)
        || records.filter(value => value.resolution === "pending").length > 1)
        throw unknown("配置复制原请求无法核对");
      const pending = records.find(value => value.resolution === "pending");
      if (!pending) return null;
      record = checked(pending);
      return JSON.stringify(record.request);
    }
    async function writePending(raw: string | null): Promise<void> {
      if (raw !== null) {
        let value: unknown;
        try { value = JSON.parse(raw); } catch { throw unknown("配置复制原请求无法解析，未提交"); }
        if (!valid(value)) throw unavailable("配置复制原请求无效，未提交");
        record = checked(await evidence.write({ operation: workflowConfigurationCopyOperation,
          idempotency_key: value.idempotency_key, owner_kind: workflowConfigurationCopyOwnerKind,
          owner_id: sourceId, request: graphClone(value) as unknown as Record<string, unknown> }));
        if (record.resolution !== "pending" || graphJsonSignature(record.request) !== graphJsonSignature(value))
          throw unknown("保存的原请求不同，业务未提交");
        await confirmCurrentPendingEvidence(evidence, record);
        return;
      }
      if (!record) throw unknown("配置复制原请求证据缺失，未结案");
      const original = record;
      await confirmEvidenceForResolution(evidence, original);
      const resolved = checked(await evidence.resolve({ operation: workflowConfigurationCopyOperation,
        idempotency_key: original.idempotency_key, request_digest: original.request_digest,
        resolution: "verified" }));
      if (resolved.resolution !== "verified" || resolved.request_digest !== original.request_digest
        || graphJsonSignature(resolved.request) !== graphJsonSignature(original.request))
        throw unknown("配置复制原请求结案回执不同，原请求仍待核实");
      record = null;
    }
    async function invoke(request: WorkflowConfigurationCopyRequest, reconcile = false):
      Promise<WorkflowConfigurationCopyResult> {
      if (!reconcile && ports.current?.(sourceId) === false)
        throw new WorkbenchApiError("rejected", "原工作流已关闭，业务未提交");
      const result = await (reconcile ? readReceipt : send)(graphClone(request));
      // Never accept a transport value as the formal result before its own shape,
      // source/target binding and initial counters were validated.
      if (!isWorkflowConfigurationCopyResult(result, request))
        throw new WorkbenchApiError("unknown", "配置复制正式结果与请求不匹配，保留原请求");
      lastResult.value = graphClone(result);
      outbox.progress.value = "business-confirmed";
      if (ports.accept) {
        try { await ports.accept(request, result, reconcile); }
        catch (cause) {
          outbox.progress.value = "acceptance-failed";
          throw new WorkbenchApiError("unknown",
            `配置复制正式结果已确认，当前视图接纳失败：${cause instanceof Error ? cause.message : String(cause)}`);
        }
      }
      return result;
    }
    const outbox = createResourceOutbox<WorkflowConfigurationCopyRequest>({
      readPending, writePending,
      save: request => invoke(request),
      readReceipt: request => invoke(request, true),
    }, valid, async request => {
      if (request && lastResult.value) await ports.completed?.(request, lastResult.value);
    }, "工作流配置复制");
    async function submit(request: WorkflowConfigurationCopyRequest): Promise<boolean> {
      if (!valid(request)) throw unavailable("配置复制请求字段无效，未提交");
      lastResult.value = null;
      return outbox.send(graphClone(request));
    }
    async function reconcile(): Promise<boolean> {
      if (!await outbox.initialize()) return false;
      if (!outbox.pending.value) return false;
      return outbox.reconcile();
    }
    return { pending: outbox.pending, error: outbox.error, busy: outbox.busy, locked: outbox.locked,
      progress: outbox.progress,
      completed: outbox.completed,
      reloadPending: outbox.reloadPending,
      lastResult, initialize: outbox.initialize, submit, reconcile,
      matchesEvidence: (value: PendingRequestEvidence) => !!record
        && evidenceCoordinate(record) === evidenceCoordinate(value) && record.request_digest === value.request_digest,
      note(message: string) { outbox.error.value = message; } };
  }
  return {
    forSource(sourceWorkflowDefinitionId: string) {
      if (!graphUuid(sourceWorkflowDefinitionId))
        throw unavailable("配置复制来源身份无效");
      const sourceId = sourceWorkflowDefinitionId.toLowerCase();
      let entry = entries.get(sourceId);
      if (!entry) { entry = createEntry(sourceId); entries.set(sourceId, entry); }
      return entry;
    },
  };
}
