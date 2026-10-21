import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone, type GraphNodeType } from "../domain/workflowGraph";
import { isGraphConfigurationDocument, type GraphConfigurationDocument,
  type GraphConfigurationResult, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

// 1.4-A/1.4-B: current formal reads and a late directory response are checked against
// the already accepted configuration basis. This fixture never writes product code.
const nodeType: GraphNodeType = {
  component_id: "sample.text", component_version: "1", display_name: "Text", category: "Test",
  config_schema: { type: "object", properties: { text: { type: "string" } } },
  default_config: { text: "A" }, inputs: [], outputs: [], executable: true, is_output: false,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

// Only adjustable edits may keep the fixed revision while edit_sequence advances; the
// first save (expected_edit_sequence 0) must be a fixed revision 1 result.
function savedResult(request: GraphConfigurationUpdateRequest, revision: number, editSequence: number,
  categories: GraphConfigurationResult["changed_categories"]): GraphConfigurationResult {
  const document: GraphConfigurationDocument = { ...graphClone(request.document), revision, edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id,
    definition_revision: revision, edit_sequence: editSequence,
    source_has_effective_session: false, changed_categories: categories,
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}

let pinia: Pinia | null = null;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => {
  if (pinia) disposePinia(pinia);
  pinia = null;
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

// Shared fixture: an unsaved draft with one text node is edited only after the formal
// application is installed, so A (revision 1, edit_sequence 1, text A) is accepted
// through the real store/application path rather than assigned to the store.
async function acceptedA() {
  const evidence = memoryRequestEvidence();
  // The store's management outbox reads the same original-request evidence port; it
  // must be initialized before editing so canEdit() is not blocked by its startup read.
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const updates: GraphConfigurationUpdateRequest[] = [];
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult> => {
    updates.push(graphClone(request));
    return savedResult(request, request.expected_edit_sequence === 0 ? 1 : request.document.revision,
      request.expected_edit_sequence + 1, request.expected_edit_sequence === 0 ? ["fixed"] : ["adjustable"]);
  });
  const application = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => [], update,
    readReceipt: vi.fn(async () => { throw new Error("No configuration receipt read in this fixture"); }),
  });
  const graph = useWorkflowGraphStore();
  graph.catalog = [graphClone(nodeType)];
  const id = graph.createWorkflow();
  await graph.reconcile(id);
  const nodeId = graph.addNode("sample.text@1", { x: 0, y: 0 })!;
  graph.installWorkflowConfigurationApplication(application);
  await application.forWorkflow(id).initialize();
  expect(graph.renameWorkflow("A")).toBe(id);
  await vi.waitFor(() => expect(updates).toHaveLength(1));
  await vi.waitFor(() => expect(application.document(id)?.edit_sequence).toBe(1));
  await vi.waitFor(() => expect(graph.entries[id]!.document.edit_sequence).toBe(1));
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const aDocument = graphClone(application.document(id)!) as GraphConfigurationDocument;
  expect(aDocument).toMatchObject({ revision: 1, edit_sequence: 1 });
  expect(aDocument.nodes[0]!.config.text).toBe("A");
  return { evidence, updates, update, application, graph, id, nodeId, aDocument };
}

describe("workflow configuration basis re-read and late directory responses", () => {
  it("re-reads current B from the formal directory while the fixed revision stays A and an old receipt cannot replace current", async () => {
    const initial = await acceptedA();
    expect(initial.graph.patchNode(initial.nodeId, { config: { text: "B" } })).toBe(initial.id);
    await vi.waitFor(() => expect(initial.updates).toHaveLength(2));
    await vi.waitFor(() => expect(initial.application.document(initial.id)?.edit_sequence).toBe(2));
    await vi.waitFor(() => expect(initial.graph.entries[initial.id]!.document.nodes[0]!.config.text).toBe("B"));
    await vi.waitFor(() => expect(initial.graph.locked).toBe(false));
    const bDocument = graphClone(initial.application.document(initial.id)!) as GraphConfigurationDocument;
    expect(bDocument).toMatchObject({ revision: 1, edit_sequence: 2 });
    expect(bDocument.nodes[0]!.config.text).toBe("B");

    // Rebuild the browser state: new pinia, new store and a new application; only the
    // formal directory response below restores B.
    disposePinia(pinia!);
    pinia = createPinia(); setActivePinia(pinia);
    const graph = useWorkflowGraphStore();
    graph.catalog = [graphClone(nodeType)];
    const application = createWorkflowConfigurationApplication({
      evidence: initial.evidence.ports, extensions: () => [],
      update: vi.fn(async () => { throw new Error("No configuration write during re-read"); }),
      readReceipt: vi.fn(async () => { throw new Error("No configuration receipt read during re-read"); }),
    });
    graph.installWorkflowConfigurationApplication(application);
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      if (path === `/api/graph/definitions/${initial.id}`)
        return new Response(JSON.stringify(bDocument));
      if (path === `/api/graph/definitions/${initial.id}/revisions/1`)
        return new Response(JSON.stringify(initial.aDocument));
      if (path === `/api/graph/definitions/${initial.id}/sessions`)
        return new Response(JSON.stringify([]));
      if (path === "/api/graph/definitions")
        return new Response(JSON.stringify([{ document: bDocument, archived: false }]));
      throw new Error(`Unexpected re-read request: ${path}`);
    }));

    expect(await graph.loadDefinitions()).toBe(true);
    const workspace = useWorkspaceStore();
    workspace.openWorkflow(initial.id);
    await vi.waitFor(() => expect(application.selection.value?.workflowId).toBe(initial.id));
    // Opening the workflow is what reads the formal current; the directory row alone
    // must not create an editor entry.
    expect(graph.entries[initial.id]).toBeUndefined();
    await graph.activate(initial.id);
    await application.forWorkflow(initial.id).initialize();

    expect(graph.entries[initial.id]!.document).toMatchObject({ revision: 1, edit_sequence: 2 });
    expect(graph.entries[initial.id]!.document.nodes[0]!.config.text).toBe("B");
    expect(graph.entries[initial.id]!.saved_revision).toBe(1);
    expect(application.current.value).toMatchObject({ workflow_definition_id: initial.id,
      revision: 1, edit_sequence: 2 });
    expect(application.current.value!.nodes[0]!.config.text).toBe("B");

    // Fixed revision query: revision 1 is still A on the fixed axis while the editable
    // current (definitions list) is B at edit_sequence 2.
    const fixed = await graph.management.queries.definition(initial.id, 1);
    expect(fixed.revision).toBe(1);
    expect(fixed.edit_sequence).toBe(1);
    expect(fixed.nodes[0]!.config.text).toBe("A");
    expect(isGraphConfigurationDocument(fixed)).toBe(true);

    // Feeding the fixed revision through the fixed source must not replace the
    // editable current B, even if a later fixed revision carried larger counters.
    application.observe(fixed as GraphConfigurationDocument, "fixed");
    expect(application.current.value).toMatchObject({ edit_sequence: 2 });
    expect(application.current.value!.nodes[0]!.config.text).toBe("B");
    expect(graph.entries[initial.id]!.document.nodes[0]!.config.text).toBe("B");
  });

  it("rejects an edit whose captured baseline changes while initialization is still waiting", async () => {
    const initial = await acceptedA();
    const { application, graph, id, updates } = initial;
    const channel = application.forWorkflow(id);
    let release!: () => void;
    const waiting = new Promise<void>(resolve => { release = resolve; });
    channel.initialize = vi.fn(async () => { await waiting; return true; });
    const before = updates.length;
    expect(graph.renameWorkflow("Changed while waiting")).toBe(id);

    // A newer current is accepted while the captured body is still waiting.
    const newer = { ...graphClone(application.document(id)!), revision: 2, edit_sequence: 3,
      name: "Formal elsewhere" };
    application.observe(newer);
    release();

    await vi.waitFor(() => expect(useWorkbenchNoticesStore().error?.reason).toContain("依据已变化"));
    expect(updates).toHaveLength(before);
    expect(application.document(id)).toMatchObject({ revision: 2, edit_sequence: 3, name: "Formal elsewhere" });
    // The rejected edit did not renumber the old body or send it as a new write.
    expect(graph.entries[id]!.document).toMatchObject({ revision: 2, edit_sequence: 3, name: "Formal elsewhere" });
    expect(graph.entries[id]!.document.name).not.toBe("Changed while waiting");
  });
  it("keeps the accepted store B when a late directory A for the same fixed revision arrives", async () => {
    const initial = await acceptedA();
    const { evidence, updates, application, graph, id, nodeId, aDocument } = initial;

    // The directory request is issued and captures A; its response is held until after B
    // has been accepted through the real configuration save path.
    const requested = deferred<boolean>(), released = deferred<boolean>();
    let captured: GraphConfigurationDocument | null = null;
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      if (path !== "/api/graph/definitions") throw new Error(`Unexpected request during the race: ${path}`);
      const snapshot = graphClone(graph.entries[id]!.document) as GraphConfigurationDocument;
      captured = snapshot;
      requested.resolve(true);
      await released.promise;
      return new Response(JSON.stringify([{ document: snapshot, archived: false }]));
    }));
    const loading = graph.loadDefinitions();
    await requested.promise;
    expect(captured).toEqual(aDocument);
    expect(captured!.edit_sequence).toBe(1);

    expect(graph.patchNode(nodeId, { config: { text: "B" } })).toBe(id);
    await vi.waitFor(() => expect(updates).toHaveLength(2));
    await vi.waitFor(() => expect(application.document(id)?.edit_sequence).toBe(2));
    await vi.waitFor(() => expect(graph.entries[id]!.document.nodes[0]!.config.text).toBe("B"));
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    expect(application.document(id)).toMatchObject({ revision: 1, edit_sequence: 2 });
    expect(graph.entries[id]!.saved_revision).toBe(1);

    released.resolve(true);
    expect(await loading).toBe(true);

    // A late directory row only updates the directory projection. It must not bypass
    // the single configuration acceptance path to replace the already accepted B.
    const workspaceRow = useWorkspaceStore().workflows.find(row => row.id === id);
    expect(workspaceRow?.title).toBe("A");
    const entry = graph.entries[id]!, current = application.current.value;
    // The late row is stale by edit_sequence; both accepted readers stay on B.
    expect(entry.document.revision).toBe(1);
    expect(entry.document.edit_sequence).toBe(2);
    expect(entry.document.nodes[0]!.config.text).toBe("B");
    expect(entry.saved_document?.nodes[0]!.config.text).toBe("B");
    expect(entry.saved_revision).toBe(1);
    expect(entry.latest_published_revision).toBe(1);
    expect(graph.document!.nodes[0]!.config.text).toBe("B");
    expect(application.document(id)).toMatchObject({ revision: 1, edit_sequence: 2 });
    expect(application.document(id)!.nodes[0]!.config.text).toBe("B");
    expect(current).not.toBeNull();
    expect(current!.edit_sequence).toBe(2);
    expect(current!.nodes[0]!.config.text).toBe("B");
    // Store projection and accepted current agree after the late directory row.
    expect(entry.document.edit_sequence).toBe(application.document(id)!.edit_sequence);
    expect(entry.document.nodes[0]!.config.text)
      .toBe(application.document(id)!.nodes[0]!.config.text);
    expect(evidence.records.size).toBe(2);
  });
});
