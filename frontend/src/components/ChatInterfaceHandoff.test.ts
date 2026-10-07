import { parse } from "@vue/compiler-dom";
import type { ElementNode, TemplateChildNode } from "@vue/compiler-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import App from "../App.vue";
import { graphClone } from "../domain/workflowGraph";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

vi.mock("@vue-flow/core", () => ({ MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right" }, Handle: { render: () => null }, VueFlow: { render: () => null } }));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); vi.stubGlobal("fetch", vi.fn()); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function elements(nodes: readonly TemplateChildNode[]): ElementNode[] {
  return nodes.flatMap(node => node.type === 1 ? [node, ...elements(node.children)] : []);
}
function fixture() {
  let raw: string | null = null;
  const storage = { getItem: () => raw, setItem: vi.fn((_key: string, value: string) => { raw = value; }) };
  useWorkbenchPersistenceStore().initialize(storage);
  const graph = useWorkflowGraphStore(); vi.spyOn(graph, "activate").mockResolvedValue();
  const creation = [vi.spyOn(graph, "createSession"), vi.spyOn(graph, "selectSession"), vi.spyOn(graph, "submitPrimary")];
  async function render() {
    const html = await renderToString(createSSRApp(App).use(pinia));
    const anchor = elements(parse(html).children).find(node => node.tag === "a"
      && node.props.some(prop => prop.type === 6 && prop.name === "aria-label" && prop.value?.content === "打开聊天前端"))!;
    return Object.fromEntries(anchor.props.flatMap(prop => prop.type === 6 ? [[prop.name, prop.value?.content ?? ""]] : []));
  }
  function passive() { expect(fetch).not.toHaveBeenCalled(); creation.forEach(spy => expect(spy).not.toHaveBeenCalled()); }
  return { graph, storage, render, passive };
}
describe("current-only App chat handoff", () => {
  it("disables an unsaved graph without creating a session or sending a request", async () => {
    const f = fixture(), original = graphClone(f.graph.storeSnapshot()), anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("保存工作流后打开聊天前端");
    expect(f.graph.storeSnapshot()).toEqual(original); f.passive();
  });
  it("opens the exact saved graph and selected session without changing state or outboxes", async () => {
    const f = fixture(), entry = f.graph.active!, session = crypto.randomUUID();
    entry.saved_revision = 1; entry.session_id = session;
    const original = graphClone(f.graph.storeSnapshot()), writes = f.storage.setItem.mock.calls.length;
    const anchor = await f.render(), url = new URL(anchor.href!);
    expect([...url.searchParams]).toEqual([["graph_workflow", entry.document.workflow_definition_id], ["graph_session", session]]);
    expect(f.graph.storeSnapshot()).toEqual(original); expect(f.storage.setItem).toHaveBeenCalledTimes(writes); f.passive();
  });
  it("keeps the original pending body and disables the link until receipt reconciliation", async () => {
    const f = fixture(), entry = f.graph.active!;
    entry.saved_revision = 1; entry.pending = { action: "save", path: "/api/graph/definitions",
      body: { document: graphClone(entry.document), expected_revision: 1, idempotency_key: crypto.randomUUID() } };
    const pending = entry.pending, original = graphClone(pending), anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor.title).toBe("核实原请求后打开聊天前端");
    expect(entry.pending).toBe(pending); expect(entry.pending).toEqual(original); f.passive();
  });
  it.each(["definition", "session"] as const)("rejects an invalid %s identity", async field => {
    const f = fixture(), entry = f.graph.active!; entry.saved_revision = 1;
    if (field === "definition") entry.document.workflow_definition_id = "invalid"; else entry.session_id = "invalid";
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true"); f.passive();
  });
});
