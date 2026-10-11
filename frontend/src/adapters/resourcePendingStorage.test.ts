import { afterEach, describe, expect, it, vi } from "vitest";
import { createResourcePendingStorage } from "./resourcePendingStorage";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { createResourceOutbox } from "../application/resourceOutbox";
import { WorkbenchApiError } from "./workbenchApi";
import { graphObject } from "../domain/workflowGraph";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const request = () => ({ idempotency_key: crypto.randomUUID(), text: "Original request" });
describe("independent original request evidence", () => {
  it("retains old browser bytes without importing or writing them to the new evidence store", async () => {
    const memory = memoryRequestEvidence(), raw = '{"old":"original bytes"}';
    const storage = { getItem: vi.fn(() => raw), setItem: vi.fn(), removeItem: vi.fn() };
    vi.stubGlobal("localStorage", storage);
    const first = createResourcePendingStorage("resource-intent", "old-browser-outbox", "resource.save", memory.ports);
    expect(await first.readPending()).toBeNull();
    expect(first.readLegacyPending()).toBe(raw);
    await first.writePending(null);
    expect(memory.ports.write).not.toHaveBeenCalled();
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
    expect("importLegacyPending" in first).toBe(false);
  });
  it("reopens the exact request and rejects another page's new pending request", async () => {
    const memory = memoryRequestEvidence(), original = request(), raw = JSON.stringify(original);
    const first = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    const stale = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    expect(await stale.readPending()).toBeNull();
    await first.writePending(raw);
    const reopened = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    expect(await reopened.readPending()).toBe(raw);
    await expect(stale.writePending(JSON.stringify(request()))).rejects.toMatchObject({
      code: "request_evidence_pending",
    });
    expect(await reopened.readPending()).toBe(raw);
    await reopened.writePending(null);
    expect(await reopened.readPending()).toBeNull();
    expect([...memory.records.values()][0]).toMatchObject({ request: original, resolution: "verified" });
  });
  it("archives exact evidence through the outbox without deleting bytes or replaying the business save", async () => {
    const memory = memoryRequestEvidence(), original = request();
    const storage = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "Response lost"); });
    const outbox = createResourceOutbox({ ...storage, save, readReceipt: vi.fn() },
      (value): value is ReturnType<typeof request> => graphObject(value)
        && typeof value.idempotency_key === "string" && typeof value.text === "string",
      async () => {}, "resource");
    await outbox.initialize();
    expect(await outbox.send(original)).toBe(false);
    expect(outbox.locked.value).toBe(true);
    expect(await outbox.abandon(true)).toBe(true);
    expect(outbox.pending.value).toBeNull();
    expect(save).toHaveBeenCalledOnce();
    expect([...memory.records.values()][0]).toMatchObject({ request: original, resolution: "archived" });
    const reopened = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    expect(await reopened.readPending()).toBeNull();
    await reopened.writePending(JSON.stringify(request()));
    expect(memory.records.size).toBe(2);
  });
  it("keeps the unknown original when the archive write cannot be confirmed", async () => {
    const memory = memoryRequestEvidence(), original = request();
    const storage = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    await storage.writePending(JSON.stringify(original));
    vi.mocked(memory.ports.resolve).mockRejectedValue(new WorkbenchApiError("unknown", "Evidence response lost"));
    await expect(storage.archivePending(JSON.stringify(original))).rejects.toThrow("response lost");
    expect(await storage.readPending()).toBe(JSON.stringify(original));
    expect([...memory.records.values()][0]?.resolution).toBe("pending");
  });
  it("verifies an archive whose response was lost without resolving or dispatching it again", async () => {
    const memory = memoryRequestEvidence(), original = request();
    const storage = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "Business response lost"); });
    const outbox = createResourceOutbox({ ...storage, save, readReceipt: vi.fn() },
      (value): value is ReturnType<typeof request> => graphObject(value) && "idempotency_key" in value,
      async () => {}, "resource");
    await outbox.initialize();
    expect(await outbox.send(original)).toBe(false);
    const resolve = memory.ports.resolve;
    memory.ports.resolve = vi.fn(async request => {
      await resolve(request);
      throw new WorkbenchApiError("unknown", "Archive response lost");
    });
    expect(await outbox.abandon(true)).toBe(false);
    expect([...memory.records.values()][0]?.resolution).toBe("archived");
    expect(await outbox.abandon(true)).toBe(true);
    expect(outbox.pending.value).toBeNull();
    expect(memory.ports.resolve).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledOnce();
    expect(await storage.readPending()).toBeNull();
  });
  it("does not dispatch a business save if the evidence write fails", async () => {
    const memory = memoryRequestEvidence(), original = request();
    vi.mocked(memory.ports.write).mockRejectedValue(new Error("Evidence unavailable"));
    const storage = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    const save = vi.fn(), outbox = createResourceOutbox({ ...storage, save, readReceipt: vi.fn() },
      (value): value is ReturnType<typeof request> => graphObject(value) && "idempotency_key" in value,
      async () => {}, "resource");
    await outbox.initialize();
    expect(await outbox.send(original)).toBe(false);
    expect(save).not.toHaveBeenCalled();
  });
  it("keeps a durable original available for explicit archive after its evidence response is lost", async () => {
    const memory = memoryRequestEvidence(), original = request();
    const write = memory.ports.write;
    memory.ports.write = vi.fn(async request => {
      await write(request);
      throw new WorkbenchApiError("unknown", "Evidence response lost");
    });
    const storage = createResourcePendingStorage("resource", "", "resource.save", memory.ports);
    const save = vi.fn(), outbox = createResourceOutbox({ ...storage, save, readReceipt: vi.fn() },
      (value): value is ReturnType<typeof request> => graphObject(value) && "idempotency_key" in value,
      async () => {}, "resource");
    await outbox.initialize();
    expect(await outbox.send(original)).toBe(false);
    expect(outbox.pending.value).toEqual(original);
    expect(outbox.locked.value).toBe(true);
    expect(save).not.toHaveBeenCalled();
    expect(await outbox.abandon(true)).toBe(true);
    expect(outbox.locked.value).toBe(false);
    expect([...memory.records.values()][0]).toMatchObject({ request: original, resolution: "archived" });
    expect(memory.ports.write).toHaveBeenCalledOnce();
  });
});
