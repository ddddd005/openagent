import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { useFormDraftsStore } from "./formDrafts";
import { WorkbenchApiError } from "../adapters/workbenchApi";
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); });
function backend() {
  const memory = memoryLocalDraftStorage();
  vi.spyOn(localDraftPorts, "read").mockImplementation(memory.ports.read);
  vi.spyOn(localDraftPorts, "save").mockImplementation(memory.ports.save);
  vi.spyOn(localDraftPorts, "receipt").mockImplementation(memory.ports.receipt);
  return memory;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
describe("raw form draft custody", () => {
  it("debounces unfinished raw fields, restores staged memory and survives a new store", async () => {
    const memory = backend(), store = useFormDraftsStore(), basis = { workflowId: crypto.randomUUID(), config: { text: "" } };
    expect(store.stage("node-fields", basis, { json: "{unfinished" })).toBe(false);
    expect(await store.load("node-fields", basis)).toMatchObject({ value: null, conflict: false });
    expect(store.stage("node-fields", basis, { json: "{unfinished", number: "1e-" })).toBe(true);
    expect((await store.load("node-fields", basis)).value).toEqual({ json: "{unfinished", number: "1e-" });
    await vi.advanceTimersByTimeAsync(200);
    expect(store.hasUnsaved).toBe(false); expect(memory.records.size).toBe(1);
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    expect((await useFormDraftsStore().load("node-fields", basis)).value).toEqual({ json: "{unfinished", number: "1e-" });
  });
  it("does not silently apply an old basis, permits an explicit rebase, and does not globally block settled old drafts", async () => {
    backend();
    const store = useFormDraftsStore(), oldBasis = { config: { text: "old" } }, nextBasis = { config: { text: "new" } };
    await store.load("prompt", oldBasis); store.stage("prompt", oldBasis, "unfinished");
    expect(await store.flush()).toBe(true);
    expect(await store.load("prompt", nextBasis)).toMatchObject({ value: "unfinished", conflict: true });
    expect(store.stage("prompt", nextBasis, "changed")).toBe(false);
    expect(await store.flush()).toBe(true);
    expect(store.rebase("prompt", nextBasis)).toBe(true);
    expect(store.stage("prompt", nextBasis, "explicitly restored")).toBe(true);
    expect(await store.flush()).toBe(true);
  });
  it("keeps load failures blocked rather than creating a replacement empty record", async () => {
    const memory = backend();
    vi.spyOn(localDraftPorts, "read").mockRejectedValue(new Error("disk unreadable"));
    const store = useFormDraftsStore();
    await expect(store.load("prompt", {})).rejects.toThrow("disk unreadable");
    expect(store.stage("prompt", {}, "lost edit")).toBe(false);
    expect(await store.flush()).toBe(false);
    expect(memory.records.size).toBe(0); expect(store.errors.prompt).toContain("disk unreadable");
  });
  it("flushes newer staged input after an in-flight earlier save without reporting clean early", async () => {
    const memory = backend(), gate = deferred<void>(), dispatched = deferred<void>();
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      dispatched.resolve(); await gate.promise; return memory.ports.save(request);
    });
    const store = useFormDraftsStore(), basis = {};
    await store.load("prompt", basis); store.stage("prompt", basis, "first raw");
    const flushing = store.flush();
    await dispatched.promise; expect(store.hasUnsaved).toBe(true);
    store.stage("prompt", basis, "second raw while saving");
    gate.resolve(); expect(await flushing).toBe(true);
    expect([...memory.records.values()][0]?.value).toEqual({ basis, raw: "second raw while saving" });
    expect(store.hasUnsaved).toBe(false);
  });
  it("does not resurrect old raw input when a debounce flush overlaps a pending tombstone", async () => {
    const memory = backend(), gate = deferred<void>(), dispatched = deferred<void>();
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      dispatched.resolve(); await gate.promise; return memory.ports.save(request);
    });
    const store = useFormDraftsStore(), basis = {};
    await store.load("prompt", basis); store.stage("prompt", basis, "discarded raw");
    const removing = store.remove("prompt", basis);
    await dispatched.promise; await vi.advanceTimersByTimeAsync(200);
    gate.resolve(); expect(await removing).toBe(true);
    expect(await store.flush()).toBe(true);
    expect([...memory.records.values()][0]?.deleted).toBe(true);
    expect((await store.load("prompt", basis)).value).toBeNull();
  });
  it("preserves input staged while a tombstone is in flight and writes it after clearing", async () => {
    const memory = backend(), gate = deferred<void>(), dispatched = deferred<void>();
    const store = useFormDraftsStore(), basis = {};
    await store.load("prompt", basis); store.stage("prompt", basis, "initial raw"); await store.flush();
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      dispatched.resolve(); await gate.promise; return memory.ports.save(request);
    });
    const removing = store.remove("prompt", basis); await dispatched.promise;
    expect(store.stage("prompt", basis, "newer raw while clearing")).toBe(true);
    const flushing = store.flush();
    gate.resolve(); expect(await removing).toBe(true); expect(await flushing).toBe(true);
    expect([...memory.records.values()][0]?.value).toEqual({ basis, raw: "newer raw while clearing" });
    expect((await store.load("prompt", basis)).value).toBe("newer raw while clearing");
    expect(store.hasUnsaved).toBe(false);
  });
  it("keeps an unknown clear pending and flushes only its frozen receipt before storing newer input", async () => {
    const memory = backend(), store = useFormDraftsStore(), basis = {};
    await store.load("prompt", basis); store.stage("prompt", basis, "initial raw"); await store.flush();
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      await memory.ports.save(request);
      throw new WorkbenchApiError("unknown", "clear response lost");
    });
    expect(await store.remove("prompt", basis)).toBe(false);
    expect(store.hasUnsaved).toBe(true); expect(store.status.prompt).toBe("blocked");
    const clears = vi.mocked(localDraftPorts.save).mock.calls.filter(([request]) => request.value === null);
    expect(clears).toHaveLength(1);
    store.stage("prompt", basis, "typed while clear unresolved");
    expect(await store.flush()).toBe(true);
    expect(localDraftPorts.receipt).toHaveBeenCalledWith(clears[0]![0]);
    expect(vi.mocked(localDraftPorts.save).mock.calls.filter(([request]) => request.value === null)).toHaveLength(1);
    expect([...memory.records.values()][0]?.value).toEqual({ basis, raw: "typed while clear unresolved" });
    expect(store.hasUnsaved).toBe(false);
  });
  it("fences late loads against an explicit rebase or a more recent load context", async () => {
    backend();
    const store = useFormDraftsStore(), oldBasis = { config: "old" }, nextBasis = { config: "new" };
    await store.load("prompt", oldBasis); store.stage("prompt", oldBasis, "old raw"); await store.flush();
    const loading = store.load("prompt", oldBasis);
    expect(store.rebase("prompt", nextBasis)).toBe(true);
    expect(store.stage("prompt", nextBasis, "rebased raw")).toBe(true);
    expect(await loading).toMatchObject({ value: "rebased raw", conflict: false });
    expect(await store.flush()).toBe(true);
    const stale = store.load("prompt", oldBasis), current = store.load("prompt", nextBasis);
    expect(await current).toMatchObject({ value: "rebased raw", conflict: false });
    await stale;
    expect(store.status.prompt).toBe("saved"); expect(store.errors.prompt).toBe("");
  });
  it("preserves both sides of a CAS conflict in separate backend records", async () => {
    const memory = backend(), store = useFormDraftsStore(), basis = {};
    await store.load("prompt", basis); store.stage("prompt", basis, "first raw"); await store.flush();
    const original = [...memory.records.values()][0]!;
    await memory.ports.save({ address: original.address, format_version: 1, value: { basis, raw: "other page" },
      expected_sequence: original.sequence, idempotency_key: crypto.randomUUID() });
    store.stage("prompt", basis, "this page");
    expect(await store.flush()).toBe(false);
    expect(memory.records.get(memory.key(original.address))?.value).toEqual({ basis, raw: "other page" });
    expect([...memory.records.values()].find(record => record.address.key.includes(":conflict:"))?.value)
      .toEqual({ basis, raw: "this page" });
    expect(store.hasUnsaved).toBe(true); expect(store.errors.prompt).toContain("独立保存");
    expect(store.stage("prompt", basis, "cannot silently overwrite")).toBe(false);
  });
  it("rejects future raw form formats without overwriting their original value", async () => {
    const memory = backend(), basis = {}, original = { basis, raw: "future raw" };
    await memory.ports.save({ address: { scope: "management", kind: "form", key: "prompt",
      workflow_definition_id: null, workflow_session_id: null }, format_version: 2, value: original,
      expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    const store = useFormDraftsStore();
    await expect(store.load("prompt", basis)).rejects.toThrow("版本不兼容");
    expect(store.stage("prompt", basis, "overwrite")).toBe(false); expect(await store.flush()).toBe(false);
    expect([...memory.records.values()][0]?.value).toEqual(original);
    expect(vi.mocked(localDraftPorts.save)).not.toHaveBeenCalled();
  });
});
