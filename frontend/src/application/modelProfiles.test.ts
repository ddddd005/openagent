import { afterEach, describe, expect, it, vi } from "vitest";
import { createSSRApp, defineComponent } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createModelProfiles, modelProfilesKey, useModelProfiles } from "./modelProfiles";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { readModelProfileReceipt, saveModelProfile, type ModelProfileSaveRequest } from "../adapters/workflowModelProfilesApi";
import { newModelProfile, newProvider, modelProfileIdentity, type CurrentModelProfile } from "../domain/workflowModelResources";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());
type Ports = Parameters<typeof createModelProfiles>[0];
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
function profile(): CurrentModelProfile {
  const value = newModelProfile(newProvider());
  value.value.name = "Shared configuration";
  value.value.parameters.model = "offline-test-chat";
  value.value.capacity.context_window_tokens = 4096;
  value.value.capacity.max_cold_input_tokens = 2048;
  return value;
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (failure: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
function fixture(overrides: Partial<Ports> = {}) {
  let raw: string | null = null;
  const ports: Ports = { list: vi.fn(async () => []), save: vi.fn(async () => ({})),
    readReceipt: vi.fn(async () => ({})), readPending: () => raw,
    writePending: vi.fn(value => { raw = value; }), ...overrides };
  return { ports, resources: createModelProfiles(ports), raw: () => raw };
}
function sorted(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sorted);
  if (value !== null && typeof value === "object")
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, sorted(item)]));
  return value;
}

describe("global named model configuration coordination", () => {
  it("persists the complete cloned CAS intent before dispatch and clears before refresh", async () => {
    let raw: string | null = null;
    const order: string[] = [], original = profile(), requests: ModelProfileSaveRequest[] = [];
    original.scope = "project"; original.update_sequence = 4;
    original.value.parameters.temperature = 0.25;
    const initial = clone(original);
    const resources = createModelProfiles({
      readPending: () => raw, writePending: value => { order.push(value === null ? "clear" : "persist"); raw = value; },
      save: async request => { order.push("dispatch"); requests.push(clone(request)); return {}; },
      readReceipt: vi.fn(), list: async () => { order.push("refresh"); return [initial]; },
    });
    expect(await resources.save(original, 4)).toBe(true);
    expect(order).toEqual(["persist", "dispatch", "clear", "refresh"]);
    expect(requests[0]).toMatchObject({ expected_sequence: 4, record: { scope: "project", update_sequence: 5,
      value: original.value } });
    expect(requests[0]?.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
    expect(original).toEqual(initial);
    expect(resources.completed.value).toEqual(requests[0]);
    expect(resources.records.value).toEqual([initial]);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false); expect(raw).toBeNull();
  });

  it("freezes the full unknown original through caller and port mutations and reopens without replay", async () => {
    let raw: string | null = null;
    const sent: ModelProfileSaveRequest[] = [], read: ModelProfileSaveRequest[] = [];
    const ports: Ports = {
      list: async () => [], readPending: () => raw, writePending: value => { raw = value; },
      save: async request => {
        sent.push(clone(request));
        request.record.value.name = "Changed port copy";
        request.record.value.capacity.context_window_tokens = 99999;
        throw new WorkbenchApiError("unknown", "No acceptance received");
      },
      readReceipt: async request => {
        read.push(clone(request));
        request.record.value.provider.resource_id = crypto.randomUUID();
        return {};
      },
    };
    const original = profile(), initial = clone(original), resources = createModelProfiles(ports);
    expect(await resources.save(original, 0)).toBe(false);
    const durable = raw;
    original.value.name = "Changed caller";
    original.value.parameters.temperature = 2;
    original.value.capacity.context_window_tokens = 50000;
    expect(resources.pending.value?.record.value).toEqual(initial.value);
    expect(await resources.save(profile(), 0)).toBe(false); expect(raw).toBe(durable);
    const reopened = createModelProfiles(ports);
    expect(await reopened.reconcile()).toBe(true);
    expect(sent).toHaveLength(1); expect(read).toEqual(sent);
    expect(raw).toBeNull(); expect(reopened.locked.value).toBe(false);
    expect(reopened.completed.value).toEqual(sent[0]);
  });

  it.each(["saved", "rejected", "unknown"] as const)(
    "recognizes backend-normalized nested key order for a %s result", async outcome => {
      let raw: string | null = null;
      const save = vi.fn(async () => {
        if (outcome !== "saved") throw new WorkbenchApiError(outcome === "rejected" ? "rejected" : "unknown", outcome, 409);
        return {};
      });
      const resources = createModelProfiles({ list: async () => [], save, readReceipt: vi.fn(),
        readPending: () => raw, writePending: value => { raw = value === null ? null : JSON.stringify(sorted(JSON.parse(value))); } });
      expect(await resources.save(profile(), 0)).toBe(outcome === "saved");
      expect(resources.pending.value === null).toBe(outcome !== "unknown");
      expect(resources.locked.value).toBe(outcome === "unknown");
      expect(save).toHaveBeenCalledOnce();
      if (outcome === "unknown") {
        expect(JSON.parse(raw!)).toEqual(resources.pending.value);
        expect(resources.error.value).toBe("unknown");
      } else expect(raw).toBeNull();
    });

  it("compares normalized key order when reconciling an original without dispatching it again", async () => {
    const original: ModelProfileSaveRequest = { record: profile(), expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    let raw: string | null = JSON.stringify(sorted(original));
    const save = vi.fn(), readReceipt = vi.fn(async () => ({}));
    const resources = createModelProfiles({ list: async () => [], save, readReceipt,
      readPending: () => raw, writePending: value => { raw = value; } });
    expect(resources.pending.value).toEqual(original);
    expect(await resources.reconcile()).toBe(true);
    expect(readReceipt).toHaveBeenCalledWith(original); expect(save).not.toHaveBeenCalled();
    expect(raw).toBeNull(); expect(resources.completed.value).toEqual(original);
  });

  it("clears only a first definitive rejection and creates a new identity after explicit revision refresh", async () => {
    let reject = true;
    const original = profile(); original.update_sequence = 3;
    const current = clone(original); current.update_sequence = 4;
    const save = vi.fn(async (_request: ModelProfileSaveRequest) => {
      if (reject) throw new WorkbenchApiError("rejected", "Provider configuration changed", 409, "stale_revision");
      return {};
    });
    const { resources, raw } = fixture({ save, list: async () => [current] });
    expect(await resources.save(original, 3)).toBe(false);
    expect(resources.pending.value).toBeNull(); expect(resources.locked.value).toBe(false); expect(raw()).toBeNull();
    await resources.refresh(); reject = false;
    const edited = clone(resources.records.value[0]!); edited.value.parameters.temperature = 1;
    expect(await resources.save(edited, 4)).toBe(true);
    expect(save.mock.calls).toHaveLength(2);
    const first = save.mock.calls[0]![0] as unknown as ModelProfileSaveRequest;
    const second = save.mock.calls[1]![0] as unknown as ModelProfileSaveRequest;
    expect(first).toMatchObject({ expected_sequence: 3, record: { update_sequence: 4 } });
    expect(second).toMatchObject({ expected_sequence: 4, record: { update_sequence: 5 } });
    expect(first.idempotency_key).not.toBe(second.idempotency_key);
    expect(original.value.parameters.temperature).toBeUndefined();
  });

  it.each(["unknown", "unavailable", "idempotency_conflict"])("keeps an unresolved first result: %s", async kind => {
    const failure = kind === "idempotency_conflict"
      ? new WorkbenchApiError("rejected", "Identity conflict", 409, kind)
      : new WorkbenchApiError(kind as "unknown" | "unavailable", kind);
    const save = vi.fn(async () => { throw failure; });
    const { resources, raw } = fixture({ save });
    expect(await resources.save(profile(), 0)).toBe(false);
    expect(resources.pending.value).not.toBeNull(); expect(resources.locked.value).toBe(true);
    expect(raw()).not.toBeNull();
    expect(await resources.save(profile(), 0)).toBe(false); expect(save).toHaveBeenCalledOnce();
  });

  it.each(["stale_revision", "invalid_origin", "idempotency_conflict", "application_identity_missing", "not_found"])(
    "does not release an unknown original after later %s receipt failure", async code => {
      let raw: string | null = null;
      const save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "lost"); });
      const readReceipt = vi.fn(async (_request: ModelProfileSaveRequest) => { throw new WorkbenchApiError("rejected", code, 409, code); });
      const ports: Ports = { save, readReceipt, list: async () => [],
        readPending: () => raw, writePending: value => { raw = value; } };
      const resources = createModelProfiles(ports);
      await resources.save(profile(), 0);
      const original = raw;
      expect(await resources.reconcile()).toBe(false);
      const reopened = createModelProfiles(ports);
      expect(await reopened.reconcile()).toBe(false);
      expect(raw).toBe(original); expect(reopened.locked.value).toBe(true);
      expect(await reopened.save(profile(), 0)).toBe(false);
      expect(save).toHaveBeenCalledOnce(); expect(readReceipt).toHaveBeenCalledTimes(2);
      expect(readReceipt.mock.calls.map(call => call[0])).toEqual([JSON.parse(original!), JSON.parse(original!)]);
    });

  it("does not dispatch when the configuration or durable intent is unavailable", async () => {
    const save = vi.fn();
    const invalid = profile(); invalid.value.capacity.context_window_tokens = 0;
    const { resources } = fixture({ save, writePending: () => { throw new Error("Local intent unavailable"); } });
    expect(await resources.save(invalid, 0)).toBe(false);
    expect(await resources.save(profile(), 0)).toBe(false);
    expect(save).not.toHaveBeenCalled(); expect(resources.pending.value).toBeNull();
    expect(resources.locked.value).toBe(false);
  });

  it("retains an accepted request after a failed local clear and later reads only its original receipt", async () => {
    let raw: string | null = null, failClear = true;
    const save = vi.fn(async () => ({})), readReceipt = vi.fn(async () => ({}));
    const resources = createModelProfiles({ save, readReceipt, list: async () => [],
      readPending: () => raw, writePending: value => {
        if (value === null && failClear) throw new Error("Local clear unavailable");
        raw = value;
      } });
    expect(await resources.save(profile(), 0)).toBe(false);
    const original = raw;
    expect(resources.pending.value).toEqual(JSON.parse(original!)); expect(resources.locked.value).toBe(true);
    failClear = false;
    expect(await resources.reconcile()).toBe(true);
    expect(readReceipt).toHaveBeenCalledWith(JSON.parse(original!)); expect(save).toHaveBeenCalledOnce();
    expect(raw).toBeNull();
  });

  it.each(["missing-acceptance", "wrong-key", "wrong-sequence"])(
    "keeps a malformed API receipt pending until exact read-only reconciliation: %s", async kind => {
      const requests: ModelProfileSaveRequest[] = [];
      const fetcher = vi.fn(async (path: string, options: RequestInit) => {
        const body = JSON.parse(String(options.body));
        const original = body.parameters as ModelProfileSaveRequest;
        requests.push(clone(original));
        const result = { reference: modelProfileIdentity(original.record),
          update_sequence: original.record.update_sequence, deleted: false };
        if (path === "/api/graph/receipts/read") return graphReceiptReadResponse("resource.save", body.parameters, result);
        const receipt: Record<string, unknown> = { schema_version: 1, kind: "workflow.application-receipt",
          operation: "resource.save", operation_scope: "management", idempotency_key: original.idempotency_key,
          request_sha256: "a".repeat(64), authority: "service_receipt", target: {}, accepted: result };
        if (kind === "missing-acceptance") delete receipt.accepted;
        if (kind === "wrong-key") receipt.idempotency_key = crypto.randomUUID();
        if (kind === "wrong-sequence") receipt.accepted = { ...result, update_sequence: result.update_sequence + 1 };
        return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", receipt, result }));
      });
      vi.stubGlobal("fetch", fetcher);
      const { resources, raw } = fixture({ save: saveModelProfile, readReceipt: readModelProfileReceipt });
      expect(await resources.save(profile(), 0)).toBe(false);
      const original = raw();
      expect(original).not.toBeNull(); expect(resources.locked.value).toBe(true);
      expect(await resources.reconcile()).toBe(true);
      expect(requests).toEqual([JSON.parse(original!), JSON.parse(original!)]);
      expect(fetcher.mock.calls.map(([path]) => path)).toEqual(["/api/graph/commands", "/api/graph/receipts/read"]);
      expect(raw()).toBeNull(); expect(resources.pending.value).toBeNull();
    });

  it.each(["invalid-json", "provider-record", "wrong-sequence", "extra-field"])(
    "fails closed without overwriting malformed persisted profile intents: %s", async kind => {
      const original: ModelProfileSaveRequest = { record: profile(), expected_sequence: 0, idempotency_key: crypto.randomUUID() };
      const values: Record<string, string> = {
        "invalid-json": "{", "provider-record": JSON.stringify({ ...original, record: newProvider() }),
        "wrong-sequence": JSON.stringify({ ...original, expected_sequence: 1 }),
        "extra-field": JSON.stringify({ ...original, extra: true }),
      };
      const save = vi.fn(), readReceipt = vi.fn(), writePending = vi.fn();
      const { resources } = fixture({ readPending: () => values[kind]!, save, readReceipt, writePending });
      expect(resources.locked.value).toBe(true);
      expect(await resources.save(profile(), 0)).toBe(false); expect(await resources.reconcile()).toBe(false);
      expect(save).not.toHaveBeenCalled(); expect(readReceipt).not.toHaveBeenCalled(); expect(writePending).not.toHaveBeenCalled();
    });

  it("preserves scope identity when two named configurations share a UUID", async () => {
    const workspace = profile(), project = { ...profile(), scope: "project", resource_id: workspace.resource_id };
    project.update_sequence = 5;
    const save = vi.fn(async (_request: ModelProfileSaveRequest) => ({}));
    const { resources } = fixture({ list: async () => [workspace, project], save });
    await resources.refresh(); expect(resources.records.value).toHaveLength(2);
    expect(await resources.save(resources.records.value[1]!, 5)).toBe(true);
    expect(modelProfileIdentity(save.mock.calls[0]![0].record)).toEqual(modelProfileIdentity(project));
    expect(save.mock.calls[0]![0].expected_sequence).toBe(5);
  });

  it("keeps the newest list response and the previous records on a later read failure", async () => {
    const older = deferred<CurrentModelProfile[]>(), current = profile();
    let calls = 0;
    const { resources } = fixture({ list: () => {
      if (++calls === 1) return older.promise;
      if (calls === 2) return Promise.resolve([current]);
      return Promise.reject(new Error("Configuration read failed"));
    } });
    const first = resources.refresh();
    expect(resources.loading.value).toBe(true);
    await resources.refresh(); older.resolve([]); await first;
    expect(resources.records.value).toEqual([current]); expect(resources.loading.value).toBe(false);
    await resources.refresh();
    expect(resources.records.value).toEqual([current]); expect(resources.error.value).toBe("Configuration read failed");
    expect(resources.loading.value).toBe(false);
  });

  it("clears the previous read error after a successful current refresh", async () => {
    const current = profile(), list = vi.fn()
      .mockRejectedValueOnce(new Error("Configuration read failed"))
      .mockResolvedValueOnce([current]);
    const { resources } = fixture({ list });
    await resources.refresh();
    expect(resources.error.value).toBe("Configuration read failed");
    await resources.refresh();
    expect(resources.records.value).toEqual([current]);
    expect(resources.error.value).toBe("");
  });

  it.each(["rejected", "unknown"] as const)("preserves the %s save error across failed and successful reads", async kind => {
    const current = profile();
    let failRead = false;
    const save = vi.fn(async () => { throw new WorkbenchApiError(kind, "Original save diagnostic", 409); });
    const { resources, raw } = fixture({ save, list: async () => {
      if (failRead) throw new Error("Configuration read failed");
      return [current];
    } });
    expect(await resources.save(current, 0)).toBe(false);
    const original = raw();
    failRead = true; await resources.refresh();
    expect(resources.error.value).toBe("Original save diagnostic");
    failRead = false; await resources.refresh();
    expect(resources.error.value).toBe("Original save diagnostic");
    expect(resources.records.value).toEqual([current]);
    expect(raw()).toBe(original);
    expect(resources.locked.value).toBe(kind === "unknown");
    expect(save).toHaveBeenCalledOnce();
  });

  it("ignores an older read failure after a newer successful refresh", async () => {
    const older = deferred<CurrentModelProfile[]>(), current = profile();
    const list = vi.fn().mockImplementationOnce(() => older.promise).mockResolvedValueOnce([current]);
    const { resources } = fixture({ list });
    const first = resources.refresh();
    await resources.refresh(); older.reject(new Error("Old read failed")); await first;
    expect(resources.records.value).toEqual([current]);
    expect(resources.error.value).toBe(""); expect(resources.loading.value).toBe(false);
  });

  it("blocks overlapping saves and receipt reads while the original dispatch is active", async () => {
    const gate = deferred<unknown>(), save = vi.fn(() => gate.promise);
    const { resources } = fixture({ save });
    const sending = resources.save(profile(), 0);
    expect(resources.busy.value).toBe(true); expect(resources.locked.value).toBe(true);
    expect(await resources.save(profile(), 0)).toBe(false); expect(await resources.reconcile()).toBe(false);
    gate.resolve({});
    expect(await sending).toBe(true); expect(save).toHaveBeenCalledOnce(); expect(resources.busy.value).toBe(false);
  });

  it.each(["success", "failure", "durable-change"])("does not clear a newer intent after a late receipt %s", async kind => {
    let raw: string | null = null;
    const gate = deferred<unknown>(), save = vi.fn(async () => { throw new WorkbenchApiError("unknown", "lost"); });
    const writePending = vi.fn(value => { raw = value; });
    const resources = createModelProfiles({ save, list: async () => [], readPending: () => raw,
      writePending, readReceipt: () => gate.promise });
    await resources.save(profile(), 0);
    const original = clone(resources.pending.value!), reading = resources.reconcile(), newer = clone(original);
    newer.record.value.name = "Newer body, original UUID";
    if (kind === "durable-change") raw = JSON.stringify(newer);
    else resources.pending.value = newer;
    resources.error.value = "Newer diagnostic";
    const durable = raw, writes = writePending.mock.calls.length;
    if (kind === "failure") gate.reject(new WorkbenchApiError("rejected", "Old receipt rejected", 409));
    else gate.resolve({});
    expect(await reading).toBe(false); expect(raw).toBe(durable);
    expect(resources.pending.value).toEqual(kind === "durable-change" ? original : newer);
    expect(resources.error.value).toBe("Newer diagnostic"); expect(writePending).toHaveBeenCalledTimes(writes);
    expect(save).toHaveBeenCalledOnce();
  });

  it("uses the explicitly provided global profile controller without another resource controller", async () => {
    const { resources } = fixture();
    let observed: unknown;
    const component = defineComponent({ setup() { observed = useModelProfiles(); return () => null; } });
    await renderToString(createSSRApp(component).provide(modelProfilesKey, resources));
    expect(observed).toBe(resources);
  });
});
