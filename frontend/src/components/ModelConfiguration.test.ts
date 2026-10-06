import { describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import ProviderPanel from "./ProviderPanel.vue";
import ModelProviderNode from "./ModelProviderNode.vue";
import WorkbenchCanvas from "./WorkbenchCanvas.vue";
import App from "../App.vue";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { dependencyTarget } from "../domain/modelConfiguration";
import { legacyWorkbenchRaw } from "../testUtils/legacyWorkbenchStorage";

vi.mock("@vue-flow/core", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" }, Position: { Left: "left", Right: "right" },
  Handle: { render: () => null }, VueFlow: { render: () => null },
}));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));

describe("model configuration component rendering", () => {
  it("renders global provider metadata and a reference-only credential control", async () => {
    const pinia = createPinia();
    const models = useModelConfigurationStore(pinia);
    models.editProvider(null);
    const html = await renderToString(createSSRApp(ProviderPanel).use(pinia));
    expect(html).toContain("供应商");
    expect(html).toContain("全局");
    expect(html).toContain("Chat Completions");
    expect(html).toContain("DEEPSEEK_API_KEY");
    expect(html).not.toContain('type="password"');
    models.$dispose();
  });
  it("renders a model node and its explicit A dependency separately from the fixed main graph", async () => {
    const pinia = createPinia();
    const models = useModelConfigurationStore(pinia);
    const id = models.addNode(MAIN_WORKFLOW_ID, { x: 120, y: -80 })!;
    models.connect(MAIN_WORKFLOW_ID, id, dependencyTarget("A"), "model-out", "model-in");
    const html = await renderToString(createSSRApp(ModelProviderNode, {
      id, selected: true, data: { node: models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0] },
    }).use(pinia));
    expect(html).toContain("模型提供");
    expect(html).toContain("Agent A");
    expect(html).toContain("max_tokens");
    expect(html).toContain("未保存");
    expect(html).not.toContain("运行接入待完成");
    const canvas = await renderToString(createSSRApp(WorkbenchCanvas).use(pinia));
    expect(canvas).toContain("4 节点");
    expect(canvas).toContain("3 连线");
    expect(useWorkspaceStore(pinia).nodes).toHaveLength(3);
    expect(useWorkspaceStore(pinia).edges).toHaveLength(2);
    models.$dispose();
  });
  it("renders sidebar navigation without removing the current workbench", async () => {
    const pinia = createPinia();
    const workspace = useWorkspaceStore(pinia);
    let raw: string | null = legacyWorkbenchRaw();
    const persistence = useWorkbenchPersistenceStore(pinia);
    persistence.initialize({ getItem: () => raw, setItem: (_key, value) => { raw = value; } });
    workspace.sidebarSection = "providers";
    const html = await renderToString(createSSRApp(App).use(pinia));
    expect(html).toContain('aria-label="供应商" aria-current="page"');
    expect(html).toContain('aria-label="工作流节点画布"');
    expect(html).toContain("默认测试工作流");
    expect(html).toContain('aria-label="工作流列表"');
    persistence.$dispose();
    useModelConfigurationStore(pinia).$dispose();
  });
  it("renders safe inline provider validation and read-only existing adapter capabilities", async () => {
    const pinia = createPinia();
    const models = useModelConfigurationStore(pinia);
    models.editProvider(null);
    models.providerForm!.base_url = "http://remote.test";
    const html = await renderToString(createSSRApp(ProviderPanel).use(pinia));
    expect(html).toContain("provider_address_invalid");
    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain('aria-label="现有 Chat 适配器能力"');
    expect(html).toContain("disabled");
    expect(html).not.toContain("type=\"password\"");
    models.$dispose();
  });
  it("renders historical provider binding without silently rebinding to the current revision", async () => {
    const pinia = createPinia();
    const models = useModelConfigurationStore(pinia);
    const id = models.addNode(MAIN_WORKFLOW_ID, { x: 0, y: 0 })!;
    const node = models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0];
    const providerId = "00000000-0000-4000-8000-000000000201";
    node.provider_ref = { provider_id: providerId, revision: 1 };
    models.providersLoaded = true;
    models.providers = [{
      schema_version: 1, kind: "chat_provider", provider_id: providerId, revision: 2,
      name: "DeepSeek", protocol: "chat", base_url: "https://api.deepseek.com",
      credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
    }];
    node.parameters.max_tokens = 9000;
    const render = () => renderToString(createSSRApp(ModelProviderNode, { id, data: { node } }).use(pinia));
    let html = await render();
    expect(html).toContain("绑定 r1 · 最新 r2");
    expect(html).toContain("model_max_tokens_invalid");
    expect(html).toContain('max="8192"');
    expect(html).toContain("非流式");
    expect(node.provider_ref).toEqual({ provider_id: providerId, revision: 1 });
    models.providers[0].enabled = false;
    html = await render();
    expect(html).toContain("provider_unavailable");
    expect(html).toContain("供应商已停用");
    expect(node.provider_ref.revision).toBe(1);
    models.$dispose();
  });
});
