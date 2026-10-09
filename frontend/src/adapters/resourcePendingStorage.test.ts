import { afterEach, describe, expect, it, vi } from "vitest";
import { localDraftPorts } from "./localDraftsApi";
import { createResourcePendingStorage } from "./resourcePendingStorage";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("backend resource import marker", () => {
  it("retains original browser bytes and does not offer a settled legacy outbox again on reopening", async () => {
    const memory = memoryLocalDraftStorage(), raw = JSON.stringify({ frozen: "original request" });
    vi.spyOn(localDraftPorts, "read").mockImplementation(memory.ports.read);
    vi.spyOn(localDraftPorts, "save").mockImplementation(memory.ports.save);
    vi.spyOn(localDraftPorts, "receipt").mockImplementation(memory.ports.receipt);
    const storage = { getItem: vi.fn(() => raw), setItem: vi.fn(), removeItem: vi.fn() };
    vi.stubGlobal("localStorage", storage);
    const first = createResourcePendingStorage("resource-intent", "old-browser-outbox");
    expect(await first.readPending()).toBeNull(); expect(first.readLegacyPending()).toBe(raw);
    await first.importLegacyPending(raw);
    expect(await first.readPending()).toBe(raw);
    await first.writePending(null);
    const reopened = createResourcePendingStorage("resource-intent", "old-browser-outbox");
    expect(await reopened.readPending()).toBeNull(); expect(reopened.readLegacyPending()).toBeNull();
    expect(storage.getItem()).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled(); expect(storage.removeItem).not.toHaveBeenCalled();
    expect([...memory.records.values()][0]?.value).toEqual({ request: null, legacy_imported: true });
  });
});
