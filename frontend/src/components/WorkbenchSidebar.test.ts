import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchSidebar from "./WorkbenchSidebar.vue";
import { useWorkspaceStore } from "../stores/workspace";
import { sidebarPreferenceKey } from "../composables/useWorkbenchSidebar";

const state = vi.hoisted(() => ({ mounts: { workflows: 0, providers: 0, content: 0 } }));
vi.mock("./WorkflowSidebar.vue", async () => {
  const { defineComponent, h } = await import("vue");
  return { default: defineComponent({
    setup: () => {
      state.mounts.workflows++;
      return () => h("section", { "data-business": "workflows" });
    },
  }) };
});
vi.mock("./ProviderSidebar.vue", async () => {
  const { defineComponent, h, ref } = await import("vue");
  return { default: defineComponent({
    setup: () => {
      state.mounts.providers++;
      const draft = ref("");
      return () => h("section", { "data-business": "providers" }, [
        h("input", { "aria-label": "Provider draft", value: draft.value,
          onInput: (event: { target: { value: string } }) => { draft.value = event.target.value; } }),
      ]);
    },
  }) };
});
vi.mock("./ContentSidebar.vue", async () => {
  const { defineComponent, h, ref } = await import("vue");
  return { default: defineComponent({
    setup: () => {
      state.mounts.content++;
      const draft = ref("");
      return () => h("section", { "data-business": "content" }, [
        h("input", { "aria-label": "Content draft", value: draft.value,
          onInput: (event: { target: { value: string } }) => { draft.value = event.target.value; } }),
      ]);
    },
  }) };
});

class HostNode {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  style = { display: "" };
  text = "";
  constructor(readonly tag = "") {}
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag),
  createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child);
  },
  remove: node => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = null;
  },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _previous, value) => { node.props[key] = value; },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
function elements(node: HostNode): HostNode[] { return [node, ...node.children.flatMap(elements)]; }
function find(root: HostNode, name: string) {
  const node = elements(root).find(row => row.props["aria-label"] === name);
  expect(node, name).toBeDefined(); return node!;
}
async function click(root: HostNode, name: string) {
  (find(root, name).props.onClick as () => void)();
  await nextTick();
}
async function mount(collapsed = false) {
  state.mounts = { workflows: 0, providers: 0, content: 0 };
  const values = new Map([[sidebarPreferenceKey, String(collapsed)]]);
  const storage = {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { values.set(key, value); }),
  };
  vi.stubGlobal("localStorage", storage);
  const filename = "WorkbenchSidebar.vue";
  const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  const render = new Function("Vue", transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText)(Vue);
  const pinia = createPinia(); setActivePinia(pinia);
  const collapse = vi.fn(), root = new HostNode("root");
  const app = renderer.createApp({ ...WorkbenchSidebar, render }, { onCollapsed: collapse }).use(pinia);
  app.provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await nextTick();
  return { root, workspace: useWorkspaceStore(), collapse, storage };
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); });

describe("thin workbench sidebar", () => {
  it("navigates between peer businesses without remounting or resetting their input", async () => {
    const f = await mount();
    await click(f.root, "供应商");
    const input = find(f.root, "Provider draft");
    (input.props.onInput as (event: unknown) => void)({ target: { value: "Unsubmitted provider" } });
    await nextTick();
    await click(f.root, "内容");
    (find(f.root, "Content draft").props.onInput as (event: unknown) => void)({
      target: { value: "Unsubmitted content" },
    });
    f.workspace.initializeFreshWorkspace({ id: crypto.randomUUID(), title: "New workflow", description: "", nodeCount: 0 });
    await nextTick();
    expect(find(f.root, "内容").props["aria-current"]).toBe("page");
    await click(f.root, "工作流"); await click(f.root, "供应商");
    expect(find(f.root, "Provider draft")).toBe(input);
    expect(input.props.value).toBe("Unsubmitted provider");
    expect(find(f.root, "Content draft").props.value).toBe("Unsubmitted content");
    expect(state.mounts).toEqual({ workflows: 1, providers: 1, content: 1 });
    expect("sidebarSection" in f.workspace).toBe(false);
  });
  it("owns collapse preferences without writing product storage", async () => {
    const sidebar = await mount(true);
    expect(sidebar.collapse).toHaveBeenLastCalledWith(true);
    await click(sidebar.root, "供应商");
    expect(sidebar.collapse).toHaveBeenLastCalledWith(false);
    expect(sidebar.storage.setItem).toHaveBeenCalledExactlyOnceWith(sidebarPreferenceKey, "false");
    expect(sidebar.storage.getItem).toHaveBeenCalledExactlyOnceWith(sidebarPreferenceKey);
    expect(sidebar.storage.setItem).toHaveBeenCalledTimes(1);
  });
});
