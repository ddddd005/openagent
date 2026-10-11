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
