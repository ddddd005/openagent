import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useGlobalContentStore } from "./globalContent";
import { useWorkspaceStore } from "./workspace";
import { CORE_SESSION_NOTE, type GlobalContent } from "../domain/workbenchResources";

const ID = "00000000-0000-4000-8000-000000000701";
const KEY = "workflow-workbench:content-drafts:v1";
const content = (revision = 1): GlobalContent => ({
  schema_version: 1, kind: "global_prompt", resource_id: ID, revision, name: "Global",
  enabled: true, members: [{ id: "00000000-0000-4000-8000-000000000702", name: "Member",
    text: `v${revision}`, role: "system", placement: "before", depth: null, order: 0, enabled: true }],
});
function memory() {
  const entries = new Map<string, string>();
  return { getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => { entries.set(key, value); } };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => vi.unstubAllGlobals());
describe("global content ownership and durable drafts", () => {
  it("recovers unsaved content without marking the workflow edited", async () => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path.endsWith("/definitions") ? [CORE_SESSION_NOTE] : [content()]))));
    const store = useGlobalContentStore();
    await store.initialize();
    store.selected!.members[0]!.text = "unsaved";
    expect(useWorkspaceStore().activeWorkflow.state).toBe("saved");
    store.$dispose();
    setActivePinia(createPinia());
    const recovered = useGlobalContentStore();
    await recovered.initialize();
    expect(recovered.selected?.members[0]?.text).toBe("unsaved");
    expect(recovered.dirty).toBe(true);
  });
  it("keeps the exact uncertain write across reload without redispatch", async () => {
    const storage = memory();
    let uncertain = true;
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (!init?.body) return new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()]));
      if (uncertain) throw new Error("lost response");
      return new Response(JSON.stringify(JSON.parse(String(init.body)).record));
    });
    vi.stubGlobal("window", { localStorage: storage });
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    store.selected!.members[0]!.text = "pending";
    await store.save();
    const original = JSON.stringify(store.pending);
    expect(store.pending).not.toBeNull();
    store.$dispose();
    setActivePinia(createPinia());
    const recovered = useGlobalContentStore();
    await recovered.initialize();
    expect(JSON.stringify(recovered.pending)).toBe(original);
    uncertain = false;
    const raw = storage.getItem(KEY);
    await recovered.save(true);
    expect(JSON.stringify(recovered.pending)).toBe(original);
    expect(storage.getItem(KEY)).toBe(raw);
    expect(recovered.error).toContain("保留原请求");
    const writes = fetcher.mock.calls.filter(([, init]) => !!init?.body);
    expect(writes).toHaveLength(1);
  });
  it("refreshes clean cached content but blocks overwriting a changed draft baseline", async () => {
    const storage = memory();
    let revision = 1;
    const fetcher = vi.fn(async (path: string) => new Response(JSON.stringify(
      path.endsWith("/definitions") ? [] : [content(revision)])));
    vi.stubGlobal("window", { localStorage: storage });
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    revision = 2;
    await store.load();
    expect(store.selected?.members[0]?.text).toBe("v2");
    expect(store.dirty).toBe(false);
    store.selected!.members[0]!.text = "local edit";
    revision = 3;
    await store.load();
    await store.save();
    expect(store.selected?.members[0]?.text).toBe("local edit");
    expect(store.pending).toBeNull();
    expect(store.error).toContain("基线过期");
    expect(fetcher.mock.calls).toHaveLength(6);
  });
  it("preserves an incompatible recovery record rather than replacing it", async () => {
    const storage = memory();
    const original = JSON.stringify({ schemaVersion: 1, drafts: {}, selectedId: {}, pending: null });
    storage.setItem(KEY, original);
    vi.stubGlobal("window", { localStorage: storage });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
    const store = useGlobalContentStore();
    await store.initialize();
    store.create("role_card");
    expect(storage.getItem(KEY)).toBe(original);
    expect(store.error).toContain("不兼容");
  });
});

describe("native content response custody", () => {
  it.each([1, 2].flatMap(attempt => ["dispose", "replace"].flatMap(action =>
    [false, true].map(fail => ({ attempt, action, fail })))))(
    "does not submit after initial synchronous storage custody changes $action/$attempt/throws=$fail", async ({ attempt, action, fail }) => {
      const storage = memory();
      const write = storage.setItem;
      vi.stubGlobal("window", { localStorage: storage });
      const fetcher = vi.fn(async (path: string) =>
        new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()])));
      vi.stubGlobal("fetch", fetcher);
      const store = useGlobalContentStore();
      await store.initialize();
      const previousVersion = store.version;
      let writes = 0;
      let changed = false;
      let replacement: unknown;
      let preservedRaw: string | null = null;
      storage.setItem = (key, raw) => {
        write(key, raw);
        const snapshot = JSON.parse(raw);
        if (changed || snapshot.pending === null || ++writes !== attempt) return;
        changed = true;
        if (action === "dispose") store.$dispose();
        else {
          replacement = { ...snapshot.pending, record: { ...snapshot.pending.record, name: "replacement command" } };
          store.pending = replacement as NonNullable<typeof store.pending>;
          write(key, JSON.stringify({ ...snapshot, pending: replacement }));
          store.busy = true;
        }
        store.error = "new storage custody";
        preservedRaw = storage.getItem(KEY);
        if (fail) throw new Error("old storage failure");
      };
      await store.save();
      expect(changed).toBe(true);
      expect(fetcher).toHaveBeenCalledTimes(2);
      expect(store.version).toBe(previousVersion);
      expect(store.records).toEqual([content()]);
      expect(store.error).toBe("new storage custody");
      expect(storage.getItem(KEY)).toBe(preservedRaw);
      if (action === "replace") {
        expect(store.pending).toEqual(replacement);
        expect(store.busy).toBe(true);
        store.$dispose();
      }
    },
  );

  it.each([200, 409].flatMap(status => [1, 2].flatMap(attempt =>
    ["dispose", "replace"].flatMap(action => [false, true].map(fail => ({ status, attempt, action, fail }))))))(
    "stops settlement after synchronous storage custody changes HTTP $status/$action/$attempt/throws=$fail", async ({ status, attempt, action, fail }) => {
      const storage = memory();
      const write = storage.setItem;
      vi.stubGlobal("window", { localStorage: storage });
      let command: unknown;
      const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
        if (!init?.body) return new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()]));
        command = JSON.parse(String(init.body));
        return status === 200 ? new Response(JSON.stringify((command as { record: GlobalContent }).record))
          : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status });
      });
      vi.stubGlobal("fetch", fetcher);
      const store = useGlobalContentStore();
      await store.initialize();
      const previousVersion = store.version;
      let clears = 0;
      let changed = false;
      let replacement: unknown;
      let preservedRaw: string | null = null;
      storage.setItem = (key, raw) => {
        write(key, raw);
        const snapshot = JSON.parse(raw);
        if (changed || command === undefined || snapshot.pending !== null || ++clears !== attempt) return;
        changed = true;
        if (action === "dispose") store.$dispose();
        else {
          const original = command as { record: GlobalContent };
          replacement = { ...command as object, record: { ...original.record, name: "replacement command" } };
          store.pending = replacement as NonNullable<typeof store.pending>;
          write(key, JSON.stringify({ ...snapshot, pending: replacement }));
          store.busy = true;
        }
        store.error = "new storage custody";
        preservedRaw = storage.getItem(KEY);
        if (fail) throw new Error("old storage failure");
      };
      await store.save();
      expect(changed).toBe(true);
      expect(fetcher).toHaveBeenCalledTimes(3);
      expect(store.version).toBe(previousVersion);
      expect(store.error).toBe("new storage custody");
      expect(storage.getItem(KEY)).toBe(preservedRaw);
      if (action === "replace") {
        expect(store.pending).toEqual(replacement);
        expect(store.busy).toBe(true);
        store.$dispose();
      }
    },
  );

  it("retains the original content request on first idempotency conflict", async () => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    let original: unknown;
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (!init?.body) return new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()]));
      original = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ error: { reason_code: "idempotency_conflict" } }), { status: 409 });
    });
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    await store.save();
    expect(store.pending).toEqual(original);
    expect(store.error).toContain("无法证明未发生");
    expect(JSON.parse(storage.getItem(KEY)!).pending).toEqual(original);
    const raw = storage.getItem(KEY);
    await store.save(true);
    expect(storage.getItem(KEY)).toBe(raw);
    expect(fetcher.mock.calls.filter(([, init]) => !!init?.body)).toHaveLength(1);
    store.$dispose();
  });

  it("does not send or retain an owned unsent content request after initial persistence fails", async () => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    const fetcher = vi.fn(async (path: string) =>
      new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()])));
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    const raw = storage.getItem(KEY);
    storage.setItem = () => { throw new Error("quota"); };
    await store.save();
    expect(store.pending).toBeNull();
    expect(store.error).toBe("quota");
    expect(storage.getItem(KEY)).toBe(raw);
    expect(fetcher).toHaveBeenCalledTimes(2);
    store.$dispose();
  });

  it.each([200, 409])("restores the original pending if local settlement fails after HTTP %i", async status => {
    const storage = memory();
    const write = storage.setItem;
    let failSettlement = false;
    storage.setItem = (key, raw) => {
      if (failSettlement && JSON.parse(raw).pending === null) throw new Error("quota");
      write(key, raw);
    };
    vi.stubGlobal("window", { localStorage: storage });
    let original: unknown;
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (!init?.body) return new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()]));
      original = JSON.parse(String(init.body));
      failSettlement = true;
      return status === 200 ? new Response(JSON.stringify((original as { record: GlobalContent }).record))
        : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status });
    });
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    store.selected!.members[0]!.text = "first";
    await store.save();
    expect(store.pending).toEqual(original);
    expect(JSON.parse(storage.getItem(KEY)!).pending).toEqual(original);
    const raw = storage.getItem(KEY);
    await store.save(true);
    expect(storage.getItem(KEY)).toBe(raw);
    expect(fetcher.mock.calls.filter(([, init]) => !!init?.body)).toHaveLength(1);
    store.$dispose();
  });

  it.each([200, 409])("ignores HTTP %i for a changed pending body even when its key is unchanged", async status => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn((path: string, init?: RequestInit) => init?.body
      ? new Promise<Response>(resolve => { deliver = resolve; })
      : Promise.resolve(new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()])))));
    const store = useGlobalContentStore();
    await store.initialize();
    const saving = store.save();
    const original = JSON.parse(JSON.stringify(store.pending));
    const replacement = structuredClone(original);
    replacement.record.name = "replacement pending";
    store.pending = replacement;
    store.selected!.members[0]!.text = "new draft";
    const raw = storage.getItem(KEY);
    deliver(status === 200 ? new Response(JSON.stringify(original.record))
      : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status }));
    await saving;
    expect(store.pending).toEqual(replacement);
    expect(store.selected?.members[0]?.text).toBe("new draft");
    expect(store.records).toEqual([content()]);
    expect(storage.getItem(KEY)).toBe(raw);
    expect(store.error).toBeNull();
    store.$dispose();
  });

  it("keeps content edits made while the original first save is in flight", async () => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn((path: string, init?: RequestInit) => init?.body
      ? new Promise<Response>(resolve => { deliver = resolve; })
      : Promise.resolve(new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()])))));
    const store = useGlobalContentStore();
    await store.initialize();
    const saving = store.save();
    const original = JSON.parse(JSON.stringify(store.pending));
    store.selected!.members[0]!.text = "later draft";
    deliver(new Response(JSON.stringify(original.record)));
    await saving;
    expect(store.selected?.members[0]?.text).toBe("later draft");
    expect(store.records[0].revision).toBe(2);
    expect(store.pending).toBeNull();
    store.$dispose();
  });

  it.each([200, 409])("does not write disposed content state after HTTP %i", async status => {
    const storage = memory();
    vi.stubGlobal("window", { localStorage: storage });
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn((path: string, init?: RequestInit) => init?.body
      ? new Promise<Response>(resolve => { deliver = resolve; })
      : Promise.resolve(new Response(JSON.stringify(path.endsWith("/definitions") ? [] : [content()])))));
    const store = useGlobalContentStore();
    await store.initialize();
    const saving = store.save();
    const original = JSON.parse(JSON.stringify(store.pending));
    store.$dispose();
    const raw = storage.getItem(KEY);
    deliver(status === 200 ? new Response(JSON.stringify(original.record))
      : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status }));
    await saving;
    expect(store.pending).toEqual(original);
    expect(storage.getItem(KEY)).toBe(raw);
  });
});
