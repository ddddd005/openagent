import { readFileSync } from "node:fs";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ScriptTarget, ModuleKind, transpileModule } from "typescript";
import { afterEach, describe, expect, it, vi } from "vitest";
import CurrentProviderPanel from "./CurrentProviderPanel.vue";
import CurrentModelProfilesPanel from "./CurrentModelProfilesPanel.vue";
import RawNumberInput from "./RawNumberInput.vue";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { createModelProfiles, modelProfilesKey } from "../application/modelProfiles";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { localDraftPorts } from "../adapters/localDraftsApi";
import * as providerConfigurationApi from "../adapters/providerConfigurationApi";
import * as workflowModelProfilesApi from "../adapters/workflowModelProfilesApi";
import { newModelProfile, newProvider, type CurrentProvider } from "../domain/workflowModelResources";

vi.mock("./ProviderCredentialFields.vue", async () => {
  const { defineComponent, h } = await import("vue");
  return { default: defineComponent({
    props: ["owner", "reference", "basis", "disabled", "acceptSaved"],
    emits: ["blocked", "mutation", "dirty"],
    setup(_props, { emit }) {
      return () => h("button", { type: "button", "aria-label": "模拟凭据未决",
        onClick: () => emit("mutation", true) }, "模拟凭据未决");
    },
  }) };
});

class HostNode extends EventTarget {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  value: unknown = "";
  checked = false;
  selected = false;
  selectedIndex = -1;
  _value?: unknown;
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  get tagName() { return this.tag.toUpperCase(); }
  get type() { return String(this.props.type ?? ""); }
  get multiple() { return !!this.props.multiple; }
  get options(): HostNode[] { return this.children.flatMap(child => child.tag === "option" ? [child] : child.options); }
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert: (child, parent, anchor = null) => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child);
  },
  remove: node => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _previous, value) => {
    node.props[key] = value;
    if (key === "value") { node.value = value; node._value = value; }
  },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
function elements(root: HostNode): HostNode[] { return [root, ...root.children.flatMap(elements)]; }
function text(root: HostNode) { return elements(root).map(node => node.text).join(""); }
function find(root: HostNode, predicate: (node: HostNode) => boolean) {
  const found = elements(root).find(predicate);
  expect(found, "rendered control").toBeDefined();
  return found!;
}
const labeled = (root: HostNode, label: string) => find(root, node => node.props["aria-label"] === label);
const namedButton = (root: HostNode, name: string) => find(root, node => node.tag === "button" && text(node).includes(name));
async function flush() { for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick(); } }
function event(node: HostNode, name: "Click" | "Submit") {
  return (node.props[`on${name}`] as (event: { preventDefault(): void }) => Promise<unknown> | undefined)({ preventDefault() {} });
}
async function input(node: HostNode, value: string) {
  node.value = value;
  node.dispatchEvent(new Event("input"));
  const listener = node.props.onInput as ((event: { target: HostNode }) => void) | undefined;
  listener?.({ target: node });
  await flush();
}
const field = (root: HostNode, name: string) => find(
  find(root, node => node.tag === "label" && text(node).includes(name)), node => node.tag === "input");
function browserMemoryOnly() {
  vi.stubGlobal("window", { confirm: vi.fn(() => true) });
  const storage = { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() };
  vi.stubGlobal("localStorage", storage);
  const draft = { read: vi.spyOn(localDraftPorts, "read"), save: vi.spyOn(localDraftPorts, "save"),
    receipt: vi.spyOn(localDraftPorts, "receipt") };
  return () => {
    for (const method of Object.values(draft)) expect(method).not.toHaveBeenCalled();
    for (const method of Object.values(storage)) expect(method).not.toHaveBeenCalled();
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (failure: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
function compile(component: typeof CurrentProviderPanel | typeof CurrentModelProfilesPanel | typeof RawNumberInput, filename: string) {
  const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  const code = transpileModule(template.code, { compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None } }).outputText;
  return { ...component, render: new Function("Vue", code)(Vue) };
}
function fixture(sharedProvider = false) {
  const provider = newProvider();
  provider.value.name = "Provider being saved";
  provider.value.credential_ref = `local:${crypto.randomUUID()}`;
  const unrelated: CurrentProvider = { ...newProvider(), scope: "project", resource_id: provider.resource_id };
  unrelated.value.name = "Independent provider";
  unrelated.value.credential_ref = `local:${crypto.randomUUID()}`;
  const profile = newModelProfile(sharedProvider ? provider : unrelated);
  profile.value.name = "Independent model";
  profile.value.model.id = "offline-model";
  profile.value.model.capacity = { context_window_tokens: 32768, input_limit_tokens: 30000, max_output_tokens: 16384 };
  profile.value.runtime.context_length_tokens = 16000;
  profile.value.runtime.output_limit_tokens = 4096;
  const gate = deferred<unknown>(), providerSave = vi.fn(() => gate.promise);
  const modelSave = vi.fn(async (_request: workflowModelProfilesApi.ModelProfileSaveRequest) => ({}));
  let providerRaw: string | null = null, modelRaw: string | null = null;
  const providers = [provider, unrelated];
  const resources = createProviderResources({ list: async () => providers, save: providerSave, readReceipt: vi.fn(),
    readPending: () => providerRaw, writePending: value => { providerRaw = value; } });
  const modelPorts = { list: async () => [profile], save: modelSave, readReceipt: vi.fn(),
    readPending: () => modelRaw, writePending: (value: string | null) => { modelRaw = value; } };
  const profiles = createModelProfiles(modelPorts);
  return { provider, providers, profile, gate, providerSave, modelSave, resources, profiles,
    modelPorts, providerRaw: () => providerRaw, modelRaw: () => modelRaw };
}
async function mount(f: ReturnType<typeof fixture>, modelOnly = false) {
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  Object.assign(CurrentModelProfilesPanel, { render: compile(CurrentModelProfilesPanel, "CurrentModelProfilesPanel.vue").render });
  Object.assign(RawNumberInput, { render: compile(RawNumberInput, "RawNumberInput.vue").render });
  const app = modelOnly
    ? renderer.createApp(CurrentModelProfilesPanel, { providers: f.providers })
    : renderer.createApp(compile(CurrentProviderPanel, "CurrentProviderPanel.vue"));
  app.provide(modelResourceControllerKey, f.resources).provide(modelProfilesKey, f.profiles).provide(Vue.ssrContextKey, {});
  const root = new HostNode("root"); apps.push(app); app.mount(root);
  await flush();
  return root;
}
afterEach(() => { for (const app of apps.splice(0)) app.unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("provider and named model management ownership", () => {
  it("edits only memory and submits one complete configuration explicitly", async () => {
    const noDraftWrites = browserMemoryOnly();
    const f = fixture(), root = await mount(f, true);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    expect(text(root)).not.toMatch(/温度|输出预留|摘要最大输出|摘要冷输入/);
    expect(field(root, "输出上限 tokens").props.max).toBeUndefined();
    await input(field(root, "配置名称"), "Edited profile");
    await input(field(root, "模型名称"), "edited-model");
    await input(field(root, "输出上限 tokens"), "12000");
    await input(field(root, "压缩阈值 %"), "-");
    expect(f.modelSave).not.toHaveBeenCalled(); expect(f.modelRaw()).toBeNull();
    await event(find(root, node => node.tag === "form"), "Submit"); await flush();
    expect(f.modelSave).not.toHaveBeenCalled();
    expect(text(root)).toContain("数字参数未填写完整或超出范围");
    await input(field(root, "压缩阈值 %"), "85.5");
    await event(find(root, node => node.tag === "form"), "Submit"); await flush();
    expect(f.modelSave).toHaveBeenCalledOnce();
    const request = f.modelSave.mock.calls[0]![0]!;
    expect(request).toMatchObject({ expected_sequence: 1, record: {
      resource_id: f.profile.resource_id, update_sequence: 2, value: {
        name: "Edited profile", provider: f.profile.value.provider,
        model: { id: "edited-model", capacity: {
          context_window_tokens: 32768, input_limit_tokens: 30000, max_output_tokens: 16384,
        } },
        runtime: { thinking: "disabled", context_length_tokens: 16000,
          compaction_threshold_bps: 8550, output_limit_tokens: 12000 }, enabled: true,
      },
    } });
    expect(request.record).not.toHaveProperty("raw");
    expect(elements(root).some(node => node.tag === "form")).toBe(false);
    expect(f.modelRaw()).toBeNull();
    noDraftWrites();
  });

  it("keeps a rejected form in memory and discards it when explicitly closed", async () => {
    const noDraftWrites = browserMemoryOnly();
    const f = fixture(), root = await mount(f, true);
    f.modelSave.mockRejectedValueOnce(new WorkbenchApiError("rejected", "Model save rejected"));
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    await input(field(root, "配置名称"), "Unsaved profile");
    await event(find(root, node => node.tag === "form"), "Submit"); await flush();
    expect(field(root, "配置名称").value).toBe("Unsaved profile");
    expect(text(root)).toContain("Model save rejected");
    expect(f.profiles.locked.value).toBe(false); expect(f.modelRaw()).toBeNull();
    await event(labeled(root, "关闭模型配置编辑"), "Click"); await flush();
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    expect(field(root, "配置名称").value).toBe(f.profile.value.name);
    expect(elements(root).some(node => node.props["aria-label"] === "未提交模型配置草稿")).toBe(false);
    expect(f.modelSave).toHaveBeenCalledOnce();
    noDraftWrites();
  });

  it("drops invalid numeric text when switching to another saved model configuration", async () => {
    const noDraftWrites = browserMemoryOnly();
    const f = fixture(), root = await mount(f, true);
    const second = newModelProfile(f.providers[1]!);
    second.value.name = "Second model";
    second.value.model.id = "second-model";
    second.value.model.capacity = { ...f.profile.value.model.capacity };
    second.value.runtime.context_length_tokens = f.profile.value.runtime.context_length_tokens;
    second.value.runtime.output_limit_tokens = f.profile.value.runtime.output_limit_tokens;
    f.profiles.records.value.push(second);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    await input(field(root, "输出上限 tokens"), "-");
    expect(field(root, "输出上限 tokens").value).toBe("-");
    expect(f.modelSave).not.toHaveBeenCalled();
    await event(namedButton(root, second.value.name), "Click"); await flush();
    expect(field(root, "输出上限 tokens").value).toBe("4096");
    await event(find(root, node => node.tag === "form"), "Submit"); await flush();
    expect(f.modelSave).toHaveBeenCalledOnce();
    expect(f.modelSave.mock.calls[0]![0]).toMatchObject({ expected_sequence: 1,
      record: { resource_id: second.resource_id, value: { runtime: { output_limit_tokens: 4096 } } } });
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    expect(field(root, "输出上限 tokens").value).toBe("4096");
    noDraftWrites();
  });

  it("reloads effective configuration directly after confirmation without archiving a form", async () => {
    const noDraftWrites = browserMemoryOnly();
    const f = fixture(), root = await mount(f, true);
    const saved = { ...f.profile, update_sequence: 2,
      value: { ...f.profile.value, name: "Effective model" } };
    const readSaved = vi.spyOn(workflowModelProfilesApi, "listModelProfiles").mockResolvedValue([saved]);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    await input(field(root, "配置名称"), "Unsaved profile");
    await event(labeled(root, "重新读取生效模型配置"), "Click"); await flush();
    expect(readSaved).toHaveBeenCalledOnce();
    expect(field(root, "配置名称").value).toBe("Effective model");
    expect(text(root)).toContain("已读取生效模型配置");
    expect(f.modelSave).not.toHaveBeenCalled(); expect(f.modelRaw()).toBeNull();
    noDraftWrites();
  });

  it("reopens only unknown-request evidence after unmount and reconciles without restoring or resubmitting a form", async () => {
    const noDraftWrites = browserMemoryOnly();
    const f = fixture(), root = await mount(f, true);
    f.modelSave.mockRejectedValueOnce(new WorkbenchApiError("unknown", "Model outcome unknown"));
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    await input(field(root, "配置名称"), "Unknown submitted profile");
    await event(find(root, node => node.tag === "form"), "Submit"); await flush();
    const original = f.modelRaw();
    expect(original).not.toBeNull();
    apps.pop()!.unmount();
    const revived = { ...f, profiles: createModelProfiles(f.modelPorts) };
    const reopened = await mount(revived, true);
    expect(elements(reopened).some(node => node.tag === "form")).toBe(false);
    expect(text(reopened)).toContain("模型配置提交结果待核实，未重新提交");
    expect(revived.profiles.locked.value).toBe(true); expect(f.modelRaw()).toBe(original);
    await event(namedButton(reopened, "核实原请求"), "Click"); await flush();
    expect(revived.profiles.pending.value).toBeNull(); expect(f.modelRaw()).toBeNull();
    expect(f.modelPorts.readReceipt).toHaveBeenCalledOnce();
    expect(f.modelPorts.readReceipt).toHaveBeenCalledWith(JSON.parse(original!));
    expect(f.modelSave).toHaveBeenCalledOnce();
    expect(elements(reopened).some(node => node.tag === "form")).toBe(false);
    noDraftWrites();
  });

  it.each([false, true])("keeps model edits and saves available while its provider submission is active or unknown (shared=%s)", async shared => {
    const f = fixture(shared), root = await mount(f);
    await event(namedButton(root, f.provider.value.name), "Click"); await flush();
    const providerForm = find(root, node => node.tag === "form" && text(node).includes("服务地址"));
    const savingProvider = event(providerForm, "Submit");
    await flush();
    expect(f.providerSave).toHaveBeenCalledOnce(); expect(f.resources.locked.value).toBe(true);
    const providerOriginal = f.providerRaw();
    expect(labeled(root, "新增供应商").props.disabled).toBe(true);
    expect(labeled(root, "新增模型配置").props.disabled).toBe(false);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    const modelPanel = labeled(root, "全局模型配置"), modelForm = find(modelPanel, node => node.tag === "form");
    expect(find(modelForm, node => node.tag === "fieldset").props.disabled).toBe(false);
    expect(labeled(modelPanel, "获取上游模型列表").props.disabled).toBe(shared);
    await event(modelForm, "Submit"); await flush();
    expect(f.modelSave).toHaveBeenCalledOnce();
    expect(f.modelSave.mock.calls[0]).toMatchObject([{ expected_sequence: 1, record: { resource_id: f.profile.resource_id } }]);
    expect(f.providerRaw()).toBe(providerOriginal);
    f.gate.reject(new WorkbenchApiError("unknown", "Provider outcome unknown")); await savingProvider; await flush();
    expect(f.providerRaw()).toBe(providerOriginal); expect(f.resources.locked.value).toBe(true);
    expect(labeled(root, "新增模型配置").props.disabled).toBe(false);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    expect(find(labeled(root, "全局模型配置"), node => node.tag === "fieldset").props.disabled).toBe(false);
    expect(labeled(root, "获取上游模型列表").props.disabled).toBe(shared);
    expect(f.providerSave).toHaveBeenCalledOnce();
  });

  it.each([false, true])("limits a credential mutation to discovery for its exact provider identity (shared=%s)", async shared => {
    const f = fixture(shared), root = await mount(f);
    await event(namedButton(root, f.provider.value.name), "Click"); await flush();
    await event(labeled(root, "模拟凭据未决"), "Click"); await flush();
    expect(labeled(root, "新增供应商").props.disabled).toBe(true);
    expect(labeled(root, "新增模型配置").props.disabled).toBe(false);
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    const modelPanel = labeled(root, "全局模型配置");
    expect(find(modelPanel, node => node.tag === "fieldset").props.disabled).toBe(false);
    expect(labeled(modelPanel, "获取上游模型列表").props.disabled).toBe(shared);
    await event(find(modelPanel, node => node.tag === "form"), "Submit"); await flush();
    expect(f.modelSave).toHaveBeenCalledOnce(); expect(f.providerSave).not.toHaveBeenCalled();
  });

  it.each(["provider", "credential"])("discards in-flight discovery when the selected %s starts mutating", async mutation => {
    const discovery = deferred<providerConfigurationApi.DiscoveredModel[]>();
    const listModels = vi.spyOn(providerConfigurationApi, "listProviderModels").mockReturnValue(discovery.promise);
    const f = fixture(true), root = await mount(f);
    await event(namedButton(root, f.provider.value.name), "Click"); await flush();
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    const discovering = event(labeled(root, "获取上游模型列表"), "Click");
    await flush();
    expect(listModels).toHaveBeenCalledOnce();
    let savingProvider: ReturnType<typeof event> = undefined;
    if (mutation === "provider") {
      savingProvider = event(find(root, node => node.tag === "form" && text(node).includes("服务地址")), "Submit");
    } else await event(labeled(root, "模拟凭据未决"), "Click");
    await flush();
    expect(labeled(root, "获取上游模型列表").props.disabled).toBe(true);
    discovery.resolve([{ model: "stale-discovery", adapted: true, input_token_limit: null,
      output_token_limit: null, context_window_tokens: null }]);
    await discovering; await flush();
    expect(elements(root).some(node => node.props["aria-label"] === "上游模型列表")).toBe(false);
    expect(text(root)).not.toContain("stale-discovery");
    if (mutation === "provider") {
      f.gate.reject(new WorkbenchApiError("unknown", "Provider outcome unknown"));
      await savingProvider; await flush();
      expect(f.providerSave).toHaveBeenCalledOnce();
    }
  });

  it("keeps the model's own unresolved request locked while provider management stays editable", async () => {
    const f = fixture(), root = await mount(f);
    f.modelSave.mockRejectedValueOnce(new WorkbenchApiError("unknown", "Model outcome unknown"));
    await event(namedButton(root, f.profile.value.name), "Click"); await flush();
    await event(find(labeled(root, "全局模型配置"), node => node.tag === "form"), "Submit"); await flush();
    const original = f.modelRaw();
    expect(original).not.toBeNull(); expect(f.profiles.locked.value).toBe(true);
    expect(labeled(root, "新增模型配置").props.disabled).toBe(true);
    expect(labeled(root, "新增供应商").props.disabled).toBe(false);
    expect(await f.profiles.save(f.profile, 1)).toBe(false);
    await f.profiles.refresh(); await flush();
    expect(f.modelRaw()).toBe(original); expect(text(root)).toContain("Model outcome unknown");
    expect(f.modelSave).toHaveBeenCalledOnce(); expect(f.providerSave).not.toHaveBeenCalled();
  });
});
