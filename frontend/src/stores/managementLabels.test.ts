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
async function seedLabel(memory: ReturnType<typeof backend>, target: Parameters<ReturnType<typeof useManagementLabelsStore>["load"]>[0],
  label: string, archived = false) {
  return await memory.ports.save({
    address: { scope: "management", kind: "form",
      key: `management-label:${JSON.stringify([target.kind, target.scope ?? "", target.id])}`,
      workflow_definition_id: null, workflow_session_id: null },
    format_version: 1, value: { basis: { managementTarget: target }, raw: { label, archived } },
    expected_sequence: 0, idempotency_key: crypto.randomUUID(),
  });
}
describe("management-only labels and directory state", () => {
  it("reads canonical historical target keys without permitting retired label writes", async () => {
    const memory = backend(), store = useManagementLabelsStore(), target = { kind: "workflow" as const, id: crypto.randomUUID() };
    const record = await seedLabel(memory, { id: target.id, kind: "workflow" }, "Existing flow");
    await store.load(target);
    expect(store.label(target, "")).toBe("Existing flow");
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const restored = useManagementLabelsStore(); await restored.load(target);
    expect(restored.error(target)).toBe(""); expect(restored.label(target, "")).toBe("Existing flow");
    expect(await restored.update(target, { archived: true })).toBe(false);
    expect(restored.archived(target)).toBe(false);
    expect(await memory.ports.read(record.address)).toEqual(record);
    expect(memory.ports.save).toHaveBeenCalledOnce();
  });
  it("reads persisted names across stores without adding anything to runtime identities", async () => {
    const memory = backend(), store = useManagementLabelsStore(), target = { kind: "session" as const, id: crypto.randomUUID() };
    const record = await seedLabel(memory, target, "First conversation");
    await store.load(target);
    expect(store.label(target, "fallback")).toBe("First conversation");
    expect([...memory.records.values()][0]?.address.kind).toBe("form");
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const restored = useManagementLabelsStore(); await restored.load(target);
    expect(restored.label(target, "fallback")).toBe("First conversation");
    expect(target).toEqual({ kind: "session", id: target.id });
    expect(await memory.ports.read(record.address)).toEqual(record);
    expect(memory.ports.save).toHaveBeenCalledOnce();
  });
  it("separates equal resource UUIDs in different scopes and session identities", async () => {
    const memory = backend(), store = useManagementLabelsStore(), id = crypto.randomUUID();
    const a = { kind: "prompt" as const, id, scope: "workspace" }, b = { ...a, scope: "shared" };
    await seedLabel(memory, a, "A");
    await store.load(a); await store.load(b);
    expect(store.label(a, "")).toBe("A");
    expect(store.label(b, "B")).toBe("B"); expect(store.label({ kind: "session", id }, "C")).toBe("C");
  });
  it("rejects retired name writes before storage while preserving the old label", async () => {
    const memory = backend(), store = useManagementLabelsStore(), target = { kind: "session" as const, id: crypto.randomUUID() };
    const record = await seedLabel(memory, target, "original");
    await store.load(target);
    vi.mocked(localDraftPorts.save).mockClear();
    vi.mocked(localDraftPorts.save).mockRejectedValue(new Error("disk error"));
    expect(await store.update(target, { label: "not saved" })).toBe(false);
    expect(store.label(target, "")).toBe("original");
    expect(store.error(target)).toContain("只读");
    expect(store.error(target)).toContain("已退役");
    expect(localDraftPorts.save).not.toHaveBeenCalled();
    expect(await memory.ports.read(record.address)).toEqual(record);
  });
});
