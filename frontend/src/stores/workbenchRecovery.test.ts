import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { WORKBENCH_RECOVERY_STORAGE_KEY as isolatedKey, WORKBENCH_STORAGE_KEY as originalKey,
  isSavedWorkbench } from "../adapters/workbenchPersistence";
import { graphClone, newGraph } from "../domain/workflowGraph";
import WorkbenchStorageRecovery from "../components/WorkbenchStorageRecovery.vue";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });
function storage(raw = "{broken") {
  const records = new Map([[originalKey, raw]]);
  return { records, getItem: (key: string) => records.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { records.set(key, value); }) };
}
describe("explicit isolated workbench recovery", () => {
  it("requires an explicit action, keeps unreadable bytes intact and dispatches no business command", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher); persistence.initialize(target);
    expect(persistence.status).toBe("blocked"); expect(persistence.canRecover).toBe(true);
    expect(target.setItem).not.toHaveBeenCalled(); expect(persistence.rawRecord()).toBe("{broken");
    expect(persistence.recoverWorkspace()).toBe(true);
    expect(target.getItem(originalKey)).toBe("{broken");
    const saved = JSON.parse(target.getItem(isolatedKey)!);
    expect(isSavedWorkbench(saved)).toBe(true);
    expect(saved.catalog).toHaveLength(1);
    expect(Object.values(saved.graph.entries)).toEqual([expect.objectContaining({
      document: expect.objectContaining({ schema_version: 2, nodes: [], edges: [] }),
      saved_revision: 0, session_id: null, pending: null,
    })]);
    expect(persistence.status).toBe("saved"); expect(persistence.isolated).toBe(true);
    expect(persistence.error).toBeNull(); expect(persistence.canRecover).toBe(false);
    expect(target.setItem.mock.calls.map(([key]) => key)).toEqual([isolatedKey]);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("preserves unsupported records and frozen request coordinates without adopting or replaying them", () => {
    const document = newGraph("Unsupported archive"), key = crypto.randomUUID();
    const raw = JSON.stringify({ schemaVersion: 99, kind: "future-workbench", document,
      pending: { action: "start", path: `/api/graph/sessions/${crypto.randomUUID()}/runs`,
        body: { idempotency_key: key, input: "retained original request" } } });
    const target = storage(raw), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target); expect(persistence.recoverWorkspace()).toBe(true);
    expect(target.getItem(originalKey)).toBe(raw); expect(persistence.rawRecord()).toBe(raw);
    expect(useWorkflowGraphStore().document!.workflow_definition_id).not.toBe(document.workflow_definition_id);
    expect(useWorkflowGraphStore().active!.pending).toBeNull();
  });
  it("reloads the isolated record and continues saving to that key only", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target); persistence.recoverWorkspace();
    useWorkflowGraphStore().document!.name = "Retained independent draft";
    expect(persistence.save()).toBe(true);
    const isolated = target.getItem(isolatedKey), graph = graphClone(useWorkflowGraphStore().storeSnapshot());
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const reopened = useWorkbenchPersistenceStore(); reopened.initialize(target);
    expect(reopened.isolated).toBe(true); expect(reopened.status).toBe("saved");
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(graph);
    expect(reopened.rawRecord()).toBe("{broken"); expect(target.getItem(isolatedKey)).toBe(isolated);
    expect(target.getItem(originalKey)).toBe("{broken");
    expect(target.setItem.mock.calls.every(([key]) => key === isolatedKey)).toBe(true);
  });
  it("keeps the isolated key authoritative even when both records contain identical bytes", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target); persistence.recoverWorkspace();
    const identical = target.getItem(isolatedKey)!;
    target.records.set(originalKey, identical); target.setItem.mockClear();
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const reopened = useWorkbenchPersistenceStore(); reopened.initialize(target);
    expect(reopened.isolated).toBe(true); expect(reopened.status).toBe("saved");
    expect(target.setItem).not.toHaveBeenCalled();
    useWorkflowGraphStore().document!.name = "Only the isolated record may change";
    expect(reopened.save()).toBe(true);
    expect(target.getItem(originalKey)).toBe(identical);
    expect(target.getItem(isolatedKey)).not.toBe(identical);
    expect(target.setItem.mock.calls.map(([key]) => key)).toEqual([isolatedKey]);
  });
  it("blocks identical malformed records in the isolated namespace without offering another recovery", () => {
    const target = storage(); target.records.set(isolatedKey, "{broken");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(target);
    expect(persistence.isolated).toBe(true); expect(persistence.status).toBe("blocked");
    expect(persistence.canRecover).toBe(false); expect(persistence.recoverWorkspace()).toBe(false);
    expect(persistence.rawRecord()).toBe("{broken"); expect(target.setItem).not.toHaveBeenCalled();
  });
  it("retains newly edited unsaved drafts and their undo history during recovery", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target);
    const graph = useWorkflowGraphStore(), id = graph.createWorkflow();
    graph.history[id] = { past: [graphClone(graph.document!)], future: [] };
    graph.document!.name = "Unsent user edit";
    const before = graph.storeSnapshot(), history = graphClone(graph.history);
    expect(persistence.recoverWorkspace()).toBe(true);
    expect(graph.storeSnapshot()).toEqual(before); expect(graph.history).toEqual(history);
    expect(useWorkspaceStore().activeWorkflowId).toBe(id);
    expect(target.getItem(originalKey)).toBe("{broken");
  });
  it("does not reset a pending session or use recovery to bypass original request protection", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore(); persistence.initialize(target);
    const graph = useWorkflowGraphStore(); graph.createWorkflow();
    graph.active!.pending = { action: "save", path: "/api/graph/definitions",
      body: { document: graphClone(graph.document!), idempotency_key: crypto.randomUUID(), expected_revision: 0 } };
    const before = graph.storeSnapshot();
    expect(persistence.canRecover).toBe(false); expect(persistence.recoverWorkspace()).toBe(false);
    expect(graph.storeSnapshot()).toEqual(before); expect(target.getItem(isolatedKey)).toBeNull();
    expect(target.getItem(originalKey)).toBe("{broken");
  });
  it("exports a record rejected by session validation without adopting or replaying its request", () => {
    const document = newGraph("Invalid saved request"), id = document.workflow_definition_id;
    const raw = JSON.stringify({ schemaVersion: 7, kind: "graph-workbench", revision: 1,
      savedAt: "2026-10-08T00:00:00.000Z", activeWorkflowId: id, selectedWorkflowId: id,
      catalog: [{ id, title: document.name, description: "", nodeCount: 0, state: "saved" }],
      graph: { schema_version: 1, entries: { [id]: { document, saved_revision: 1,
        session_id: crypto.randomUUID(), pending: { action: "start", path: "/invalid",
          body: { idempotency_key: crypto.randomUUID() } } } } } });
    const target = storage(raw), persistence = useWorkbenchPersistenceStore(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher); persistence.initialize(target);
    expect(persistence.status).toBe("blocked"); expect(persistence.rawRecord()).toBe(raw);
    expect(useWorkflowGraphStore().entries).toEqual({}); expect(persistence.canRecover).toBe(true);
    expect(persistence.recoverWorkspace()).toBe(true);
    expect(target.getItem(originalKey)).toBe(raw); expect(fetcher).not.toHaveBeenCalled();
    expect(useWorkflowGraphStore().active!.pending).toBeNull();
    expect(useWorkflowGraphStore().document!.workflow_definition_id).not.toBe(id);
  });
  it.each(["command", "copy"] as const)("does not isolate while a draft is locked by %s", mode => {
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage());
    const graph = useWorkflowGraphStore(); graph.createWorkflow();
    if (mode === "command") graph.busy = "save";
    else useWorkspaceStore().activeWorkflow.copyPending = true;
    expect(persistence.canRecover).toBe(false); expect(persistence.recoverWorkspace()).toBe(false);
  });
  it("leaves all original state unchanged when isolated storage rejects a write", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore(); persistence.initialize(target);
    target.setItem.mockImplementationOnce(() => { throw new Error("quota exceeded"); });
    const before = useWorkflowGraphStore().storeSnapshot();
    expect(persistence.recoverWorkspace()).toBe(false); expect(persistence.status).toBe("blocked");
    expect(persistence.isolated).toBe(false); expect(persistence.error).toBe("quota exceeded");
    expect(target.getItem(originalKey)).toBe("{broken"); expect(target.getItem(isolatedKey)).toBeNull();
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(before);
  });
  it("refuses a changed original record and a newly occupied isolated key", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore(); persistence.initialize(target);
    target.records.set(originalKey, "newer original record");
    expect(persistence.recoverWorkspace()).toBe(false); expect(target.getItem(isolatedKey)).toBeNull();
    target.records.set(originalKey, "{broken"); target.records.set(isolatedKey, "other page record");
    expect(persistence.recoverWorkspace()).toBe(false);
    expect(target.getItem(isolatedKey)).toBe("other page record");
    expect(target.setItem).not.toHaveBeenCalled();
  });
  it("keeps the isolated compare-and-swap gate and refuses a third recovery namespace", () => {
    const target = storage(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target); persistence.recoverWorkspace();
    target.records.set(isolatedKey, "newer isolated record");
    useWorkflowGraphStore().document!.name = "Stale local edit";
    expect(persistence.save()).toBe(false); expect(persistence.status).toBe("blocked");
    expect(persistence.canRecover).toBe(false);
    expect(target.getItem(isolatedKey)).toBe("newer isolated record");
    expect(target.getItem(originalKey)).toBe("{broken");
  });
  it("guards storage events for the active isolated key, not the preserved primary key", () => {
    const listeners = new Map<string, (event: StorageEvent) => void>();
    vi.stubGlobal("window", { addEventListener: (name: string, callback: (event: StorageEvent) => void) => {
      listeners.set(name, callback);
    }, removeEventListener: vi.fn() });
    const target = storage(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(target); persistence.recoverWorkspace();
    const changed = listeners.get("storage")!;
    changed({ key: originalKey, newValue: "different primary record" } as StorageEvent);
    expect(persistence.status).toBe("saved");
    changed({ key: isolatedKey, newValue: target.getItem(isolatedKey) } as StorageEvent);
    expect(persistence.status).toBe("saved");
    changed({ key: isolatedKey, newValue: "other page isolated record" } as StorageEvent);
    expect(persistence.status).toBe("blocked"); expect(persistence.canRecover).toBe(false);
    expect(target.getItem(originalKey)).toBe("{broken");
  });
  it("preserves a malformed isolated record instead of overwriting it on reload", () => {
    const target = storage(); target.records.set(isolatedKey, "{broken isolation");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(target);
    expect(persistence.status).toBe("blocked"); expect(persistence.isolated).toBe(true);
    expect(persistence.canRecover).toBe(false); expect(persistence.rawRecord()).toBe("{broken isolation");
    expect(target.setItem).not.toHaveBeenCalled();
  });
  it("renders recovery commands only for safely isolated unreadable records", async () => {
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage());
    const blocked = await renderToString(createSSRApp(WorkbenchStorageRecovery).use(pinia));
    expect(blocked).toContain("保存记录无法读取"); expect(blocked).toContain('aria-label="导出原记录"');
    expect(blocked).toContain('aria-label="另建本机工作区"');
    persistence.recoverWorkspace();
    const recovered = await renderToString(createSSRApp(WorkbenchStorageRecovery).use(pinia));
    expect(recovered).toContain("原记录未覆盖"); expect(recovered).not.toContain('aria-label="另建本机工作区"');
    expect(recovered).toContain('aria-label="导出原记录"');
  });
});
