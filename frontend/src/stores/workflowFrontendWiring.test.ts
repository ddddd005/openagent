import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone } from "../domain/workflowGraph";
import { explicitFrontendGraph, frontendCatalog, frontendExecutionLock, frontendProjectLock } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

async function fixture() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  graph.catalog = graphClone(frontendCatalog); graph.packageLock = graphClone(frontendProjectLock);
  graph.executionPackageLock = graphClone(frontendExecutionLock);
  const id = graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const explicit = explicitFrontendGraph(id);
  graph.entries[id].document = graphClone(explicit.document);
  return { graph, id, ...explicit, evidence };
}
function configurationResult(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  const revision = request.document.revision + 1, editSequence = request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), revision, edit_sequence: editSequence };
  return { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
    edit_sequence: editSequence, source_has_effective_session: false, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});
describe("existing explicit frontend definition edits", () => {
  it("creates only a blank ordinary workflow even with the frontend package available", () => {
    const graph = useWorkflowGraphStore(); graph.catalog = graphClone(frontendCatalog);
    graph.packageLock = graphClone(frontendProjectLock); graph.executionPackageLock = graphClone(frontendExecutionLock);
    graph.createWorkflow();
    expect(graph.document!.nodes).toEqual([]); expect(graph.document!.edges).toEqual([]);
    expect(graph.document!.object_bindings).toEqual([]); expect(graph.document!.package_lock).toEqual(frontendExecutionLock);
    expect("attachFrontendDisplay" in graph).toBe(false);
  });
  it("edits role and object key using ordinary config patches and undoes the complete config", async () => {
    const { graph, appendId } = await fixture();
    const node = graph.document!.nodes.find(row => row.node_binding_id === appendId)!;
    const before = graphClone(graph.document);
    graph.patchNode(appendId, { config: { ...node.config, role: "system", object_key: "other" } });
    expect(graph.document!.nodes.find(row => row.node_binding_id === appendId)?.config)
      .toEqual({ role: "system", object_key: "other" });
    expect(graph.document!.object_bindings).toEqual(before!.object_bindings);
    graph.undo(); expect(graph.document).toEqual(before);
  });
  it("replaces, adds and removes source edges as undoable edits without renumbering existing edges", async () => {
    const { graph, sourceId, appendId } = await fixture();
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const edge = graph.document!.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    const edgeId = edge.edge_id, before = graphClone(graph.document);
    expect(graph.patchFrontendSource(appendId, edgeId, { nodeId: replacement, portId: "output" })).toBeTruthy();
    expect(graph.document!.edges.find(row => row.edge_id === edgeId)).toMatchObject({ order: 0, source_node_id: replacement });
    graph.undo(); expect(graph.document).toEqual(before); graph.undo(true);
    graph.patchFrontendSource(appendId, null, { nodeId: sourceId, portId: "output" });
    const added = graph.document!.edges.find(row => row.target_node_id === appendId
      && row.target_port_id === "content" && row.edge_id !== edgeId)!;
    expect(added.order).toBe(1);
    graph.patchFrontendSource(appendId, added.edge_id, null);
    expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(false);
    graph.undo(); expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(true);
  });
  it("keeps source edits in the same workflow and restores roots, controls and permissions on undo", async () => {
    const { graph, id, sourceId, appendId } = await fixture();
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const edge = graph.document!.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    const before = graphClone(graph.document!); useWorkspaceStore().markWorkflowSaved(id);
    graph.entries[id].saved_document = graphClone(before); graph.entries[id].saved_revision = 1;
    expect(graph.patchFrontendSource(appendId, edge.edge_id, { nodeId: replacement, portId: "output" })).toBe(id);
    expect(graph.entries[id].saved_document).toEqual(before);
    expect(graph.document!.object_bindings).toEqual(before.object_bindings);
    expect(graph.document!.execution_roots).toEqual(before.execution_roots);
    expect(graph.document!.control_edges).toEqual(before.control_edges);
    graph.undo(); expect(graph.document).toEqual(before);
    expect(graph.document!.edges.find(row => row.edge_id === edge.edge_id)?.source_node_id).toBe(sourceId);
  });
  it("saves an exact source replacement through the formal schema 3 configuration store", async () => {
    const { graph, id, appendId, evidence } = await fixture();
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const document = { ...graphClone(graph.document!), schema_version: 3 as const, revision: 1, edit_sequence: 1 };
    document.nodes = document.nodes.map(node => ({ ...node,
      configuration_policy: { schema_version: 1 as const, prompt_mode: "fixed" as const } }));
    graph.entries[id].document = graphClone(document); graph.entries[id].saved_document = graphClone(document);
    graph.entries[id].saved_revision = 1;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => configurationResult(request));
    graph.installWorkflowConfigurationApplication(createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [], update, readReceipt: async request => configurationResult(request),
    }));
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const edge = document.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    expect(graph.patchFrontendSource(appendId, edge.edge_id, { nodeId: replacement, portId: "output" })).toBe(id);
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(graph.entries[id].saved_revision).toBe(2));
    const submitted = update.mock.calls[0]![0].document;
    expect(submitted.nodes).toEqual(document.nodes);
    expect(submitted.object_bindings).toEqual(document.object_bindings);
    expect(submitted.execution_roots).toEqual(document.execution_roots);
    expect(submitted.edges.find(row => row.edge_id === edge.edge_id)).toMatchObject({
      source_node_id: replacement, target_node_id: appendId, target_port_id: "content", order: edge.order,
    });
  });
  it("rolls back incompatible sources and blocks source changes while a command is pending", async () => {
    const { graph, appendId } = await fixture();
    const edge = graph.document!.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    const before = graphClone(graph.document);
    expect(graph.patchFrontendSource(appendId, edge.edge_id, { nodeId: appendId, portId: "view" })).toBeNull();
    expect(graph.document).toEqual(before);
    graph.busy = "start";
    expect(graph.patchFrontendSource(appendId, edge.edge_id, null)).toBeNull();
    expect(graph.document).toEqual(before);
  });
});
