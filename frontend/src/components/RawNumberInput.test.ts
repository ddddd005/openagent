import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, nextTick, ref, type App } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import RawNumberInput from "./RawNumberInput.vue";
import { localDraftPorts } from "../adapters/localDraftsApi";

class HostNode {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  validationMessage = "";
  constructor(readonly tag = "") {}
  setCustomValidity(message: string) { this.validationMessage = message; }
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag),
  createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const at = anchor ? parent.children.indexOf(anchor) : -1;
    if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child);
  },
  remove(node) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = null;
  },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _before, value) => { node.props[key] = value; },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
async function flush() { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick(); }
function elements(root: HostNode): HostNode[] { return [root, ...root.children.flatMap(elements)]; }
function installRender() {
  const filename = "RawNumberInput.vue";
  const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "raw-number-input" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "raw-number-input",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  Object.assign(RawNumberInput, { render: new Function("Vue", code)(Vue) });
}
async function mount(identity: string) {
  const update = vi.fn(), validity = vi.fn();
  const currentIdentity = ref(identity);
  installRender();
  const root = new HostNode("root");
  const app = renderer.createApp({
    setup() {
      return () => Vue.h(RawNumberInput, { modelValue: 8, integer: true, identity: currentIdentity.value,
        "onUpdate:modelValue": update, onValidity: validity });
    },
  }).provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush();
  return { input: elements(root).find(node => node.tag === "input")!, update, validity, currentIdentity };
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.restoreAllMocks(); });

describe("raw number input after editor-draft retirement", () => {
  it("keeps incomplete text in memory and dispatches no draft reads or writes", async () => {
    const read = vi.spyOn(localDraftPorts, "read"), save = vi.spyOn(localDraftPorts, "save");
    const f = await mount("node-a:limit");
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "1e-" } });
    await flush();
    expect(f.input.props.value).toBe("1e-");
    expect(f.input.validationMessage).toBe("数值尚未填写完整");
    expect(f.validity).toHaveBeenLastCalledWith(false);
    expect(f.update).not.toHaveBeenCalled();
    expect(read).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it("accepts a complete edit in memory without a persistence gate", async () => {
    const read = vi.spyOn(localDraftPorts, "read"), save = vi.spyOn(localDraftPorts, "save");
    const f = await mount("node-a:limit");
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "-" } });
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "12" } });
    await flush();
    expect(f.input.props.value).toBe("12");
    expect(f.update).toHaveBeenLastCalledWith(12);
    expect(f.validity).toHaveBeenLastCalledWith(true);
    expect(read).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it("drops incomplete text when the field identity changes", async () => {
    const read = vi.spyOn(localDraftPorts, "read"), save = vi.spyOn(localDraftPorts, "save");
    const f = await mount("node-a:limit");
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "-" } });
    await flush();
    expect(f.input.props.value).toBe("-");
    f.currentIdentity.value = "node-b:limit";
    await flush();
    expect(f.input.props.value).toBe("8");
    expect(read).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });
});
