import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, type GraphNodeType } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { RequestEvidencePorts } from "../adapters/requestEvidenceApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

/**
 * Independent verifier acceptance checks for the step 1 frontend behavior.
 *
 * Covers V1-01(1) refused overlapping edits, V1-03 awaited outcomes and
 * persistent rejection feedback (including the derivation-ask scope), V1-04
 * response-lost arbitration/closure recovery on the frontend and V1-05 late
 * results after a view switch.  Everything is driven through the real pinia
 * stores and the injected configuration application; no browser is needed.
 */

const nodeType: GraphNodeType = {
  component_id: "sample.node", component_version: "1", display_name: "Sample", category: "Test",
  default_config: { text: "before" },
  config_schema: { type: "object", properties: { text: { type: "string" } } },
  inputs: [], outputs: [], executable: true, is_output: false,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function accepted(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  const revision = request.expected_edit_sequence === 0 ? 1 : request.document.revision + 1;
  const editSequence = request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), revision, edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
    edit_sequence: editSequence, source_has_effective_session: false, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}

function configurationResult(request: GraphConfigurationUpdateRequest, options: {
  outcome: GraphConfigurationResult["outcome"];
  categories: GraphConfigurationResult["changed_categories"];
  target?: string;
  hasSession?: boolean;
}): GraphConfigurationResult {
  const derived = options.outcome === "derived";
  const target = options.target ?? (derived ? crypto.randomUUID() : request.workflow_definition_id);
  const revision = derived ? 1 : options.outcome === "saved"
    ? request.document.revision + Number(options.categories.includes("fixed"))
    : request.document.revision;
  const editSequence = options.outcome === "confirmation_required"
    ? request.expected_edit_sequence : derived ? 1 : request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), workflow_definition_id: target, revision, edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: options.outcome,
    source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: target,
    definition_revision: revision, edit_sequence: editSequence,
    source_has_effective_session: options.hasSession ?? false, changed_categories: options.categories,
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}

type Harness = {
  graph: ReturnType<typeof useWorkflowGraphStore>;
  workspace: ReturnType<typeof useWorkspaceStore>;
  application: ReturnType<typeof createWorkflowConfigurationApplication>;
  evidence: ReturnType<typeof memoryRequestEvidence>;
  requests: GraphConfigurationUpdateRequest[];
  update: ReturnType<typeof vi.fn>;
  abandon: ReturnType<typeof vi.fn>;
  control: { next: ((request: GraphConfigurationUpdateRequest) => Promise<GraphConfigurationResult>) | null };
};

async function harness(options: {
  abandon?: (request: { idempotency_key: string; target_idempotency_key: string },
    original: GraphConfigurationUpdateRequest) => Promise<unknown>;
} = {}): Promise<Harness & { workflowId: string }> {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const requests: GraphConfigurationUpdateRequest[] = [];
  const control: Harness["control"] = { next: null };
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
    requests.push(graphClone(request));
    const pending = control.next;
    control.next = null;
    return pending ? pending(request) : accepted(request);
  });
  const abandon = vi.fn(options.abandon ?? (async () => {
    throw new WorkbenchApiError("unknown", "仲裁响应丢失");
  }));
  const application = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => [], update,
    readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)),
    abandon: abandon as never,
  });
  const workspace = useWorkspaceStore();
  const graph = useWorkflowGraphStore();
  graph.catalog = [graphClone(nodeType)];
  graph.packageLock = [{ package_id: "sample", version: "1.0.0" }];
  graph.installWorkflowConfigurationApplication(application);
  const workflowId = await graph.createWorkflow();
  expect(graph.entries[workflowId]?.saved_revision).toBe(1);
  expect(graph.locked).toBe(false);
  return { graph, workspace, application, evidence, requests, update, abandon, control, workflowId };
}

let activePinia: Pinia | null = null;
beforeEach(() => { activePinia = createPinia(); setActivePinia(activePinia); });
afterEach(() => {
  if (activePinia) disposePinia(activePinia);
  activePinia = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// V1-01(1): an overlapping edit is refused with a reason, never a silent null,
// and nothing is projected before the formal acceptance.
// ---------------------------------------------------------------------------
describe("V1-01 in-flight edit refusal", () => {
  it("refuses a second edit with a visible reason and keeps the accepted document", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    expect(nodeId).toBeTruthy();

    const gate = deferred<GraphConfigurationResult>();
    f.control.next = async () => gate.promise;
    const before = f.requests.length;
    const moving = f.graph.moveNodes([{ id: nodeId, position: { x: 5, y: 5 } }]);
    expect(f.graph.document!.nodes[0]!.position).toEqual({ x: 0, y: 0 });
    expect(f.graph.locked).toBe(true);
    await vi.waitFor(() => expect(f.requests.length).toBe(before + 1));
    const submitted = f.requests.length;

    const blocked = await f.graph.renameWorkflow("blocked while saving");
    expect(blocked).toMatchObject({ outcome: "failed" });
    expect((blocked as { error: Error }).error.message).toContain("工作流配置正在提交");
    expect(useWorkbenchNoticesStore().notices.some(row =>
      row.kind === "rejected" && row.reason.includes("工作流配置正在提交"))).toBe(true);

    // The refused add neither submits nor changes the accepted document/selection.
    expect(await f.graph.addNode("sample.node@1", { x: 10, y: 0 })).toBeNull();
    expect(f.graph.selectedNodeIds).toEqual([nodeId]);
    expect(f.graph.document!.nodes).toHaveLength(1);
    expect(f.graph.document!.name).not.toBe("blocked while saving");
    expect(f.requests.length).toBe(submitted);

    gate.resolve(accepted(f.requests.at(-1)!));
    expect(await moving).toMatchObject({ outcome: "accepted" });
    expect(f.graph.document!.nodes[0]!.position).toEqual({ x: 5, y: 5 });
    expect(f.graph.document!.name).not.toBe("blocked while saving");
  });

  it("returns no-change without submitting or claiming a save", async () => {
    const f = await harness();
    await f.graph.renameWorkflow("same name");
    const calls = f.requests.length;
    const same = await f.graph.renameWorkflow("same name");
    expect(same).toMatchObject({ outcome: "no-change" });
    expect(f.requests.length).toBe(calls);
    expect(useWorkbenchNoticesStore().notices.filter(row => row.kind)).toEqual([]);
    await expect(f.graph.renameWorkflow("")).rejects.toMatchObject({ kind: "rejected" });
    expect(f.requests.length).toBe(calls);
  });
});

// ---------------------------------------------------------------------------
// V1-03: the caller awaits real outcomes; a definite refusal keeps the failed
// proposal, its baseline and a persistent reason; nothing is auto-resubmitted.
// ---------------------------------------------------------------------------
describe("V1-03 awaited outcomes and persistent rejection", () => {
  it("keeps the rejected proposal and never resubmits it", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    const before = graphClone(f.graph.document!), history = graphClone(f.graph.history[f.workflowId]);
    f.control.next = async () => {
      throw new WorkbenchApiError("rejected", "工作流修改基线已变化", 409, "stale_edit_sequence");
    };

    const result = await f.graph.patchNode(nodeId, { config: { text: "refused body" } });
    const calls = f.requests.length;
    expect(result).toMatchObject({ outcome: "failed" });
    expect(f.graph.document).toEqual(before);
    expect(f.graph.history[f.workflowId]).toEqual(history);

    const rejected = f.graph.activeRejectedEdit;
    expect(rejected).not.toBeNull();
    expect(rejected!.reason).toContain("基线已变化");
    expect(rejected!.code).toBe("stale_edit_sequence");
    expect(rejected!.request.document.nodes[0]!.config.text).toBe("refused body");
    expect(rejected!.request.expected_edit_sequence).toBe(f.requests.at(-1)!.expected_edit_sequence);
    expect(rejected!.request.idempotency_key).toBeTypeOf("string");
    expect(useWorkbenchNoticesStore().notices.some(row =>
      row.kind === "rejected" && row.reason.includes("基线已变化"))).toBe(true);

    // The failed proposal is never automatically resubmitted, and the failed body
    // is not projected onto the accepted document.
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.requests.length).toBe(calls);
    expect(f.graph.document!.nodes[0]!.config.text).toBe("before");
    f.graph.dismissRejectedEdit();
    expect(f.graph.activeRejectedEdit).toBeNull();
  });

  it("waits for a formal result before changing selection and closing the entry", async () => {
    const f = await harness();
    const gate = deferred<GraphConfigurationResult>();
    f.control.next = async () => gate.promise;
    const adding = f.graph.addNode("sample.node@1", { x: 20, y: 20 });
    expect(f.graph.selectedNodeIds).toEqual([]);
    expect(f.graph.document!.nodes).toHaveLength(0);
    await vi.waitFor(() => expect(f.requests.length).toBe(2));
    gate.resolve(accepted(f.requests.at(-1)!));
    const nodeId = await adding;
    expect(nodeId).toBeTruthy();
    expect(f.graph.document!.nodes).toHaveLength(1);
    expect(f.graph.selectedNodeIds).toEqual([nodeId]);
  });

  it("scopes the derivation ask to its own workflow and re-checks the proposal basis", async () => {
    const f = await harness();
    const firstWorkflow = f.workflowId;
    f.control.next = async (request: GraphConfigurationUpdateRequest) =>
      configurationResult(request, { outcome: "confirmation_required", categories: ["fixed"], hasSession: true });
    expect(await f.graph.renameWorkflow("needs derivation")).toMatchObject({ outcome: "confirmation-required" });
    expect(f.graph.activeDerivationAsk).not.toBeNull();
    // The fixed-revision question writes nothing and projects nothing.
    expect(f.graph.document!.name).not.toBe("needs derivation");

    const secondWorkflow = await f.graph.createWorkflow();
    expect(secondWorkflow).not.toBe(firstWorkflow);
    expect(f.graph.activeDerivationAsk).toBeNull();
    f.workspace.openWorkflow(firstWorkflow);
    expect(f.graph.activeDerivationAsk).not.toBeNull();

    // A changed accepted baseline makes the old proposal unconfirmable.
    const current = f.application.accepted(firstWorkflow)!;
    f.application.observe({ ...current, name: "formal elsewhere",
      edit_sequence: current.edit_sequence + 1, revision: current.revision + 1 });
    const calls = f.requests.length;
    expect(await f.graph.confirmDerivation(firstWorkflow)).toBe(false);
    expect(f.graph.activeDerivationAsk).toBeNull();
    expect(f.requests.length).toBe(calls);
    expect(useWorkbenchNoticesStore().notices.some(row =>
      row.kind === "rejected" && row.reason.includes("基线"))).toBe(true);

    // A fresh ask can be explicitly declined and is not projected.
    const thirdWorkflow = await f.graph.createWorkflow();
    f.control.next = async (request: GraphConfigurationUpdateRequest) =>
      configurationResult(request, { outcome: "confirmation_required", categories: ["fixed"], hasSession: true });
    await f.graph.renameWorkflow("derive again");
    expect(f.graph.activeDerivationAsk).not.toBeNull();
    expect(f.graph.declineDerivation(thirdWorkflow)).toBe(true);
    expect(f.graph.activeDerivationAsk).toBeNull();
    expect(f.graph.document!.name).not.toBe("derive again");
  });

  it("confirms a fixed derivation into the new formal identity", async () => {
    const f = await harness();
    const source = f.workflowId;
    f.control.next = async (request: GraphConfigurationUpdateRequest) =>
      configurationResult(request, { outcome: "confirmation_required", categories: ["fixed"], hasSession: true });
    expect(await f.graph.renameWorkflow("derive from here")).toMatchObject({ outcome: "confirmation-required" });
    expect(f.graph.activeDerivationAsk).not.toBeNull();

    const target = crypto.randomUUID();
    f.control.next = async (request: GraphConfigurationUpdateRequest) =>
      configurationResult(request, { outcome: "derived", categories: ["fixed"], target, hasSession: true });
    expect(await f.graph.confirmDerivation(source)).toBe(true);
    expect(f.application.accepted(target)).not.toBeNull();
    expect(f.graph.entries[target]).toBeTruthy();
    // The accepted derivation is projected as the new formal workflow; the source
    // workflow keeps its own accepted document.
    expect(f.graph.document?.workflow_definition_id).toBe(target);
    expect(f.graph.entries[source]!.document.name).not.toBe("derive from here");
  });
});

// ---------------------------------------------------------------------------
// V1-04 frontend: unknown result -> explicit terminal arbitration; a lost
// arbitration retries with the same key; unavailable keeps the block.
// ---------------------------------------------------------------------------
describe("V1-04 unknown result arbitration", () => {
  it("keeps the request pending and never re-sends it after a lost business response", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    f.control.next = async () => {
      throw new WorkbenchApiError("unknown", "business response lost");
    };
    expect(await f.graph.patchNode(nodeId, { config: { text: "lost body" } }))
      .toMatchObject({ outcome: "failed" });
    const calls = f.requests.length;

    const channel = f.application.forWorkflow(f.workflowId);
    expect(channel.pending.value).not.toBeNull();
    expect(channel.progress.value).toBe("result-unknown");
    expect(f.graph.locked).toBe(true);
    expect(f.graph.configurationPendingRequests()).toHaveLength(1);
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.requests.length).toBe(calls);
    // The accepted projection is not overwritten by an unconfirmed result.
    expect(f.graph.document!.nodes[0]!.config.text).toBe("before");
  });

  it("retries a lost arbitration with the same key and unlocks only on a terminal answer", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    f.control.next = async () => {
      throw new WorkbenchApiError("unknown", "business response lost");
    };
    await f.graph.patchNode(nodeId, { config: { text: "lost body" } });
    const updateCalls = f.requests.length;

    // First arbitration: the answer is lost, so the block must stay.
    f.abandon.mockImplementationOnce(async () => {
      throw new WorkbenchApiError("unknown", "arbitration response lost");
    });
    expect(await f.graph.abandonConfiguration(f.workflowId)).toBe(false);
    expect(f.graph.configurationPendingRequests()).toHaveLength(1);
    expect(f.graph.locked).toBe(true);
    expect(useWorkbenchNoticesStore().notices.some(row => row.title === "结果未决")).toBe(true);
    const first = f.abandon.mock.calls[0]![0] as { idempotency_key: string };

    // Second arbitration reuses the same key and receives the terminal answer.
    f.abandon.mockImplementationOnce(async () => ({ outcome: "abandoned" }));
    expect(await f.graph.abandonConfiguration(f.workflowId)).toBe(true);
    const second = f.abandon.mock.calls[1]![0] as { idempotency_key: string };
    expect(second.idempotency_key).toBe(first.idempotency_key);
    expect(f.application.forWorkflow(f.workflowId).pending.value).toBeNull();
    expect(f.application.forWorkflow(f.workflowId).progress.value).toBe("abandoned");
    expect(f.graph.locked).toBe(false);
    expect(f.graph.configurationPendingRequests()).toEqual([]);
    expect(f.requests.length).toBe(updateCalls);
    expect(useWorkbenchNoticesStore().notices.some(row => row.title === "原请求已终止")).toBe(true);
  });

  it("accepts a committed arbitration result instead of claiming a cancellation", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    f.control.next = async () => {
      throw new WorkbenchApiError("unknown", "business response lost");
    };
    await f.graph.patchNode(nodeId, { config: { text: "actually committed" } });
    f.abandon.mockImplementationOnce(async (request: { target_idempotency_key: string },
      original: GraphConfigurationUpdateRequest) => ({
      outcome: "committed",
      result: accepted({ ...original, expected_edit_sequence: original.expected_edit_sequence }),
    }));
    expect(await f.graph.abandonConfiguration(f.workflowId)).toBe(true);
    expect(f.application.forWorkflow(f.workflowId).pending.value).toBeNull();
    expect(f.application.forWorkflow(f.workflowId).progress.value).toBe("completed");
    expect(f.graph.document!.nodes[0]!.config.text).toBe("actually committed");
    expect(f.graph.locked).toBe(false);
    expect(useWorkbenchNoticesStore().notices.some(row => row.title === "原请求已由服务端确认提交")).toBe(true);
  });

  it("repairs a closure failure by re-reading the formal current without resubmitting", async () => {
    const f = await harness();
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    const resolve = f.evidence.ports.resolve;
    f.evidence.ports.resolve = vi.fn(async () => { throw new Error("evidence disk unavailable"); });
    expect(await f.graph.patchNode(nodeId, { config: { text: "saved but not closed" } }))
      .toMatchObject({ outcome: "failed" });
    expect(f.application.forWorkflow(f.workflowId).progress.value).toBe("closure-failed");
    expect(f.graph.locked).toBe(true);
    const updateCalls = f.update.mock.calls.length;
    expect(f.graph.configurationPendingRequests()).toHaveLength(1);

    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe(`/api/graph/definitions/${f.workflowId}`);
      return new Response(JSON.stringify(f.application.accepted(f.workflowId)));
    }));
    expect(await f.graph.reloadAcceptedConfiguration(f.workflowId)).toBe(true);
    expect(f.update.mock.calls.length).toBe(updateCalls);
    // The formal result was accepted before closure; only the evidence remains open.
    expect(f.graph.document!.nodes[0]!.config.text).toBe("saved but not closed");
    expect(f.graph.locked).toBe(true);

    f.evidence.ports.resolve = resolve;
    expect(await f.graph.reconcileConfiguration(f.workflowId)).toBe(true);
    expect(f.graph.configurationPendingRequests()).toEqual([]);
    expect(f.graph.locked).toBe(false);
    expect(f.update.mock.calls.length).toBe(updateCalls);
  });
});

// ---------------------------------------------------------------------------
// V1-05: a late formal result for the original workflow never overwrites the
// workflow the user switched to.
// ---------------------------------------------------------------------------
describe("V1-05 late results after switching views", () => {
  it("keeps the switched-to workflow intact and preserves the original result", async () => {
    const f = await harness();
    const firstWorkflow = f.workflowId;
    const nodeId = (await f.graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    const secondWorkflow = await f.graph.createWorkflow();
    expect(secondWorkflow).not.toBe(firstWorkflow);
    f.workspace.openWorkflow(firstWorkflow);

    const gate = deferred<GraphConfigurationResult>();
    f.control.next = async () => gate.promise;
    const moving = f.graph.moveNodes([{ id: nodeId, position: { x: 42, y: 42 } }]);
    await vi.waitFor(() => expect(f.requests.length).toBe(4));
    const secondBefore = graphClone(f.graph.entries[secondWorkflow]!.document);

    f.workspace.openWorkflow(secondWorkflow);
    const secondId = f.graph.document!.workflow_definition_id;
    gate.resolve(accepted(f.requests.at(-1)!));
    expect(await moving).toMatchObject({ outcome: "accepted" });

    // The current view stays on the second workflow, untouched.
    expect(f.graph.document!.workflow_definition_id).toBe(secondId);
    expect(f.graph.entries[secondWorkflow]!.document).toEqual(secondBefore);
    // The original workflow's formal result is retained on its own entry.
    expect(f.graph.entries[firstWorkflow]!.document.nodes[0]!.position).toEqual({ x: 42, y: 42 });
    expect(f.application.accepted(firstWorkflow)!.nodes[0]!.position).toEqual({ x: 42, y: 42 });
    // Reopening the original workflow shows its accepted result.
    f.workspace.openWorkflow(firstWorkflow);
    await vi.waitFor(() => expect(f.graph.document!.workflow_definition_id).toBe(firstWorkflow));
    expect(f.graph.document!.nodes[0]!.position).toEqual({ x: 42, y: 42 });
  });
});


