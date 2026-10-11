import { createHash } from "node:crypto";
import { vi } from "vitest";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import type { RequestEvidencePorts, RequestEvidenceRecord } from "../adapters/requestEvidenceApi";

export function memoryRequestEvidence() {
  const records = new Map<string, RequestEvidenceRecord>();
  const identity = (operation: string, key: string) => JSON.stringify([operation, key]);
  const ports: RequestEvidencePorts = {
    read: vi.fn(async (ownerKind, ownerId) => graphClone([...records.values()]
      .filter(record => record.owner_kind === ownerKind && record.owner_id === ownerId))),
    write: vi.fn(async request => {
      const key = identity(request.operation, request.idempotency_key), prior = records.get(key);
      if (prior) {
        if (prior.owner_kind !== request.owner_kind || prior.owner_id !== request.owner_id
          || graphJsonSignature(prior.request) !== graphJsonSignature(request.request))
          throw new WorkbenchApiError("rejected", "Original request differs", 409, "idempotency_conflict");
        return graphClone({ ...prior, resolution: "pending" as const });
      }
      if ([...records.values()].some(record => record.owner_kind === request.owner_kind
        && record.owner_id === request.owner_id && record.resolution === "pending"))
        throw new WorkbenchApiError("rejected", "Original request is unresolved", 409, "request_evidence_pending");
      const record: RequestEvidenceRecord = { ...graphClone(request), schema_version: 1,
        kind: "workflow.request-evidence", resolution: "pending",
        request_digest: createHash("sha256").update(graphJsonSignature(request.request)).digest("hex") };
      records.set(key, graphClone(record));
      return graphClone(record);
    }),
    resolve: vi.fn(async request => {
      const record = records.get(identity(request.operation, request.idempotency_key));
      if (!record || record.request_digest !== request.request_digest
        || record.resolution !== "pending" && record.resolution !== request.resolution)
        throw new WorkbenchApiError("rejected", "Original request differs", 409, "request_evidence_conflict");
      record.resolution = request.resolution;
      return graphClone(record);
    }),
  };
  return { records, ports, identity };
}

export function requestEvidenceCommandResponse(operation: string, record: RequestEvidenceRecord) {
  const accepted = Object.fromEntries(["operation", "idempotency_key", "owner_kind", "owner_id",
    "request_digest", "resolution"].map(field => [field, record[field as keyof RequestEvidenceRecord]]));
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result: record,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
      idempotency_key: record.idempotency_key, request_sha256: "a".repeat(64),
      authority: "service_receipt", target: {}, accepted } }));
}
