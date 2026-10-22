import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { graphClone, isGraphDocument } from "../domain/workflowGraph";
import type { GraphConfigurationUpdateRequest, GraphConfigurationResult } from "../domain/workflowConfiguration";
import { preloadBinding, preloadPlainType, preloadVariableType, preloadVariableValue } from "../testUtils/sessionPreloadFixture";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { variableCatalog } from "../testUtils/variableFixture";
import { frontendExecutionLock, frontendSession } from "../testUtils/frontendFixture";
import { useWorkspaceStore } from "./workspace";
import { useWorkflowGraphStore } from "./workflowGraph";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });
async function setup() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  graph.catalog = graphClone(variableCatalog); graph.executionPackageLock = graphClone(frontendExecutionLock);
  graph.dataTypes = [preloadPlainType, preloadVariableType];
  const id = graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  return { graph, id, evidence };
}
describe("atomic preload editing", () => {
  it("selects the exact session type when the same content identity appears first in the catalog", async () => {
    const { graph } = await setup();
    graph.dataTypes = [{ ...preloadPlainType, scope: "content" }, preloadPlainType];
    expect(graph.setSessionPreload(preloadBinding, { label: "contentFirst" })).toBeTruthy();
    expect(graph.document!.session_preloads).toEqual([
      { object_key: preloadBinding.object_key, value: { label: "contentFirst" } },
    ]);
    graph.dataTypes = [{ ...preloadPlainType, scope: "content" }];
    expect(graph.setSessionPreload(preloadBinding, { label: "contentOnly" }, preloadBinding.object_key)).toBeNull();
    expect(graph.document!.session_preloads![0]!.value).toEqual({ label: "contentFirst" });
  });
  it("keeps omitted fields, rejects conflicts and retains provider bindings", async () => {
    const { graph } = await setup();
    expect(Object.hasOwn(graph.document!, "session_preloads")).toBe(false);
    expect(graph.setSessionPreload(preloadBinding, { value: NaN })).toBeNull();
    const provider = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    expect(graph.setSessionPreload(preloadBinding, { label: "seed" })).toBeTruthy();
    expect(graph.document!.session_preloads).toEqual([{ object_key: preloadBinding.object_key, value: { label: "seed" } }]);
    expect(graph.setObjectBindings(graph.document!.object_bindings!.filter(binding => binding.object_key !== preloadBinding.object_key))).toBeNull();
    expect(graph.setSessionPreload(preloadBinding, { label: "duplicate" })).toBeNull();
    expect(graph.setSessionPreload({ ...preloadBinding, object_key: "shared/renamed" }, { label: "renamed" },
      preloadBinding.object_key)).toBeNull();
    expect(graph.document!.session_preloads![0]!.object_key).toBe(preloadBinding.object_key);
    graph.replaceNode(provider, "tools.variable-register@2");
    expect(graph.document!.session_preloads).toHaveLength(1);
    expect(graph.document!.object_bindings).toHaveLength(2);
    expect(isGraphDocument(graph.document)).toBe(true);
    graph.removeSessionPreload(preloadBinding.object_key);
    expect(graph.document!.session_preloads).toEqual([]);
    expect(graph.document!.object_bindings).toHaveLength(1);
  });
  it("copies private initial values with the owner and prunes them on owner removal", async () => {
    const { graph } = await setup();
    const owner = graph.addNode("tools.variable-to-content@1", { x: 0, y: 0 })!;
    const binding = { ...preloadBinding, type_id: "workflow.variable", scope: "private" as const, owner_node_id: owner, readers: [owner], writers: [] };
    graph.setSessionPreload(binding, preloadVariableValue);
    graph.patchNode(owner, { config: { object_key: binding.object_key } });
    graph.selectedNodeIds = [owner]; graph.duplicateSelection();
    const copied = graph.document!.session_preloads!.find(entry => entry.object_key !== binding.object_key)!;
    expect(copied.value).toEqual(preloadVariableValue);
    expect(graph.document!.object_bindings!.find(item => item.object_key === copied.object_key)?.owner_node_id).toBe(graph.selectedNodeIds[0]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === graph.selectedNodeIds[0])!.config.object_key).toBe(copied.object_key);
    graph.removeSelection();
    expect(graph.document!.session_preloads).toEqual([{ object_key: binding.object_key, value: preloadVariableValue }]);
    expect(isGraphDocument(graph.document)).toBe(true);
  });
  it("blocks running, archived, fixed and unknown request states at the store boundary", async () => {
    const { graph, id } = await setup();
    const view = frontendSession(id); view.status = "running"; view.can_submit = true;
    graph.entries[id]!.session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    expect(graph.setSessionPreload(preloadBinding, {})).toBeNull();
    graph.entries[id]!.session_id = null;
    const row = useWorkspaceStore().workflows.find(row => row.id === id)!;
    row.archived = true; expect(graph.setSessionPreload(preloadBinding, {})).toBeNull();
    row.archived = false; graph.entries[id]!.fixed_only = true;
    expect(graph.setSessionPreload(preloadBinding, {})).toBeNull();
    graph.entries[id]!.fixed_only = false;
    graph.entries[id]!.pending = { action: "save" } as never;
    expect(graph.setSessionPreload(preloadBinding, {})).toBeNull();
    expect(graph.document!.session_preloads).toBeUndefined();
  });
  it("submits binding and initial value through one formal evidence-protected configuration request", async () => {
    const { graph, evidence } = await setup();
    const requests: GraphConfigurationUpdateRequest[] = [];
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult> => {
      requests.push(graphClone(request));
      const document = { ...graphClone(request.document), revision: request.expected_edit_sequence === 0 ? 1 : request.document.revision + 1,
        edit_sequence: request.expected_edit_sequence + 1 };
      return { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
        source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: request.workflow_definition_id,
        definition_revision: document.revision, edit_sequence: document.edit_sequence, source_has_effective_session: false,
        changed_categories: ["fixed"], node_id_map: {}, session_id: null, document };
    });
    graph.installWorkflowConfigurationApplication(createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [], update }), { seed: true });
    await vi.waitFor(() => expect(graph.editorLocked).toBe(false));
    graph.setSessionPreload(preloadBinding, { label: "formal" });
    await vi.waitFor(() => expect(requests).toHaveLength(1));
    await vi.waitFor(() => expect(graph.editorLocked).toBe(false));
    expect(requests[0]!.document).toMatchObject({ object_bindings: [preloadBinding],
      session_preloads: [{ object_key: preloadBinding.object_key, value: { label: "formal" } }] });
    expect(graph.active!.saved_document!.session_preloads).toEqual(requests[0]!.document.session_preloads);
  });
});
