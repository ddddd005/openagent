import { WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { graphApplicationQuery } from "./workflowApplicationApi";
import { graphClone, graphObject, graphUuid, isGraphJsonValue } from "../domain/workflowGraph";

export interface LocalDraftAddress {
  scope: "management";
  kind: "workbench" | "form" | "intent";
  key: string;
  workflow_definition_id: string | null;
  workflow_session_id: string | null;
}
export interface LocalDraftRecord {
  schema_version: 1;
  kind: "workflow.local-draft";
  address: LocalDraftAddress;
  format_version: number;
  sequence: number;
  updated_at: string;
  value: unknown;
  deleted: boolean;
}
export interface LocalDraftSaveRequest {
  address: LocalDraftAddress;
  format_version: number;
  value: unknown;
  expected_sequence: number;
  idempotency_key: string;
}
export const workbenchDraftAddress: LocalDraftAddress = {
  scope: "management", kind: "workbench", key: "workbench:v1",
  workflow_definition_id: null, workflow_session_id: null,
};
function same(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) || Array.isArray(right))
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => same(value, right[index]));
  if (graphObject(left) || graphObject(right))
    return graphObject(left) && graphObject(right) && Object.keys(left).length === Object.keys(right).length
      && Object.keys(left).every(key => Object.hasOwn(right, key) && same(left[key], right[key]));
  return left === right;
}
export function isLocalDraftAddress(value: unknown): value is LocalDraftAddress {
  return graphObject(value) && Object.keys(value).length === 5 && value.scope === "management"
    && ["workbench", "form", "intent"].includes(String(value.kind))
    && typeof value.key === "string" && value.key.length > 0 && value.key.length <= 512
    && value.key.trim() === value.key && !/[\u0000-\u001f\u007f]/.test(value.key)
    && (value.workflow_definition_id === null || graphUuid(value.workflow_definition_id))
    && (value.workflow_session_id === null || graphUuid(value.workflow_session_id)
      && value.workflow_definition_id !== null);
}
export function isLocalDraftRecord(value: unknown): value is LocalDraftRecord {
  return graphObject(value) && Object.keys(value).length === 8 && value.schema_version === 1
    && value.kind === "workflow.local-draft" && isLocalDraftAddress(value.address)
    && Number.isSafeInteger(value.format_version) && Number(value.format_version) > 0
    && Number.isSafeInteger(value.sequence) && Number(value.sequence) > 0
    && typeof value.updated_at === "string" && Number.isFinite(Date.parse(value.updated_at))
    && typeof value.deleted === "boolean" && isGraphJsonValue(value.value)
    && value.deleted === (value.value === null);
}
export async function readLocalDraft(address: LocalDraftAddress): Promise<LocalDraftRecord | null> {
  const value = await graphApplicationQuery("draft.read", { address });
  if (value === null) return null;
  if (!isLocalDraftRecord(value) || !same(value.address, address))
    throw new WorkbenchApiError("unavailable", "本机草稿身份或格式无效，原记录未覆盖");
  return graphClone(value);
}
export async function listLocalDrafts(parameters: { kind?: LocalDraftAddress["kind"]; workflow_definition_id?: string } = {}) {
  const value = await graphApplicationQuery("draft.list", parameters);
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.local-drafts"
    || !Array.isArray(value.records) || !value.records.every(isLocalDraftRecord))
    throw new WorkbenchApiError("unavailable", "本机草稿目录契约无效");
  return graphClone(value.records);
}
function result(value: unknown, request: LocalDraftSaveRequest): LocalDraftRecord {
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  const record = graphObject(value) ? value.result : null;
  const accepted = { draft_address: request.address, draft_sequence: request.expected_sequence + 1,
    draft_format_version: request.format_version, deleted: request.value === null };
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-command"
    || !receipt || receipt.schema_version !== 1 || receipt.kind !== "workflow.application-receipt"
    || receipt.operation !== "draft.save" || receipt.operation_scope !== "management"
    || receipt.authority !== "service_receipt" || receipt.idempotency_key !== request.idempotency_key
    || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256)
    || !same(receipt.target, {}) || !same(receipt.accepted, accepted)
    || !isLocalDraftRecord(record) || !same(record.address, request.address)
    || record.format_version !== request.format_version || record.sequence !== request.expected_sequence + 1
    || !same(record.value, request.value) || record.deleted !== (request.value === null))
    throw new WorkbenchApiError("unknown", "本机草稿回执与原请求不匹配，保留原请求");
  return graphClone(record);
}
export async function saveLocalDraft(request: LocalDraftSaveRequest): Promise<LocalDraftRecord> {
  void request;
  throw new WorkbenchApiError("rejected", "持久编辑草稿写入已退役；当前输入仅保留在内存中",
    undefined, "local_draft_write_retired");
}
export async function readLocalDraftReceipt(request: LocalDraftSaveRequest): Promise<LocalDraftRecord> {
  const value = await workbenchRequest<unknown>("/api/graph/receipts/read", {
    body: { operation: "draft.save", parameters: graphClone(request) }, readOnly: true,
  });
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-receipt-read")
    throw new WorkbenchApiError("unknown", "本机草稿原回执契约无效，保留原请求");
  if (value.outcome === "unresolved")
    throw new WorkbenchApiError("unknown", "本机草稿提交结果仍待核实，原请求未重发");
  if (value.outcome !== "matched" || value.reason_code !== "receipt_matched")
    throw new WorkbenchApiError("unknown", "本机草稿原回执契约无效，保留原请求");
  return result({ schema_version: 1, kind: "workflow.application-command",
    receipt: value.receipt, result: value.result }, request);
}

export interface LocalDraftPorts {
  read(address: LocalDraftAddress): Promise<LocalDraftRecord | null>;
  save(request: LocalDraftSaveRequest): Promise<LocalDraftRecord>;
  receipt(request: LocalDraftSaveRequest): Promise<LocalDraftRecord>;
}
export const localDraftPorts: LocalDraftPorts = {
  read: readLocalDraft, save: saveLocalDraft, receipt: readLocalDraftReceipt,
};

// A transport-unknown draft write is settled only by its original receipt.
export function createLocalDraftChannel(address: LocalDraftAddress, ports: LocalDraftPorts = localDraftPorts) {
  let record: LocalDraftRecord | null = null;
  let loaded = false;
  let pending: LocalDraftSaveRequest | null = null;
  let queue = Promise.resolve();
  let reading: Promise<void> | null = null;
  async function load(refresh = false) {
    if (!loaded || refresh) {
      reading ??= ports.read(address).then(value => { record = value; loaded = true; }).finally(() => { reading = null; });
      await reading;
    }
    return graphClone(record);
  }
  function read(refresh = false) {
    if (!refresh) return load();
    const operation = queue.then(() => load(true));
    queue = operation.then(() => {}, () => {});
    return operation;
  }
  async function write(value: unknown, formatVersion = 1) {
    let accepted!: LocalDraftRecord;
    const operation = queue.then(async () => {
      await read();
      if (pending) {
        record = await ports.receipt(graphClone(pending));
        pending = null;
      }
      if (record && record.format_version === formatVersion && same(record.value, value)) {
        accepted = graphClone(record); return;
      }
      const request: LocalDraftSaveRequest = { address: graphClone(address), format_version: formatVersion,
        value: graphClone(value), expected_sequence: record?.sequence ?? 0, idempotency_key: crypto.randomUUID() };
      pending = graphClone(request);
      try { record = await ports.save(request); pending = null; accepted = graphClone(record); }
      catch (failure) {
        if (failure instanceof WorkbenchApiError && failure.kind === "rejected"
          && failure.code !== "idempotency_conflict") pending = null;
        throw failure;
      }
    });
    queue = operation.catch(() => {});
    await operation;
    return accepted;
  }
  return { read, write, pending: () => graphClone(pending), record: () => graphClone(record) };
}
