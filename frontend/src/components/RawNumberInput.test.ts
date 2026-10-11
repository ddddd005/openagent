import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import RawNumberInput from "./RawNumberInput.vue";
import { useFormDraftsStore } from "../stores/formDrafts";

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
async function mount(raw: string | null, persist = true) {
  vi.stubGlobal("window", {});
  const pinia = createPinia(); setActivePinia(pinia);
  const forms = useFormDraftsStore();
  vi.spyOn(forms, "load").mockResolvedValue({ value: raw, conflict: false, record: null });
  const stage = vi.spyOn(forms, "stage").mockReturnValue(true);
  const update = vi.fn(), validity = vi.fn();
  installRender();
  const root = new HostNode("root");
  const app = renderer.createApp(RawNumberInput, { modelValue: 8, integer: true, min: 0, persist,
    draftScope: "resource:entries:item:stable-id:scan", draftBasis: { resourceId: "stable", sequence: 7 },
    "onUpdate:modelValue": update, onValidity: validity }).use(pinia)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush();
  return { input: elements(root).find(node => node.tag === "input")!, stage, update, validity, forms };
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("raw number form control", () => {
  it("keeps incomplete input in memory without loading or staging numeric drafts", async () => {
    const f = await mount("99", false);
    expect(f.forms.load).not.toHaveBeenCalled();
    expect(f.input.props.value).toBe("8");
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "1e-" } });
    await flush();
    expect(f.input.props.value).toBe("1e-");
    expect(f.input.validationMessage).toBe("数值尚未填写完整");
    expect(f.validity).toHaveBeenLastCalledWith(false);
    expect(f.update).not.toHaveBeenCalled();
    expect(f.stage).not.toHaveBeenCalled();
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "12" } });
    await flush();
    expect(f.update).toHaveBeenLastCalledWith(12);
    expect(f.validity).toHaveBeenLastCalledWith(true);
    expect(f.stage).not.toHaveBeenCalled();
  });
  it.each(["-", "1e"])("restores incomplete %s verbatim and invalidates the old numeric value", async raw => {
    const f = await mount(raw);
    expect(f.input.props.type).toBe("text");
    expect(f.input.props.value).toBe(raw);
    expect(f.input.props["aria-invalid"]).toBe(true);
    expect(f.input.validationMessage).toBe("数值尚未填写完整");
    expect(f.validity).toHaveBeenLastCalledWith(false);
    expect(f.update).not.toHaveBeenCalled();
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "12" } });
    await flush();
    expect(f.input.props.value).toBe("12");
    expect(f.input.validationMessage).toBe("");
    expect(f.update).toHaveBeenCalledWith(12);
    expect(f.stage).toHaveBeenLastCalledWith("resource:entries:item:stable-id:scan",
      { resourceId: "stable", sequence: 7 }, "12");
    expect(f.validity).toHaveBeenLastCalledWith(true);
  });
  it("stages exact incomplete text without emitting NaN or the old numeric value", async () => {
    const f = await mount(null);
    (f.input.props.onInput as (event: unknown) => void)({ target: { value: "1e-" } });
    await flush();
    expect(f.input.props.value).toBe("1e-");
    expect(f.stage.mock.calls.at(-1)?.[2]).toBe("1e-");
    expect(f.update).not.toHaveBeenCalled();
    expect(f.validity).toHaveBeenLastCalledWith(false);
  });
});
