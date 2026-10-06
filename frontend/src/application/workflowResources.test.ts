import { describe, expect, it, vi } from "vitest";
import { createProviderResources } from "./workflowResources";
import { newProvider } from "../domain/workflowModelResources";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { ProviderSaveRequest } from "../adapters/workflowResourcesApi";

describe("provider resource request coordination", () => {
  it("persists unknown submissions and reads the identical key/body after reopening without resubmitting", async () => {
    let raw: string | null = null;
    const requests: ProviderSaveRequest[] = [];
    const reads: ProviderSaveRequest[] = [];
    const ports = { list: async () => [], readPending: () => raw, writePending: (value: string | null) => { raw = value; },
      save: async (request: ProviderSaveRequest) => { requests.push(request); throw new WorkbenchApiError("unknown", "unknown"); },
      readReceipt: async (request: ProviderSaveRequest) => { reads.push(request); } };
    const first = createProviderResources(ports), record = newProvider();
    expect(await first.save(record, 0)).toBe(false);
    record.value.name = "edited after submission";
    expect(first.pending.value?.record.value.name).toBe("DeepSeek");
    expect(first.locked.value).toBe(true);
    expect(await first.save(record, 0)).toBe(false);
    const reopened = createProviderResources(ports);
    expect(await reopened.reconcile()).toBe(true);
    expect(requests).toHaveLength(1); expect(reads).toEqual(requests); expect(raw).toBeNull();
  });
  it("uses the observed CAS sequence and leaves conflicts available for explicit refresh/edit", async () => {
    const save = vi.fn(async (_request: ProviderSaveRequest) => { throw new WorkbenchApiError("rejected", "changed", 409, "stale_revision"); });
    const resources = createProviderResources({ save, readReceipt: vi.fn(), list: async () => [],
      readPending: () => null, writePending: () => {} });
    const record = newProvider(); record.update_sequence = 7;
    expect(await resources.save(record, 7)).toBe(false);
    expect(save.mock.calls[0]![0].record.update_sequence).toBe(8);
    expect(save.mock.calls[0]![0].expected_sequence).toBe(7);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false);
  });
  it.each(["invalid_origin", "idempotency_conflict", "stale_revision", "not_found", "gone"])(
    "keeps an unknown original request when later reconciliation is rejected with %s", async code => {
    let raw: string | null = null;
    const requests: ProviderSaveRequest[] = [];
    const reads: ProviderSaveRequest[] = [];
    const ports = { list: async () => [], readPending: () => raw,
      writePending: (value: string | null) => { raw = value; },
      save: async (request: ProviderSaveRequest) => {
        requests.push(request);
        throw new WorkbenchApiError("unknown", "unknown");
      },
      readReceipt: async (request: ProviderSaveRequest) => {
        reads.push(request); throw new WorkbenchApiError("rejected", "policy rejected", 409, code);
      } };
    const original = createProviderResources(ports);
    expect(await original.save(newProvider(), 0)).toBe(false);
    const body = raw;
    const reopened = createProviderResources(ports);
    expect(await reopened.reconcile()).toBe(false);
    expect(raw).toBe(body); expect(reopened.locked.value).toBe(true);
    expect(await reopened.save(newProvider(), 0)).toBe(false);
    expect(requests).toHaveLength(1); expect(reads).toEqual(requests);
  });
  it("does not dispatch when request persistence fails or the record carries a secret field", async () => {
    const save = vi.fn();
    const resources = createProviderResources({ save, readReceipt: vi.fn(), list: async () => [], readPending: () => null,
      writePending: () => { throw new Error("disk"); } });
    expect(await resources.save(newProvider(), 0)).toBe(false);
    const forbidden = newProvider(); Object.assign(forbidden.value, { api_key: "forbidden" });
    expect(await resources.save(forbidden, 0)).toBe(false);
    expect(save).not.toHaveBeenCalled();
  });
  it("rejects a stale list response that finishes after a newer refresh", async () => {
    let complete!: (value: ReturnType<typeof newProvider>[]) => void;
    const older = new Promise<ReturnType<typeof newProvider>[]>(resolve => { complete = resolve; });
    let count = 0;
    const record = newProvider();
    const resources = createProviderResources({ save: async () => ({}), readReceipt: vi.fn(),
      list: () => ++count === 1 ? older : Promise.resolve([record]),
      readPending: () => null, writePending: () => {} });
    const first = resources.refresh(); await resources.refresh(); complete([]); await first;
    expect(resources.records.value).toEqual([record]);
  });

  it.each(["success", "failure", "durable replacement"])("preserves a newer pending across a late receipt %s", async outcome => {
    let raw: string | null = null, finish!: (value: unknown) => void, fail!: (failure: unknown) => void;
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "lost"); });
    const ports = { save, list: async () => [], readPending: () => raw,
      writePending: vi.fn((value: string | null) => { raw = value; }),
      readReceipt: () => new Promise((resolve, reject) => { finish = resolve; fail = reject; }) };
    const resources = createProviderResources(ports);
    await resources.save(newProvider(), 0);
    const original = JSON.parse(raw!) as ProviderSaveRequest;
    const reading = resources.reconcile();
    const newer = JSON.parse(raw!) as ProviderSaveRequest; newer.record.value.name = "newer original with same key";
    if (outcome === "durable replacement") raw = JSON.stringify(newer);
    else resources.pending.value = newer;
    resources.error.value = "newer diagnostic";
    const durable = raw, writes = ports.writePending.mock.calls.length;
    if (outcome === "failure") fail(new WorkbenchApiError("rejected", "old read denial", 409));
    else finish({});
    expect(await reading).toBe(false);
    expect(raw).toBe(durable); expect(resources.error.value).toBe("newer diagnostic");
    expect(resources.pending.value).toEqual(outcome === "durable replacement" ? original : newer);
    expect(ports.writePending).toHaveBeenCalledTimes(writes); expect(save).toHaveBeenCalledOnce();
  });

  it("keeps a confirmed provider request when clearing its durable pending throws", async () => {
    let raw: string | null = null, clearing = false;
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "lost"); });
    const readReceipt = vi.fn(async () => ({}));
    const resources = createProviderResources({ list: async () => [], save, readReceipt, readPending: () => raw,
      writePending: value => { if (value === null && !clearing) throw new Error("disk"); raw = value; } });
    await resources.save(newProvider(), 0);
    const original = raw;
    expect(await resources.reconcile()).toBe(false);
    expect(raw).toBe(original); expect(resources.pending.value).toEqual(JSON.parse(original!));
    clearing = true; expect(await resources.reconcile()).toBe(true);
    expect(raw).toBeNull(); expect(save).toHaveBeenCalledOnce(); expect(readReceipt).toHaveBeenCalledTimes(2);
  });
});
