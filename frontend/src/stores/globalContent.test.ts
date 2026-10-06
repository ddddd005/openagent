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
  it("keeps the exact uncertain write across reload and replays its key", async () => {
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
    await recovered.save(true);
    expect(recovered.pending).toBeNull();
    expect(recovered.selected?.revision).toBe(2);
    const writes = fetcher.mock.calls.filter(([, init]) => !!init?.body);
    expect(writes[0]?.[1]?.body).toBe(writes[1]?.[1]?.body);
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
