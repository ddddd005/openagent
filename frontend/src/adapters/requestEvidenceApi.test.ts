import { afterEach, describe, expect, it, vi } from "vitest";
import { requestEvidencePorts, readPendingEvidence, confirmCurrentPendingEvidence,
  confirmEvidenceForResolution, type RequestEvidencePorts, type RequestEvidenceRecord } from "./requestEvidenceApi";
import { memoryRequestEvidence, requestEvidenceCommandResponse } from "../testUtils/requestEvidence";
import { graphClone } from "../domain/workflowGraph";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function original() {
  const idempotency_key = crypto.randomUUID();
  return { operation: "resource.save", idempotency_key, owner_kind: "resource-channel", owner_id: "prompts",
    request: { idempotency_key, expected_sequence: 0, record: { value: "Original" } } };
}
describe("request evidence API boundaries", () => {
  it("uses only named evidence operations and verifies their exact identity and acceptance", async () => {
    const memory = memoryRequestEvidence(), request = original(), operations: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_path: string, init: RequestInit) => {
      const command = JSON.parse(String(init.body)); operations.push(command.operation);
      if (command.operation === "request-evidence.read")
        return new Response(JSON.stringify(await memory.ports.read(command.parameters.owner_kind, command.parameters.owner_id)));
      const record = await (command.operation === "request-evidence.write"
        ? memory.ports.write : memory.ports.resolve)(command.parameters);
      return requestEvidenceCommandResponse(command.operation, record);
    }));
    expect(await requestEvidencePorts.read(request.owner_kind, request.owner_id)).toEqual([]);
    const record = await requestEvidencePorts.write(request);
    expect(record.request).toEqual(request.request);
    const resolved = await requestEvidencePorts.resolve({ operation: request.operation,
      idempotency_key: request.idempotency_key, request_digest: record.request_digest, resolution: "archived" });
    expect(await requestEvidencePorts.read(request.owner_kind, request.owner_id)).toEqual([resolved]);
    expect(operations).toEqual(["request-evidence.read", "request-evidence.write",
      "request-evidence.resolve", "request-evidence.read"]);
  });
  it("rejects evidence belonging to another channel", async () => {
    const request = original(), memory = memoryRequestEvidence(), record = await memory.ports.write(request);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ ...record, owner_id: "foreign" }]))));
    await expect(requestEvidencePorts.read(request.owner_kind, request.owner_id)).rejects.toMatchObject({
      kind: "unavailable",
    });
  });
  it("rejects a write receipt with altered original bytes", async () => {
    const request = original(), memory = memoryRequestEvidence(), record = await memory.ports.write(request);
    vi.stubGlobal("fetch", vi.fn(async () => requestEvidenceCommandResponse("request-evidence.write", {
      ...record, request: { ...record.request, expected_sequence: 1 },
    })));
    await expect(requestEvidencePorts.write(request)).rejects.toMatchObject({ kind: "unknown" });
    expect([...memory.records.values()][0]?.request).toEqual(request.request);
  });
  it("rejects a resolved result whose frozen digest differs", async () => {
    const request = original(), memory = memoryRequestEvidence(), record = await memory.ports.write(request);
    vi.stubGlobal("fetch", vi.fn(async () => requestEvidenceCommandResponse("request-evidence.resolve", {
      ...record, request_digest: "0".repeat(64), resolution: "verified",
    })));
    await expect(requestEvidencePorts.resolve({ operation: request.operation,
      idempotency_key: request.idempotency_key, request_digest: record.request_digest, resolution: "verified" }))
      .rejects.toMatchObject({ kind: "unknown" });
  });
});

describe("Q07 narrow evidence reads", () => {
  function workflowOriginal() {
    const idempotency_key = crypto.randomUUID();
    return { operation: "definition.update", idempotency_key,
      owner_kind: "workflow-configuration", owner_id: crypto.randomUUID(),
      request: { workflow_definition_id: crypto.randomUUID(), expected_edit_sequence: 0,
        idempotency_key } };
  }
  function memoryRecord(request: ReturnType<typeof workflowOriginal>, resolution: "pending" | "verified" = "pending") {
    const record: RequestEvidenceRecord = { schema_version: 1, kind: "workflow.request-evidence",
      operation: request.operation, idempotency_key: request.idempotency_key,
      owner_kind: request.owner_kind, owner_id: request.owner_id,
      request: graphClone(request.request), request_digest: "c".repeat(64), resolution };
    return record;
  }
  function summary(record: RequestEvidenceRecord) {
    return { owner_kind: record.owner_kind, owner_id: record.owner_id, operation: record.operation,
      idempotency_key: record.idempotency_key, request_digest: record.request_digest,
      resolution: "pending" as const };
  }
  function narrowPorts(records: RequestEvidenceRecord[]) {
    const read = vi.fn(async () => graphClone(records));
    const readPending = vi.fn(async (owner_kind: string, owner_id: string) =>
      records.filter(record => record.owner_kind === owner_kind && record.owner_id === owner_id
        && record.resolution === "pending").map(summary));
    const readPoint = vi.fn(async (owner_kind: string, owner_id: string,
      operation: string, idempotency_key: string) => records.filter(record =>
      record.owner_kind === owner_kind && record.owner_id === owner_id
      && record.operation === operation && record.idempotency_key === idempotency_key)
      .map(record => graphClone(record)));
    const ports: RequestEvidencePorts = { read, readPending, readPoint,
      write: vi.fn(), resolve: vi.fn() };
    return { ports, read, readPending, readPoint };
  }
  it("reads one exact record by owner+operation+key with complete identity checks", async () => {
    const request = workflowOriginal(), record = memoryRecord(request);
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_path: string, init: RequestInit) => {
      const command = JSON.parse(String(init.body)); bodies.push(command);
      return new Response(JSON.stringify(command.parameters.operation ? [record] : [record]));
    }));
    expect(await requestEvidencePorts.readPoint!(record.owner_kind, record.owner_id,
      request.operation, request.idempotency_key)).toEqual([record]);
    expect(bodies[0]).toMatchObject({ operation: "request-evidence.read", parameters: {
      owner_kind: record.owner_kind, owner_id: record.owner_id,
      operation: request.operation, idempotency_key: request.idempotency_key } });

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([record, record]))));
    await expect(requestEvidencePorts.readPoint!(record.owner_kind, record.owner_id,
      request.operation, request.idempotency_key)).rejects.toMatchObject({ kind: "unavailable" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([
      { ...record, owner_id: "foreign" }]))));
    await expect(requestEvidencePorts.readPoint!(record.owner_kind, record.owner_id,
      request.operation, request.idempotency_key)).rejects.toMatchObject({ kind: "unavailable" });
  });
  it("reads the owner's pending summary from the existing pending page", async () => {
    const request = workflowOriginal(), record = memoryRecord(request);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.request-evidence-page", next_cursor: null,
      items: [summary(record)] }))));
    expect(await requestEvidencePorts.readPending!(record.owner_kind, record.owner_id))
      .toEqual([summary(record)]);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.request-evidence-page", next_cursor: null,
      items: [summary(record), { ...summary(record), idempotency_key: "other" }] }))));
    await expect(requestEvidencePorts.readPending!(record.owner_kind, record.owner_id))
      .rejects.toMatchObject({ kind: "unavailable" });
  });
  it("layered initialization reads the summary first and only then the exact body", async () => {
    const request = workflowOriginal(), record = memoryRecord(request);
    const empty = narrowPorts([]);
    expect(await readPendingEvidence(empty.ports, record.owner_kind, record.owner_id)).toBeNull();
    expect(empty.readPending).toHaveBeenCalledTimes(1);
    expect(empty.readPoint).not.toHaveBeenCalled();
    expect(empty.read).not.toHaveBeenCalled();

    const present = narrowPorts([record]);
    expect(await readPendingEvidence(present.ports, record.owner_kind, record.owner_id)).toEqual(record);
    expect(present.readPending).toHaveBeenCalledWith(record.owner_kind, record.owner_id);
    expect(present.readPoint).toHaveBeenCalledWith(record.owner_kind, record.owner_id,
      request.operation, request.idempotency_key);
    expect(present.read).not.toHaveBeenCalled();
  });
  it("never treats an untrustworthy summary as no pending and never restores a mismatched body", async () => {
    const request = workflowOriginal(), record = memoryRecord(request);
    const duplicate = narrowPorts([record, { ...record, idempotency_key: "other" }]);
    await expect(readPendingEvidence(duplicate.ports, record.owner_kind, record.owner_id))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(duplicate.readPoint).not.toHaveBeenCalled();

    const malformed = narrowPorts([record]);
    malformed.readPending.mockResolvedValueOnce([{ ...summary(record), request_digest: "x" }]);
    await expect(readPendingEvidence(malformed.ports, record.owner_kind, record.owner_id))
      .rejects.toMatchObject({ kind: "unavailable" });

    const mismatched = narrowPorts([record]);
    mismatched.readPoint.mockResolvedValueOnce([{ ...record, request_digest: "d".repeat(64) }]);
    await expect(readPendingEvidence(mismatched.ports, record.owner_kind, record.owner_id))
      .rejects.toMatchObject({ kind: "unknown" });
  });
  it("confirms the write and the closure through point reads only", async () => {
    const request = workflowOriginal(), record = memoryRecord(request);
    const ports = narrowPorts([record]);
    await confirmCurrentPendingEvidence(ports.ports, record);
    expect(ports.readPoint).toHaveBeenCalledTimes(1);
    expect(ports.read).not.toHaveBeenCalled();
    await confirmEvidenceForResolution(ports.ports, record);
    expect(ports.readPoint).toHaveBeenCalledTimes(2);
    expect(ports.read).not.toHaveBeenCalled();

    const altered = narrowPorts([{ ...record, request: { ...record.request, record: { value: "Other" } } }]);
    await expect(confirmCurrentPendingEvidence(altered.ports, record))
      .rejects.toMatchObject({ kind: "unknown" });
    const resolved = narrowPorts([{ ...record, resolution: "verified" }]);
    await confirmEvidenceForResolution(resolved.ports, record);
    const missing = narrowPorts([]);
    await expect(confirmCurrentPendingEvidence(missing.ports, record))
      .rejects.toMatchObject({ kind: "unknown" });
  });
  it("keeps the full-history fallback for ports without narrow methods", async () => {
    const request = original(), memory = memoryRequestEvidence();
    const record = await memory.ports.write(request);
    await confirmCurrentPendingEvidence(memory.ports, record);
    await confirmEvidenceForResolution(memory.ports, record);
    expect(vi.mocked(memory.ports.read)).toHaveBeenCalledTimes(2);
    expect(await readPendingEvidence(memory.ports, record.owner_kind, record.owner_id)).toEqual(record);
  });
});
