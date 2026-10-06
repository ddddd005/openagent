import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { usePreparationStore } from "./preparation";
import { useWorkspaceStore } from "./workspace";
import { useExposuresStore } from "./exposures";
import { useWorkbenchRuntimeStore } from "./workbenchRuntime";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { readWorkbench } from "../adapters/workbenchPersistence";
import { legacyWorkbenchStorage } from "../testUtils/legacyWorkbenchStorage";

const SID = "00000000-0000-4000-8000-000000000901";
const REQUEST = "00000000-0000-4000-8000-000000000902";
function memory() {
  return legacyWorkbenchStorage();
}
function idle() {
  return {
    workflow_session_id: SID, revision: 1, mode: "offline", can_submit: true,
    available_actions: [], chains: [], messages: [],
    nodes: [...Object.values(AGENT_BINDINGS), "7be319b8-30bd-4674-b7bf-d1cf54a1a10a"]
      .map((binding) => ({ node_binding_id: binding, label: binding, status: "idle", run_id: null, revision: null })),
  };
}
beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});
afterEach(() => {
  useWorkbenchPersistenceStore().$dispose();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
describe("workbench configuration recovery", () => {
  it("recovers an unsaved independent edit without overwriting the saved source", () => {
    const storage = memory();
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const preparation = usePreparationStore();
    const workspace = useWorkspaceStore();
    const exposures = useExposuresStore();
    const prompt = preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find((node) => node.kind === "prompt-item")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", prompt.id, { text: "saved fixed prompt" });
    const draftId = workspace.activeWorkflowId;
    expect(draftId).not.toBe(MAIN_WORKFLOW_ID);
    workspace.moveNodes([{ id: workspace.nodes[0].id, position: { x: 145, y: 215 } }]);
    exposures.register(draftId, "B", "B.result", "result", ["delivered_text"]);
    expect(persistence.status).toBe("pending");
    vi.advanceTimersByTime(250);
    expect(persistence.status).toBe("saved");
    expect(preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(n => n.id === prompt.id)?.config).not.toMatchObject({ text: "saved fixed prompt" });
    expect(workspace.activeWorkflow.state).toBe("draft");
    expect(fetcher).not.toHaveBeenCalled();
    persistence.$dispose();
    setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore();
    restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useWorkspaceStore().activeWorkflowId).toBe(draftId);
    expect(useWorkspaceStore().activeWorkflow.state).toBe("draft");
    const restoredPrompt = usePreparationStore().getDraft(draftId, "A").nodes.find((node) => node.id === prompt.id)!;
    expect(restoredPrompt.config).toMatchObject({ text: "saved fixed prompt" });
    expect(useWorkspaceStore().nodes[0].position).toEqual({ x: 145, y: 215 });
    expect(useExposuresStore().registrations[0].publicName).toBe("B.result");
    expect(useExposuresStore().observations).toEqual({});
    expect(usePreparationStore().canUndo(draftId, "A")).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("restores the original uncertain request and session reference, never the old authority view", async () => {
    const storage = memory();
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/api/sessions" ? [] : path === "/api/active-session"
        ? { active_workflow_session_id: null, revision: 0 } : idle(),
    ), { status: 200 })));
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    runtime.runtimes[MAIN_WORKFLOW_ID].pending = {
      path: `/api/sessions/${SID}/inputs`, requestId: REQUEST, action: "start", replayable: true,
      body: { text: "original input", idempotency_key: REQUEST },
    };
    expect(persistence.save()).toBe(true);
    const saved = readWorkbench(storage);
    expect(JSON.stringify(saved.value?.runtime)).not.toContain('"view"');
    expect(JSON.stringify(saved.value?.runtime)).not.toContain('"messages"');
    persistence.$dispose();
    setActivePinia(createPinia());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    useWorkbenchPersistenceStore().initialize(storage);
    const recovered = useWorkbenchRuntimeStore().exportRuntimeSnapshot();
    const session = recovered.sessions.find((row) => row.sessionId === SID)!;
    expect(session.pending?.requestId).toBe(REQUEST);
    expect(session.pending?.body).toEqual({ text: "original input", idempotency_key: REQUEST });
    expect(session.requiresRefresh).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("blocks mutations if durable storage fails, without replacing the original record", async () => {
    const backing = memory();
    let fail = false;
    const storage = {
      getItem: backing.getItem,
      setItem: (key: string, value: string) => {
        if (fail) throw new Error("storage quota exceeded");
        backing.setItem(key, value);
      },
    };
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const original = backing.getItem();
    fail = true;
    const fetcher = vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/api/health" ? { mode: "offline" } : path === "/api/sessions" ? []
        : path === "/api/active-session" ? { active_workflow_session_id: null, revision: 0 }
          : { error: { code: "not_found" } },
    ), { status: path.startsWith("/api/prompt-configs") ? 404 : 200 }));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("must not dispatch");
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.some((call) => (call as unknown as [string, RequestInit])[1]?.method === "POST")).toBe(false);
    expect(persistence.status).toBe("blocked");
    expect(persistence.error).toContain("quota");
    expect(backing.getItem()).toBe(original);
    expect(runtime.unknown).toBeNull();
  });
  it("preserves a corrupted saved record and refuses automatic overwrite", () => {
    const storage = memory();
    storage.setItem("ignored", "{broken record");
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    expect(persistence.status).toBe("blocked");
    usePreparationStore().setRootText(MAIN_WORKFLOW_ID, "A", "unsaved edit");
    vi.advanceTimersByTime(250);
    expect(persistence.save()).toBe(false);
    expect(storage.getItem()).toBe("{broken record");
  });
  it("does not replace the original with an unrecoverable in-memory request", async () => {
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    expect(persistence.save()).toBe(true);
    const original = storage.getItem();
    runtime.runtimes[MAIN_WORKFLOW_ID].pending = {
      path: "/api/sessions/foreign/inputs", requestId: REQUEST, action: "start", replayable: true,
      body: { text: "wrong scope", idempotency_key: REQUEST },
    };
    expect(persistence.save()).toBe(false);
    expect(persistence.status).toBe("blocked");
    expect(persistence.error).toContain("无法安全恢复");
    expect(storage.getItem()).toBe(original);
  });
  it("preserves the first edit and exact lost-copy request without resubmitting or discarding it", async () => {
    const storage = memory();
    const childId = "00000000-0000-4000-8000-000000000903";
    let lost = true;
    const copies: string[] = [];
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/sessions") return new Response(JSON.stringify([
        { workflow_session_id: SID, created_at: null, mode: "offline" },
      ]));
      if (path === "/api/active-session")
        return new Response(JSON.stringify({ active_workflow_session_id: SID, revision: 1 }));
      if (path.endsWith("/data/revision")) return new Response('{"revision":0}');
      if (path.endsWith("/copy")) {
        copies.push(String(init?.body));
        if (lost) { lost = false; throw new Error("lost copy receipt"); }
        return new Response(JSON.stringify({ ...idle(), workflow_session_id: childId }));
      }
      return new Response(JSON.stringify({ ...idle(), revision: lost ? 1 : 2 }));
    });
    vi.stubGlobal("fetch", fetcher);
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    const preparation = usePreparationStore();
    const prompt = preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(n => n.kind === "prompt-item")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", prompt.id, { text: "first edit survives" });
    await vi.advanceTimersByTimeAsync(0);
    const workspace = useWorkspaceStore();
    const copying = workspace.workflows.find(w => w.copyPending)!;
    expect(copying).toBeDefined();
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    workspace.openWorkflow(copying.id);
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(runtime.unknown?.action).toBe(`copy-workflow:${copying.id}`);
    expect(persistence.save()).toBe(true);
    persistence.$dispose();
    runtime.$dispose();
    setActivePinia(createPinia());
    const recovered = useWorkbenchPersistenceStore();
    recovered.initialize(storage);
    const restoredRuntime = useWorkbenchRuntimeStore();
    await restoredRuntime.activateWorkflow(MAIN_WORKFLOW_ID);
    const original = storage.getItem();
    const requests = fetcher.mock.calls.length;
    await recovered.reconcileCopy();
    expect(copies).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledTimes(requests);
    expect(storage.getItem()).toBe(original);
    expect(useWorkspaceStore().activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(useWorkspaceStore().workflows.find(row => row.id === copying.id)?.copyPending).toBe(true);
    expect(usePreparationStore().getDraft(copying.id, "A").nodes.find(n => n.id === prompt.id)?.config)
      .toMatchObject({ text: "first edit survives" });
    expect(restoredRuntime.unknown?.body).toEqual(JSON.parse(copies[0]!));
    useWorkspaceStore().activeWorkflowId = copying.id;
    recovered.discardDraft();
    expect(useWorkspaceStore().workflows.find(row => row.id === copying.id)?.copyPending).toBe(true);
    expect(restoredRuntime.exportRuntimeSnapshot().sessions.some(row =>
      row.pending?.action === `copy-workflow:${copying.id}`)).toBe(true);
  });
  it("does not reconstruct a recovered copy operation when its original request is missing", async () => {
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const workspace = useWorkspaceStore();
    const target = "00000000-0000-4000-8000-000000000903";
    workspace.cloneWorkflow(MAIN_WORKFLOW_ID, target);
    usePreparationStore().cloneWorkflow(MAIN_WORKFLOW_ID, target);
    workspace.setCopyPending(target, true);
    expect(persistence.save()).toBe(true);
    const original = storage.getItem();
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await persistence.reconcileCopy();
    expect(fetcher).not.toHaveBeenCalled();
    expect(storage.getItem()).toBe(original);
    expect(workspace.workflows.find(row => row.id === target)?.copyPending).toBe(true);
  });
});
