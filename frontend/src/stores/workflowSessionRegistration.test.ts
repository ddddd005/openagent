import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { frontendNodeInputs, frontendNodeLaunchTarget } from "../application/frontendNodeEntry";
import { buildNodeFamilies, nodeMenuGroups } from "../domain/nodeCatalog";
import { graphClone } from "../domain/workflowGraph";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock } from "../testUtils/frontendFixture";
import { frontendNodeFixture, frontendNodeType } from "../testUtils/frontendNodeFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose();
  useWorkspaceStore().$dispose();
  vi.restoreAllMocks();
});

async function setup() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  graph.setPersistenceGuard(() => true);
  graph.catalog = graphClone(frontendCatalog);
  graph.packageLock = graphClone(frontendProjectLock);
  graph.executionPackageLock = graphClone(frontendExecutionLock);
  const workflowId = graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  return { graph, workflowId, evidence };
}

describe("registered session items", () => {
  it("creates @2 from the workbench default with independent chat items and preserves existing @1 nodes", async () => {
    const { graph, workflowId } = await setup();
    const chatFixture = frontendNodeFixture("2");
    graph.catalog = graphClone([...graph.catalog, chatFixture.catalog[0]!, frontendNodeType]);

    const legacyId = graph.addNode("frontend.node@1", { x: 0, y: 0 })!;
    const legacyBinding = graphClone(graph.document!.object_bindings!.find(row =>
      row.registrar_node_id === legacyId)!);
    const families = buildNodeFamilies(graph.catalog);
    const family = families.find(row => row.id === "frontend.node")!;
    expect(nodeMenuGroups(families).find(row => row.id === "presentation")!.items)
      .toContainEqual({ id: "frontend.node", label: "前端节点", disabled: false });
    expect(family.profiles.map(row => row.key)).toEqual(["frontend.node@2"]);

    const firstId = graph.addNode(family.preferredKey!, { x: 250, y: 0 })!;
    const secondId = graph.addNode(family.preferredKey!, { x: 500, y: 0 })!;
    const first = graph.document!.nodes.find(row => row.node_binding_id === firstId)!;
    const second = graph.document!.nodes.find(row => row.node_binding_id === secondId)!;
    const bindings = graph.document!.object_bindings!.filter(row => row.type_id === "workflow.chat-context");

    expect(first.component_version).toBe("2");
    expect(second.component_version).toBe("2");
    expect(bindings).toHaveLength(2);
    expect(new Set(bindings.map(row => row.object_key)).size).toBe(2);
    expect(bindings).toEqual(expect.arrayContaining([
      expect.objectContaining({ object_key: first.config.object_key, schema_version: 1,
        registrar_node_id: firstId, scope: "shared", owner_node_id: null, readers: [firstId], writers: [firstId] }),
      expect.objectContaining({ object_key: second.config.object_key, schema_version: 1,
        registrar_node_id: secondId, scope: "shared", owner_node_id: null, readers: [secondId], writers: [secondId] }),
    ]));
    expect(graph.document!.nodes.find(row => row.node_binding_id === legacyId)?.component_version).toBe("1");
    expect(graph.document!.object_bindings!.find(row => row.registrar_node_id === legacyId)).toEqual(legacyBinding);
    expect(graph.entries[workflowId]!.session_id).toBeNull();

    const inputId = graph.addNode("tools.editable-input@1", { x: 0, y: 150 })!;
    graph.patchNode(firstId, { config: { ...first.config, input_node_id: inputId } });
    const entry = graph.entries[workflowId]!;
    const savedDocument = graphClone(entry.document);
    savedDocument.schema_version = 3;
    savedDocument.edit_sequence = 1;
    const savedEntry = { ...entry, document: savedDocument, saved_document: graphClone(savedDocument),
      saved_revision: savedDocument.revision };
    const savedNode = savedDocument.nodes.find(row => row.node_binding_id === firstId)!;

    expect(frontendNodeInputs(savedDocument, graph.catalog).map(row => row.node_binding_id)).toEqual([inputId]);
    expect(savedNode.config.input_node_id).toBe(inputId);
    expect(frontendNodeLaunchTarget(savedEntry, { node: savedNode, document: savedDocument }, null).owner)
      .toEqual({ workflow_definition_id: workflowId, definition_revision: savedEntry.saved_revision,
        node_binding_id: firstId, session_id: null });
  });

});
