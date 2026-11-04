import { describe, expect, it, vi } from "vitest";
import { createWorkflowEditing, type WorkflowEditingPorts } from "./workflowEditing";
import { graphClone, newGraph } from "../domain/workflowGraph";
import type { GraphConfigurationResult } from "../domain/workflowConfiguration";

// The editor port contract: one edit is one awaited formal submission. These tests fix
// that contract without an application instance, so a store change cannot silently fall
// back to "return the id and submit later".
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function formalResult(workflowId: string, target = workflowId): GraphConfigurationResult {
  const document = { ...newGraph("accepted"), workflow_definition_id: target, schema_version: 3 as const,
    revision: 2, edit_sequence: 2, object_bindings: [] };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: workflowId, workflow_definition_id: target,
    definition_revision: 2, edit_sequence: 2, source_has_effective_session: false,
    changed_categories: ["fixed"], node_id_map: {}, session_id: null,
    document: document as unknown as GraphConfigurationResult["document"],
  };
}

function fixture() {
  const entry = { document: newGraph("original"), saved_revision: 1, session_id: null as string | null};
  const acceptance = deferred<Awaited<ReturnType<WorkflowEditingPorts["acceptDraft"]>>>();
  const ports: WorkflowEditingPorts = {
    entry: () => entry,
    refusal: () => null,
    observedSession: () => undefined,
    acceptDraft: vi.fn(() => acceptance.promise),
  };
  return { entry, acceptance, ports, editing: createWorkflowEditing(ports) };
}

describe("workflow editing result contract", () => {
  it("returns no-change for an edit that submits nothing", async () => {
    const f = fixture();
    expect(await f.editing.edit({ workflowId: f.entry.document.workflow_definition_id }, () => false))
      .toMatchObject({ outcome: "no-change" });
    expect(f.ports.acceptDraft).not.toHaveBeenCalled();
    expect(f.ports.entry(f.entry.document.workflow_definition_id)).toBe(f.entry);
  });

  it("does not resolve before the formal acceptance and follows the accepted identity", async () => {
    const f = fixture(), workflowId = f.entry.document.workflow_definition_id;
    const edited = f.editing.edit({ workflowId }, document => { document.name = "next"; return true; });
    let settled = false; void edited.then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(f.ports.acceptDraft).toHaveBeenCalledOnce();
    const [acceptedId, acceptedDocument] = vi.mocked(f.ports.acceptDraft).mock.calls[0]!;
    expect(acceptedId).toBe(workflowId);
    expect(acceptedDocument.name).toBe("next");
    expect(acceptedDocument).not.toBe(f.entry.document);
    expect(f.entry.document.name).toBe("original");
    const target = crypto.randomUUID();
    f.acceptance.resolve({ result: formalResult(workflowId, target) });
    await expect(edited).resolves.toMatchObject({ outcome: "accepted", workflowId: target });
  });

  it("reports a declined confirmation as declined rather than saved", async () => {
    const f = fixture(), workflowId = f.entry.document.workflow_definition_id;
    const edited = f.editing.edit({ workflowId }, document => { document.name = "derive me"; return true; });
    f.acceptance.resolve(null);
    await expect(edited).resolves.toMatchObject({ outcome: "declined", workflowId });
  });

  it("throws a reasoned rejection instead of returning null for a refused edit", async () => {
    const f = fixture(), workflowId = f.entry.document.workflow_definition_id;
    f.ports.refusal = () => "工作流配置正在提交，请等待当前请求结束";
    await expect(f.editing.edit({ workflowId }, () => true)).rejects.toMatchObject({
      kind: "rejected", reason: "工作流配置正在提交，请等待当前请求结束", code: "graph_edit_unavailable",
    });
    expect(f.ports.acceptDraft).not.toHaveBeenCalled();
  });

  it("refuses to edit a running session without submitting", async () => {
    const f = fixture(), workflowId = f.entry.document.workflow_definition_id;
    f.entry.session_id = crypto.randomUUID();
    f.ports.observedSession = () => ({ status: "running", can_submit: true } as never);
    await expect(f.editing.edit({ workflowId }, document => { document.name = "x"; return true; }))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(f.ports.acceptDraft).not.toHaveBeenCalled();
  });

  it("does not hand the caller's mutable document to the submission port", async () => {
    const f = fixture(), workflowId = f.entry.document.workflow_definition_id;
    f.acceptance.resolve({ result: null });
    await f.editing.edit({ workflowId }, document => {
      document.name = "edited"; document.revision = 9; return true;
    });
    expect(f.entry.document.name).toBe("original");
    expect(f.entry.document.revision).toBe(1);
    expect(f.entry.document).toEqual({ ...newGraph("original"),
      workflow_definition_id: f.entry.document.workflow_definition_id });
  });
});
