import { afterEach, describe, expect, it, vi } from "vitest";
import { saveLocalDraft, workbenchDraftAddress } from "../adapters/localDraftsApi";
import { readGraphReceipt, sendGraphCommand } from "../adapters/workflowApplicationApi";
import { readLegacyWorkbenchEvidence } from "../adapters/workbenchPersistence";
import { graphClone, newGraph, type GraphCommand } from "../domain/workflowGraph";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());

describe("legacy graph command retirement", () => {
  it("rejects direct whole-graph save transport and receipt reads before fetch", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);

    await expect(sendGraphCommand("/api/graph/definitions", {
      document: newGraph("Retired save"), expected_revision: 0, idempotency_key: crypto.randomUUID(),
    })).rejects.toMatchObject({ kind: "unavailable", code: "graph_definition_save_retired" });

    await expect(readGraphReceipt("/api/graph/definitions", {
      document: newGraph("Retired save"), expected_revision: 0, idempotency_key: crypto.randomUUID(),
    })).rejects.toMatchObject({ kind: "unavailable", code: "graph_definition_save_retired" });

    expect(fetcher).not.toHaveBeenCalled();
  });

  it("marks a frozen whole-graph save request as retired evidence without a verify path", () => {
    const document = newGraph("Legacy save");
    const workflowId = document.workflow_definition_id;
    const pending: GraphCommand = {
      path: "/api/graph/definitions",
      body: { document: graphClone(document), expected_revision: 0, idempotency_key: crypto.randomUUID() },
      action: "save",
    };
    const value = {
      schemaVersion: 7, kind: "graph-workbench", revision: 1, savedAt: "2026-10-14T00:00:00Z",
      activeWorkflowId: workflowId, selectedWorkflowId: workflowId,
      graph: { schema_version: 1, entries: { [workflowId]: {
        document, saved_revision: 0, session_id: null, pending: graphClone(pending),
      } } },
    };
    const before = graphClone(value);

    const evidence = readLegacyWorkbenchEvidence(value);

    expect(evidence.pending).toEqual([{
      key: `${workflowId}:${pending.body.idempotency_key}`,
      workflowId,
      command: pending,
      legacy_action: null,
      basis: { definitionId: workflowId, savedRevision: 0, sessionId: null },
      status: "retired",
      result: "旧整图保存入口已退役；原保存回执不可再核实，原记录保留",
    }]);
    expect(evidence.unreadablePending).toBe(0);
    expect(value).toEqual(before);
  });

  it("rejects direct session.copy transport while keeping exact receipt reads available", async () => {
    const sid = crypto.randomUUID(), path = `/api/graph/sessions/${sid}/copy`;
    const body = {
      document: newGraph("Frozen copy"), expected_session_revision: 2, expected_data_revision: 1,
      expected_definition_revision: 1, expected_head_revision: 1, idempotency_key: crypto.randomUUID(),
    };
    const original = graphClone(body), parameters = { ...original, session_id: sid };
    const result = {
      workflow_definition_id: body.document.workflow_definition_id, definition_revision: 1,
      workflow_session_id: crypto.randomUUID(), revision: 1, data_revision: 1,
      head_revision: 1, head_commit_id: crypto.randomUUID(), active_chain_run_id: null,
    };
    const fetcher = vi.fn(async (target: string, init: RequestInit) => {
      expect(target).toBe("/api/graph/receipts/read");
      expect(JSON.parse(String(init.body))).toEqual({ operation: "session.copy", parameters });
      return graphReceiptReadResponse("session.copy", parameters, result);
    });
    vi.stubGlobal("fetch", fetcher);

    await expect(sendGraphCommand(path, body)).rejects.toMatchObject({ kind: "rejected" });
    expect(fetcher).not.toHaveBeenCalled();
    await expect(readGraphReceipt(path, body)).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(body).toEqual(original);
  });

  it("rejects direct management draft writes before fetch", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);

    await expect(saveLocalDraft({
      address: workbenchDraftAddress, format_version: 1, value: { raw: "unsaved" },
      expected_sequence: 0, idempotency_key: crypto.randomUUID(),
    })).rejects.toMatchObject({ kind: "rejected", code: "local_draft_write_retired" });

    expect(fetcher).not.toHaveBeenCalled();
  });

  it("projects only pending evidence, its frozen basis, and selection preferences from a legacy envelope", () => {
    const document = newGraph("Legacy editor document");
    const workflowId = document.workflow_definition_id;
    const sessionId = crypto.randomUUID();
    const pending: GraphCommand = {
      path: `/api/graph/sessions/${sessionId}/runs`,
      body: { expected_revision: 4, inputs: { prompt: "original request" },
        idempotency_key: crypto.randomUUID() },
      action: "start",
    };
    const value = {
      schemaVersion: 7, kind: "graph-workbench", revision: 8, savedAt: "2026-10-14T00:00:00Z",
      activeWorkflowId: workflowId, selectedWorkflowId: workflowId,
      catalog: [{ id: workflowId, title: "Legacy editor document", description: "do not restore", nodeCount: 1 }],
      graph: { schema_version: 1, entries: { [workflowId]: {
        document, saved_document: graphClone(document), session_document: graphClone(document),
        saved_revision: 3, session_id: sessionId, pending: graphClone(pending),
        external_inputs: { prompt: "later editor value" }, diagnostics: [{ message: "stale" }],
      } } },
    };
    const before = graphClone(value);

    const evidence = readLegacyWorkbenchEvidence(value);

    expect(evidence).toEqual({
      activeWorkflowId: workflowId,
      selectedWorkflowId: workflowId,
      pending: [{
        key: `${workflowId}:${pending.body.idempotency_key}`,
        workflowId,
        command: pending,
        legacy_action: null,
        basis: { definitionId: workflowId, savedRevision: 3, sessionId },
        status: "pending",
      }],
      unreadablePending: 0,
      unreadableRecord: false,
    });
    expect(evidence).not.toHaveProperty("catalog");
    expect(evidence).not.toHaveProperty("document");
    expect(evidence).not.toHaveProperty("external_inputs");
    expect(JSON.stringify(evidence)).not.toContain("later editor value");
    expect(value).toEqual(before);
  });
  it("recognizes an old budget-extension request only as readonly receipt evidence", () => {
    const document = newGraph("Legacy budget request");
    const workflowId = document.workflow_definition_id, sessionId = crypto.randomUUID();
    const pending: GraphCommand = {
      path: `/api/graph/sessions/${sessionId}/control`,
      action: "extend_budget",
      body: { action: "extend_budget", expected_revision: 3, add_model_requests: 1,
        add_model_attempts: 4, idempotency_key: crypto.randomUUID() },
    };
    const value = { schemaVersion: 7, kind: "graph-workbench", revision: 1,
      savedAt: "2026-10-14T00:00:00Z", activeWorkflowId: workflowId, selectedWorkflowId: workflowId,
      graph: { schema_version: 1, entries: { [workflowId]: {
        document, saved_revision: 1, session_id: sessionId, pending,
      } } } };
    const before = graphClone(value);

    const evidence = readLegacyWorkbenchEvidence(value);

    expect(evidence.pending).toMatchObject([{
      command: pending, legacy_action: "extend_budget", status: "pending",
    }]);
    expect(evidence.unreadablePending).toBe(0);
    expect(value).toEqual(before);
  });
});
