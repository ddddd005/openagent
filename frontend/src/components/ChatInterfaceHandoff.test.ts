import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import App from "../App.vue";
import WorkflowSidebar from "./WorkflowSidebar.vue";
import { graphClone } from "../domain/workflowGraph";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";

vi.mock("./WorkbenchSidebar.vue", () => ({ default: { render: () => null } }));
vi.mock("./WorkflowWorkspace.vue", () => ({ default: { render: () => null } }));
vi.mock("./GraphRunControls.vue", () => ({ default: { render: () => null } }));
vi.mock("./GraphWorkbench.vue", () => ({ default: { render: () => null } }));
vi.mock("./GraphSessions.vue", () => ({ default: { render: () => null } }));
vi.mock("../composables/useWorkflowFrontendHost", () => ({ useWorkflowFrontendHost: () => {} }));
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.stubGlobal("fetch", vi.fn()); });
afterEach(() => { disposePinia(pinia); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function fixture() {
  let raw: string | null = null;
  const storage = { getItem: () => raw, setItem: vi.fn((_key: string, value: string) => { raw = value; }) };
  await useWorkbenchPersistenceStore().initialize(storage, memoryLocalDraftStorage().ports);
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  vi.spyOn(graph, "activate").mockResolvedValue();
  vi.spyOn(graph, "loadDefinitions").mockResolvedValue(false);
  vi.spyOn(useWorkbenchPersistenceStore(), "selectLoadedWorkflow").mockImplementation(() => false);
  graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const mutations = [vi.spyOn(graph, "createSession"), vi.spyOn(graph, "selectSession"), vi.spyOn(graph, "submitPrimary")];
  return { graph, mutations, storage };
}
describe("ordinary workbench entry retirement", () => {
  it.each(["unsaved", "saved", "pending"] as const)(
    "renders %s without a workflow-only chat link or any opening side effect", async state => {
      const f = await fixture(), entry = f.graph.active!;
      if (state !== "unsaved") { entry.saved_revision = 1; entry.saved_document = graphClone(entry.document); }
      if (state === "saved") entry.session_id = crypto.randomUUID();
      if (state === "pending") entry.pending = { action: "save", path: "/api/graph/definitions",
        body: { document: graphClone(entry.document), expected_revision: 1, idempotency_key: crypto.randomUUID() } };
      const before = graphClone(f.graph.storeSnapshot()), pending = entry.pending;
      const writes = f.storage.setItem.mock.calls.length;
      const html = await renderToString(createSSRApp(App).use(pinia));
      expect(html).not.toContain("打开聊天前端");
      expect(html).not.toContain("workbench-interface-rail");
      expect(html).not.toContain("graph_workflow=");
      expect(f.graph.storeSnapshot()).toEqual(before);
      expect(entry.pending).toBe(pending);
      expect(f.storage.setItem).toHaveBeenCalledTimes(writes);
      expect(fetch).not.toHaveBeenCalled();
      f.mutations.forEach(spy => expect(spy).not.toHaveBeenCalled());
    });
  it("offers ordinary workflow creation without Tavern, serial Agent or compacting presets", async () => {
    const f = await fixture(), before = graphClone(f.graph.storeSnapshot());
    const html = await renderToString(createSSRApp(WorkflowSidebar).use(pinia));
    expect(html).toContain('aria-label="新建工作流"');
    for (const label of ["创建酒馆工作流", "创建串行 Agent 示例", "创建上下文精简串行示例"])
      expect(html).not.toContain(label);
    for (const method of ["createWorkflowDraft", "createSerialAgentExample", "createCompactingSerialAgentExample", "createSavedSession"])
      expect(method in f.graph).toBe(false);
    expect(f.graph.storeSnapshot()).toEqual(before);
    expect(fetch).not.toHaveBeenCalled();
  });
});
