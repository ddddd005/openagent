import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useModelConfigurationStore } from "./modelConfiguration";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkbenchRuntimeStore } from "./workbenchRuntime";
import { useWorkspaceStore } from "./workspace";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { createWorkspaceDrafts, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { createPreparationDraft } from "../fixtures/preparation";
import { dependencyTarget, isChatProvider, isModelConfigurationSnapshot, type ChatProvider } from "../domain/modelConfiguration";
import { readWorkbench, WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import { legacyWorkbenchStorage } from "../testUtils/legacyWorkbenchStorage";

const PROVIDER = "00000000-0000-4000-8000-000000000101";
function provider(): ChatProvider {
  return {
    schema_version: 1, kind: "chat_provider", provider_id: PROVIDER, revision: 1,
    name: "DeepSeek", protocol: "chat", base_url: "https://api.deepseek.com",
    credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
  };
}
function memory() {
  return legacyWorkbenchStorage();
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers(); });
afterEach(() => {
  useWorkbenchPersistenceStore().$dispose();
  useModelConfigurationStore().$dispose();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("global providers and typed model dependencies", () => {
  it("switches sidebar content without changing the workflow, selection, graph or session", async () => {
    const workspace = useWorkspaceStore();
    const models = useModelConfigurationStore();
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    const node = models.addNode(MAIN_WORKFLOW_ID, { x: 100, y: -50 })!;
    workspace.selectNodes([workspace.nodes[0].id]);
    const graph = JSON.stringify(workspace.drafts);
    const token = workspace.currentViewToken();
    const sessions = runtime.exportRuntimeSnapshot();
    workspace.sidebarSection = "providers";
    models.editProvider(provider());
    models.providerForm!.name = "unsaved edit";
    workspace.sidebarSection = "workflows";
    expect(workspace.currentViewToken()).toEqual(token);
    expect(workspace.selectedNodeIds).toEqual([workspace.nodes[0].id]);
    expect(JSON.stringify(workspace.drafts)).toBe(graph);
    expect(models.selectedNodeId).toBe(node);
    expect(models.providerForm?.name).toBe("unsaved edit");
    expect(runtime.exportRuntimeSnapshot()).toEqual(sessions);
  });
  it("allows one model to supply A and B, rejecting business handles, foreign targets and conflicts", () => {
    const models = useModelConfigurationStore();
    const first = models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 })!;
    const second = models.addNode(MAIN_WORKFLOW_ID, { x: 50, y: 0 })!;
    const connect = (id: string, stage: "A" | "B") => models.connect(MAIN_WORKFLOW_ID, id, dependencyTarget(stage), "model-out", "model-in");
    expect(models.connect(MAIN_WORKFLOW_ID, first, dependencyTarget("A"), "model-out", "in")).toBe(false);
    expect(connect(first, "A")).toBe(true);
    expect(connect(first, "B")).toBe(true);
    expect(connect(second, "A")).toBe(false);
    expect(models.getDraft(MAIN_WORKFLOW_ID)?.edges.map((edge) => edge.target_binding_id)).toEqual(Object.values(AGENT_BINDINGS));
    models.removeNode(MAIN_WORKFLOW_ID, first);
    expect(models.getDraft(MAIN_WORKFLOW_ID)?.edges).toEqual([]);
    expect(models.getDraft("foreign")).toBeNull();
  });
  it("preserves exact provider references across global refresh and never selects the latest version", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    const node = models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0];
    node.provider_ref = { provider_id: PROVIDER, revision: 1 };
    vi.stubGlobal("fetch", vi.fn(async () => response([{ ...provider(), revision: 2 }])));
    await models.loadProviders();
    expect(models.providers[0].revision).toBe(2);
    expect(node.provider_ref.revision).toBe(1);
  });
  it("saves model nodes, layouts, connections and references locally without copying the global provider table", () => {
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const models = useModelConfigurationStore();
    models.providers = [provider()];
    models.addNode(MAIN_WORKFLOW_ID, { x: 27, y: -45 });
    const workflowId = useWorkspaceStore().activeWorkflowId;
    const id = models.selectedNodeId!;
    const draft = models.getDraft(workflowId)!;
    draft.nodes[0].provider_ref = { provider_id: PROVIDER, revision: 1 };
    draft.nodes[0].parameters.temperature = 0.3;
    models.connect(workflowId, id, dependencyTarget("A"), "model-out", "model-in");
    expect(persistence.save()).toBe(true);
    expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    expect(storage.getItem()).not.toContain("base_url");
    expect(storage.getItem()).not.toContain("credential_ref");
    persistence.$dispose();
    models.$dispose();
    setActivePinia(createPinia());
    useWorkbenchPersistenceStore().initialize(storage);
    const restored = useModelConfigurationStore();
    expect(restored.getDraft(workflowId)).toEqual(draft);
    expect(restored.providers).toEqual([]);
    expect(restored.selectedNodeId).toBeNull();
  });
  it("keeps invalid editable model parameters for diagnosis but refuses publishing them", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.temperature = -1;
    expect(isModelConfigurationSnapshot(models.exportConfiguration())).toBe(true);
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveModels(MAIN_WORKFLOW_ID)).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
    expect(models.pending).toBeNull();
  });
  it("reports a safe field-specific provider error without submitting or discarding the edit", async () => {
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    models.providerForm!.base_url = "https://host.test?api_key=PRIVATE";
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(models.providerDiagnostics[0]?.field).toBe("base_url");
    expect(await models.saveProvider()).toBe(false);
    expect(models.error?.code).toBe("provider_address_invalid");
    expect(models.error?.reason).not.toContain("PRIVATE");
    expect(models.providerForm!.base_url).toContain("PRIVATE");
    expect(fetcher).not.toHaveBeenCalled();
    expect(models.pending).toBeNull();
  });
  it("blocks unsupported Chat token limits before publication while retaining the editable draft", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.max_tokens = 8193;
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveModels(MAIN_WORKFLOW_ID)).toBe(false);
    expect(models.error?.code).toBe("model_max_tokens_invalid");
    expect(models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.max_tokens).toBe(8193);
    expect(isModelConfigurationSnapshot(models.exportConfiguration())).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("marks a saved model draft unsaved again after parameter, layout or binding edits", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    models.setPersistenceGuard(() => true);
    vi.stubGlobal("fetch", vi.fn(async (_path: string, options: RequestInit) =>
      response(JSON.parse(options.body as string).record, 201)));
    await models.publishModels(MAIN_WORKFLOW_ID);
    const draft = models.getDraft(MAIN_WORKFLOW_ID)!;
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(true);
    draft.nodes[0].parameters.temperature = 0.5;
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(false);
    delete draft.nodes[0].parameters.temperature;
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(true);
    draft.nodes[0].position.x = 5;
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(false);
    draft.nodes[0].position.x = 0;
    draft.nodes[0].provider_ref = { provider_id: PROVIDER, revision: 1 };
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(false);
    const snapshot = models.exportConfiguration();
    expect(models.restoreConfiguration(snapshot)).toBe(true);
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(false);
  });
  it("accepts only known model diagnostics for this exact saved graph and ignores supplied private fields", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    models.setPersistenceGuard(() => true);
    const nodeId = models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].id;
    let problems: unknown[] = [{ target: nodeId, code: "credential_reference_missing" }];
    vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/diagnostics")) {
        const draft = models.getDraft(MAIN_WORKFLOW_ID)!;
        return response({
          config_id: draft.configId, revision: draft.backendRevision,
          execution_supported: true, diagnostics: problems,
        });
      }
      return response(JSON.parse(options!.body as string).record, 201);
    }));
    expect(await models.saveModels(MAIN_WORKFLOW_ID)).toBe(true);
    expect(models.diagnostics).toEqual([{ target: nodeId, code: "credential_reference_missing" }]);
    problems = [{ target: nodeId, code: "PRIVATE", detail: "PRIVATE" }];
    expect(await models.saveModels(MAIN_WORKFLOW_ID)).toBe(true);
    expect(models.error?.reason).toBe("模型诊断读取契约无效");
    expect(JSON.stringify(models.diagnostics)).not.toContain("PRIVATE");
    problems = [{ target: crypto.randomUUID(), code: "provider_unavailable" }];
    await models.saveModels(MAIN_WORKFLOW_ID);
    expect(models.error?.reason).toBe("模型诊断读取契约无效");
  });
  it("persists an uncertain provider mutation and preserves it without redispatch after reinitialization", async () => {
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    models.providerForm!.name = "new name";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("lost acknowledgement"); }));
    expect(await models.saveProvider()).toBe(false);
    expect(models.error?.kind).toBe("unknown");
    const original = models.exportConfiguration().pending!;
    expect(persistence.save()).toBe(true);
    persistence.$dispose();
    models.$dispose();
    setActivePinia(createPinia());
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => response(JSON.parse(options.body as string).record, 201));
    vi.stubGlobal("fetch", fetcher);
    useWorkbenchPersistenceStore().initialize(storage);
    const restored = useModelConfigurationStore();
    expect(restored.pending).toEqual(original);
    expect(fetcher).not.toHaveBeenCalled();
    await restored.reconcile();
    expect(fetcher).not.toHaveBeenCalled();
    expect(restored.pending).toEqual(original);
    expect(restored.providers).toEqual([]);
    expect(restored.error?.kind).toBe("unknown");
  });
  it("blocks dispatch on failed local persistence and keeps the existing saved record", async () => {
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const original = storage.getItem();
    storage.setItem = () => { throw new Error("quota"); };
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveProvider()).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
    expect(storage.getItem()).toBe(original);
    expect(models.pending).toBeNull();
  });
  it("keeps mismatched receipts unknown and does not ask a later rejection to settle them", async () => {
    useWorkbenchPersistenceStore().initialize(memory());
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    vi.stubGlobal("fetch", vi.fn(async (_path: string, options: RequestInit) => {
      const request = JSON.parse(options.body as string);
      return response({ ...request.record, name: "wrong receipt" }, 201);
    }));
    expect(await models.saveProvider()).toBe(false);
    expect(models.error?.kind).toBe("unknown");
    expect(models.pending).not.toBeNull();
    const original = models.exportConfiguration().pending;
    const fetcher = vi.fn(async () => response({ error: { reason_code: "stale_revision" } }, 409));
    vi.stubGlobal("fetch", fetcher);
    await models.reconcile();
    expect(fetcher).not.toHaveBeenCalled();
    expect(models.pending).toEqual(original);
    expect(models.error?.kind).toBe("unknown");
  });
  it("refuses a missing model dependency without submitting an input", async () => {
    const models = useModelConfigurationStore();
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    runtime.setInputText("test");
    const fetcher = vi.fn(async (_path: string) => response({ mode: "offline" }));
    vi.stubGlobal("fetch", fetcher);
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.every(([path]) => path === "/api/health")).toBe(true);
    expect(runtime.error?.reason).toContain("模型依赖缺失");
  });
  it("refuses to read or persist credential plaintext and protocol extensions", () => {
    expect(isChatProvider({ ...provider(), api_key: "PRIVATE" })).toBe(false);
    expect(isChatProvider({ ...provider(), protocol: "responses" })).toBe(false);
    expect(isChatProvider({ ...provider(), base_url: "https://host.test?key=PRIVATE" })).toBe(false);
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    useModelConfigurationStore().addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    expect(persistence.save()).toBe(true);
    const parsed = JSON.parse(storage.getItem()!);
    const workflow = useWorkspaceStore().activeWorkflowId;
    const model = parsed.documents[workflow].document.nodes.find((node: { component_id: string }) => node.component_id === "legacy.model-provider");
    model.config.api_key = "PRIVATE";
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(parsed));
    const original = storage.getItem();
    expect(readWorkbench(storage).error).toBeTruthy();
    expect(storage.getItem()).toBe(original);
  });
  it("reads a real v1 workbench and explicitly saves a v6 record without dispatch", () => {
    const storage = memory();
    const value = { schemaVersion: 1, kind: "fixed-workbench", revision: 1, savedAt: "2026-10-01T00:00:00.000Z",
      activeWorkflowId: MAIN_WORKFLOW_ID, selectedWorkflowId: MAIN_WORKFLOW_ID,
      layouts: Object.fromEntries(Object.entries(createWorkspaceDrafts()).map(([id, draft]) => [id,
        Object.fromEntries(draft.nodes.map(node => [node.id, { ...node.position }]))])),
      preparations: [createPreparationDraft(MAIN_WORKFLOW_ID, "A"), createPreparationDraft(MAIN_WORKFLOW_ID, "B")],
      registrations: [], runtime: { schemaVersion: 1, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null } };
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(value));
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const restored = useWorkbenchPersistenceStore();
    restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useModelConfigurationStore().drafts).toEqual([]);
    expect(restored.save()).toBe(true);
    expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("restores a real v5 model snapshot and rejects undeclared credentials before upgrading to v6", () => {
    const storage = memory(); const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const models = useModelConfigurationStore(); models.addNode(MAIN_WORKFLOW_ID, { x: 12, y: 34 });
    const workflowId = useWorkspaceStore().activeWorkflowId;
    models.getDraft(workflowId)!.nodes[0].provider_ref = { provider_id: PROVIDER, revision: 1 };
    persistence.save();
    const projected = readWorkbench(storage).value!;
    const { graph: _graph, ...legacy } = projected;
    const v5 = { ...legacy, schemaVersion: 5 };
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(v5));
    expect(readWorkbench(storage).error).toBeNull();
    const bad = JSON.parse(JSON.stringify(v5)); bad.models.api_key = "PRIVATE";
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(bad));
    expect(readWorkbench(storage).error).toBeTruthy();
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(v5));
    persistence.$dispose(); models.$dispose(); setActivePinia(createPinia());
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useModelConfigurationStore().getDraft(workflowId)?.nodes[0].provider_ref).toEqual({ provider_id: PROVIDER, revision: 1 });
    expect(restored.save()).toBe(true); expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("native configuration response custody", () => {
  it.each(["provider", "model"])("retains the original %s request on first idempotency conflict", async kind => {
    const models = useModelConfigurationStore();
    const guard = vi.fn(() => true);
    models.setPersistenceGuard(guard);
    if (kind === "provider") models.editProvider(provider());
    else models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    let original: unknown;
    const fetcher = vi.fn(async () => {
      original = models.exportConfiguration().pending;
      return response({ error: { reason_code: "idempotency_conflict" } }, 409);
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await (kind === "provider" ? models.saveProvider() : models.saveModels(MAIN_WORKFLOW_ID))).toBe(false);
    expect(models.pending).toEqual(original);
    expect(models.error?.kind).toBe("unknown");
    expect(models.error?.code).toBe("idempotency_conflict");
    await models.reconcile();
    expect(models.pending).toEqual(original);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(guard).toHaveBeenCalledTimes(1);
  });

  it("clears only its own unsent provider request when initial persistence throws", async () => {
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    models.setPersistenceGuard(() => { throw new Error("quota"); });
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveProvider()).toBe(false);
    expect(models.pending).toBeNull();
    expect(models.error?.kind).toBe("unavailable");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not clear a request restored by a throwing initial persistence guard", async () => {
    const models = useModelConfigurationStore();
    models.editProvider(provider());
    let restored = models.exportConfiguration();
    models.setPersistenceGuard(() => {
      restored = models.exportConfiguration();
      models.restoreConfiguration(restored);
      throw new Error("quota");
    });
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveProvider()).toBe(false);
    expect(models.exportConfiguration()).toEqual(restored);
    expect(models.pending).not.toBeNull();
    expect(models.error).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("clears a first definite rejection only after its local settlement is saved", async () => {
    const models = useModelConfigurationStore();
    const guard = vi.fn(() => true);
    models.setPersistenceGuard(guard);
    models.editProvider(provider());
    vi.stubGlobal("fetch", vi.fn(async () => response({ error: { reason_code: "stale_revision" } }, 409)));
    expect(await models.saveProvider()).toBe(false);
    expect(models.pending).toBeNull();
    expect(models.error?.code).toBe("stale_revision");
    expect(guard).toHaveBeenCalledTimes(2);
  });

  it.each([201, 409])("restores pending when local settlement fails after HTTP %i", async status => {
    const models = useModelConfigurationStore();
    const guard = vi.fn().mockReturnValueOnce(true).mockReturnValue(false);
    models.setPersistenceGuard(guard);
    models.editProvider(provider());
    let original: unknown;
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      original = models.exportConfiguration().pending;
      return status === 201 ? response(JSON.parse(options.body as string).record, status)
        : response({ error: { reason_code: "stale_revision" } }, status);
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveProvider()).toBe(false);
    expect(models.pending).toEqual(original);
    expect(models.error?.kind).toBe("unknown");
    await models.reconcile();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(guard).toHaveBeenCalledTimes(2);
    expect(models.pending).toEqual(original);
  });

  it("restores pending when the settlement guard throws", async () => {
    const models = useModelConfigurationStore();
    models.setPersistenceGuard(vi.fn().mockReturnValueOnce(true).mockImplementation(() => { throw new Error("quota"); }));
    models.editProvider(provider());
    let original: unknown;
    vi.stubGlobal("fetch", vi.fn(async (_path: string, options: RequestInit) => {
      original = models.exportConfiguration().pending;
      return response(JSON.parse(options.body as string).record);
    }));
    expect(await models.saveProvider()).toBe(false);
    expect(models.pending).toEqual(original);
    expect(models.error?.kind).toBe("unknown");
  });

  it.each([201, 409, 404, 410])("isolates HTTP %i after restoring the identical native provider request", async status => {
    const models = useModelConfigurationStore();
    const guard = vi.fn(() => true);
    models.setPersistenceGuard(guard);
    models.editProvider(provider());
    let deliver!: (value: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const saving = models.saveProvider();
    const snapshot = models.exportConfiguration();
    expect(models.restoreConfiguration(snapshot)).toBe(true);
    models.addNode(MAIN_WORKFLOW_ID, { x: 4, y: 8 });
    const restored = models.exportConfiguration();
    deliver(status === 201 ? response(snapshot.pending!.body.record, status)
      : response({ error: { reason_code: "stale_revision" } }, status));
    expect(await saving).toBe(false);
    expect(models.exportConfiguration()).toEqual(restored);
    expect(models.providers).toEqual([]);
    expect(models.error).toBeNull();
    expect(guard).toHaveBeenCalledTimes(1);
  });

  it.each([201, 409])("checks the whole pending body, not just the key, for HTTP %i", async status => {
    const models = useModelConfigurationStore();
    models.setPersistenceGuard(() => true);
    models.editProvider(provider());
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; })));
    const saving = models.saveProvider();
    const original = models.exportConfiguration().pending!;
    const replacement = structuredClone(original);
    if (!("provider_id" in replacement.body.record)) throw new Error("Provider request expected");
    replacement.body.record.name = "new pending body";
    models.pending = replacement;
    deliver(status === 201 ? response(original.body.record, status)
      : response({ error: { reason_code: "stale_revision" } }, status));
    expect(await saving).toBe(false);
    expect(models.pending).toEqual(replacement);
    expect(models.providers).toEqual([]);
    expect(models.error).toBeNull();
  });

  it("does not update restored model drafts or request diagnostics from their late receipt", async () => {
    const models = useModelConfigurationStore();
    models.setPersistenceGuard(() => true);
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    let deliver!: (value: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const saving = models.saveModels(MAIN_WORKFLOW_ID);
    const snapshot = models.exportConfiguration();
    expect(models.restoreConfiguration(snapshot)).toBe(true);
    models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.temperature = 0.7;
    const restored = models.exportConfiguration();
    deliver(response(snapshot.pending!.body.record));
    expect(await saving).toBe(false);
    expect(models.exportConfiguration()).toEqual(restored);
    expect(models.isDraftSaved(MAIN_WORKFLOW_ID)).toBe(false);
    expect(models.error).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not diagnose a model receipt when persistence restores the original request during settlement", async () => {
    const models = useModelConfigurationStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 });
    let original = models.exportConfiguration();
    let writes = 0;
    models.setPersistenceGuard(() => {
      if (++writes === 1) original = models.exportConfiguration();
      else models.restoreConfiguration(original);
      return true;
    });
    const fetcher = vi.fn(async (_path: string, options: RequestInit) =>
      response(JSON.parse(options.body as string).record));
    vi.stubGlobal("fetch", fetcher);
    expect(await models.saveModels(MAIN_WORKFLOW_ID)).toBe(false);
    expect(models.exportConfiguration()).toEqual(original);
    expect(models.error).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("keeps provider edits made while their first request is in flight", async () => {
    const models = useModelConfigurationStore();
    models.setPersistenceGuard(() => true);
    models.editProvider(provider());
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; })));
    const saving = models.saveProvider();
    const command = models.exportConfiguration().pending!;
    models.providerForm!.name = "later edit";
    deliver(response(command.body.record));
    expect(await saving).toBe(true);
    expect(models.providerForm?.name).toBe("later edit");
    expect(models.pending).toBeNull();
  });
});
