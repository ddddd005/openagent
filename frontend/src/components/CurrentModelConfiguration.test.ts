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
import { createModelProfiles, modelProfilesKey } from "../application/modelProfiles";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { newProvider, newModelProfile, modelProfileIdentity, type CurrentProvider } from "../domain/workflowModelResources";
import type { GraphDocument, GraphNode, GraphNodeType } from "../domain/workflowGraph";

function fixture(provider: CurrentProvider | null = newProvider()) {
  const fallback = provider ?? newProvider(), workflowId = crypto.randomUUID();
  const profile = newModelProfile(fallback);
  profile.value.name = "Named shared profile"; profile.value.model.id = "chosen-model";
  profile.value.model.capacity = { context_window_tokens: 128000, input_limit_tokens: 120000, max_output_tokens: 8192 };
  profile.value.runtime.context_length_tokens = 128000;
  profile.value.runtime.output_limit_tokens = 4096;
  const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: "models.source", component_version: "7",
    title: "Model source", position: { x: 0, y: 0 }, config: { reference: modelProfileIdentity(profile) } };
  const document: GraphDocument = { schema_version: 2, workflow_definition_id: workflowId, revision: 1, name: "config",
    nodes: [node], edges: [], object_bindings: [], control_edges: [],
    package_lock: [{ package_id: "workflow.content", version: "1.0.0" }, { package_id: "workflow.models", version: "1.0.0" }] };
  const definition: GraphNodeType = { component_id: "models.source", component_version: "7", display_name: "Model source",
    category: "Models", default_config: node.config, inputs: [],
    outputs: [{ port_id: "output", data_type: "MODEL_BINDING", required: true, multiple: false }],
    executable: true, is_output: false, input_storage: "references", capabilities: ["models:resolve", "resources:read"],
    config_schema: { type: "object", required: ["reference"], additionalProperties: false,
      properties: { reference: { type: "object" } } } };
  const resources = createProviderResources({ list: async () => [], save: async () => ({}), readReceipt: vi.fn(),
    readPending: () => null, writePending: () => {} });
  resources.records.value = provider ? [provider] : [];
  const profiles = createModelProfiles({ list: async () => [], save: async () => ({}), readReceipt: vi.fn(),
    readPending: () => null, writePending: () => {} });
  profiles.records.value = provider ? [profile] : [];
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => workflowId), document: computed(() => document), catalog: computed(() => [definition]),
    packages: computed(() => [{ package_id: "workflow.models", version: "1.0.0" }]), session: computed(() => null),
    locked: computed(() => false), lifecycle: computed(() => "fixed"),
    configureNode: vi.fn(async () => ({ workflowId, nodeId: node.node_binding_id, outcome: "saved" as const })),
  };
  const host = createWorkbenchFrontendHost(() => modelFrontendExtensions, () => sdk.packages.value);
  return { node, document, definition, sdk, host, resources, profiles, profile };
}
describe("current model package UI", () => {
  it("renders only a shared configuration selector, never Key, capacity, parameters or discovery", async () => {
    const f = fixture(), pinia = createPinia();
    const app = createSSRApp(GraphNodeConfiguration, { node: f.node, document: f.document, definition: f.definition })
      .use(pinia).provide(workbenchFrontendHostKey, f.host).provide(workflowFrontendSdkKey, f.sdk)
      .provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles);
    const html = await renderToString(app);
    expect(html).toContain("全局模型配置"); expect(html).toContain("chosen-model");
    expect(html).toContain("保存模型引用");
    expect(html).not.toContain("上下文窗口"); expect(html).not.toContain("获取上游");
    expect(html).not.toContain("<textarea"); expect(html).not.toContain('type="password"');
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
    pinia._s.forEach(store => store.$dispose());
  });
  it.each(["missing", "disabled"] as const)("renders the actual %s profile diagnosis", async mode => {
    const record = newProvider();
    const f = fixture(mode === "missing" ? null : record);
    if (mode === "disabled") f.profile.value.enabled = false;
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain(mode === "missing" ? "全局模型配置缺失" : "已停用");
  });
  it("loads exact models declarations and unmounts them when the package or declaration changes", async () => {
    const f = fixture();
    expect(f.host.extensions.value.map(row => row.declaration.extension_id))
      .toEqual(["workflow.models.workbench-panel", "workflow.models.node-fields-v7"]);
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain("DeepSeek"); expect(html).toContain("模型配置");
    const mismatch = modelFrontendExtensions.map(row => ({ ...row, entrypoint: row.entrypoint + ".forged" }));
    const host = createWorkbenchFrontendHost(() => mismatch, () => f.sdk.packages.value);
    expect(host.extensions.value).toEqual([]);
    expect(createWorkbenchFrontendHost(() => modelFrontendExtensions, () => []).extensions.value).toEqual([]);
  });
  it("shows the empty resource state and keeps explicit creation available", async () => {
    const f = fixture(null);
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain("暂无供应商当前资源");
    expect(html).toContain('aria-label="新增供应商"');
    expect(html).not.toContain('aria-label="新增供应商" disabled');
  });
  it("does not describe resource-read or original-request failures as an empty provider list", async () => {
    const f = fixture(null);
    for (const message of ["供应商读取失败", "原资源请求无法读取，资源修改已禁用"]) {
      f.resources.error.value = message;
      const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
      expect(html).toContain(message); expect(html).not.toContain("暂无供应商当前资源");
    }
  });
  it("does not show the empty resource state while loading", async () => {
    const f = fixture(null); f.resources.loading.value = true;
    const html = await renderToString(createSSRApp(CurrentProviderPanel).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain("读取中"); expect(html).not.toContain("暂无供应商当前资源");
  });
  it("does not accept the retired inline configuration as a current workflow reference", async () => {
    const f = fixture();
    f.node.config = { ...f.node.config,
      runtime: { thinking: "disabled", context_length_tokens: 128000,
        compaction_threshold_bps: 8500, output_limit_tokens: 4096 },
      model: { id: "inline-model", capacity: { context_window_tokens: 128000, input_limit_tokens: 120000,
        max_output_tokens: 8192 } } };
    const app = createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles);
    const html = await renderToString(app);
    expect(html).toContain("模型配置引用无效");
    for (const label of ["上下文长度 tokens", "压缩阈值 %", "输出上限 tokens", "真实模型容量"])
      expect(html).not.toContain(label);
    expect(html).not.toContain("<textarea");
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
  it("keeps missing configuration explicit without guessing model or capacity", async () => {
    const f = fixture();
    f.node.config = { reference: { ...f.node.config.reference as object, resource_id: "" } };
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain("模型配置引用无效");
    expect(html).not.toContain('value="1024"'); expect(html).not.toContain('value="128"');
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
  it("selects a Gemini shared configuration without copying thinking settings into the node", async () => {
    const provider = newProvider();
    provider.value = { name: "Gemini", protocol: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta",
      credential_ref: `local:${crypto.randomUUID()}`, enabled: true };
    const f = fixture(provider);
    f.profile.value.model.id = "gemini-3-flash-preview";
    f.profile.value.runtime.thinking = { mode: "level", level: "medium", include_summary: true };
    const html = await renderToString(createSSRApp(ModelSourceFields, { node: f.node, document: f.document })
      .provide(workflowFrontendSdkKey, f.sdk).provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles));
    expect(html).toContain("gemini-3-flash-preview");
    expect(html).not.toContain('aria-label="思考模式"'); expect(html).not.toContain('aria-label="思考强度"');
    expect(html).not.toContain("思考预算 tokens");
    expect(html).not.toContain("上下文窗口 tokens");
    expect(f.sdk.configureNode).not.toHaveBeenCalled();
  });
});
