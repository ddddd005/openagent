import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { graphClone, newGraph, type GraphDocument, type GraphNodeType } from "../domain/workflowGraph";
import type { WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { frontendSession } from "../testUtils/frontendFixture";
import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

const constructor = vi.hoisted(() => vi.fn());
vi.mock("../domain/serialAgentDemo", () => ({ createSerialAgentDemo: constructor }));

const type: GraphNodeType = { component_id: "plugin.config", component_version: "1",
  display_name: "Config", category: "Test", default_config: { text: "original", enabled: true },
  config_schema: { type: "object", properties: { text: { type: "string" }, enabled: { type: "boolean" } } },
  inputs: [], outputs: [], executable: true, is_output: false };
const extension: FrontendExtension = { schema_version: 2, host_protocol_version: 1,
  extension_id: "plugin.fields", kind: "field-editor", entrypoint: "plugin.fields",
  component_id: "plugin.config", component_version: "1", package_id: "plugin", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "node-fields", target: { component_id: "plugin.config", component_version: "1" } } };
function fixture() {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  graph.setPersistenceGuard(() => true);
  graph.catalog = [graphClone(type)]; graph.packageLock = [{ package_id: "plugin", version: "1.0.0" }];
  graph.frontendExtensions = [graphClone(extension)];
  const workflowId = graph.createWorkflow(), nodeId = graph.addNode("plugin.config@1", { x: 0, y: 0 })!;
  const request: WorkflowNodeConfigurationRequest = { workflowId, nodeId, componentId: "plugin.config",
    componentVersion: "1", expectedConfig: graphClone(type.default_config), patch: { text: "edited" },
    extensionId: extension.extension_id, lifecycle: "host-checked" };
  return { graph, workspace, workflowId, nodeId, request };
}
beforeEach(() => { setActivePinia(createPinia()); constructor.mockReset(); });
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });

describe("package configuration edits and serial example drafts", () => {
  it("keeps package edits undoable, detached and limited to declared configuration fields", () => {
    const { graph, workflowId, nodeId, request } = fixture();
    expect(graph.patchNodeConfiguration(request)).toEqual({ workflowId, nodeId });
    request.patch.text = "mutated outside";
    expect(graph.document!.nodes[0].config).toEqual({ text: "edited", enabled: true });
    graph.undo(); expect(graph.document!.nodes[0].config).toEqual(type.default_config);
    expect(graph.patchNodeConfiguration({ ...request, patch: { public_outputs: ["secret"] } })).toBeNull();
    expect(graph.document!.nodes[0].public_outputs).toBeUndefined();
  });
  it("rejects a stale configuration, another active workflow, disabled packages and mismatched declarations", () => {
    const { graph, workspace, workflowId, request } = fixture();
    graph.patchNode(request.nodeId, { config: { text: "newer", enabled: true } });
    expect(graph.patchNodeConfiguration(request)).toBeNull();
    graph.undo(); graph.createWorkflow();
    expect(graph.patchNodeConfiguration(request)).toBeNull();
    workspace.openWorkflow(workflowId);
    expect(graph.patchNodeConfiguration({ ...request, componentVersion: "2" })).toBeNull();
    graph.packageLock = [];
    expect(graph.patchNodeConfiguration(request)).toBeNull();
    expect(graph.entries[workflowId].document.nodes[0].config).toEqual(type.default_config);
  });
  it("returns the real copy identity when editing a saved definition and preserves the original", () => {
    const { graph, workspace, workflowId, nodeId, request } = fixture();
    workspace.activeWorkflow.state = "saved";
    const original = graphClone(graph.document);
    const result = graph.patchNodeConfiguration(request)!;
    expect(result.workflowId).not.toBe(workflowId); expect(result.nodeId).toBe(nodeId);
    expect(workspace.activeWorkflowId).toBe(result.workflowId);
    expect(graph.entries[workflowId].document).toEqual(original);
    expect(graph.document!.nodes[0].config.text).toBe("edited");
  });
  it("creates a stable independent draft without publishing, starting or changing blank-new behavior", () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const doc: GraphDocument = { ...newGraph("Serial"), schema_version: 2, package_lock: [],
      object_bindings: [], execution_roots: [], control_edges: [] };
    constructor.mockReturnValue(doc);
    const persist = vi.fn(() => true), fetcher = vi.fn();
    graph.setPersistenceGuard(persist); vi.stubGlobal("fetch", fetcher);
    const id = graph.createSerialAgentExample()!;
    expect(workspace.activeWorkflowId).toBe(id);
    expect(graph.entries[id].saved_revision).toBe(0); expect(graph.entries[id].session_id).toBeNull();
    expect(persist).toHaveBeenCalledOnce(); expect(fetcher).not.toHaveBeenCalled();
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    expect(graph.entries[id].document).toEqual(doc);
    graph.createWorkflow(); expect(graph.document!.nodes).toEqual([]);
  });
  it("does not create a draft when the exact template prerequisites are unavailable", () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const before = graphClone(workspace.workflows);
    constructor.mockImplementation(() => { throw new Error("context.merge@2 missing"); });
    expect(graph.createSerialAgentExample()).toBeNull();
    expect(workspace.workflows).toEqual(before); expect(Object.keys(graph.entries)).toEqual([]);
  });
  it("preserves a lost failed-node retry command across reload and only reconciles its original key", async () => {
    const { graph, workflowId } = fixture();
    const view = { ...frontendSession(workflowId), status: "failed", can_submit: false,
      available_actions: ["retry_failed_node", "close"] };
    graph.entries[workflowId].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    stubGraphApplicationFetch(vi.fn().mockRejectedValue(new Error("response lost")));
    expect(await graph.submitAgentAction("retry_failed_node")).toBe(false);
    const original = graphClone(graph.pending)!;
    expect(original.body.action).toBe("retry_failed_node");
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    const accepted = { ...view, revision: 2, status: "succeeded", can_submit: true, available_actions: [] };
    const fetcher = vi.fn(async (path, init) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body);
        expect(envelope).toEqual({ operation: "run.control",
          parameters: { ...original.body, session_id: view.workflow_session_id } });
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, accepted);
      }
      expect(path).not.toBe(original.path);
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [accepted] : accepted));
    });
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile();
    expect(fetcher.mock.calls.filter(([path]) => path === "/api/graph/receipts/read")).toHaveLength(1);
    expect(fetcher.mock.calls.some(([path]) => path === original.path)).toBe(false);
    expect(graph.pending).toBeNull();
  });
});
