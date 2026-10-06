import { readFileSync } from "node:fs";
import { compileScript, parse as parseSfc } from "@vue/compiler-sfc";
import type { ElementNode, TemplateChildNode } from "@vue/compiler-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia, type Pinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import ContentSidebar from "./ContentSidebar.vue";
import { createPromptResources, promptResourceControllerKey } from "../application/promptResources";
import { graphClone } from "../domain/workflowGraph";
import { newPromptMember, newPromptResource } from "../domain/workflowPromptResources";
import { EMPTY_WORKFLOW_ID } from "../fixtures/workflows";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import { useGlobalContentStore } from "../stores/globalContent";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

const stores: Pinia[] = [];
function fixture(generic = true) {
  const pinia = createPinia();
  stores.push(pinia);
  const workspace = useWorkspaceStore(pinia), graph = useWorkflowGraphStore(pinia);
  if (generic) {
    graph.ensureEmpty(EMPTY_WORKFLOW_ID);
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
  }
  graph.frontendExtensions = graphClone(promptFrontendExtensions);
  graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" }];
  const record = newPromptResource();
  record.value.members = [newPromptMember("Current prompt")];
  const resources = createPromptResources({
    list: vi.fn(async () => [record]), save: vi.fn(async () => ({})), readReceipt: vi.fn(),
    readPending: () => null, writePending: vi.fn(),
  });
  resources.records.value = [record];
  const render = () => renderToString(createSSRApp(ContentSidebar).use(pinia)
    .provide(promptResourceControllerKey, resources));
  return { pinia, workspace, graph, resources, render };
}
afterEach(() => {
  stores.splice(0).forEach(pinia => pinia._s.forEach(store => store.$dispose()));
  vi.unstubAllGlobals();
});

describe("content sidebar architecture routing", () => {
  it("uses only the exact trusted prompt panel for an ordinary graph without legacy content state or requests", async () => {
    const f = fixture(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const html = await f.render();
    expect(html).toContain('aria-label="普通图提示词"');
    expect(html).toContain("提示词 · 当前资源");
    expect(html).toContain("Current prompt");
    expect(html).not.toContain("角色卡");
    expect(f.pinia._s.has("global-content")).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("locks resource edits independently of workflow mutations", async () => {
    const f = fixture();
    f.graph.busy = "workflow mutation";
    expect(f.graph.locked).toBe(true);
    expect(await f.render()).not.toMatch(/aria-label="新增提示词资源"[^>]*disabled/);
    f.resources.busy.value = true;
    expect(await f.render()).toMatch(/aria-label="新增提示词资源"[^>]*disabled/);
  });

  it.each(["package missing", "package version", "declaration missing", "entrypoint", "declaration",
    "protocol", "duplicate", "conflicting declaration"] as const)(
    "reports unavailable %s without falling back or creating legacy state", async mode => {
      const f = fixture(), fetcher = vi.fn();
      vi.stubGlobal("fetch", fetcher);
      if (mode === "package missing") f.graph.packageLock = [];
      if (mode === "package version") f.graph.packageLock[0]!.version = "2.0.0";
      if (mode === "declaration missing") f.graph.frontendExtensions = [];
      if (mode === "entrypoint") f.graph.frontendExtensions[0]!.entrypoint += ".forged";
      if (mode === "declaration") f.graph.frontendExtensions[0]!.binding.target = { forged: true };
      if (mode === "protocol") Object.assign(f.graph.frontendExtensions[0]!, { host_protocol_version: 2 });
      if (mode === "duplicate") f.graph.frontendExtensions.push(graphClone(f.graph.frontendExtensions[0]!));
      if (mode === "conflicting declaration") {
        const conflicting = graphClone(f.graph.frontendExtensions[0]!);
        conflicting.entrypoint += ".conflict";
        f.graph.frontendExtensions.push(conflicting);
      }
      const html = await f.render();
      expect(html).toContain("提示词资源不可用");
      expect(html).not.toContain("提示词 · 当前资源");
      expect(html).not.toContain("Current prompt");
      expect(html).not.toContain("角色卡");
      expect(f.pinia._s.has("global-content")).toBe(false);
      expect(fetcher).not.toHaveBeenCalled();
    },
  );

  it("keeps catalog loading and failure explicit", async () => {
    const f = fixture();
    f.graph.frontendExtensions = [];
    f.graph.catalogLoading = true;
    expect(await f.render()).toContain("读取中");
    f.graph.catalogLoading = false;
    f.graph.catalogError = "节点目录读取失败";
    expect(await f.render()).toContain("节点目录读取失败");
    expect(f.pinia._s.has("global-content")).toBe(false);
  });

  it("retains the legacy content sidebar and its exact pending record across routing changes", async () => {
    const f = fixture(false), content = useGlobalContentStore(f.pinia);
    content.create("global_prompt");
    content.selected!.name = "Original legacy prompt";
    content.pending = {
      record: graphClone(content.selected!), expected_revision: 0, idempotency_key: crypto.randomUUID(),
    };
    const original = graphClone(content.pending);
    const html = await f.render();
    expect(html).toContain('class="content-sidebar"');
    expect(html).toContain("Original legacy prompt");
    expect(html).toContain("角色卡");
    expect(html).not.toContain("提示词 · 当前资源");
    expect(content.pending).toEqual(original);
    f.graph.ensureEmpty(EMPTY_WORKFLOW_ID);
    f.workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    expect(await f.render()).toContain("提示词 · 当前资源");
    expect(content.pending).toEqual(original);
    expect(content.selected?.name).toBe("Original legacy prompt");
  });
});

describe("actual App content routing guards", () => {
  const { descriptor, errors } = parseSfc(readFileSync(new URL("../App.vue", import.meta.url), "utf8"));
  function allElements(nodes: readonly TemplateChildNode[]): ElementNode[] {
    return nodes.flatMap(node => node.type === 1 ? [node, ...allElements(node.children)] : []);
  }
  const elements = allElements(descriptor.template!.ast!.children);

  it("defines the old main editor only for content navigation on a legacy workflow", () => {
    expect(errors).toEqual([]);
    const ast = compileScript(descriptor, { id: "app-routing-test" }).scriptSetupAst!;
    const declaration = ast.flatMap(node => node.type === "VariableDeclaration" ? node.declarations : [])
      .find(node => node.id.type === "Identifier" && node.id.name === "legacyContentEditor");
    expect(declaration?.init).toMatchObject({
      type: "CallExpression", callee: { type: "Identifier", name: "computed" },
      arguments: [{ type: "ArrowFunctionExpression", body: {
        type: "LogicalExpression", operator: "&&",
        left: { type: "BinaryExpression", operator: "===",
          left: { type: "MemberExpression", object: { name: "workspace" }, property: { name: "sidebarSection" } },
          right: { type: "StringLiteral", value: "content" } },
        right: { type: "UnaryExpression", operator: "!",
          argument: { type: "CallExpression",
            callee: { type: "MemberExpression", object: { name: "graph" }, property: { name: "isGeneric" } },
            arguments: [{ type: "MemberExpression", object: { name: "workspace" }, property: { name: "activeWorkflowId" } }] } },
      } }],
    });
  });

  it("mounts the routed sidebar and leaves the ordinary canvas and topbar outside the legacy editor", () => {
    const sidebar = elements.find(node => node.tag === "ContentSidebar")!;
    expect(sidebar.props).toContainEqual(expect.objectContaining({
      type: 7, name: "if", exp: expect.objectContaining({ content: "workspace.sidebarSection === 'content'" }),
    }));
    const editor = elements.filter(node => node.tag === "ContentLibrary");
    expect(editor).toHaveLength(1);
    expect(editor[0]!.props).toContainEqual(expect.objectContaining({
      type: 7, name: "if", exp: expect.objectContaining({ content: "legacyContentEditor" }),
    }));
    expect(editor[0]!.props).toContainEqual(expect.objectContaining({
      type: 6, name: "view", value: expect.objectContaining({ content: "editor" }),
    }));
    const topbar = elements.find(node => node.tag === "header"
      && node.props.some(prop => prop.type === 6 && prop.name === "class" && prop.value?.content === "workbench-topbar"))!;
    expect(topbar.props).toContainEqual(expect.objectContaining({
      type: 7, name: "if", exp: expect.objectContaining({ content: "!legacyContentEditor" }),
    }));
    expect(elements.find(node => node.tag === "GraphWorkbench")!.props).toContainEqual(expect.objectContaining({
      type: 7, name: "else-if", exp: expect.objectContaining({ content: "graph.isGeneric(workspace.activeWorkflowId)" }),
    }));
  });

  it("activates restored workflows without implicitly converting a legacy empty document", () => {
    const ast = compileScript(descriptor, { id: "app-activation-test" }).scriptSetupAst!;
    const watcher = ast.find(node => node.type === "ExpressionStatement"
      && node.expression.type === "CallExpression" && node.expression.callee.type === "Identifier"
      && node.expression.callee.name === "watch"
      && node.expression.arguments[0]?.type === "ArrowFunctionExpression"
      && node.expression.arguments[0].body.type === "MemberExpression"
      && node.expression.arguments[0].body.property.type === "Identifier"
      && node.expression.arguments[0].body.property.name === "activeWorkflowId");
    if (watcher?.type !== "ExpressionStatement" || watcher.expression.type !== "CallExpression")
      throw new Error("Active workflow watcher missing");
    const handler = watcher.expression.arguments[1];
    if (handler?.type !== "ArrowFunctionExpression" || handler.body.type !== "BlockStatement")
      throw new Error("Active workflow handler missing");
    expect(handler.body.body).toHaveLength(1);
    expect(handler.body.body[0]).toMatchObject({
      type: "IfStatement",
      test: { type: "CallExpression", callee: { type: "MemberExpression",
        object: { name: "graph" }, property: { name: "isGeneric" } },
        arguments: [{ type: "Identifier", name: "workflowId" }] },
      consequent: { type: "ExpressionStatement", expression: { type: "UnaryExpression", operator: "void",
        argument: { type: "CallExpression", callee: { type: "MemberExpression",
          object: { name: "graph" }, property: { name: "activate" } } } } },
      alternate: { type: "ExpressionStatement", expression: { type: "UnaryExpression", operator: "void",
        argument: { type: "CallExpression", callee: { type: "MemberExpression",
          object: { name: "runtime" }, property: { name: "activateWorkflow" } } } } },
    });
  });
});
