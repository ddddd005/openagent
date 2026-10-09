import { randomUUID } from "node:crypto";
import { vi } from "vitest";

const clone = (value: any) => JSON.parse(JSON.stringify(value));

export function consumerDraftStateAddress(workflow: string, namespace = "generic") {
  return { scope: "consumer", kind: "chat-client", key: namespace + ":state",
    workflow_definition_id: workflow, workflow_session_id: null };
}

export function consumerKeyFactory(first?: string) {
  let unused = first;
  return vi.fn(() => {
    const key = unused ?? randomUUID();
    unused = undefined;
    return key;
  });
}

export function memoryConsumerDraftPort() {
  const records = new Map<string, any>(), receipts = new Map<string, any>();
  const identity = (address: any) => JSON.stringify(address);
  const read = vi.fn(async (address: any) => clone(records.get(identity(address)) ?? null));
  const save = vi.fn(async (request: any, reconcile = false) => {
    const prior = receipts.get(request.idempotency_key);
    if (prior) {
      if (JSON.stringify(prior.request) !== JSON.stringify(request))
        throw new Error("Draft idempotency request changed");
      return clone(prior.record);
    }
    if (reconcile) throw new Error("Original local draft receipt remains unknown");
    const key = identity(request.address), sequence = records.get(key)?.sequence ?? 0;
    if (sequence !== request.expected_sequence)
      throw Object.assign(new Error("Local draft CAS conflict"), { definite: true });
    const record = { schema_version: 1, kind: "workflow.local-draft", address: clone(request.address),
      format_version: request.format_version, sequence: sequence + 1,
      updated_at: "2026-10-09T00:00:00Z", value: clone(request.value), deleted: request.value === null };
    records.set(key, clone(record));
    receipts.set(request.idempotency_key, { request: clone(request), record: clone(record) });
    return clone(record);
  });
  return {
    read, save, records, receipts,
    seed(address: any, value: any) {
      records.set(identity(address), { schema_version: 1, kind: "workflow.local-draft", address: clone(address),
        format_version: 1, sequence: 1, updated_at: "2026-10-09T00:00:00Z",
        value: clone(value), deleted: value === null });
    },
    value(address: any) { return clone(records.get(identity(address))?.value ?? null); },
  };
}
