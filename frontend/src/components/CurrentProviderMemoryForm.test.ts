import { readFileSync } from "node:fs";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ScriptTarget, ModuleKind, transpileModule } from "typescript";
import { afterEach, describe, expect, it, vi } from "vitest";
import CurrentProviderPanel from "./CurrentProviderPanel.vue";
import ProviderCredentialFields from "./ProviderCredentialFields.vue";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";
import type { CredentialStatus, ProviderCredentialRequest, ProviderCredentialResult } from "../adapters/providerConfigurationApi";
import type { ProviderSaveRequest } from "../adapters/workflowResourcesApi";

const credentialPorts = vi.hoisted(() => ({
  protection: vi.fn(), status: vi.fn(), delete: vi.fn(), receipt: vi.fn(),
  saveBound: vi.fn(), boundReceipt: vi.fn(), readProvider: vi.fn(),
}));
vi.mock("../adapters/providerConfigurationApi", async importOriginal => ({
  ...await importOriginal<typeof import("../adapters/providerConfigurationApi")>(),
  providerConfigurationPorts: credentialPorts,
}));
vi.mock("../adapters/workflowResourcesApi", async importOriginal => ({
  ...await importOriginal<typeof import("../adapters/workflowResourcesApi")>(),
  readCurrentProvider: credentialPorts.readProvider,
}));
vi.mock("./CurrentModelProfilesPanel.vue", async () => {
  const { defineComponent, h } = await import("vue");
  return { default: defineComponent({ setup: () => () => h("section", { "aria-label": "模型配置" }) }) };
});

class HostNode extends EventTarget {
  parent: HostNode | null = null; children: HostNode[] = [];
  props: Record<string, unknown> = {}; text = ""; value = "";
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  get tagName() { return this.tag.toUpperCase(); }
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(node, parent, anchor = null) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
  },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
  setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
  patchProp(node, key, _previous, value) { node.props[key] = value; if (key === "value") node.value = String(value); },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
function nodes(node: HostNode): HostNode[] { return [node, ...node.children.flatMap(nodes)]; }
function text(node: HostNode) { return nodes(node).map(item => item.text).join(""); }
function find(root: HostNode, predicate: (node: HostNode) => boolean) {
  const result = nodes(root).find(predicate); expect(result, "rendered provider control").toBeDefined(); return result!;
}
const label = (root: HostNode, name: string) => find(root, node => node.props["aria-label"] === name);
async function flush() { for (let index = 0; index < 16; index++) { await Promise.resolve(); await nextTick(); } }
async function trigger(node: HostNode, event: "Click" | "Submit") {
  await (node.props[`on${event}`] as (event: unknown) => Promise<unknown>)({ preventDefault() {} }); await flush();
}
function model(node: HostNode, value: string) { (node.props["onUpdate:modelValue"] as (value: string) => void)(value); }
function compile(component: typeof CurrentProviderPanel | typeof ProviderCredentialFields, filename: string) {
  const descriptor = parse(readFileSync(new URL(filename, import.meta.url), "utf8")).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  return { ...component, render: new Function("Vue", transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText)(Vue) };
}
async function fixture(savedKey = false) {
  vi.stubGlobal("window", { addEventListener() {}, removeEventListener() {}, confirm: vi.fn(() => true) });
  vi.stubGlobal("document", { activeElement: null }); vi.stubGlobal("Document", class {}); vi.stubGlobal("ShadowRoot", class {});
  const memory = memoryLocalDraftStorage();
  vi.spyOn(localDraftPorts, "read").mockImplementation(memory.ports.read);
  vi.spyOn(localDraftPorts, "save").mockImplementation(memory.ports.save);
  vi.spyOn(localDraftPorts, "receipt").mockImplementation(memory.ports.receipt);
  const provider = newProvider(); provider.value.name = "Saved provider";
  if (savedKey) provider.value.credential_ref = `local:${crypto.randomUUID()}`;
  const records = [graphClone(provider)];
  let generation = savedKey ? 1 : 0;
  const status = (reference: string): CredentialStatus => ({
    schema_version: 1, kind: "workflow.credential-status", credential_ref: reference,
    generation, state: generation ? "saved" : "missing",
  });
  let lostResponse = false;
  credentialPorts.protection.mockResolvedValue(true);
  credentialPorts.status.mockImplementation(async reference => status(reference));
  credentialPorts.readProvider.mockImplementation(async identity =>
    graphClone(records.find(record => record.scope === identity.scope && record.resource_id === identity.resource_id) ?? null));
  let accepted: ProviderCredentialResult | null = null;
  credentialPorts.saveBound.mockImplementation(async (request: ProviderCredentialRequest, _secret: string) => {
    const saved = { ...graphClone(provider), scope: request.identity.scope, resource_id: request.identity.resource_id,
      update_sequence: request.expected_sequence + 1,
      value: { ...request.provider_seed!, credential_ref: request.credential_ref } };
    records.splice(0, records.length, saved); generation = request.expected_generation + 1;
    accepted = { schema_version: 1, kind: "workflow.provider-credential-save", outcome: "saved",
      reason_code: "provider_credential_saved", credential: status(request.credential_ref),
      provider: { reference: request.identity, update_sequence: saved.update_sequence, deleted: false } };
    if (lostResponse) throw new WorkbenchApiError("unknown", "Controlled Key response loss");
    return graphClone(accepted);
  });
  credentialPorts.boundReceipt.mockImplementation(async () => graphClone(accepted));
  const resourceSave = vi.fn(async (request: ProviderSaveRequest) => {
    records.splice(0, records.length, graphClone(request.record)); return {};
  });
  let raw: string | null = null;
  const resources = createProviderResources({ list: async () => graphClone(records),
    save: resourceSave, readReceipt: vi.fn(async () => ({})), readPending: () => raw, writePending: value => { raw = value; } });
  Object.assign(ProviderCredentialFields, { render: compile(ProviderCredentialFields, "ProviderCredentialFields.vue").render });
  const app = renderer.createApp(compile(CurrentProviderPanel, "CurrentProviderPanel.vue"));
  app.provide(modelResourceControllerKey, resources).provide(Vue.ssrContextKey, { modules: new Set() });
  const root = new HostNode(); apps.push(app); app.mount(root); await flush();
  return { root, resources, provider, resourceSave, memory, records, get raw() { return raw; },
    set lostResponse(value: boolean) { lostResponse = value; } };
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); });

describe("ordinary provider form editing", () => {
  it("does not persist or restore unsubmitted forms while switching, closing or reopening", async () => {
    const f = await fixture();
    await trigger(label(f.root, "新增供应商"), "Click");
    model(label(f.root, "供应商名称"), "Unsaved provider"); await flush();
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    await trigger(find(f.root, node => node.tag === "button" && text(node).startsWith("Saved provider")), "Click");
    expect(label(f.root, "供应商名称").value).toBe("Saved provider");
    await trigger(label(f.root, "取消编辑"), "Click");
    expect(nodes(f.root).some(node => node.props["aria-label"] === "供应商配置")).toBe(false);
    await trigger(label(f.root, "新增供应商"), "Click");
    expect(label(f.root, "供应商名称").value).toBe("DeepSeek");
    expect(f.resourceSave).not.toHaveBeenCalled();
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    const source = readFileSync(new URL("CurrentProviderPanel.vue", import.meta.url), "utf8");
    expect(source).not.toMatch(/localDraft|useResourceFormDraft|FormDraftNotice|editor\.resume/);
  });
  it("retains the memory Key through other field edits and saves the latest entire provider in one action", async () => {
    const f = await fixture();
    await trigger(label(f.root, "新增供应商"), "Click");
    model(label(f.root, "API Key"), "controlled-memory-key"); await flush();
    model(label(f.root, "供应商名称"), "Latest name");
    model(label(f.root, "供应商服务地址"), "https://edited.invalid/v1"); await flush();
    expect(label(f.root, "API Key").value).toBe("controlled-memory-key");
    expect(find(f.root, node => node.tag === "fieldset").props.disabled).toBe(false);
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    await trigger(label(f.root, "供应商配置"), "Submit");
    expect(credentialPorts.saveBound).toHaveBeenCalledOnce();
    expect(credentialPorts.saveBound.mock.calls[0]).toMatchObject([{
      expected_sequence: 0, provider_seed: { name: "Latest name", base_url: "https://edited.invalid/v1", enabled: true },
    }, "controlled-memory-key"]);
    expect(f.resourceSave).not.toHaveBeenCalled();
    expect(f.records[0]!.value.name).toBe("Latest name");
    expect(text(f.root)).toContain("供应商配置与 Key 已保存");
    expect(label(f.root, "API Key").value).toBe("");
    for (const request of vi.mocked(f.memory.ports.save).mock.calls.map(call => call[0])) {
      expect(request.address.kind).toBe("intent");
      expect(request.address.key).toMatch(/^credential:/);
      expect(JSON.stringify(request)).not.toContain("controlled-memory-key");
    }
  });
  it("preserves the saved Key when the provider is saved with an empty Key input", async () => {
    const f = await fixture(true);
    await trigger(find(f.root, node => node.tag === "button" && text(node).startsWith("Saved provider")), "Click");
    expect(label(f.root, "API Key").value).toBe("");
    model(label(f.root, "供应商名称"), "Edited configuration"); await flush();
    await trigger(label(f.root, "供应商配置"), "Submit");
    expect(f.resourceSave).toHaveBeenCalledOnce();
    expect(f.resourceSave.mock.calls[0]![0]).toMatchObject({
      expected_sequence: 1, record: { value: { name: "Edited configuration", credential_ref: f.provider.value.credential_ref } },
    });
    expect(credentialPorts.saveBound).not.toHaveBeenCalled();
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    expect(nodes(f.root).some(node => node.props["aria-label"] === "供应商配置")).toBe(false);
  });
  it("retains only nonsecret unknown Key evidence and verifies without another submission", async () => {
    const f = await fixture(); f.lostResponse = true;
    await trigger(label(f.root, "新增供应商"), "Click");
    model(label(f.root, "API Key"), "controlled-memory-key");
    await trigger(label(f.root, "供应商配置"), "Submit");
    expect(credentialPorts.saveBound).toHaveBeenCalledOnce();
    expect(label(f.root, "API Key").value).toBe("");
    expect(label(f.root, "新增供应商").props.disabled).toBe(true);
    const preserved = graphClone([...f.memory.records.values()][0]!.value) as { request: ProviderCredentialRequest };
    expect(JSON.stringify(preserved)).not.toContain("controlled-memory-key");
    expect(text(f.root)).toContain("Controlled Key response loss");
    await trigger(find(f.root, node => node.tag === "button" && text(node).includes("核实原 Key 请求")), "Click");
    expect(credentialPorts.saveBound).toHaveBeenCalledOnce();
    expect(credentialPorts.boundReceipt).toHaveBeenCalledWith(expect.objectContaining(preserved.request), false);
    expect(label(f.root, "新增供应商").props.disabled).toBe(false);
    expect(text(f.root)).toContain("供应商配置与 Key 已保存");
    expect(f.resourceSave).not.toHaveBeenCalled();
  });
  it("reloads saved values explicitly without archiving or saving the ordinary form", async () => {
    const f = await fixture(true);
    await trigger(find(f.root, node => node.tag === "button" && text(node).startsWith("Saved provider")), "Click");
    model(label(f.root, "供应商名称"), "Unsubmitted edit"); await flush();
    await trigger(label(f.root, "重新读取生效供应商配置"), "Click");
    expect(label(f.root, "供应商名称").value).toBe("Saved provider");
    expect(credentialPorts.readProvider).toHaveBeenCalledWith(providerIdentity(f.provider));
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    expect(f.resourceSave).not.toHaveBeenCalled();
    expect(text(f.root)).toContain("已读取生效供应商配置");
    expect(text(f.root)).not.toContain("已归档");
  });
  it("reports a rejected configuration save directly and keeps input only until the form is closed", async () => {
    const f = await fixture();
    await trigger(find(f.root, node => node.tag === "button" && text(node).startsWith("Saved provider")), "Click");
    model(label(f.root, "供应商名称"), "Rejected input"); await flush();
    f.resourceSave.mockRejectedValueOnce(new WorkbenchApiError("rejected", "Controlled configuration rejection", 409, "stale_revision"));
    await trigger(label(f.root, "供应商配置"), "Submit");
    expect(label(f.root, "供应商名称").value).toBe("Rejected input");
    expect(text(f.root)).toContain("Controlled configuration rejection");
    expect(f.raw).toBeNull();
    expect(f.memory.ports.save).not.toHaveBeenCalled();
    expect(text(f.root)).not.toContain("草稿");
    expect(f.records[0]!.value.name).toBe("Saved provider");
    await trigger(label(f.root, "取消编辑"), "Click");
    await trigger(find(f.root, node => node.tag === "button" && text(node).startsWith("Saved provider")), "Click");
    expect(label(f.root, "供应商名称").value).toBe("Saved provider");
    expect(f.resourceSave).toHaveBeenCalledOnce();
  });
});
