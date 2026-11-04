import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { graphClone, type GraphNodeType } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";

/**
 * Independent verifier acceptance for step 5 frontend (V5-02 external formal-version
 * change / local conflict feedback, V5-01 read and preview do not take the edit lock).
 * Uses the real pinia store, the injected configuration/evidence ports and a stubbed
 * transport; no browser and no product service.
 */

const type: GraphNodeType = { component_id: "verifier.text", component_version: "1",
  display_name: "text", category: "content", config_schema: { type: "object" },
  default_config: { text: "" }, inputs: [], outputs: [], is_output: false, executable: true };

async function configured() {
  const graph = useWorkflowGraphStore();
  graph.catalog = [graphClone(type)];
  const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
  const id = await graph.createWorkflow();
  await configuration.settled(id);
  const node = (await graph.addNode("verifier.text@1", { x: 0, y: 0 }))!;
  await configuration.settled(id);
  return { graph, configuration, id, node };
}

function formalDocument(identity: string, sequence: number, revision: number) {
  const accepted = { schema_version: 3, workflow_definition_id: identity, revision,
    edit_sequence: sequence, name: "外部 Agent 正式配置", nodes: [], edges: [],
    object_bindings: [], package_lock: [], control_edges: [] };
  return accepted;
}

function stubDefinitionRead(document: unknown) {
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    requests.push(String(body.operation ?? path));
    return new Response(JSON.stringify(document));
  }));
  return requests;
}

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("V5-02 external formal-version change and local conflict feedback", () => {
  it.each(["stale_edit_sequence", "stale_configuration_basis", "stale_revision"])(
    "keeps an %s rejection visible with the local proposal and never resubmits",
    async code => {
      const { graph, configuration, id, node } = await configured();
      const before = graphClone(graph.document!);
      const updateCalls = configuration.update.mock.calls.length;
      const error = new WorkbenchApiError("rejected",
        `配置保存被拒 [HTTP 409 / ${code}]`, 409, code);
      configuration.update.mockRejectedValueOnce(error);
      expect(await graph.patchNode(node, { title: "external conflict edit" }))
        .toMatchObject({ outcome: "failed" });
      expect(configuration.update.mock.calls.length).toBe(updateCalls + 1);
      expect(graph.document).toEqual(before); // nothing was projected as accepted
      const conflict = graph.activeExternalConflict!;
      expect(conflict).not.toBeNull();
      expect(conflict.code).toBe(code);
      expect(conflict.reason).toContain(code);
      expect(conflict.workflowId).toBe(id);
      expect(conflict.targetLabel).toBeTruthy();
      expect(conflict.localEditSequence).toBe(before.edit_sequence);
      expect(conflict.localRevision).toBe(before.revision);
      expect(conflict.proposal).toMatchObject({ nodes: expect.any(Array) });
      expect(conflict.resolved).toBe(false);
      // Persistent: clearing the transient notices keeps the conflict row.
      const notices = useWorkbenchNoticesStore();
      notices.notices.splice(0, notices.notices.length);
      expect(graph.activeExternalConflict).not.toBeNull();
      // The rejection did not retry or overwrite: exactly one update attempt.
      expect(configuration.update.mock.calls.length).toBe(updateCalls + 1);
      graph.dismissExternalConflict();
      expect(graph.activeExternalConflict).toBeNull();
    });

  it("re-reads the formal current explicitly, adopts it and keeps the local proposal", async () => {
    const { graph, configuration, id, node } = await configured();
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "配置保存被拒 [HTTP 409 / stale_edit_sequence]", 409, "stale_edit_sequence"));
    expect(await graph.patchNode(node, { title: "conflicting local proposal" }))
      .toMatchObject({ outcome: "failed" });
    const conflict = graph.activeExternalConflict!;
    expect(conflict.resolved).toBe(false);
    const updateCalls = configuration.update.mock.calls.length;
    // The formal current now carries a newer edit sequence and revision.
    const external = formalDocument(id, 5, 2);
    const requests = stubDefinitionRead(external);
    expect(await graph.reloadAcceptedConfiguration(id)).toBe(true);
    // A read only: no save was resubmitted, and the accepted projection moved forward.
    expect(configuration.update.mock.calls.length).toBe(updateCalls);
    expect(requests).toContain("definition.read");
    expect(configuration.application.accepted(id)!.edit_sequence).toBe(5);
    const resolved = graph.activeExternalConflict!;
    expect(resolved).not.toBeNull();
    expect(resolved.resolved).toBe(true);
    expect(resolved.externalEditSequence).toBe(5);
    expect(resolved.externalRevision).toBe(2);
    expect(resolved.proposal).not.toBeNull(); // the local proposal stays available
    expect(conflict.code).toBe("stale_edit_sequence");
    // Reading the formal current did not take the edit lock.
    expect(graph.locked).toBe(false);
  });

  it("detects a newer formal current read and reports external_current", async () => {
    const { graph, configuration, id } = await configured();
    expect(graph.activeExternalConflict).toBeNull();
    stubDefinitionRead(formalDocument(id, 3, 2));
    expect(await graph.reloadAcceptedConfiguration(id)).toBe(true);
    const conflict = graph.activeExternalConflict!;
    expect(conflict).not.toBeNull();
    expect(conflict.code).toBe("external_current");
    expect(conflict.resolved).toBe(true);
    expect(conflict.reason).toContain("外部");
    expect(configuration.application.accepted(id)!.edit_sequence).toBe(3);
  });

  it("keeps the conflict scoped to its own workflow", async () => {
    const { graph, configuration, id, node } = await configured();
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "配置保存被拒 [HTTP 409 / stale_edit_sequence]", 409, "stale_edit_sequence"));
    await graph.patchNode(node, { title: "conflict on the first workflow" });
    expect(graph.activeExternalConflict).not.toBeNull();
    const second = await graph.createWorkflow();
    await configuration.settled(second);
    expect(graph.activeExternalConflict).toBeNull(); // no cross-workflow leak
    useWorkspaceStore().openWorkflow(id);
    expect(graph.activeExternalConflict!.code).toBe("stale_edit_sequence");
  });
});

describe("V5-01 reads and preview never take the edit lock or the save right", () => {
  it("previews concurrently with an accepted edit and rechecks save without extra writes", async () => {
    const { graph, configuration, id, node } = await configured();
    const accepted = graphClone(graph.document!);
    let resolvePreview: (() => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => {
      resolvePreview = () => resolve(new Response(JSON.stringify({
        schema_version: 1, kind: "workflow.configuration-preview", contract_version: 2,
        read_only: true, reserves_save_right: false,
        structure: { ok: true, diagnostics: [] },
        declared_references: { ok: true, diagnostics: [] },
        compilation: { ok: true, skipped: false, target_node_ids: [], ordered_node_count: 1 },
        baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
          occupying_sessions: [], has_effective_session: false },
        changes: { categories: ["display"], would_outcome: "saved",
          would_require_confirmation: false, would_derive_after_confirmation: false },
        run_conditions: { package_lock_available: true, checked: ["structure"],
          not_checked: ["修改资格与提交时 CAS 复查"] },
        notes: [] })));
    })));
    const updates = configuration.update.mock.calls.length;
    const previewing = graph.previewConfiguration(id);
    await vi.waitFor(() => expect(resolvePreview).not.toBeNull());
    // While the preview is in flight the store still accepts a real edit: a read does
    // not take the modification entitlement or the submit lock.
    expect(graph.locked).toBe(false);
    expect(await graph.patchNode(node, { title: "edit during preview" }))
      .toMatchObject({ outcome: "accepted" });
    resolvePreview!();
    expect(await previewing).toBe(true);
    expect(graph.activePreview!.preview).not.toBeNull();
    expect(graph.activePreview!.stale).toBe(true); // describes the document it read
    expect(configuration.update.mock.calls.length).toBe(updates + 1); // only the real edit
    expect(graph.locked).toBe(false);
    // The actual save path still rechecks the accepted basis (no reserved right).
    expect((await graph.patchNode(node, { title: "after preview" })))
      .toMatchObject({ outcome: "accepted" });
  });
});
