import { describe, expect, it, vi } from "vitest";
import { graphClone } from "../../domain/workflowGraph";
import { WorkbenchApiError } from "../../adapters/workbenchApi";
import { createLorebookResources, type LorebookResourcesPorts } from "./lorebookResourceController";
import { newLorebookResource } from "./lorebookResources";
import type { LorebookSaveRequest } from "./lorebookResourcesApi";

function fixture(overrides: Partial<LorebookResourcesPorts> = {}, initialRaw: string | null = null) {
  let raw: string | null = initialRaw;
  const save = vi.fn(async (_request: LorebookSaveRequest) => ({}));
  const readReceipt = vi.fn(async (_request: LorebookSaveRequest) => ({}));
  const ports: LorebookResourcesPorts = {
    list: async () => [], save, readReceipt, readPending: () => raw,
    writePending: value => { raw = value; }, ...overrides,
  };
  return { ports, save, readReceipt, resources: createLorebookResources(ports),
    raw: () => raw, replace: (value: string | null) => { raw = value; } };
}
describe("lorebook original request protection", () => {
  it("persists the exact request before dispatch and preserves the caller draft", async () => {
    const record = newLorebookResource();
    record.update_sequence = 8;
    const original = graphClone(record);
    const f = fixture({ save: vi.fn(async request => {
      expect(JSON.parse(f.raw()!)).toEqual(request);
      expect(request.expected_sequence).toBe(8);
      expect(request.record.update_sequence).toBe(9);
      request.record.value.name = "Adapter mutation";
    }) });
    expect(await f.resources.save(record, 8)).toBe(true);
    expect(record).toEqual(original);
    expect(f.resources.pending.value).toBeNull();
    expect(f.raw()).toBeNull();
  });
  it("retains unknown requests across reopen and only queries the original receipt", async () => {
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "Response lost"); });
    const f = fixture({ save });
    expect(await f.resources.save(newLorebookResource(), 0)).toBe(false);
    const original = graphClone(f.resources.pending.value!);
    expect(f.resources.locked.value).toBe(true);
    expect(await f.resources.save(newLorebookResource(), 0)).toBe(false);
    const reopened = createLorebookResources(f.ports);
    expect(reopened.pending.value).toEqual(original);
    expect(save).toHaveBeenCalledTimes(1);
    expect(await reopened.reconcile()).toBe(true);
    expect(f.readReceipt).toHaveBeenCalledWith(original);
    expect(save).toHaveBeenCalledTimes(1);
    expect(f.raw()).toBeNull();
  });
  it.each(["unknown", "rejected"] as const)("receipt %s cannot settle an unknown mutation", async kind => {
    const f = fixture({
      save: async () => { throw new WorkbenchApiError("unknown", "Response lost"); },
      readReceipt: async () => { throw new WorkbenchApiError(kind, "Receipt unavailable"); },
    });
    await f.resources.save(newLorebookResource(), 0);
    const original = f.raw();
    expect(await f.resources.reconcile()).toBe(false);
    expect(f.raw()).toBe(original);
    expect(f.resources.locked.value).toBe(true);
  });
  it.each(["stale_revision", "idempotency_conflict"])("settles only authoritative rejection: %s", async code => {
    const f = fixture({ save: async () => { throw new WorkbenchApiError("rejected", code, undefined, code); } });
    expect(await f.resources.save(newLorebookResource(), 0)).toBe(false);
    expect(f.resources.pending.value !== null).toBe(code === "idempotency_conflict");
  });
  it("fails closed for corrupt pending storage and prevents a request without durable storage", async () => {
    const corrupt = fixture({}, "{corrupt");
    expect(corrupt.resources.locked.value).toBe(true);
    expect(await corrupt.resources.save(newLorebookResource(), 0)).toBe(false);
    expect(corrupt.save).not.toHaveBeenCalled();
    const broken = fixture({ writePending: () => { throw new Error("Storage denied"); } });
    expect(await broken.resources.save(newLorebookResource(), 0)).toBe(false);
    expect(broken.save).not.toHaveBeenCalled();
  });
  it("blocks double submission and ignores a late response after the durable request changes", async () => {
    let complete!: () => void;
    const save = vi.fn(() => new Promise<void>(resolve => { complete = resolve; }));
    const f = fixture({ save });
    const pending = f.resources.save(newLorebookResource(), 0);
    expect(await f.resources.save(newLorebookResource(), 0)).toBe(false);
    const other = { ...graphClone(f.resources.pending.value!), idempotency_key: crypto.randomUUID() };
    f.replace(JSON.stringify(other));
    complete();
    expect(await pending).toBe(false);
    expect(JSON.parse(f.raw()!)).toEqual(other);
    expect(f.resources.pending.value).not.toBeNull();
    expect(save).toHaveBeenCalledTimes(1);
  });
  it("keeps pending state if clearing its durable record fails", async () => {
    const f = fixture();
    const write = f.ports.writePending;
    f.ports.writePending = value => { if (value === null) throw new Error("Cannot clear"); write(value); };
    expect(await f.resources.save(newLorebookResource(), 0)).toBe(false);
    expect(f.resources.pending.value).not.toBeNull();
    expect(f.raw()).not.toBeNull();
  });
  it("does not let stale list responses overwrite the latest read", async () => {
    const first = newLorebookResource(), second = newLorebookResource();
    let resolveFirst!: (value: typeof first[]) => void;
    const list = vi.fn().mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
      .mockResolvedValueOnce([second]);
    const f = fixture({ list });
    const pending = f.resources.refresh();
    await f.resources.refresh();
    resolveFirst([first]);
    await pending;
    expect(f.resources.records.value).toEqual([second]);
  });
});
