import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import type { WorkflowPreview } from "../domain/workflowPreview";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import GraphDiagnosticList from "../components/GraphDiagnosticList.vue";
import { diagnosticExpectation, diagnosticFailureStage, diagnosticLocation,
  occupyingSessionLabels } from "../domain/diagnosticPresentation";
import { availableControlLabels, closeRunConsequences } from "../domain/runClosePresentation";

/**
 * Independent verifier acceptance for step 3 frontend (V3-01..V3-05).
 *
 * Everything is driven through the real pinia store and the injected
 * configuration/evidence ports; no real browser or product service is used.
 * The preview read and the management commands are exercised through the real
 * HTTP adapters with a stubbed ``fetch``, so the diagnostics consumed by the UI
 * are produced by the same normalization path the product uses.
 */

type Harness = Awaited<ReturnType<typeof configured>>;

const port = (port_id: string, data_type: string, data_schema_version = 2) =>
  ({ port_id, data_type, data_schema_version, required: false, multiple: false });
const type = (component_id: string, version: string, overrides: Partial<GraphNodeType> = {}): GraphNodeType => ({
  component_id, component_version: version, display_name: component_id, category: "verifier",
  config_schema: { type: "object" }, default_config: {}, inputs: [], outputs: [],
  executable: true, is_output: false, ...overrides,
});
const reference = (config_field: string, target: string) => ({
  config_field, multiple: false as const, allow_empty: true as const,
  target_types: [{ component_id: target, component_version: "1" }],
});

const catalog: GraphNodeType[] = [
  type("verifier.source", "1", { outputs: [port("output", "TEXT")] }),
  type("verifier.output", "1", { inputs: [port("input", "TEXT")], is_output: true }),
  type("verifier.refer", "1", { inputs: [port("input", "TEXT")], outputs: [port("output", "TEXT")],
    config_node_references: [reference("source_ref", "verifier.source")] }),
  type("verifier.access", "1", { outputs: [port("output", "TEXT")], capabilities: ["objects:read"],
    object_accesses: [{ config_field: "object_key", type_id: "verifier.object", schema_version: 1,
      access: "read" as const, multiple: false as const }] }),
  type("verifier.provider", "1", { outputs: [port("output", "TEXT")],
    provided_session_item_types: [{ type_id: "verifier.object", schema_version: 1 }] }),
  // Replacement family: one compatible component, one with an unrelated input port.
  type("verifier.alt", "1", { outputs: [port("output", "TEXT")] }),
  type("verifier.incompatible", "1", { inputs: [port("other", "PROMPT")] }),
  // accepted_types family: a widening input accepts JSON@1 even though it declares TEXT@2.
  type("verifier.json-source", "1", { outputs: [port("output", "JSON", 1)] }),
  type("verifier.json-alt", "1", { outputs: [port("output", "JSON", 1)] }),
  type("verifier.widen", "1", { inputs: [{ ...port("input", "TEXT", 2),
    accepted_types: [{ data_type: "JSON", data_schema_version: 1 }] }], is_output: true }),
  type("verifier.narrow", "1", { inputs: [port("input", "TEXT", 2)], is_output: true }),
];

function sessionView(document: GraphDocument, overrides: Partial<GraphSession> = {}): GraphSession {
  return {
    schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    available_actions: ["close"], nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} }, ...overrides,
  } as GraphSession;
}

function previewPayload(overrides: Partial<WorkflowPreview> = {}): Record<string, unknown> {
  const base = {
    schema_version: 1, kind: "workflow.configuration-preview", contract_version: 2, read_only: true,
    reserves_save_right: false,
    structure: { ok: true, diagnostics: [] },
    declared_references: { ok: true, diagnostics: [] },
    compilation: { ok: true, skipped: false, target_node_ids: [], ordered_node_count: 2 },
    baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
      occupying_sessions: [], has_effective_session: false },
    changes: { categories: ["display"], would_outcome: "saved", would_require_confirmation: false,
      would_derive_after_confirmation: false },
    run_conditions: { package_lock_available: true,
      checked: ["structure", "declared_references", "compilation", "package_lock_membership"],
      not_checked: ["modification entitlement and CAS re-check at submit", "model calls"] },
    notes: ["preview writes nothing"],
  };
  return { ...base, ...overrides } as Record<string, unknown>;
}

async function configured() {
  const graph = useWorkflowGraphStore();
  graph.catalog = graphClone(catalog);
  const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
  const id = await graph.createWorkflow();
  await configuration.settled(id);
  return { graph, configuration, id };
}

async function addNode(harness: Harness, component: string, position = { x: 0, y: 0 }) {
  const nodeId = await harness.graph.addNode(component, position);
  expect(nodeId).toBeTruthy();
  await harness.configuration.settled(harness.id);
  return nodeId!;
}

async function connect(harness: Harness, source: string, target: string) {
  const result = await harness.graph.connect({ source_node_id: source, source_port_id: "output",
    target_node_id: target, target_port_id: "input" });
  expect(result).toMatchObject({ outcome: "accepted" });
  await harness.configuration.settled(harness.id);
}

let pinia: ReturnType<typeof createPinia> | null = null;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => {
  if (pinia) { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); pinia = null; }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// V3-02 frontend: declared reference / object binding deletion and replacement
// blockers with the impact list, no submission, and fix-then-delete.
// ---------------------------------------------------------------------------
describe("V3-02 deletion/replacement blockers", () => {
  it("blocks deleting a node that a surviving node still declares, lists the impact and submits nothing", async () => {
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    const referId = await addNode(harness, "verifier.refer@1", { x: 300, y: 0 });
    await harness.graph.patchNode(referId, { config: { source_ref: sourceId } });
    await harness.configuration.settled(harness.id);
    const before = graphClone(harness.graph.document!);
    const calls = harness.configuration.update.mock.calls.length;

    harness.graph.selectNodes([sourceId]);
    const result = await harness.graph.removeSelection();
    expect(result).toMatchObject({ outcome: "failed" });
    // Nothing was submitted and the accepted document is untouched.
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    expect(harness.graph.document).toEqual(before);

    const block = harness.graph.activeEditBlock!;
    expect(block).not.toBeNull();
    expect(block.action).toBe("delete-selection");
    expect(block.targetIds).toEqual([sourceId]);
    expect(block.summary).toContain("未提交");
    const item = block.items[0]!;
    expect(item.kind).toBe("node-reference");
    expect(item.nodeId).toBe(referId);
    expect(item.configField).toBe("source_ref");
    expect(item.path).toBe(`/nodes/${before.nodes.findIndex(node => node.node_binding_id === referId)}/config/source_ref`);
    expect(item.targetNodeId).toBe(sourceId);
    expect(item.targetNodeTitle).toBeTruthy();
    expect(item.nodeTitle).toBeTruthy();

    // The persistent block is independent from the transient notices.
    const notices = useWorkbenchNoticesStore();
    notices.notices.splice(0, notices.notices.length);
    expect(harness.graph.activeEditBlock).not.toBeNull();
    harness.graph.dismissEditBlock();
    expect(harness.graph.activeEditBlock).toBeNull();
  });

  it("blocks deleting a registrar whose object key a surviving node still uses", async () => {
    const harness = await configured();
    const providerId = await addNode(harness, "verifier.provider@1");
    const accessId = await addNode(harness, "verifier.access@1", { x: 300, y: 0 });
    const seeded = graphClone(harness.graph.document!);
    seeded.object_bindings = [{ object_key: "ctx", type_id: "verifier.object", schema_version: 1,
      scope: "shared", readers: [accessId], writers: [], owner_node_id: null,
      registrar_node_id: providerId }];
    seeded.session_preloads = [{ object_key: "ctx", value: null }];
    seeded.nodes.find(node => node.node_binding_id === accessId)!.config.object_key = "ctx";
    harness.graph.entries[harness.id]!.document = seeded;
    harness.graph.selectNodes([providerId]);

    const result = await harness.graph.removeSelection();
    expect(result).toMatchObject({ outcome: "failed" });
    const item = harness.graph.activeEditBlock!.items.find(row => row.kind === "object-access")!;
    expect(item.objectKey).toBe("ctx");
    expect(item.typeId).toBe("verifier.object");
    expect(item.nodeId).toBe(accessId);
    expect(item.configField).toBe("object_key");
    expect(item.path).toMatch(/^\/nodes\/\d+\/config\/object_key$/);
    expect(item.targetNodeId).toBe(providerId);
  });

  it("deletes after the reference is fixed, and a stale Delete submits nothing", async () => {
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    const referId = await addNode(harness, "verifier.refer@1", { x: 300, y: 0 });
    await harness.graph.patchNode(referId, { config: { source_ref: sourceId } });
    await harness.configuration.settled(harness.id);
    harness.graph.selectNodes([sourceId]);
    expect(await harness.graph.removeSelection()).toMatchObject({ outcome: "failed" });
    expect(harness.graph.activeEditBlock).not.toBeNull();

    // Fix the declared reference first; the block clears on the accepted edit.
    expect(await harness.graph.patchNode(referId, { config: { source_ref: "" } }))
      .toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.activeEditBlock).toBeNull();

    harness.graph.selectNodes([sourceId]);
    expect(await harness.graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.document!.nodes.some(node => node.node_binding_id === sourceId)).toBe(false);

    // A later Delete with an already-deleted identity is a no-change submission.
    const calls = harness.configuration.update.mock.calls.length;
    harness.graph.selectNodes([sourceId]);
    expect(await harness.graph.removeSelection()).toMatchObject({ outcome: "no-change" });
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    expect(harness.graph.selectedNodeIds).toEqual([]);
  });

  it("blocks an incompatible replacement with edge details and keeps a compatible edge identity/order", async () => {
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    const outputId = await addNode(harness, "verifier.output@1", { x: 400, y: 0 });
    await connect(harness, sourceId, outputId);
    const before = graphClone(harness.graph.document!);
    const calls = harness.configuration.update.mock.calls.length;

    // verifier.incompatible has no "output" port: the old edge cannot be kept.
    expect(await harness.graph.replaceNode(sourceId, "verifier.incompatible@1"))
      .toMatchObject({ outcome: "failed" });
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    expect(harness.graph.document).toEqual(before);
    const block = harness.graph.activeEditBlock!;
    expect(block.action).toBe("replace-node");
    const edgeBlocker = block.items.find(row => row.kind === "edge-port")!;
    expect(edgeBlocker.edgeId).toBe(before.edges[0]!.edge_id);
    expect(edgeBlocker.path).toMatch(/^\/edges\/0\/(source|target)_port_id$/);
    expect(edgeBlocker.message).toContain(before.edges[0]!.edge_id);
    harness.graph.dismissEditBlock();

    // verifier.alt has the same declared output port: the edge survives unchanged.
    expect(await harness.graph.replaceNode(sourceId, "verifier.alt@1"))
      .toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    const after = harness.graph.document!;
    expect(after.nodes.find(node => node.node_binding_id === sourceId)!.component_id).toBe("verifier.alt");
    expect(after.edges.map(edge => [edge.edge_id, edge.order]))
      .toEqual(before.edges.map(edge => [edge.edge_id, edge.order]));
    expect(harness.graph.selectedNodeIds).toEqual([sourceId]);
  });

  it("blocks a replacement that would leave a surviving declared reference to the old component", async () => {
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    const referId = await addNode(harness, "verifier.refer@1", { x: 300, y: 0 });
    await harness.graph.patchNode(referId, { config: { source_ref: sourceId } });
    await harness.configuration.settled(harness.id);

    expect(await harness.graph.replaceNode(sourceId, "verifier.alt@1"))
      .toMatchObject({ outcome: "failed" });
    const item = harness.graph.activeEditBlock!.items.find(row => row.kind === "node-reference")!;
    expect(item.nodeId).toBe(referId);
    expect(item.targetNodeId).toBe(sourceId);
    expect(item.configField).toBe("source_ref");
    expect(item.message).toContain("verifier.alt");
  });

  it("honours the integrated accepted_types rule when checking edge compatibility", async () => {
    // The widening input declares accepted_types JSON@1, so replacing the TEXT@2 source
    // by a JSON@1 output keeps the edge compatible — the same rule as the backend.
    const harness = await configured();
    const textSource = await addNode(harness, "verifier.source@1");
    const widen = await addNode(harness, "verifier.widen@1", { x: 400, y: 0 });
    await connect(harness, textSource, widen);
    expect(await harness.graph.replaceNode(textSource, "verifier.json-alt@1"))
      .toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.document!.edges).toHaveLength(1);

    // Against an identical input without accepted_types the same replacement is blocked.
    const second = await configured();
    const textSource2 = await addNode(second, "verifier.source@1");
    const narrow = await addNode(second, "verifier.narrow@1", { x: 400, y: 0 });
    await connect(second, textSource2, narrow);
    expect(await second.graph.replaceNode(textSource2, "verifier.json-alt@1"))
      .toMatchObject({ outcome: "failed" });
    expect(second.graph.activeEditBlock!.items.some(row => row.kind === "edge-type")).toBe(true);
  });

  it("records a locatable local save rejection for a dangling ordinary edge without submitting", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    const seeded = graphClone(harness.graph.document!);
    const missing = crypto.randomUUID();
    seeded.edges = [{ edge_id: crypto.randomUUID(), source_node_id: nodeId, source_port_id: "output",
      target_node_id: missing, target_port_id: "input", order: 0 }];
    harness.graph.entries[harness.id]!.document = seeded;
    const calls = harness.configuration.update.mock.calls.length;

    expect(await harness.graph.patchNode(nodeId, { title: "renamed with dangling edge" }))
      .toMatchObject({ outcome: "failed" });
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    const failure = harness.graph.activeEditFailure!;
    expect(failure.code).toBe("graph_dangling_edge");
    expect(failure.diagnostics[0]!.edge_id).toBe(seeded.edges[0]!.edge_id);
    expect(failure.diagnostics[0]!.node_id).toBe(missing);
    expect(failure.reason).toContain("保存被阻止");
  });

  it("deletes a referrer together with its referenced node in the same batch", async () => {
    // Q05-2 allows fixing the impact inside one modification: removing both the
    // referenced node and its surviving referrer leaves no live declared reference.
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    const referId = await addNode(harness, "verifier.refer@1", { x: 300, y: 0 });
    await harness.graph.patchNode(referId, { config: { source_ref: sourceId } });
    await harness.configuration.settled(harness.id);

    harness.graph.selectNodes([sourceId, referId]);
    expect(await harness.graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.document!.nodes).toHaveLength(0);
    expect(harness.graph.activeEditBlock).toBeNull();
  });

  it("still saves an incomplete graph: compile-only problems are not pre-blocked locally", async () => {
    // V3-01: a graph without connected outputs or with unconnected inputs is savable; only
    // the local dangling-edge rule is a save block, the rest stays a compile diagnostic.
    const harness = await configured();
    const sourceId = await addNode(harness, "verifier.source@1");
    expect(await harness.graph.patchNode(sourceId, { title: "unconnected but saved" }))
      .toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.document!.edges).toEqual([]);
    expect(harness.graph.activeEditFailure).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// V3-03 frontend: structured diagnostics (phase/path/expected/actual) survive
// the acceptance boundary, stay reachable after switching, and render.
// ---------------------------------------------------------------------------
describe("V3-03 structured save diagnostics", () => {
  it("keeps the located backend diagnostics of an explicit save rejection", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    const error = new WorkbenchApiError("rejected",
      "数据边端点节点不存在 [HTTP 400 / graph_missing_edge_node]", 400, "graph_missing_edge_node");
    error.diagnostics = [{ reason_code: "graph_missing_edge_node", code: "graph_missing_edge_node",
      message: "An ordinary edge endpoint names a missing node", phase: "save",
      path: "/edges/0/target_node_id", edge_id: crypto.randomUUID(), node_id: crypto.randomUUID(),
      expected: { node_id_exists: true }, actual: { node_id_exists: false } }];
    harness.configuration.update.mockRejectedValueOnce(error);

    expect(await harness.graph.patchNode(nodeId, { title: "rejected save" }))
      .toMatchObject({ outcome: "failed" });
    const failure = harness.graph.activeEditFailure!;
    expect(failure).not.toBeNull();
    expect(failure.code).toBe("graph_missing_edge_node");
    expect(failure.status).toBe(400);
    expect(failure.diagnostics).toHaveLength(1);
    expect(failure.diagnostics[0]).toMatchObject({ phase: "save", path: "/edges/0/target_node_id" });
    expect(failure.targetLabel).toContain("保存校验");

    // Presentation helpers expose phase/code/path/expected/actual without a payload dump.
    const diagnostic = failure.diagnostics[0]!;
    expect(diagnosticFailureStage(diagnostic)).toContain("保存校验");
    expect(diagnosticLocation(diagnostic)).toContain("graph_missing_edge_node");
    expect(diagnosticLocation(diagnostic)).toContain("/edges/0/target_node_id");
    expect(diagnosticExpectation(diagnostic)).toContain("期望");
    expect(diagnosticExpectation(diagnostic)).toContain("实际");
    const html = await renderToString(createSSRApp(GraphDiagnosticList, { diagnostics: [diagnostic] }));
    expect(html).toContain("保存校验");
    expect(html).toContain("graph_missing_edge_node");
    expect(html).toContain("/edges/0/target_node_id");

    // The failure is a persistent store record, not a five-second notice.
    const notices = useWorkbenchNoticesStore();
    notices.notices.splice(0, notices.notices.length);
    expect(harness.graph.activeEditFailure).not.toBeNull();

    // Switching workflows keeps the original target identity; coming back restores it.
    const otherId = await harness.graph.createWorkflow();
    await harness.configuration.settled(otherId);
    expect(harness.graph.activeEditFailure).toBeNull();
    harness.graph.entries[otherId]; // keep the second workflow available
    useWorkspaceStore().openWorkflow(harness.id);
    const restored = harness.graph.activeEditFailure!;
    expect(restored).not.toBeNull();
    expect(restored.diagnostics[0]!.path).toBe("/edges/0/target_node_id");
    expect(restored.targetLabel).toContain("保存校验");
    harness.graph.dismissEditFailure();
    expect(harness.graph.activeEditFailure).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// V3-04 frontend: read-only preview display, no write, no save right, failure
// path unchanged.
// ---------------------------------------------------------------------------
describe("V3-04 read-only save preview", () => {
  it("reads the preview with the accepted baseline, displays every section and writes nothing", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    await harness.graph.patchNode(nodeId, { title: "pending preview edit" });
    await harness.configuration.settled(harness.id);
    const occupant = crypto.randomUUID(), chain = crypto.randomUUID();
    const requests: { path: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      requests.push({ path, body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify(previewPayload({
        baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: true,
          occupying_sessions: [{ workflow_session_id: occupant, active_chain_run_id: chain }],
          has_effective_session: true },
        changes: { categories: ["display"], would_outcome: "confirmation_required",
          would_require_confirmation: true, would_derive_after_confirmation: false },
      })));
    }));
    const documentBefore = graphClone(harness.graph.document!);
    const calls = harness.configuration.update.mock.calls.length;

    expect(await harness.graph.previewConfiguration(harness.id)).toBe(true);
    expect(requests).toHaveLength(1);
    expect(requests[0]!.path).toBe("/api/graph/queries");
    expect(requests[0]!.body).toMatchObject({ operation: "definition.preview",
      parameters: { workflow_definition_id: harness.id } });
    expect((requests[0]!.body.parameters as { expected_edit_sequence: number }).expected_edit_sequence)
      .toBe(harness.configuration.application.accepted(harness.id)!.edit_sequence);
    // The preview is a read: it neither writes the document nor takes the save right.
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    expect(harness.graph.document).toEqual(documentBefore);
    expect(harness.graph.locked).toBe(false);

    const state = harness.graph.activePreview!;
    expect(state).toMatchObject({ busy: false, stale: false, error: "" });
    const preview = state.preview!;
    expect(preview).toMatchObject({ contract_version: 2, read_only: true, reserves_save_right: false });
    expect(preview.changes.would_outcome).toBe("confirmation_required");
    expect(preview.changes.would_require_confirmation).toBe(true);
    expect(preview.run_conditions.checked).toContain("compilation");
    expect(preview.run_conditions.not_checked.length).toBeGreaterThan(0);
    expect(occupyingSessionLabels(preview.baseline.occupying_sessions)).toEqual([
      `会话 ${occupant} · 活动链 ${chain}`]);

    // A later accepted edit marks the preview stale; the actual save path still works.
    expect(await harness.graph.patchNode(nodeId, { title: "after preview" }))
      .toMatchObject({ outcome: "accepted" });
    await harness.configuration.settled(harness.id);
    expect(harness.graph.activePreview!.stale).toBe(true);
    expect(harness.configuration.update.mock.calls.length).toBe(calls + 1);
    harness.graph.dismissPreview();
    expect(harness.graph.activePreview).toBeNull();
  });

  it("shows the preview read failure without touching the document or the save path", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(
      previewPayload({ contract_version: 1 })))));
    const documentBefore = graphClone(harness.graph.document!);
    const calls = harness.configuration.update.mock.calls.length;

    expect(await harness.graph.previewConfiguration(harness.id)).toBe(false);
    const state = harness.graph.activePreview!;
    expect(state.preview).toBeNull();
    expect(state.error).toContain("预演契约无效");
    expect(state.failure).not.toBeNull();
    expect(state.failure!.reason).toContain("预演契约无效");
    expect(harness.graph.document).toEqual(documentBefore);
    expect(harness.configuration.update.mock.calls.length).toBe(calls);
    // The real save path is unchanged after a failed preview.
    expect(await harness.graph.patchNode(nodeId, { title: "save after failed preview" }))
      .toMatchObject({ outcome: "accepted" });
    expect(harness.configuration.update.mock.calls.length).toBe(calls + 1);
  });
});

// ---------------------------------------------------------------------------
// V3-05 frontend: G21 occupant display and explicit close semantics (no auto
// close, original error response retained).
// ---------------------------------------------------------------------------
describe("V3-05 G21 occupancy and explicit close", () => {
  it("renders the occupying sessions of a G21 rejection and keeps them off the save path", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    const occupant = crypto.randomUUID(), chain = crypto.randomUUID();
    const error = new WorkbenchApiError("rejected",
      "工作流会话正在运行或被占用，配置修改被阻止 [HTTP 409 / configuration_run_locked]", 409,
      "configuration_run_locked");
    error.diagnostics = [{ reason_code: "configuration_run_locked", code: "configuration_run_locked",
      message: "Configuration cannot change while a workflow run is unfinished", phase: "save",
      path: "", occupying_sessions: [{ workflow_session_id: occupant, active_chain_run_id: chain }] }];
    harness.configuration.update.mockRejectedValueOnce(error);

    expect(await harness.graph.patchNode(nodeId, { title: "blocked by G21" }))
      .toMatchObject({ outcome: "failed" });
    const failure = harness.graph.activeEditFailure!;
    expect(failure.diagnostics[0]!.occupying_sessions).toEqual([
      { workflow_session_id: occupant, active_chain_run_id: chain }]);
    const html = await renderToString(createSSRApp(GraphDiagnosticList, {
      diagnostics: failure.diagnostics, sessionLabels: { [occupant]: "长跑会话" } }));
    expect(html).toContain("占用：长跑会话");
    expect(html).toContain(chain);
    // The store exposes no silent bypass: the accepted document is untouched.
    expect(harness.graph.document!.nodes[0]!.title).not.toBe("blocked by G21");
  });

  it("closes only when explicitly allowed and records the original close error", async () => {
    const harness = await configured();
    const view = sessionView(harness.graph.document!, { status: "paused",
      available_actions: ["resume", "close"], can_submit: false });
    harness.graph.entries[harness.id]!.session_id = view.workflow_session_id;
    harness.graph.views[view.workflow_session_id] = view;

    // The close control is explicit: its label and consequences never promise a full unlock.
    expect(availableControlLabels(["resume", "close"])).toEqual(["继续（resume）", "关闭本次运行（close）"]);
    expect(closeRunConsequences.join(" ")).toContain("不会自动关闭");
    expect(closeRunConsequences.join(" ")).toContain("不等于清空所有运行占用");

    // Without the explicit available action the store submits no close request.
    harness.graph.views[view.workflow_session_id] = { ...view, available_actions: ["resume"] } as GraphSession;
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      calls.push(String(path));
      return new Response(JSON.stringify({ error: { reason_code: "invalid_state" } }), { status: 409 });
    }));
    await harness.graph.closeRun();
    expect(calls).toEqual([]);

    // A rejected explicit close keeps the original response (code/status/phase/diagnostics).
    harness.graph.views[view.workflow_session_id] = view;
    stubGraphApplicationFetch(async (path: string) => {
      calls.push(path);
      return new Response(JSON.stringify({ error: { reason_code: "invalid_state", diagnostics: [{
        code: "invalid_state", message: "Close is not allowed in the current state",
        phase: "run", path: "/status", expected: { allowed: ["paused"] }, actual: { status: "paused" },
      }] } }), { status: 409 });
    });
    await harness.graph.closeRun();
    const failure = harness.graph.activeRunFailure!;
    expect(failure).not.toBeNull();
    expect(failure.operation).toBe("close");
    expect(failure.code).toBe("invalid_state");
    expect(failure.status).toBe(409);
    expect(failure.phase).toBe("run");
    expect(failure.diagnostics[0]).toMatchObject({ phase: "run", path: "/status" });
    expect(calls.some(path => path.endsWith("/control"))).toBe(true);
    expect(harness.graph.activeRunFailure).not.toBeNull(); // persistent until dismissed
    harness.graph.dismissRunFailure();
    expect(harness.graph.activeRunFailure).toBeNull();
  });

  it("stores a prepare-phase start failure with the original server response", async () => {
    const harness = await configured();
    const nodeId = await addNode(harness, "verifier.source@1");
    const view = sessionView(harness.graph.document!, { available_actions: ["close"] });
    harness.graph.entries[harness.id]!.session_id = view.workflow_session_id;
    harness.graph.views[view.workflow_session_id] = view;
    harness.graph.entries[harness.id]!.external_inputs = {};

    stubGraphApplicationFetch(async (path: string) => {
      if (path === `/api/graph/sessions/${view.workflow_session_id}`)
        return new Response(JSON.stringify(view));
      if (path.endsWith(`/sessions/${view.workflow_session_id}/runs`))
        return new Response(JSON.stringify({ error: { reason_code: "package_missing_dependency",
          correlation_id: crypto.randomUUID(),
          diagnostics: [{ code: "package_missing_dependency",
            message: "Execution package dependency is missing", phase: "prepare",
            path: "/package_lock", expected: { packages_available: true },
            actual: { missing: ["verifier.missing@1"] },
            cause_code: "model_provider_disabled", recovery: "start_new_run" }] } }), { status: 409 });
      return new Response(JSON.stringify({ error: { reason_code: "not_found" } }), { status: 404 });
    });
    await harness.graph.submitPrimary();
    const failure = harness.graph.activeRunFailure!;
    expect(failure).not.toBeNull();
    expect(failure.operation).toBe("start");
    expect(failure.code).toBe("package_missing_dependency");
    expect(failure.status).toBe(409);
    expect(failure.phase).toBe("prepare");
    expect(failure.diagnostics[0]).toMatchObject({ phase: "prepare", path: "/package_lock" });
    expect(failure.diagnostics[0]!.expected).toEqual({ packages_available: true });
    expect(failure.diagnostics[0]!.actual).toEqual({ missing: ["verifier.missing@1"] });
    // O01: the original response text/guidance is retained, not replaced by a bare notice.
    expect(failure.reason).toContain("package_missing_dependency");

    // It survives a workflow switch and is keyed to its own workflow.
    const otherId = await harness.graph.createWorkflow();
    await harness.configuration.settled(otherId);
    expect(harness.graph.activeRunFailure).toBeNull();
    useWorkspaceStore().openWorkflow(harness.id);
    expect(harness.graph.activeRunFailure!.code).toBe("package_missing_dependency");
    expect(nodeId).toBeTruthy();
  });
});
