import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone } from "../domain/workflowGraph";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock } from "../testUtils/frontendFixture";
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
  it("assigns a fresh independent key to each declared provider instance without creating a session", async () => {
    const { graph, workflowId } = await setup();
    const first = graph.addNode("tools.output@2", { x: 0, y: 0 })!;
    const second = graph.addNode("tools.output@2", { x: 250, y: 0 })!;
    expect(first).toBeTruthy(); expect(second).toBeTruthy();
    const bindings = graph.document!.object_bindings!;

    expect(bindings).toHaveLength(2);
    expect(new Set(bindings.map(binding => binding.object_key)).size).toBe(2);
    expect(bindings.map(binding => binding.registrar_node_id)).toEqual([first, second]);
    expect(bindings.every(binding => binding.type_id === "workflow.frontend-state"
      && binding.schema_version === 1 && binding.scope === "shared" && !binding.readers.length
      && !binding.writers.length)).toBe(true);
    expect(bindings.every(binding => ![first, second].includes(binding.object_key))).toBe(true);
    expect(graph.entries[workflowId]!.session_id).toBeNull();
    expect(graph.session).toBeNull();
  });

  it("copies a provider registration to a new key and rewires only copied exact-type consumers", async () => {
    const { graph, workflowId } = await setup();
    const provider = graph.addNode("tools.output@2", { x: 0, y: 0 })!;
    expect(provider).toBeTruthy();
    const sourceKey = graph.document!.object_bindings!.find(binding =>
      binding.registrar_node_id === provider)!.object_key;
    const reader = graph.addNode("frontend.state.output@1", { x: 200, y: 0 })!;
    const writer = graph.addNode("frontend.state.append@1", { x: 400, y: 0 })!;
    graph.patchNode(reader, { config: { object_key: sourceKey } });
    graph.patchNode(writer, { config: { object_key: sourceKey, role: "assistant" } });
    const sourceBinding = { ...graph.document!.object_bindings![0]!,
      readers: [reader], writers: [writer], default_value: { entries: [], view_ref: null } };
    graph.setObjectBindings([sourceBinding]);
    const before = graphClone(graph.document!);

    graph.selectedNodeIds = [provider, reader, writer];
    graph.duplicateSelection();

    const copies = new Map(graph.document!.nodes.filter(node => !before.nodes.some(old => old.node_binding_id === node.node_binding_id))
      .map(node => [node.component_id, node]));
    const copyProvider = copies.get("tools.output")!;
    const copyReader = copies.get("frontend.state.output")!;
    const copyWriter = copies.get("frontend.state.append")!;
    const copiedBinding = graph.document!.object_bindings!.find(binding =>
      binding.registrar_node_id === copyProvider.node_binding_id)!;

    expect(copiedBinding.object_key).not.toBe(sourceKey);
    expect(copiedBinding.object_key).not.toBe(copyProvider.node_binding_id);
    expect(copiedBinding).toMatchObject({
      type_id: "workflow.frontend-state", schema_version: 1,
      registrar_node_id: copyProvider.node_binding_id,
      readers: [copyReader.node_binding_id], writers: [copyWriter.node_binding_id],
      default_value: { entries: [], view_ref: null },
    });
    expect(copyReader.config.object_key).toBe(copiedBinding.object_key);
    expect(copyWriter.config.object_key).toBe(copiedBinding.object_key);
    expect(graph.document!.object_bindings!.find(binding =>
      binding.registrar_node_id === provider)).toEqual(sourceBinding);
    expect(graph.document!.nodes.find(node => node.node_binding_id === reader)?.config.object_key).toBe(sourceKey);
    expect(graph.document!.nodes.find(node => node.node_binding_id === writer)?.config.object_key).toBe(sourceKey);
    expect(graph.entries[workflowId]!.session_id).toBeNull();
  });

  it("keeps unselected consumers on the source item and removes only the edited registration when its provider is removed", async () => {
    const { graph } = await setup();
    const provider = graph.addNode("tools.output@2", { x: 0, y: 0 })!;
    expect(provider).toBeTruthy();
    const sourceBinding = graph.document!.object_bindings![0]!;
    const reader = graph.addNode("frontend.state.output@1", { x: 200, y: 0 })!;
    graph.patchNode(reader, { config: { object_key: sourceBinding.object_key } });
    const beforeBinding = graphClone(sourceBinding);

    graph.selectedNodeIds = [provider];
    graph.duplicateSelection();
    const copiedProvider = graph.document!.nodes.find(node => node.component_id === "tools.output"
      && node.node_binding_id !== provider)!;
    const copiedBinding = graph.document!.object_bindings!.find(binding =>
      binding.registrar_node_id === copiedProvider.node_binding_id)!;
    expect(copiedBinding.object_key).not.toBe(sourceBinding.object_key);
    expect(copiedBinding.readers).toEqual([]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === reader)?.config.object_key)
      .toBe(sourceBinding.object_key);
    expect(graph.document!.object_bindings!.find(binding => binding.registrar_node_id === provider))
      .toEqual(beforeBinding);

    graph.selectedNodeIds = [provider];
    graph.removeSelection();
    expect(graph.document!.object_bindings!.some(binding => binding.registrar_node_id === provider)).toBe(false);
    expect(graph.document!.object_bindings!.some(binding => binding.registrar_node_id === copiedProvider.node_binding_id)).toBe(true);
  });

  it("preserves an exact registration through replacement and drops it when the replacement declares none", async () => {
    const { graph } = await setup();
    const provider = graph.addNode("tools.output@2", { x: 0, y: 0 })!;
    expect(provider).toBeTruthy();
    const original = graphClone(graph.document!.object_bindings![0]!);
    graph.replaceNode(provider, "tools.output@2");
    expect(graph.document!.object_bindings![0]).toEqual(original);

    graph.replaceNode(provider, "tools.output@1");
    expect(graph.document!.object_bindings!.some(binding => binding.registrar_node_id === provider)).toBe(false);
  });
});
