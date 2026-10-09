import { vi } from "vitest";
import { graphClone } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { LocalDraftPorts, LocalDraftRecord, LocalDraftSaveRequest } from "../adapters/localDraftsApi";

export function memoryLocalDraftStorage() {
  const records = new Map<string, LocalDraftRecord>();
  const receipts = new Map<string, { request: LocalDraftSaveRequest; result: LocalDraftRecord }>();
  const key = (address: LocalDraftRecord["address"]) => JSON.stringify(address);
  const ports: LocalDraftPorts = {
    read: vi.fn(async address => graphClone(records.get(key(address)) ?? null)),
    save: vi.fn(async request => {
      const current = records.get(key(request.address));
      if ((current?.sequence ?? 0) !== request.expected_sequence)
        throw new WorkbenchApiError("rejected", "Draft was changed", 409, "stale_draft_sequence");
      const result: LocalDraftRecord = { schema_version: 1, kind: "workflow.local-draft",
        address: graphClone(request.address), format_version: request.format_version,
        sequence: request.expected_sequence + 1, updated_at: new Date().toISOString(),
        value: graphClone(request.value), deleted: request.value === null };
      records.set(key(request.address), graphClone(result));
      receipts.set(request.idempotency_key, { request: graphClone(request), result: graphClone(result) });
      return graphClone(result);
    }),
    receipt: vi.fn(async request => {
      const receipt = receipts.get(request.idempotency_key);
      if (!receipt || JSON.stringify(receipt.request) !== JSON.stringify(request))
        throw new WorkbenchApiError("unknown", "Original draft receipt is unresolved");
      return graphClone(receipt.result);
    }),
  };
  return { ports, records, receipts, key };
}
export function localDraftCommandResponse(request: LocalDraftSaveRequest, result: LocalDraftRecord) {
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "draft.save",
      operation_scope: "management", idempotency_key: request.idempotency_key,
      request_sha256: "a".repeat(64), authority: "service_receipt", target: {},
      accepted: { draft_address: request.address, draft_sequence: result.sequence,
        draft_format_version: result.format_version, deleted: result.deleted } } }));
}
