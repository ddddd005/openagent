import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone } from "../domain/workflowGraph";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

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
  const source = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
  const registrar = graph.addNode("tools.output@2", { x: 300, y: 0 })!;
  const objectKey = graph.document!.object_bindings!.find(binding =>
    binding.registrar_node_id === registrar)!.object_key;
  return { graph, id, source, request: { sourceNodeId: source, sourcePortId: "output",
    objectKey, role: "user" as const }, evidence };
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

describe("frontend display definition transactions", () => {
  it("keeps newly created workflows blank even with the frontend package available", () => {
    const graph = useWorkflowGraphStore(); graph.catalog = graphClone(frontendCatalog);
    graph.packageLock = graphClone(frontendProjectLock); graph.executionPackageLock = graphClone(frontendExecutionLock);
    graph.createWorkflow();
    expect(graph.document!.nodes).toEqual([]); expect(graph.document!.edges).toEqual([]);
    expect(graph.document!.schema_version).toBe(2); expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.document!.package_lock).toEqual(frontendExecutionLock);
  });
  it("keeps the legacy in-memory recommendation as one undo/redo step", async () => {
    const { graph, id, request } = await fixture(), before = graphClone(graph.document);
    expect(graph.attachFrontendDisplay(request)).toBe(id);
    const after = graphClone(graph.document);
    expect(graph.selectedNodeIds).toHaveLength(3);
    graph.undo(); expect(graph.document).toEqual(before);
    graph.undo(true); expect(graph.document).toEqual(after);
  });
  it("applies the recommendation to an existing schema 3 workflow through the formal configuration store", async () => {
    const { graph, id, source, request, evidence } = await fixture(), workspace = useWorkspaceStore();
    const legacy = graph.entries[id]!.document;
    const document = {
      schema_version: 3 as const, workflow_definition_id: id, revision: 1, edit_sequence: 1,
      name: legacy.name, nodes: legacy.nodes.map(node => ({ ...graphClone(node),
        configuration_policy: { schema_version: 1 as const, prompt_mode: "fixed" as const } })),
      edges: graphClone(legacy.edges), object_bindings: graphClone(legacy.object_bindings ?? []),
      package_lock: graphClone(legacy.package_lock ?? []),
      execution_roots: graphClone(legacy.execution_roots ?? []),
      control_edges: graphClone(legacy.control_edges ?? []),
    };
    graph.entries[id]!.document = graphClone(document);
    graph.entries[id]!.saved_document = graphClone(document);
    graph.entries[id]!.saved_revision = 1;
    workspace.markWorkflowSaved(id, document.name);
    const requests: GraphConfigurationUpdateRequest[] = [];
    const update = vi.fn(async (original: GraphConfigurationUpdateRequest) => {
      requests.push(graphClone(original));
      return configurationResult(original);
    });
    const configuration = createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [], update,
      readReceipt: async original => configurationResult(original),
    });
    graph.installWorkflowConfigurationApplication(configuration);
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const before = graphClone(graph.document!);

    expect(graph.attachFrontendDisplay(request)).toBe(id);
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(graph.entries[id]!.document.nodes).toHaveLength(before.nodes.length + 3));

    const submitted = requests[0]!.document;
    const append = submitted.nodes.find(node => node.component_id === "frontend.state.append")!;
    expect(submitted.workflow_definition_id).toBe(id);
    expect(submitted.schema_version).toBe(3);
    expect(submitted.nodes.find(node => node.node_binding_id === source)?.component_id).toBe("tools.current-input");
    expect(submitted.edges).toContainEqual(expect.objectContaining({
      source_node_id: source, source_port_id: "output",
      target_node_id: append.node_binding_id, target_port_id: "content",
    }));
    expect(submitted.object_bindings).toContainEqual(expect.objectContaining({
      object_key: request.objectKey, registrar_node_id: expect.any(String),
      readers: expect.arrayContaining([append.node_binding_id]),
      writers: [append.node_binding_id],
    }));
    const accepted = { ...graphClone(submitted), revision: 2, edit_sequence: 2 };
    expect(graph.entries[id]!.document).toEqual(accepted);
    expect(graph.entries[id]!.saved_document).toEqual(accepted);
    expect(graph.entries[id]!.saved_revision).toBe(2);
    expect(workspace.activeWorkflowId).toBe(id);
    expect([...evidence.records.values()].filter(row => row.operation === "definition.update"))
      .toMatchObject([{ resolution: "verified", request: requests[0] }]);
  });
  it("does not change the published basis when the recommendation edits the same workflow", async () => {
    const { graph, id, request } = await fixture(); const workspace = useWorkspaceStore();
    workspace.markWorkflowSaved(id);
    const before = graphClone(graph.entries[id].document);
    graph.entries[id].saved_document = graphClone(before); graph.entries[id].saved_revision = 1;
    const copiedId = graph.attachFrontendDisplay(request)!;
    expect(copiedId).toBe(id); expect(workspace.activeWorkflowId).toBe(id);
    expect(graph.entries[id].saved_document).toEqual(before);
    expect(graph.document!.nodes).toHaveLength(before.nodes.length + 3);
    graph.undo();
    expect(graph.document!.nodes).toEqual(before.nodes);
    expect(graph.document!.edges).toEqual(before.edges);
    expect(graph.entries[id].document).toEqual(before);
  });
  it("edits role and object key using ordinary config patches and undoes the complete config", async () => {
    const { graph, request } = await fixture(); graph.attachFrontendDisplay(request);
    const node = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!;
    const before = graphClone(graph.document);
    graph.patchNode(node.node_binding_id, { config: { ...node.config, role: "system", object_key: "other" } });
    expect(graph.document!.nodes.find(row => row.node_binding_id === node.node_binding_id)?.config)
      .toEqual({ role: "system", object_key: "other" });
    expect(graph.document!.object_bindings).toEqual(before!.object_bindings);
    graph.undo(); expect(graph.document).toEqual(before);
  });
  it("replaces, adds and removes source edges as complete undoable edits without renumbering old edges", async () => {
    const { graph, source, request } = await fixture(); graph.attachFrontendDisplay(request);
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const append = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!.node_binding_id;
    const edge = graph.document!.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;
    const edgeId = edge.edge_id; const before = graphClone(graph.document);
    expect(graph.patchFrontendSource(append, edgeId, { nodeId: replacement, portId: "output" })).toBeTruthy();
    expect(graph.document!.edges.find(row => row.edge_id === edgeId)).toMatchObject({ order: 0, source_node_id: replacement });
    graph.undo(); expect(graph.document).toEqual(before); graph.undo(true);
    graph.patchFrontendSource(append, null, { nodeId: source, portId: "output" });
    const added = graph.document!.edges.find(row => row.target_node_id === append && row.target_port_id === "content" && row.edge_id !== edgeId)!;
    expect(added.order).toBe(1);
    graph.patchFrontendSource(append, added.edge_id, null);
    expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(false);
    graph.undo(); expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(true);
  });
  it("keeps source edits in the same workflow and undo restores roots, controls and bindings", async () => {
    const { graph, id, source, request } = await fixture(); graph.attachFrontendDisplay(request);
    graph.attachFrontendDisplay({ ...request, role: "assistant" });
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const append = graph.document!.nodes.filter(node => node.component_id === "frontend.state.append")[1].node_binding_id;
    const edge = graph.document!.edges.find(row => row.target_node_id === append && row.target_port_id === "content")!;
    const before = graphClone(graph.document!); useWorkspaceStore().markWorkflowSaved(id);
    graph.entries[id].saved_document = graphClone(before); graph.entries[id].saved_revision = 1;
    const copiedId = graph.patchFrontendSource(append, edge.edge_id, { nodeId: replacement, portId: "output" })!;
    expect(copiedId).toBe(id);
    expect(graph.entries[id].saved_document).toEqual(before);
    expect(graph.document!.object_bindings).toEqual(before.object_bindings);
    expect(graph.document!.control_edges).toEqual(before.control_edges);
    expect(graph.document!.execution_roots).toEqual(before.execution_roots);
    expect(graph.document!.edges.find(row => row.edge_id === edge.edge_id)?.source_node_id).toBe(replacement);
    graph.undo();
    expect(graph.document!.edges).toEqual(before.edges);
    expect(graph.document!.nodes).toEqual(before.nodes);
    expect(graph.document!.edges.find(row => row.edge_id === edge.edge_id)?.source_node_id).toBe(source);
    expect(graph.document!.object_bindings).toEqual(before.object_bindings);
    expect(graph.document!.execution_roots).toEqual(before.execution_roots);
    expect(graph.document!.control_edges).toEqual(before.control_edges);
    expect(graph.entries[id].document).toEqual(before);
  });
  it("rolls back a rejected recommendation and reports its explicit package error", async () => {
    const { graph, request } = await fixture(); graph.packageLock = []; graph.executionPackageLock = [];
    const before = graphClone(graph.document), history = graph.history[useWorkspaceStore().activeWorkflowId].past.length;
    expect(graph.attachFrontendDisplay(request)).toBeNull();
    expect(graph.document).toEqual(before);
    expect(graph.history[useWorkspaceStore().activeWorkflowId].past).toHaveLength(history);
    expect(JSON.stringify(useWorkbenchNoticesStore().$state)).toContain("packages.configure");
  });
  it("rolls back an incompatible source edit and blocks all display changes while a command is pending", async () => {
    const { graph, request } = await fixture(); graph.attachFrontendDisplay(request);
    const append = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!.node_binding_id;
    const edge = graph.document!.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;
    const before = graphClone(graph.document);
    expect(graph.patchFrontendSource(append, edge.edge_id, { nodeId: append, portId: "view" })).toBeNull();
    expect(graph.document).toEqual(before);
    graph.busy = "start";
    expect(graph.attachFrontendDisplay(request)).toBeNull();
    expect(graph.patchFrontendSource(append, edge.edge_id, null)).toBeNull();
    expect(graph.document).toEqual(before);
  });
});
