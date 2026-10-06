import { afterEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia, type Pinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import ProviderSidebar from "./ProviderSidebar.vue";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { graphClone } from "../domain/workflowGraph";
import { newProvider } from "../domain/workflowModelResources";
import { EMPTY_WORKFLOW_ID } from "../fixtures/workflows";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

const stores: Pinia[] = [];
function fixture(generic = true) {
  const pinia = createPinia();
  stores.push(pinia);
  const workspace = useWorkspaceStore(pinia), graph = useWorkflowGraphStore(pinia);
  if (generic) {
    graph.ensureEmpty(EMPTY_WORKFLOW_ID);
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
  }
  graph.frontendExtensions = graphClone(modelFrontendExtensions);
  graph.packageLock = [{ package_id: "workflow.models", version: "1.0.0" }];
  const provider = newProvider();
  provider.value.name = "Current provider";
  const resources = createProviderResources({
    list: vi.fn(async () => [provider]), save: vi.fn(async () => ({})), readReceipt: vi.fn(),
    readPending: () => null, writePending: vi.fn(),
  });
  resources.records.value = [provider];
  const render = () => renderToString(createSSRApp(ProviderSidebar).use(pinia)
    .provide(modelResourceControllerKey, resources));
  return { pinia, workspace, graph, resources, render };
}
afterEach(() => {
  stores.splice(0).forEach(pinia => pinia._s.forEach(store => store.$dispose()));
  vi.unstubAllGlobals();
});

describe("provider sidebar architecture routing", () => {
  it("uses the trusted model package panel for an ordinary graph without creating the legacy model store", async () => {
    const f = fixture(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const html = await f.render();
    expect(html).toContain('aria-label="普通图供应商"');
    expect(html).toContain("模型供应商 · 当前资源");
    expect(html).toContain("Current provider");
    expect(html).not.toContain('aria-label="现有 Chat 适配器能力"');
    expect(f.pinia._s.has("model-configuration")).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("locks provider editing for resource mutations but not for workflow mutations", async () => {
    const f = fixture();
    f.graph.busy = "workflow mutation";
    expect(f.graph.locked).toBe(true);
    expect(await f.render()).not.toMatch(/aria-label="新增供应商资源"[^>]*disabled/);
    f.resources.busy.value = true;
    expect(await f.render()).toMatch(/aria-label="新增供应商资源"[^>]*disabled/);
    expect(f.graph.locked).toBe(true);
  });

  it.each(["package missing", "package version", "declaration missing", "entrypoint", "declaration", "duplicate"] as const)(
    "makes an unavailable %s explicit without falling back to the legacy provider panel",
    async mode => {
      const f = fixture(), fetcher = vi.fn();
      vi.stubGlobal("fetch", fetcher);
      if (mode === "package missing") f.graph.packageLock = [];
      if (mode === "package version") f.graph.packageLock[0]!.version = "2.0.0";
      if (mode === "declaration missing") f.graph.frontendExtensions = [];
      if (mode === "entrypoint") f.graph.frontendExtensions[0]!.entrypoint += ".forged";
      if (mode === "declaration") f.graph.frontendExtensions[0]!.binding.target = { forged: true };
      if (mode === "duplicate") f.graph.frontendExtensions.push(graphClone(f.graph.frontendExtensions[0]!));
      const html = await f.render();
      expect(html).toContain("供应商资源不可用");
      expect(html).not.toContain("模型供应商 · 当前资源");
      expect(html).not.toContain("Current provider");
      expect(f.pinia._s.has("model-configuration")).toBe(false);
      expect(fetcher).not.toHaveBeenCalled();
    },
  );

  it("reports catalog loading and failure without treating an ordinary graph as a legacy workflow", async () => {
    const f = fixture();
    f.graph.frontendExtensions = [];
    f.graph.catalogLoading = true;
    expect(await f.render()).toContain("读取中");
    f.graph.catalogLoading = false;
    f.graph.catalogError = "节点目录读取失败";
    expect(await f.render()).toContain("节点目录读取失败");
    expect(f.pinia._s.has("model-configuration")).toBe(false);
  });

  it("keeps the legacy panel and its exact pending provider mutation for a legacy workflow", async () => {
    const f = fixture(false), models = useModelConfigurationStore(f.pinia);
    models.providersLoaded = true;
    models.pending = {
      path: "/api/model-configurations/provider",
      body: {
        record: {
          schema_version: 1, kind: "chat_provider", provider_id: crypto.randomUUID(), revision: 1,
          name: "Original legacy provider", protocol: "chat", base_url: "https://api.deepseek.com",
          credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
        },
        expected_revision: 0, idempotency_key: crypto.randomUUID(),
      },
    };
    const original = graphClone(models.pending);
    const html = await f.render();
    expect(html).toContain('aria-labelledby="provider-heading"');
    expect(html).toContain("提交结果待核实");
    expect(html).not.toContain("模型供应商 · 当前资源");
    expect(models.pending).toEqual(original);
    f.graph.ensureEmpty(EMPTY_WORKFLOW_ID);
    f.workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    expect(await f.render()).toContain("模型供应商 · 当前资源");
    expect(models.pending).toEqual(original);
  });
});
