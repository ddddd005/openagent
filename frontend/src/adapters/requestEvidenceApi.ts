import { WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { graphApplicationQuery } from "./workflowApplicationApi";
import { graphClone, graphJsonSignature, graphObject, isGraphJsonValue } from "../domain/workflowGraph";

export interface RequestEvidenceWrite {
  operation: string;
  idempotency_key: string;
  owner_kind: string;
  owner_id: string;
  request: Record<string, unknown>;
}
export interface RequestEvidenceResolve {
  operation: string;
  idempotency_key: string;
  request_digest: string;
  resolution: "verified" | "archived";
}
export interface RequestEvidenceRecord extends RequestEvidenceWrite {
  schema_version: 1;
  kind: "workflow.request-evidence";
  request_digest: string;
  resolution: "pending" | "verified" | "archived";
}
export interface RequestEvidenceCursor {
  owner_kind: string;
  owner_id: string;
  operation: string;
  idempotency_key: string;
}
export interface PendingRequestEvidence extends RequestEvidenceCursor {
  request_digest: string;
  resolution: "pending";
}
export interface RequestEvidencePage {
  schema_version: 1;
  kind: "workflow.request-evidence-page";
  items: PendingRequestEvidence[];
  next_cursor: RequestEvidenceCursor | null;
}
const workflowOwners = new Set(["workflow-configuration", "workflow-configuration-copy", "workflow-management"]);
function isCursor(value: unknown): value is RequestEvidenceCursor {
  return graphObject(value) && Object.keys(value).length === 4
    && workflowOwners.has(String(value.owner_kind))
    && ["owner_kind", "owner_id", "operation", "idempotency_key"].every(key =>
      typeof value[key] === "string" && (value[key] as string).length > 0);
}
export const evidenceCoordinate = (value: RequestEvidenceCursor) =>
  JSON.stringify([value.owner_kind, value.owner_id, value.operation, value.idempotency_key]);
export async function readPendingWorkflowEvidence(
  parameters: { limit?: number; cursor?: RequestEvidenceCursor } = {},
): Promise<RequestEvidencePage> {
  const value = await graphApplicationQuery("request-evidence.pending", parameters);
  if (!graphObject(value) || Object.keys(value).length !== 4 || value.schema_version !== 1
    || value.kind !== "workflow.request-evidence-page" || !Array.isArray(value.items)
    || value.items.length > (parameters.limit ?? 50)
    || !value.items.every(row => graphObject(row) && Object.keys(row).length === 6
      && isCursor(Object.fromEntries(["owner_kind", "owner_id", "operation", "idempotency_key"]
        .map(key => [key, row[key]])))
      && row.resolution === "pending" && typeof row.request_digest === "string"
      && /^[0-9a-f]{64}$/.test(row.request_digest))
    || new Set(value.items.map(row => evidenceCoordinate(row as PendingRequestEvidence))).size !== value.items.length
    || value.next_cursor !== null && (!isCursor(value.next_cursor) || !value.items.length
      || evidenceCoordinate(value.next_cursor) !== evidenceCoordinate(value.items.at(-1) as PendingRequestEvidence)))
    throw new WorkbenchApiError("unavailable", "工作流原请求摘要格式无效，未核实或重发");
  return graphClone(value) as unknown as RequestEvidencePage;
}
export function isRequestEvidenceRecord(value: unknown): value is RequestEvidenceRecord {
  return graphObject(value) && Object.keys(value).length === 9
    && value.schema_version === 1 && value.kind === "workflow.request-evidence"
    && typeof value.operation === "string" && value.operation.length > 0
    && typeof value.idempotency_key === "string" && value.idempotency_key.length > 0
    && typeof value.owner_kind === "string" && value.owner_kind.length > 0
    && typeof value.owner_id === "string" && value.owner_id.length > 0
    && typeof value.request_digest === "string" && /^[0-9a-f]{64}$/.test(value.request_digest)
    && ["pending", "verified", "archived"].includes(String(value.resolution))
    && graphObject(value.request) && isGraphJsonValue(value.request)
    && value.request.idempotency_key === value.idempotency_key;
}
function commandResult(value: unknown, operation: string, key: string): RequestEvidenceRecord {
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  const record = graphObject(value) ? value.result : null;
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-command"
    || !receipt || receipt.schema_version !== 1 || receipt.kind !== "workflow.application-receipt"
    || receipt.operation !== operation || receipt.operation_scope !== "management"
    || receipt.idempotency_key !== key || receipt.authority !== "service_receipt"
    || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256)
    || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 0
    || !isRequestEvidenceRecord(record) || !graphObject(receipt.accepted))
    throw new WorkbenchApiError("unknown", "Original request evidence acceptance differs");
  const accepted = Object.fromEntries(["operation", "idempotency_key", "owner_kind", "owner_id",
    "request_digest", "resolution"].map(field => [field, record[field as keyof RequestEvidenceRecord]]));
  if (graphJsonSignature(receipt.accepted) !== graphJsonSignature(accepted))
    throw new WorkbenchApiError("unknown", "Original request evidence receipt differs");
  return graphClone(record);
}
export interface RequestEvidencePorts {
  read(owner_kind: string, owner_id: string): Promise<RequestEvidenceRecord[]>;
  write(request: RequestEvidenceWrite): Promise<RequestEvidenceRecord>;
  resolve(request: RequestEvidenceResolve): Promise<RequestEvidenceRecord>;
}
export async function confirmCurrentPendingEvidence(evidence: RequestEvidencePorts, record: RequestEvidenceRecord) {
  const current = await evidence.read(record.owner_kind, record.owner_id);
  if (!Array.isArray(current) || !current.every(isRequestEvidenceRecord)
    || current.some(row => row.owner_kind !== record.owner_kind || row.owner_id !== record.owner_id)
    || current.filter(row => row.resolution === "pending").length !== 1
    || !current.some(row => row.resolution === "pending" && row.operation === record.operation
      && row.idempotency_key === record.idempotency_key && row.request_digest === record.request_digest
      && graphJsonSignature(row.request) === graphJsonSignature(record.request)))
    throw new WorkbenchApiError("unknown", "当前原请求证据与写入回执不同，业务未发送；请只读核实");
}
export async function confirmEvidenceForResolution(evidence: RequestEvidencePorts, record: RequestEvidenceRecord) {
  const current = await evidence.read(record.owner_kind, record.owner_id);
  if (!Array.isArray(current) || !current.every(isRequestEvidenceRecord)
    || current.some(row => row.owner_kind !== record.owner_kind || row.owner_id !== record.owner_id)
    || current.some(row => row.resolution === "pending" && (row.operation !== record.operation
      || row.idempotency_key !== record.idempotency_key || row.request_digest !== record.request_digest))
    || !current.some(row => ["pending", "verified"].includes(row.resolution) && row.operation === record.operation
      && row.idempotency_key === record.idempotency_key && row.request_digest === record.request_digest
      && graphJsonSignature(row.request) === graphJsonSignature(record.request)))
    throw new WorkbenchApiError("unknown", "当前证据与原结案请求不同，原请求仍保留；请只读核实");
}
export const requestEvidencePorts: RequestEvidencePorts = {
  async read(owner_kind, owner_id) {
    const records = await graphApplicationQuery("request-evidence.read", { owner_kind, owner_id });
    if (!Array.isArray(records) || !records.every(record => isRequestEvidenceRecord(record)
      && record.owner_kind === owner_kind && record.owner_id === owner_id)
      || new Set(records.map(record => `${record.operation}:${record.idempotency_key}`)).size !== records.length
      || records.filter(record => record.resolution === "pending").length > 1)
      throw new WorkbenchApiError("unavailable", "Original request evidence identity or format differs");
    return graphClone(records);
  },
  async write(request) {
    const value = await workbenchRequest("/api/graph/commands", {
      body: { operation: "request-evidence.write", parameters: graphClone(request) },
    });
    const record = commandResult(value, "request-evidence.write", request.idempotency_key);
    if (record.operation !== request.operation || record.idempotency_key !== request.idempotency_key
      || record.owner_kind !== request.owner_kind || record.owner_id !== request.owner_id
      || record.resolution !== "pending" || graphJsonSignature(record.request) !== graphJsonSignature(request.request))
      throw new WorkbenchApiError("unknown", "Saved request evidence differs from the original request");
    return record;
  },
  async resolve(request) {
    const value = await workbenchRequest("/api/graph/commands", {
      body: { operation: "request-evidence.resolve", parameters: graphClone(request) },
    });
    const record = commandResult(value, "request-evidence.resolve", request.idempotency_key);
    if (record.operation !== request.operation || record.idempotency_key !== request.idempotency_key
      || record.request_digest !== request.request_digest || record.resolution !== request.resolution)
      throw new WorkbenchApiError("unknown", "Original request evidence resolution differs");
    return record;
  },
};
