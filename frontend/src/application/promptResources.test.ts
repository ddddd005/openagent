import { afterEach, describe, expect, it, vi } from "vitest";
import { createSSRApp, defineComponent } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPromptResources, promptResourceControllerKey, usePromptResources, type PromptResourcesPorts } from "./promptResources";
import { clonePromptResource, newPromptMember, newPromptResource, promptIdentity } from "../domain/workflowPromptResources";
import { newProvider } from "../domain/workflowModelResources";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { PromptSaveRequest } from "../adapters/promptResourcesApi";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());
const cloneRequest = (value: PromptSaveRequest): PromptSaveRequest => JSON.parse(JSON.stringify(value));
function fixture(overrides: Partial<PromptResourcesPorts> = {}) {
  let raw: string | null = null;
  const resources = createPromptResources({
    list: async () => [], save: async () => ({}), readReceipt: vi.fn(), readPending: () => raw,
    writePending: value => { raw = value; }, ...overrides,
  });
  return { resources, raw: () => raw };
}
describe("current independent prompt resource coordination", () => {
  it("persists before dispatch, saves an empty group and keeps other scopes and metadata intact", async () => {
    const order: string[] = [], requests: PromptSaveRequest[] = [];
    let raw: string | null = null;
    const record = newPromptResource(); record.scope = "project"; record.update_sequence = 4;
    const resources = createPromptResources({ list: async () => [record], readPending: () => raw,
      writePending: value => { order.push(value === null ? "clear" : "persist"); raw = value; },
      readReceipt: vi.fn(), save: async request => { order.push("dispatch"); requests.push(request); return {}; } });
    expect(await resources.save(record, 4)).toBe(true);
    expect(order).toEqual(["persist", "dispatch", "clear"]);
    expect(requests[0]).toMatchObject({ expected_sequence: 4, record: { scope: "project", update_sequence: 5,
      value: { enabled: true, members: [] } } });
    expect(record.update_sequence).toBe(4);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false);
    expect(resources.records.value).toEqual([record]); expect(raw).toBeNull();
    record.value.members = [newPromptMember("body")];
    record.value.members[0]!.metadata = { nested: { flags: [true, null], name: "opaque metadata" } };
    expect(await resources.save(record, 4)).toBe(true);
    expect(requests[1]!.record.value.members).toEqual(record.value.members);
  });
  it("reopens an unknown request and reads identical key and body despite caller or transport mutation", async () => {
    let raw: string | null = null;
    const requests: PromptSaveRequest[] = [];
    const reads: PromptSaveRequest[] = [];
    const record = newPromptResource(); record.scope = "project"; record.value.members = [newPromptMember("original")];
    record.value.members[0]!.metadata = { nested: { name: "original" } };
    const ports: PromptResourcesPorts = { list: async () => [], readPending: () => raw,
      writePending: value => { raw = value; },
      save: async request => {
        requests.push(cloneRequest(request));
        request.record.value.members[0]!.text = "mutated transport copy";
        throw new WorkbenchApiError("unknown", "unknown");
      },
      readReceipt: async request => {
        reads.push(cloneRequest(request)); request.record.value.members[0]!.text = "mutated read copy"; return {};
      } };
    const resources = createPromptResources(ports);
    expect(await resources.save(record, 0)).toBe(false);
    const body = raw;
    record.value.members[0]!.text = "edited caller";
    (record.value.members[0]!.metadata.nested as { name: string }).name = "edited caller";
    expect(resources.pending.value?.record.value.members[0]!.text).toBe("original");
    expect(resources.pending.value?.record.value.members[0]!.metadata).toEqual({ nested: { name: "original" } });
    expect(await resources.save(newPromptResource(), 0)).toBe(false);
    expect(raw).toBe(body);
    const reopened = createPromptResources(ports);
    expect(await reopened.reconcile()).toBe(true);
    expect(requests).toHaveLength(1); expect(reads).toEqual(requests);
    expect(reads[0]!.record.scope).toBe("project");
    expect(raw).toBeNull(); expect(reopened.locked.value).toBe(false);
  });
  it("clears a first definitive CAS rejection so a refreshed revision can be manually edited", async () => {
    let reject = true, raw: string | null = null;
    const save = vi.fn(async (_request: PromptSaveRequest) => {
      if (reject) throw new WorkbenchApiError("rejected", "changed", 409, "stale_revision");
      return {};
    });
    const record = newPromptResource(); record.update_sequence = 7; record.value.members = [newPromptMember("form")];
    const current = clonePromptResource(record); current.update_sequence = 8;
    const resources = createPromptResources({ save, readReceipt: vi.fn(), list: async () => [current], readPending: () => raw,
      writePending: value => { raw = value; } });
    expect(await resources.save(record, 7)).toBe(false);
    expect(save.mock.calls[0]![0]).toMatchObject({ expected_sequence: 7, record: { update_sequence: 8 } });
    expect(record.value.members[0]!.text).toBe("form"); expect(record.update_sequence).toBe(7);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false); expect(raw).toBeNull();
    await resources.refresh(); reject = false;
    const edited = clonePromptResource(resources.records.value[0]!); edited.value.members[0]!.text = "manual edit";
    expect(await resources.save(edited, 8)).toBe(true);
    expect(save.mock.calls[1]![0]).toMatchObject({ expected_sequence: 8, record: { update_sequence: 9 } });
  });
  it.each(["stale_revision", "invalid_origin", "idempotency_conflict", "not_found", "gone"])(
    "does not clear an unknown original after later %s rejection", async code => {
      let raw: string | null = null;
      const requests: PromptSaveRequest[] = [];
      const reads: PromptSaveRequest[] = [];
      const ports: PromptResourcesPorts = { list: async () => [], readPending: () => raw,
        writePending: value => { raw = value; },
        save: async request => {
          requests.push(cloneRequest(request));
          throw new WorkbenchApiError("unknown", "unknown");
        },
        readReceipt: async request => {
          reads.push(cloneRequest(request));
          throw new WorkbenchApiError("rejected", "rejected", 409, code);
        } };
      const resources = createPromptResources(ports);
      expect(await resources.save(newPromptResource(), 0)).toBe(false);
      const body = raw;
      expect(await resources.reconcile()).toBe(false);
      const reopened = createPromptResources(ports);
      expect(await reopened.reconcile()).toBe(false);
      expect(raw).toBe(body); expect(reopened.locked.value).toBe(true);
      expect(await reopened.save(newPromptResource(), 0)).toBe(false);
      expect(requests).toHaveLength(1);
      expect(reads).toEqual([requests[0], requests[0]]);
    });
  it.each(["unknown", "unavailable", "idempotency_conflict"])("preserves an unresolved first failure: %s", async kind => {
    const failure = kind === "idempotency_conflict"
      ? new WorkbenchApiError("rejected", "conflict", 409, kind) : new WorkbenchApiError(kind as "unknown" | "unavailable", kind);
    const { resources, raw } = fixture({ save: async () => { throw failure; } });
    expect(await resources.save(newPromptResource(), 0)).toBe(false);
    expect(resources.pending.value).not.toBeNull(); expect(resources.locked.value).toBe(true); expect(raw()).not.toBeNull();
  });
  it("does not dispatch on persistence failure or when the input would lose JSON fields", async () => {
    const save = vi.fn();
    const { resources } = fixture({ save, writePending: () => { throw new Error("disk"); } });
    expect(await resources.save(newPromptResource(), 0)).toBe(false);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false);
    const invalid = newPromptResource(); invalid.value.members = [newPromptMember("body")];
    invalid.value.members[0]!.metadata = { dropped: undefined };
    expect(await resources.save(invalid, 0)).toBe(false);
    expect(save).not.toHaveBeenCalled();
  });
  it.each([false, true])("retains a request when clearing local storage fails (server rejected: %s)", async rejected => {
    let raw: string | null = null, allowClear = false;
    const requests: PromptSaveRequest[] = [];
    const reads: PromptSaveRequest[] = [];
    const resources = createPromptResources({ list: async () => [], readPending: () => raw,
      writePending: value => { if (value === null && !allowClear) throw new Error("disk"); raw = value; },
      save: async request => {
        requests.push(cloneRequest(request));
        if (rejected && requests.length === 1) throw new WorkbenchApiError("rejected", "changed", 409, "stale_revision");
        return {};
      }, readReceipt: async request => { reads.push(cloneRequest(request)); return {}; } });
    expect(await resources.save(newPromptResource(), 0)).toBe(false);
    expect(raw).not.toBeNull(); expect(resources.pending.value).not.toBeNull(); expect(resources.locked.value).toBe(true);
    allowClear = true;
    expect(await resources.reconcile()).toBe(true);
    expect(requests).toHaveLength(1); expect(reads).toEqual(requests); expect(raw).toBeNull();
  });
  it("fails closed on corrupt pending storage without overwriting or dispatching it", async () => {
    const request = { record: newPromptResource(), expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const corrupt = ["", "{", JSON.stringify({ ...request, extra: true }),
      JSON.stringify({ ...request, expected_sequence: 1 }), JSON.stringify({ ...request, record: newProvider() }),
      JSON.stringify({ ...request, idempotency_key: request.idempotency_key.toUpperCase() })];
    for (const raw of corrupt) {
      const save = vi.fn(), writePending = vi.fn();
      const resources = createPromptResources({ list: async () => [], save, readReceipt: vi.fn(),
        readPending: () => raw, writePending });
      expect(resources.locked.value).toBe(true);
      expect(await resources.save(newPromptResource(), 0)).toBe(false);
      expect(await resources.reconcile()).toBe(false);
      expect(save).not.toHaveBeenCalled(); expect(writePending).not.toHaveBeenCalled();
    }
    const { resources } = fixture({ readPending: () => { throw new Error("storage denied"); } });
    expect(resources.locked.value).toBe(true);
  });
  it("keeps same-UUID scope coordinates separate when editing a listed record", async () => {
    const workspace = newPromptResource(), project = { ...newPromptResource(), scope: "project", resource_id: workspace.resource_id };
    project.update_sequence = 3;
    const save = vi.fn(async (_request: PromptSaveRequest) => ({}));
    const { resources } = fixture({ list: async () => [workspace, project], save });
    await resources.refresh();
    expect(resources.records.value).toHaveLength(2);
    expect(await resources.save(resources.records.value[1]!, 3)).toBe(true);
    expect(promptIdentity(save.mock.calls[0]![0].record)).toEqual(promptIdentity(project));
    expect(save.mock.calls[0]![0].expected_sequence).toBe(3);
    const wrong = { ...newProvider(), resource_id: workspace.resource_id };
    expect(await resources.save(wrong as unknown as ReturnType<typeof newPromptResource>, 0)).toBe(false);
    expect(save).toHaveBeenCalledTimes(1);
  });
  it("ignores stale refreshes and preserves the last records when a later read fails", async () => {
    let complete!: (value: ReturnType<typeof newPromptResource>[]) => void, count = 0;
    const older = new Promise<ReturnType<typeof newPromptResource>[]>(resolve => { complete = resolve; });
    const record = newPromptResource();
    const { resources } = fixture({ list: () => {
      if (++count === 1) return older;
      if (count === 2) return Promise.resolve([record]);
      return Promise.reject(new Error("read failed"));
    } });
    const first = resources.refresh(); expect(resources.loading.value).toBe(true);
    await resources.refresh(); complete([]); await first;
    expect(resources.records.value).toEqual([record]); expect(resources.loading.value).toBe(false);
    await resources.refresh(); expect(resources.records.value).toEqual([record]);
    expect(resources.error.value).toBe("read failed"); expect(resources.loading.value).toBe(false);
  });
  it("blocks overlapping saves and reconciliation while the first dispatch is active", async () => {
    let finish!: () => void;
    const save = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const { resources } = fixture({ save });
    const first = resources.save(newPromptResource(), 0);
    expect(resources.busy.value).toBe(true); expect(resources.locked.value).toBe(true);
    expect(await resources.save(newPromptResource(), 0)).toBe(false);
    expect(await resources.reconcile()).toBe(false); expect(save).toHaveBeenCalledTimes(1);
    finish(); expect(await first).toBe(true); expect(resources.busy.value).toBe(false);
  });
  it("uses a provided prompt controller without substituting any other resource controller", async () => {
    const { resources } = fixture();
    let seen: unknown;
    const component = defineComponent({ setup() { seen = usePromptResources(); return () => null; } });
    await renderToString(createSSRApp(component).provide(promptResourceControllerKey, resources));
    expect(seen).toBe(resources);
  });
  it.each(["success", "failure", "durable replacement"])("preserves a newer prompt pending across a late receipt %s", async outcome => {
    let raw: string | null = null, finish!: (value: unknown) => void, fail!: (failure: unknown) => void;
    const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "lost"); });
    const ports: PromptResourcesPorts = { save, list: async () => [], readPending: () => raw,
      writePending: vi.fn(value => { raw = value; }),
      readReceipt: () => new Promise((resolve, reject) => { finish = resolve; fail = reject; }) };
    const resources = createPromptResources(ports);
    await resources.save(newPromptResource(), 0);
    const original = JSON.parse(raw!) as PromptSaveRequest;
    const reading = resources.reconcile();
    const newer = JSON.parse(raw!) as PromptSaveRequest;
    newer.record.value.members = [newPromptMember("newer body with original key")];
    if (outcome === "durable replacement") raw = JSON.stringify(newer);
    else resources.pending.value = newer;
    resources.error.value = "newer diagnostic";
    const durable = raw, writes = vi.mocked(ports.writePending).mock.calls.length;
    if (outcome === "failure") fail(new WorkbenchApiError("rejected", "old read denial", 410));
    else finish({});
    expect(await reading).toBe(false); expect(raw).toBe(durable);
    expect(resources.pending.value).toEqual(outcome === "durable replacement" ? original : newer);
    expect(resources.error.value).toBe("newer diagnostic"); expect(ports.writePending).toHaveBeenCalledTimes(writes);
    expect(save).toHaveBeenCalledOnce();
  });
  it("uses its independent local-storage key and reopens the original default-adapter request", async () => {
    vi.resetModules();
    const storage = new Map<string, string>([
      ["workflow.models.resource.pending.v1", "provider outbox untouched"],
      ["workflow.graph.outbox.v1", "legacy outbox untouched"],
    ]);
    const getItem = vi.fn((key: string) => storage.get(key) ?? null);
    const setItem = vi.fn((key: string, value: string) => { storage.set(key, value); });
    const removeItem = vi.fn((key: string) => { storage.delete(key); });
    vi.stubGlobal("localStorage", { getItem, setItem, removeItem });
    const requests: PromptSaveRequest[] = [];
    let unknown = true;
    vi.stubGlobal("fetch", vi.fn(async (path: string, options: RequestInit) => {
      const request = JSON.parse(String(options.body));
      if (request.operation === "resource.list") return new Response("[]");
      const parameters: PromptSaveRequest = request.parameters; requests.push(parameters);
      if (unknown) throw new Error("network");
      const result = { reference: promptIdentity(parameters.record), update_sequence: parameters.record.update_sequence, deleted: false };
      expect(path).toBe("/api/graph/receipts/read");
      return graphReceiptReadResponse(request.operation, request.parameters, result);
    }));
    const first = (await import("./promptResources")).usePromptResources();
    expect(await first.save(newPromptResource(), 0)).toBe(false);
    const body = storage.get("workflow.prompts.resource.pending.v1"); expect(body).toBeDefined();
    vi.resetModules(); unknown = false;
    const reopened = (await import("./promptResources")).usePromptResources();
    expect(await reopened.reconcile()).toBe(true);
    expect(requests[1]).toEqual(requests[0]);
    expect(getItem.mock.calls.every(([key]) => key === "workflow.prompts.resource.pending.v1")).toBe(true);
    expect(setItem.mock.calls.every(([key]) => key === "workflow.prompts.resource.pending.v1")).toBe(true);
    expect(removeItem).toHaveBeenCalledWith("workflow.prompts.resource.pending.v1");
    expect(storage.get("workflow.models.resource.pending.v1")).toBe("provider outbox untouched");
    expect(storage.get("workflow.graph.outbox.v1")).toBe("legacy outbox untouched");
  });
});
