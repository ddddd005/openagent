import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, type GraphNodeType } from "../domain/workflowGraph";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { WorkbenchApiError } from "../adapters/workbenchApi";

const textType: GraphNodeType = { component_id: "workflow.text", component_version: "1",
  display_name: "text", category: "content", config_schema: { type: "object" },
  default_config: {}, inputs: [], outputs: [], is_output: false, executable: true };

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

async function configured() {
  const graph = useWorkflowGraphStore();
  graph.catalog = [graphClone(textType)];
  const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
  const id = await graph.createWorkflow();
  await configuration.settled(id);
  const node = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
  await configuration.settled(id);
  return { graph, configuration, id, node };
}

function acceptedDocument(graph: ReturnType<typeof useWorkflowGraphStore>,
  configuration: Awaited<ReturnType<typeof configured>>["configuration"], id: string) {
  const accepted = configuration.application.accepted(id)!;
  return { accepted, external: { ...graphClone(accepted), edit_sequence: accepted.edit_sequence + 1,
    revision: accepted.revision + 1, name: "外部 Agent 修改的正式配置" } };
}

describe("external formal-version conflict feedback (Q11-2)", () => {
  it.each(["stale_edit_sequence", "stale_configuration_basis", "stale_revision"])(
    "keeps a rejected %s proposal visible and never resubmits it automatically", async code => {
      const { graph, configuration, id, node } = await configured();
      const calls = configuration.update.mock.calls.length;
      configuration.update.mockRejectedValueOnce(new WorkbenchApiError("rejected",
        "配置编辑依据已变化；请重新读取工作流", 409, code));
      expect(await graph.patchNode(node, { title: "external edit" })).toMatchObject({ outcome: "failed" });

      const rejected = graph.activeRejectedEdit!;
      expect(rejected.code).toBe(code);
      expect(rejected.request.document.nodes[0]!.title).toBe("external edit");
      const conflict = graph.activeExternalConflict!;
      expect(conflict).toMatchObject({ workflowId: id, code, resolved: false });
      expect(conflict.localEditSequence).toBe(configuration.application.accepted(id)!.edit_sequence);
      expect(conflict.proposal).not.toBeNull();
      expect(graph.locked).toBe(false);
      // No silent retry: exactly the one rejected submission happened.
      expect(configuration.update.mock.calls.length).toBe(calls + 1);
      await vi.waitFor(() => expect(useWorkbenchNoticesStore().error).not.toBeNull());
    });

  it("detects a newer formal current read and resolves only after an explicit reload", async () => {
    const { graph, configuration, id, node } = await configured();
    const { accepted, external } = acceptedDocument(graph, configuration, id);
    const updateCalls = configuration.update.mock.calls.length;
    vi.spyOn(graph.management.queries, "definition").mockResolvedValueOnce(external as never);

    expect(await graph.reloadAcceptedConfiguration(id)).toBe(true);
    const conflict = graph.activeExternalConflict!;
    expect(conflict).toMatchObject({ workflowId: id, code: "external_current", resolved: true,
      externalEditSequence: accepted.edit_sequence + 1, externalRevision: accepted.revision + 1,
      localEditSequence: accepted.edit_sequence, localRevision: accepted.revision });
    // The read adopted the formal current without re-sending the local save.
    expect(graph.document?.name).toBe("外部 Agent 修改的正式配置");
    expect(conflict.proposal).toBeNull();
    expect(configuration.update.mock.calls.length).toBe(updateCalls);

    // Editing now uses the new formal baseline and clears the handled conflict.
    expect(await graph.patchNode(node, { title: "merged after reload" }))
      .toMatchObject({ outcome: "accepted" });
    expect(graph.activeExternalConflict).toBeNull();
    expect(configuration.update.mock.calls.length).toBe(updateCalls + 1);
  });

  it("fills the external counters on reload while keeping the rejected local proposal", async () => {
    const { graph, configuration, id, node } = await configured();
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError("rejected",
      "Workflow configuration changed", 409, "stale_edit_sequence"));
    expect(await graph.patchNode(node, { title: "conflicting local edit" })).toMatchObject({ outcome: "failed" });
    const pending = graph.activeExternalConflict!;
    expect(pending.externalEditSequence).toBeNull();
    expect(pending.proposal!.nodes[0]!.title).toBe("conflicting local edit");
    const { external } = acceptedDocument(graph, configuration, id);
    vi.spyOn(graph.management.queries, "definition").mockResolvedValueOnce(external as never);
    const updateCalls = configuration.update.mock.calls.length;

    expect(await graph.reloadAcceptedConfiguration(id)).toBe(true);
    const resolved = graph.activeExternalConflict!;
    expect(resolved).toMatchObject({ resolved: true, externalEditSequence: external.edit_sequence,
      externalRevision: external.revision });
    // The failed proposal is still available to merge or abandon explicitly (Q03-1).
    expect(graph.activeRejectedEdit).not.toBeNull();
    expect(resolved.proposal!.nodes[0]!.title).toBe("conflicting local edit");
    expect(configuration.update.mock.calls.length).toBe(updateCalls);

    // Abandoning the retained proposal clears both the proposal and the conflict record.
    graph.dismissRejectedEdit();
    expect(graph.activeRejectedEdit).toBeNull();
    expect(graph.activeExternalConflict).toBeNull();
  });

  it("does not take the edit lock while a formal-current read is in flight or a preview is read", async () => {
    const { graph, configuration, id } = await configured();
    let release!: (value: unknown) => void;
    const reading = new Promise(resolve => { release = resolve; });
    const definition = vi.spyOn(graph.management.queries, "definition")
      .mockImplementationOnce(async () => await reading as never);
    const reloading = graph.reloadAcceptedConfiguration(id);
    await vi.waitFor(() => expect(definition).toHaveBeenCalledOnce());
    expect(graph.locked).toBe(false);
    expect(graph.editorLocked).toBe(false);
    release(configuration.application.accepted(id)!);
    expect(await reloading).toBe(true);
    expect(graph.locked).toBe(false);

    let releasePreview!: (value: Response) => void;
    const fetching = new Promise<Response>(resolve => { releasePreview = resolve; });
    vi.stubGlobal("fetch", vi.fn(() => fetching));
    const previewing = graph.previewConfiguration(id);
    expect(graph.activePreview?.busy).toBe(true);
    expect(graph.locked).toBe(false);
    expect(graph.editorLocked).toBe(false);
    releasePreview(new Response(JSON.stringify({ contract_version: 2, read_only: true,
      reserves_save_right: false, structure: { ok: true, diagnostics: [] },
      declared_references: { ok: true, diagnostics: [] },
      compilation: { ok: true, skipped: false, target_node_ids: [], ordered_node_count: 1 },
      baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
        occupying_sessions: [], has_effective_session: false },
      changes: { categories: ["fixed"], would_outcome: "saved", would_require_confirmation: false,
        would_derive_after_confirmation: false },
      run_conditions: { package_lock_available: true, checked: [], not_checked: [] } })));
    expect(await previewing).toBe(true);
    expect(graph.locked).toBe(false);
  });

  it("records a locally detected stale basis without sending a new save", async () => {
    const { graph, configuration, id, node } = await configured();
    const { external } = acceptedDocument(graph, configuration, id);
    // Another writer's formal current is accepted by the channel before the next edit submits.
    configuration.application.observe(external as never);
    const updateCalls = configuration.update.mock.calls.length;
    expect(await graph.patchNode(node, { title: "stale basis edit" })).toMatchObject({ outcome: "failed" });
    expect(configuration.update.mock.calls.length).toBe(updateCalls);
    const conflict = graph.activeExternalConflict!;
    expect(conflict).toMatchObject({ code: "stale_configuration_basis", resolved: false,
      externalEditSequence: external.edit_sequence, externalRevision: external.revision });
    expect(conflict.proposal!.nodes[0]!.title).toBe("stale basis edit");
  });

  it("keeps external movement scoped to its own workflow", async () => {
    const { graph, configuration, id, node } = await configured();
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError("rejected",
      "Workflow configuration changed", 409, "stale_edit_sequence"));
    expect(await graph.patchNode(node, { title: "workflow A conflict" })).toMatchObject({ outcome: "failed" });
    expect(graph.activeExternalConflict).not.toBeNull();

    const other = await graph.createWorkflow();
    await configuration.settled(other);
    const otherNode = (await graph.addNode("workflow.text@1", { x: 10, y: 0 }))!;
    await configuration.settled(other);
    expect(useWorkspaceStore().activeWorkflowId).toBe(other);
    expect(graph.activeExternalConflict).toBeNull();
    expect(await graph.patchNode(otherNode, { title: "workflow B edit" })).toMatchObject({ outcome: "accepted" });
    expect(graph.activeExternalConflict).toBeNull();

    useWorkspaceStore().openWorkflow(id);
    expect(graph.activeExternalConflict).toMatchObject({ workflowId: id, code: "stale_edit_sequence" });
    expect(graph.activeRejectedEdit).not.toBeNull();
  });
});
