import { afterEach, describe, expect, it, vi } from "vitest";
import { requestEvidencePorts } from "./requestEvidenceApi";
import { memoryRequestEvidence, requestEvidenceCommandResponse } from "../testUtils/requestEvidence";

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
