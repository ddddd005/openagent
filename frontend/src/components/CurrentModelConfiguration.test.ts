import { computed, createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { describe, expect, it, vi } from "vitest";
import ModelSourceFields from "./ModelSourceFields.vue";
import CurrentProviderPanel from "./CurrentProviderPanel.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { newProvider, providerIdentity, type CurrentProvider } from "../domain/workflowModelResources";
import type { GraphDocument, GraphNode, GraphNodeType } from "../domain/workflowGraph";

function fixture(provider: CurrentProvider | null = newProvider()) {
  const fallback = provider ?? newProvider(), workflowId = crypto.randomUUID();
  const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: "models.source", component_version: "1",
    title: "Model source", position: { x: 0, y: 0 }, config: {
      reference: providerIdentity(fallback), parameters: { model: "chosen-model", thinking: "disabled", stream: false } } };
  const document: GraphDocument = { schema_version: 2, workflow_definition_id: workflowId, revision: 1, name: "config",
    nodes: [node], edges: [], object_bindings: [], execution_roots: [node.node_binding_id], control_edges: [],
    package_lock: [{ package_id: "workflow.content", version: "1.0.0" }, { package_id: "workflow.models", version: "1.0.0" }] };
  const definition: GraphNodeType = { component_id: "models.source", component_version: "1", display_name: "Model source",
    category: "Models", default_config: node.config, inputs: [],
    outputs: [{ port_id: "output", data_type: "MODEL_BINDING", required: true, multiple: false }],
    executable: true, is_output: false, input_storage: "references", capabilities: ["models:resolve", "resources:read"],
    config_schema: { type: "object", required: ["reference", "parameters"], additionalProperties: false,
      properties: { reference: { type: "object" }, parameters: { type: "object" } } } };
  const resources = createProviderResources({ list: async () => [], save: async () => ({}), readReceipt: vi.fn(),
    readPending: () => null, writePending: () => {} });
  resources.records.value = provider ? [provider] : [];
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => workflowId), document: computed(() => document), catalog: computed(() => [definition]),
    packages: computed(() => [{ package_id: "workflow.models", version: "1.0.0" }]), session: computed(() => null),
    locked: computed(() => false), lifecycle: computed(() => "fixed"),
    attachDisplay: () => false, readArtifact: vi.fn(), configureNode: vi.fn(() => ({ workflowId, nodeId: node.node_binding_id })),
  };
  const host = createWorkbenchFrontendHost(() => modelFrontendExtensions, () => sdk.packages.value);
  return { node, document, definition, sdk, host, resources };
}
describe("current model package UI", () => {
  it("renders the source selector with structured parameters and no duplicate JSON fields or legacy catalog reads", async () => {
    const f = fixture(), pinia = createPinia();
    const app = createSSRApp(GraphNodeConfiguration, { node: f.node, document: f.document, definition: f.definition })
      .use(pinia).provide(workbenchFrontendHostKey, f.host).provide(workflowFrontendSdkKey, f.sdk)
      .provide(modelResourceControllerKey, f.resources);
    const html = await renderToString(app);
    expect(html).toContain("供应商当前资源"); expect(html).toContain("chosen-model");
    expect(html).toContain("应用模型配置"); expect(html).toContain("运行准备时核实");
    expect(html).not.toContain("<textarea"); expect(html).not.toContain('type="password"');
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
    pinia._s.forEach(store => store.$dispose());
  });
  it.each(["missing", "disabled", "credential"] as const)("renders the actual %s resource diagnosis", async mode => {
    const record = newProvider();
    if (mode === "disabled") record.value.enabled = false;
    if (mode === "credential") record.value.credential_ref = null;
    const f = fixture(mode === "missing" ? null : record);
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain(mode === "missing" ? "缺失引用" : mode === "disabled" ? "已停用" : "未配置后端凭据引用");
  });
  it("loads exact models declarations and unmounts them when the package or declaration changes", async () => {
    const f = fixture();
    expect(f.host.extensions.value.map(row => row.declaration.extension_id))
      .toEqual(["workflow.models.workbench-panel", "workflow.models.node-fields", "workflow.models.node-fields-v2",
        "workflow.models.node-fields-v3", "workflow.models.node-fields-v4"]);
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain("DeepSeek"); expect(html).toContain("当前资源");
    const mismatch = modelFrontendExtensions.map(row => ({ ...row, entrypoint: row.entrypoint + ".forged" }));
    const host = createWorkbenchFrontendHost(() => mismatch, () => f.sdk.packages.value);
    expect(host.extensions.value).toEqual([]);
    expect(createWorkbenchFrontendHost(() => modelFrontendExtensions, () => []).extensions.value).toEqual([]);
  });
  it("shows the empty resource state and keeps explicit creation available", async () => {
    const f = fixture(null);
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain("暂无供应商当前资源");
    expect(html).toContain('aria-label="新增供应商资源"');
    expect(html).not.toContain('aria-label="新增供应商资源" disabled');
  });
  it("does not describe resource-read or original-request failures as an empty provider list", async () => {
    const f = fixture(null);
    for (const message of ["供应商读取失败", "原资源请求无法读取，资源修改已禁用"]) {
      f.resources.error.value = message;
      const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources));
      expect(html).toContain(message); expect(html).not.toContain("暂无供应商当前资源");
    }
  });
  it("does not show the empty resource state while loading", async () => {
    const f = fixture(null); f.resources.loading.value = true;
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain("读取中"); expect(html).not.toContain("暂无供应商当前资源");
  });
  it("renders source two capacity controls without duplicate raw configuration fields", async () => {
    const f = fixture();
    f.node.component_version = "2"; f.definition.component_version = "2";
    f.node.config = { ...f.node.config, parameters: { ...f.node.config.parameters as object, max_tokens: 1024 },
      capacity: { context_window_tokens: 128000, output_reserve_tokens: 1024,
        summary_max_tokens: 128, max_cold_input_tokens: 128000 } };
    const app = createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources);
    const html = await renderToString(app);
    for (const label of ["上下文窗口 tokens", "输出预留 tokens", "摘要最大输出 tokens", "摘要冷输入上限 tokens"])
      expect(html).toContain(`aria-label="${label}"`);
    expect(html).toContain('value="128000"'); expect(html).toContain('value="1024"');
    expect(html).not.toContain("<textarea");
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
  it("preserves explicit budget defaults while provider and window are still unconfigured", async () => {
    const f = fixture();
    f.node.component_version = "2";
    f.node.config = { reference: { ...f.node.config.reference as object, resource_id: "" },
      parameters: { model: "", thinking: "disabled", stream: false, max_tokens: 1024 },
      capacity: { context_window_tokens: 0, output_reserve_tokens: 1024,
        summary_max_tokens: 128, max_cold_input_tokens: 0 } };
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain('value="1024"'); expect(html).toContain('value="128"'); expect(html).toContain('value="0"');
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
  it.each(["3", "4"])("renders Gemini v%s thinking controls and preserves explicit settings", async version => {
    const provider = newProvider();
    provider.value = { name: "Gemini", protocol: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta",
      credential_ref: "env:GEMINI_API_KEY", enabled: true };
    provider.data_schema_version = 2;
    const f = fixture(provider); f.node.component_version = version;
    f.node.config = { reference: providerIdentity(provider),
      parameters: { model: "gemini-3-flash-preview", thinking: { mode: "level", level: "medium", include_summary: true },
        stream: false, max_tokens: 4096 },
      ...(version === "4" ? { capacity: { context_window_tokens: 128000, output_reserve_tokens: 4096,
        summary_max_tokens: 128, max_cold_input_tokens: 128000 } } : {}) };
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources));
    expect(html).toContain('aria-label="思考模式"'); expect(html).toContain('aria-label="思考强度"');
    expect(html).toMatch(/<option value="medium"[^>]* selected>/); expect(html).toContain('aria-label="请求思考摘要"');
    expect(html).not.toContain("思考预算 tokens");
    expect(html.includes("上下文窗口 tokens")).toBe(version === "4");
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
});
