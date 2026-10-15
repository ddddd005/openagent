import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { graphClone, type GraphNodeType } from "../domain/workflowGraph";
import type { WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { frontendSession } from "../testUtils/frontendFixture";
import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";

const type: GraphNodeType = { component_id: "plugin.config", component_version: "1",
  display_name: "Config", category: "Test", default_config: { text: "original", enabled: true },
  config_schema: { type: "object", properties: { text: { type: "string" }, enabled: { type: "boolean" } } },
  inputs: [], outputs: [], executable: true, is_output: false };
const extension: FrontendExtension = { schema_version: 3, host_protocol_version: 2,
  extension_id: "plugin.fields", kind: "field-editor", entrypoint: "plugin.fields",
  component_id: "plugin.config", component_version: "1", package_id: "plugin", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "node-fields", target: { component_id: "plugin.config", component_version: "1" } } };
async function fixture() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  graph.setPersistenceGuard(() => true);
  graph.catalog = [graphClone(type)]; graph.packageLock = [{ package_id: "plugin", version: "1.0.0" }];
  graph.frontendExtensions = [graphClone(extension)];
  const workflowId = graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const nodeId = graph.addNode("plugin.config@1", { x: 0, y: 0 })!;
  const request: WorkflowNodeConfigurationRequest = { workflowId, nodeId, componentId: "plugin.config",
    componentVersion: "1", expectedConfig: graphClone(type.default_config), patch: { text: "edited" },
    extensionId: extension.extension_id, lifecycle: "host-checked" };
  return { graph, workspace, workflowId, nodeId, request };
}
beforeEach(() => { setActivePinia(createPinia()); });
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("package configuration edits and ordinary workflow creation", () => {
  it("refuses the retired memory-only node configuration path", async () => {
    const { graph, request } = await fixture(), before = graphClone(graph.document);
    await expect(graph.patchNodeConfiguration(request)).rejects.toMatchObject({ kind: "unavailable" });
    await expect(graph.patchNodeConfiguration({ ...request, patch: { public_outputs: ["secret"] } }))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(graph.document).toEqual(before);
  });
  it("does not treat a legacy node configuration call as saved after selection or package changes", async () => {
    const { graph, workspace, workflowId, request } = await fixture();
    graph.createWorkflow();
    await expect(graph.patchNodeConfiguration(request)).rejects.toMatchObject({ kind: "unavailable" });
    workspace.openWorkflow(workflowId);
    graph.packageLock = [];
    await expect(graph.patchNodeConfiguration(request)).rejects.toMatchObject({ kind: "unavailable" });
    expect(graph.entries[workflowId].document.nodes[0].config).toEqual(type.default_config);
  });
  it("does not change a published basis through a retired node configuration call", async () => {
    const { graph, workspace, workflowId, request } = await fixture();
    workspace.activeWorkflow.state = "saved";
    const original = graphClone(graph.document);
    graph.entries[workflowId].saved_document = graphClone(original!); graph.entries[workflowId].saved_revision = 1;
    await expect(graph.patchNodeConfiguration(request)).rejects.toMatchObject({ kind: "unavailable" });
    expect(graph.entries[workflowId].saved_document).toEqual(original);
    expect(graph.document!.nodes[0].config.text).toBe("original");
  });
  it("creates independent blank workflows without preset builders, publishing or starting", () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    graph.selectedNodeIds = [crypto.randomUUID()]; graph.selectedEdgeId = crypto.randomUUID();
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const id = graph.createWorkflow();
    expect(workspace.activeWorkflowId).toBe(id);
    expect(graph.entries[id].saved_revision).toBe(0); expect(graph.entries[id].session_id).toBeNull();
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.history[id]).toEqual({ past: [], future: [] });
    const before = graphClone(graph.entries[id]);
    const next = graph.createWorkflow();
    expect(next).not.toBe(id); expect(graph.entries[id]).toEqual(before);
    expect(graph.document!.nodes).toEqual([]); expect(graph.document!.edges).toEqual([]);
    expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.document!.execution_roots).toEqual([]); expect(graph.document!.control_edges).toEqual([]);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("preserves a lost failed-node retry request and only reconciles its original key", async () => {
    const { graph, workflowId } = await fixture();
    const view = { ...frontendSession(workflowId), status: "failed", can_submit: false,
      available_actions: ["retry_failed_node", "close"] };
    graph.entries[workflowId].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    stubGraphApplicationFetch(vi.fn().mockRejectedValue(new Error("response lost")));
    expect(await graph.submitAgentAction("retry_failed_node")).toBe(false);
    const original = graphClone(graph.pending)!;
    expect(original.body.action).toBe("retry_failed_node");
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
