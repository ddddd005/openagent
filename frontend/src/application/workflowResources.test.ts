import { describe, expect, it, vi } from "vitest";
import { createProviderResources } from "./workflowResources";
import { newProvider } from "../domain/workflowModelResources";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { ProviderSaveRequest } from "../adapters/workflowResourcesApi";

describe("provider resource request coordination", () => {
  it("persists unknown submissions and replays the identical key/body after reopening", async () => {
    let raw: string | null = null;
    const requests: ProviderSaveRequest[] = [];
    let reject = true;
    const ports = { list: async () => [], readPending: () => raw, writePending: (value: string | null) => { raw = value; },
      save: async (request: ProviderSaveRequest) => { requests.push(request); if (reject) throw new WorkbenchApiError("unknown", "unknown"); } };
    const first = createProviderResources(ports), record = newProvider();
    expect(await first.save(record, 0)).toBe(false);
    record.value.name = "edited after submission";
    expect(first.pending.value?.record.value.name).toBe("DeepSeek");
    expect(first.locked.value).toBe(true);
    expect(await first.save(record, 0)).toBe(false);
    const reopened = createProviderResources(ports); reject = false;
    expect(await reopened.reconcile()).toBe(true);
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]); expect(raw).toBeNull();
  });
  it("uses the observed CAS sequence and leaves conflicts available for explicit refresh/edit", async () => {
    const save = vi.fn(async (_request: ProviderSaveRequest) => { throw new WorkbenchApiError("rejected", "changed", 409, "stale_revision"); });
    const resources = createProviderResources({ save, list: async () => [], readPending: () => null, writePending: () => {} });
    const record = newProvider(); record.update_sequence = 7;
    expect(await resources.save(record, 7)).toBe(false);
    expect(save.mock.calls[0]![0].record.update_sequence).toBe(8);
    expect(save.mock.calls[0]![0].expected_sequence).toBe(7);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false);
  });
  it.each(["invalid_origin", "idempotency_conflict"])("keeps an unknown original request when later reconciliation is rejected with %s", async code => {
    let raw: string | null = null;
    let calls = 0;
    const requests: ProviderSaveRequest[] = [];
    const ports = { list: async () => [], readPending: () => raw,
      writePending: (value: string | null) => { raw = value; },
      save: async (request: ProviderSaveRequest) => {
        requests.push(request);
        if (++calls === 1) throw new WorkbenchApiError("unknown", "unknown");
        throw new WorkbenchApiError("rejected", "policy rejected", 403, code);
      } };
    const original = createProviderResources(ports);
    expect(await original.save(newProvider(), 0)).toBe(false);
    const body = raw;
    const reopened = createProviderResources(ports);
    expect(await reopened.reconcile()).toBe(false);
    expect(raw).toBe(body); expect(reopened.locked.value).toBe(true);
    expect(await reopened.save(newProvider(), 0)).toBe(false);
    expect(requests).toHaveLength(2); expect(requests[1]).toEqual(requests[0]);
  });
  it("does not dispatch when request persistence fails or the record carries a secret field", async () => {
    const save = vi.fn();
    const resources = createProviderResources({ save, list: async () => [], readPending: () => null,
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
    const resources = createProviderResources({ save: async () => ({}), list: () => ++count === 1 ? older : Promise.resolve([record]),
      readPending: () => null, writePending: () => {} });
    const first = resources.refresh(); await resources.refresh(); complete([]); await first;
    expect(resources.records.value).toEqual([record]);
  });
});
