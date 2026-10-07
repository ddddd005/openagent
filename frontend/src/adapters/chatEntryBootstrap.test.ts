import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { NodeTypes, parse, type ElementNode, type TemplateChildNode } from "@vue/compiler-dom";
import { describe, expect, it, vi } from "vitest";

const entrySource = readFileSync(new URL("../../../backend/src/phase1_agent/static/chat-entry.js", import.meta.url), "utf8");
const entryHtml = readFileSync(new URL("../../../backend/src/phase1_agent/static/index.html", import.meta.url), "utf8");
const workflow = "00000000-0000-4000-8000-00000000cafe";
const session = "00000000-0000-4000-8000-00000000beef";
const config = "00000000-0000-4000-8000-00000000abcd";
const graphPaths = ["/static/graph-chat-core.js", "/static/frontend-package-host.js",
  "/static/frontend-package.js", "/static/graph-chat.js"];
interface Element { hidden: boolean; textContent: string }
interface Script { src: string; onload?: () => void; onerror?: () => void }

async function bootstrap(search: string, failed: string[] = []) {
  const elements: Record<string, Element> = {
    mode: { hidden: false, textContent: "\u5165\u53e3\u672a\u7ed1\u5b9a" },
    error: { hidden: true, textContent: "" },
    "chat-client": { hidden: true, textContent: "" },
  };
  const saved = new Map([
    [`workflow-chat:v1:${workflow}`, '{"pending":{"body":{"idempotency_key":"original-graph-key"}}}'],
    [`workflow-user-ui:v1:${session}`, '{"pending_submission":{"idempotency_key":"original-legacy-key","text":"original"}}'],
    [`workflow-user-exposure:v1:${session}`, '{"config_id":"original-reference","revision":7}'],
    ["workflow.models.resource.pending.v1", "original-provider-request"],
    ["workflow.prompts.resource.pending.v1", "original-prompt-request"],
  ]);
  const original = [...saved], scripts: string[] = [];
  const storage = {
    getItem: vi.fn((key: string) => saved.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { saved.set(key, value); }),
    removeItem: vi.fn((key: string) => { saved.delete(key); }),
    clear: vi.fn(() => { saved.clear(); }),
  };
  const storageAccess = vi.fn(() => storage);
  const sessionStorageAccess = vi.fn(() => storage);
  const fetcher = vi.fn(async () => { throw new Error("Entry dispatch must not request the service"); });
  const replaceState = vi.fn();
  function append(script: Script) {
    scripts.push(script.src);
    queueMicrotask(() => {
      if (failed.includes(script.src)) script.onerror?.(); else script.onload?.();
    });
  }
  const scope: Record<string, unknown> = {
    URLSearchParams, queueMicrotask, fetch: fetcher,
    location: { search, pathname: "/", hash: "#retained" },
    history: { replaceState, state: { retained: true } },
    document: {
      getElementById: (id: string) => elements[id] ?? null,
      createElement: () => ({ src: "" } satisfies Script),
      head: { append, appendChild: append },
    },
  };
  Object.defineProperty(scope, "localStorage", { get: storageAccess });
  Object.defineProperty(scope, "sessionStorage", { get: sessionStorageAccess });
  scope.window = scope;
  await runInNewContext(entrySource, scope);
  return { elements, scripts, storage, storageAccess, sessionStorageAccess, fetcher, replaceState,
    preserved: () => [...saved], original, scope };
}
function expectNoSideEffects(result: Awaited<ReturnType<typeof bootstrap>>) {
  expect(result.scripts).toEqual([]);
  expect(result.fetcher).not.toHaveBeenCalled();
  expect(result.storageAccess).not.toHaveBeenCalled(); expect(result.sessionStorageAccess).not.toHaveBeenCalled();
  expect(result.storage.getItem).not.toHaveBeenCalled(); expect(result.storage.setItem).not.toHaveBeenCalled();
  expect(result.storage.removeItem).not.toHaveBeenCalled(); expect(result.storage.clear).not.toHaveBeenCalled();
  expect(result.replaceState).not.toHaveBeenCalled();
  expect(result.preserved()).toEqual(result.original);
  expect(result.elements["chat-client"]!.hidden).toBe(true);
}
function htmlElements(children: TemplateChildNode[]): ElementNode[] {
  return children.flatMap(node => node.type === NodeTypes.ELEMENT ? [node, ...htmlElements(node.children)] : []);
}
function attribute(node: ElementNode, name: string) {
  return node.props.find(prop => prop.type === NodeTypes.ATTRIBUTE && prop.name === name);
}
function attributeValue(node: ElementNode, name: string) {
  const prop = attribute(node, name);
  return prop?.type === NodeTypes.ATTRIBUTE ? prop.value?.content : undefined;
}
describe("passive chat entry HTML", () => {
  it("defers only the dispatcher before any business client is chosen", () => {
    const scripts = htmlElements(parse(entryHtml).children).filter(node => node.tag === "script");
    expect(scripts).toHaveLength(1);
    expect(attributeValue(scripts[0]!, "src")).toBe("/static/chat-entry.js");
    expect(attribute(scripts[0]!, "defer")).toBeDefined();
    expect(scripts[0]!.children).toEqual([]);
  });
  it("starts with an empty current-client mount and no fixed-flow controls", () => {
    const elements = htmlElements(parse(entryHtml).children);
    const byId = (id: string) => elements.find(node => attributeValue(node, "id") === id);
    const client = byId("chat-client");
    expect(client).toBeDefined();
    expect(attribute(client!, "hidden")).toBeDefined();
    const descendants = htmlElements(client!.children);
    expect(descendants).toEqual([]);
    for (const id of ["sessions", "new-session", "stage-a", "stage-b", "stage-output", "budget-a", "budget-b",
      "run-status", "run-control", "reroll-interrupted", "close-execution", "continue-workflow", "retry-closeout",
      "start-pending", "budget-controls", "additional-model-requests", "additional-model-attempts", "extend-budget",
      "public-exposures", "public-node-outputs", "messages", "empty", "composer", "prompt-config-a", "prompt-config-b",
      "prompt-revision-a", "prompt-revision-b", "prompt-state", "reset-prompt-state", "input", "progress", "submit"]) {
      expect(byId(id), id).toBeUndefined();
    }
    expect(elements.filter(node => ["button", "form", "input", "select", "textarea"].includes(node.tag))).toEqual([]);
    for (const id of ["mode", "error"]) {
      expect(byId(id), id).toBeDefined();
      expect(descendants, id).not.toContain(byId(id));
    }
    expect(byId("mode")!.children.some(node => node.type === NodeTypes.TEXT
      && node.content === "\u5165\u53e3\u672a\u7ed1\u5b9a")).toBe(true);
    expect(attribute(byId("mode")!, "hidden")).toBeUndefined();
    expect(attribute(byId("error")!, "hidden")).toBeDefined();
  });
});
describe("strict passive chat bootstrap dispatch", () => {
  it("keeps an unbound entry terminal without scripts, service requests or outbox access", async () => {
    const result = await bootstrap("");
    expectNoSideEffects(result);
    expect(result.elements.mode!.textContent).toBe("\u5165\u53e3\u672a\u7ed1\u5b9a");
    expect(result.elements.error!.hidden).toBe(false); expect(result.elements.error!.textContent).not.toBe("");
  });
  const invalid = [
    ["bare query delimiter", "?"],
    ["missing workflow identity", `?graph_session=${session}`],
    ["empty graph identity", "?graph_workflow="],
    ["non-UUID graph identity", "?graph_workflow=workflow"],
    ["uppercase UUID spelling", `?graph_workflow=${workflow.toUpperCase()}`],
    ["wrong UUID version", `?graph_workflow=${workflow.replace("-4000-", "-1000-")}`],
    ["wrong UUID variant", `?graph_workflow=${workflow.replace("-8000-", "-0000-")}`],
    ["invalid graph session", `?graph_workflow=${workflow}&graph_session=bad`],
    ["empty graph session", `?graph_workflow=${workflow}&graph_session=`],
    ["mixed graph and legacy session", `?graph_workflow=${workflow}&session=${session}`],
    ["mixed graph and prompt refs", `?graph_workflow=${workflow}&prompt_a=${config}&prompt_a_revision=1`],
    ["mixed legacy and graph session", `?session=${session}&graph_session=${session}`],
    ["unknown parameter", `?graph_workflow=${workflow}&view=messages`],
    ["unknown legacy parameter", `?session=${session}&unknown=value`],
    ["unknown capitalized key", `?Session=${session}`],
    ["blank legacy session", "?session="],
    ["invalid legacy session", "?session=not-a-uuid"],
    ["uppercase legacy session", `?session=${session.toUpperCase()}`],
    ["missing explicit legacy session", `?prompt_a=${config}&prompt_a_revision=1`],
    ["duplicate graph workflow", `?graph_workflow=${workflow}&graph_workflow=${workflow}`],
    ["duplicate graph session", `?graph_workflow=${workflow}&graph_session=${session}&graph_session=${session}`],
    ["duplicate decoded graph key", `?graph_workflow=${workflow}&%67raph_workflow=${workflow}`],
    ["duplicate decoded legacy key", `?session=${session}&sess%69on=${session}`],
    ["duplicate legacy revision", `?session=${session}&prompt_a=${config}&prompt_a_revision=1&prompt_a_revision=1`],
    ["leading empty segment", `?&graph_workflow=${workflow}`],
    ["trailing empty segment", `?graph_workflow=${workflow}&`],
    ["middle empty segment", `?graph_workflow=${workflow}&&graph_session=${session}`],
    ["missing equals", "?graph_workflow"],
    ["empty parameter name", `?=${workflow}`],
    ["flag without equals", `?graph_workflow=${workflow}&unknown`],
    ["plus decoding in key", `?graph+_workflow=${workflow}`],
    ["plus decoding in UUID", `?graph_workflow=+${workflow}`],
    ["encoded whitespace in UUID", `?graph_workflow=${workflow}%20`],
    ["encoded newline in graph workflow UUID", `?graph_workflow=${workflow}%0A`],
    ["encoded newline in graph session UUID", `?graph_workflow=${workflow}&graph_session=${session}%0A`],
    ["encoded newline in legacy session UUID", `?session=${session}%0A`],
    ["encoded newline in prompt UUID", `?session=${session}&prompt_a=${config}%0A&prompt_a_revision=1`],
    ["encoded newline in model UUID", `?session=${session}&model_config=${config}%0A&model_revision=1`],
    ["encoded newline in exposure UUID", `?session=${session}&exposure_config=${config}%0A&exposure_revision=1`],
    ["malformed encoded key", `?graph_%ZZworkflow=${workflow}`],
    ["malformed encoded value", "?graph_workflow=%"],
    ["invalid UTF-8 value", "?graph_workflow=%FF"],
    ["missing prompt revision", `?session=${session}&prompt_a=${config}`],
    ["missing prompt identity", `?session=${session}&prompt_b_revision=1`],
    ["missing model revision", `?session=${session}&model_config=${config}`],
    ["missing model identity", `?session=${session}&model_revision=1`],
    ["missing exposure revision", `?session=${session}&exposure_config=${config}`],
    ["missing exposure identity", `?session=${session}&exposure_revision=1`],
    ["invalid prompt UUID", `?session=${session}&prompt_a=bad&prompt_a_revision=1`],
    ["uppercase model UUID", `?session=${session}&model_config=${config.toUpperCase()}&model_revision=1`],
    ["oversized raw query", `?graph_workflow=${workflow}&unknown=${"x".repeat(1024)}`],
    ["more than 11 fields", `?${Array.from({ length: 12 }, (_, index) => `field${index}=value`).join("&")}`],
  ];
  it.each(invalid)("rejects %s before choosing any client", async (_name, search) => {
    const result = await bootstrap(search!);
    expectNoSideEffects(result);
    expect(result.elements.mode!.textContent).toBe("\u5165\u53e3\u4e0d\u53ef\u7528");
    expect(result.elements.error!.hidden).toBe(false);
    expect(result.elements.error!.textContent).toBe("\u804a\u5929\u5165\u53e3\u8eab\u4efd\u65e0\u6548");
    expect((result.scope.location as { search: string }).search).toBe(search);
  });
  it.each(["", "0", "-1", "01", "+1", "%2B1", "1.0", "1e1", "0x1", "latest", "NaN", "Infinity",
    "1%20", "1%0A", "9007199254740991%0A", "9007199254740992"])("rejects an inexact or unsafe legacy revision: %s", async revision => {
      const result = await bootstrap(`?session=${session}&model_config=${config}&model_revision=${revision}`);
      expectNoSideEffects(result);
      expect(result.elements.mode!.textContent).toBe("\u5165\u53e3\u4e0d\u53ef\u7528");
      expect(result.elements.error!.textContent).toBe("\u804a\u5929\u5165\u53e3\u8eab\u4efd\u65e0\u6548");
    });
  it.each([
    `?graph_workflow=${workflow}`,
    `?graph_workflow=${workflow}&graph_session=${session}`,
    `?graph_session=${session}&graph_workflow=${workflow}`,
    `?%67raph_workflow=${workflow.replaceAll("-", "%2D")}&graph_%73ession=${session}`,
  ])("loads legal graph dependencies in order without entering the legacy client: %s", async search => {
    const result = await bootstrap(search);
    expect(result.scripts).toEqual(graphPaths);
    expect(result.scripts).not.toContain("/static/app.js");
    expect(result.fetcher).not.toHaveBeenCalled(); expect(result.storageAccess).not.toHaveBeenCalled();
    expect(result.sessionStorageAccess).not.toHaveBeenCalled(); expect(result.preserved()).toEqual(result.original);
    expect(result.replaceState).not.toHaveBeenCalled();
    expect(result.elements.error!.hidden).toBe(true);
  });
  it.each([
    `?session=${session}`,
    `?session=${session}&prompt_a=${config}&prompt_a_revision=1`,
    `?session=${session}&prompt_b=${config}&prompt_b_revision=2`,
    `?%73ession=${session.replaceAll("-", "%2D")}&model_config=${config}&model_%72evision=%31`,
    `?session=${session}&prompt_a=${config}&prompt_a_revision=9007199254740991`
      + `&prompt_b=${config}&prompt_b_revision=9007199254740991`
      + `&model_config=${config}&model_revision=9007199254740991`
      + `&exposure_config=${config}&exposure_revision=9007199254740991`,
  ])("rejects even an exact old fixed-flow handoff without scripts or storage access: %s", async search => {
    const result = await bootstrap(search);
    expectNoSideEffects(result);
    expect(result.elements.mode!.textContent).toBe("\u5165\u53e3\u4e0d\u53ef\u7528");
    expect(result.elements.error!.hidden).toBe(false);
    expect(result.elements.error!.textContent).toBe("\u804a\u5929\u5165\u53e3\u8eab\u4efd\u65e0\u6548");
    expect((result.scope.location as { search: string }).search).toBe(search);
  });
  it.each([
    { failed: ["/static/frontend-package-host.js"] },
    { failed: ["/static/frontend-package.js"] },
    { failed: ["/static/frontend-package-host.js", "/static/frontend-package.js"] },
  ])("continues the graph shell when optional UI bundles fail: $failed", async ({ failed }) => {
    const result = await bootstrap(`?graph_workflow=${workflow}&graph_session=${session}`, failed);
    expect(result.scripts).toEqual(graphPaths); expect(result.elements.error!.hidden).toBe(true);
    expect(result.scripts).not.toContain("/static/app.js");
    expect(result.fetcher).not.toHaveBeenCalled(); expect(result.storageAccess).not.toHaveBeenCalled();
    expect(result.sessionStorageAccess).not.toHaveBeenCalled(); expect(result.preserved()).toEqual(result.original);
  });
  it.each(["/static/graph-chat-core.js", "/static/graph-chat.js"])("does not fall back to legacy when %s fails", async path => {
    const result = await bootstrap(`?graph_workflow=${workflow}`, [path]);
    expect(result.scripts).toEqual(path === graphPaths[0] ? [graphPaths[0]] : graphPaths);
    expect(result.scripts).not.toContain("/static/app.js");
    expect(result.elements.mode!.textContent).toBe("\u52a0\u8f7d\u5931\u8d25");
    expect(result.elements.error!.hidden).toBe(false);
    expect(result.elements["chat-client"]!.hidden).toBe(true);
    expect(result.fetcher).not.toHaveBeenCalled(); expect(result.storageAccess).not.toHaveBeenCalled();
    expect(result.sessionStorageAccess).not.toHaveBeenCalled(); expect(result.preserved()).toEqual(result.original);
  });
  it("does not attempt an old client load even when its unavailable script is supplied as a failure fixture", async () => {
    const result = await bootstrap(`?session=${session}`, ["/static/app.js"]);
    expectNoSideEffects(result);
    expect(result.elements.mode!.textContent).toBe("\u5165\u53e3\u4e0d\u53ef\u7528");
    expect(result.elements.error!.hidden).toBe(false);
  });
});
