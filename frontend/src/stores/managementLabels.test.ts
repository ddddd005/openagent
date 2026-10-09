import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { useManagementLabelsStore } from "./managementLabels";
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });
function backend() {
  const memory = memoryLocalDraftStorage();
  vi.spyOn(localDraftPorts, "read").mockImplementation(memory.ports.read);
  vi.spyOn(localDraftPorts, "save").mockImplementation(memory.ports.save);
  vi.spyOn(localDraftPorts, "receipt").mockImplementation(memory.ports.receipt);
  return memory;
}
describe("management-only labels and directory state", () => {
  it("persists names across stores without adding anything to runtime identities", async () => {
    const memory = backend(), store = useManagementLabelsStore(), target = { kind: "session" as const, id: crypto.randomUUID() };
    expect(await store.update(target, { label: "First conversation" })).toBe(true);
    expect(store.label(target, "fallback")).toBe("First conversation");
    expect([...memory.records.values()][0]?.address.kind).toBe("form");
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const restored = useManagementLabelsStore(); await restored.load(target);
    expect(restored.label(target, "fallback")).toBe("First conversation");
  });
  it("separates equal resource UUIDs in different scopes and session identities", async () => {
    backend(); const store = useManagementLabelsStore(), id = crypto.randomUUID();
    const a = { kind: "prompt" as const, id, scope: "workspace" }, b = { ...a, scope: "shared" };
    await store.update(a, { label: "A" }); await store.load(b);
    expect(store.label(b, "B")).toBe("B"); expect(store.label({ kind: "session", id }, "C")).toBe("C");
  });
  it("serializes name and archive updates for the same target", async () => {
    backend(); const store = useManagementLabelsStore(), target = { kind: "workflow" as const, id: crypto.randomUUID() };
    expect(await Promise.all([store.update(target, { label: "Name" }), store.update(target, { archived: true })])).toEqual([true, true]);
    expect(store.label(target, "")).toBe("Name"); expect(store.archived(target)).toBe(true);
    expect(await store.update(target, { archived: false })).toBe(true);
  });
  it("does not report failed disk writes as saved names", async () => {
    backend(); const store = useManagementLabelsStore(), target = { kind: "session" as const, id: crypto.randomUUID() };
    await store.load(target);
    vi.mocked(localDraftPorts.save).mockRejectedValue(new Error("disk error"));
    expect(await store.update(target, { label: "not saved" })).toBe(false);
    expect(store.label(target, "original")).toBe("original"); expect(store.error(target)).toContain("disk error");
  });
});
