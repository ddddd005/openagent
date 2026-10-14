import { afterEach, describe, expect, it, vi } from "vitest";
import { saveLocalDraft, workbenchDraftAddress } from "../adapters/localDraftsApi";
import { sendGraphCommand } from "../adapters/workflowApplicationApi";
import { readLegacyWorkbenchEvidence } from "../adapters/workbenchPersistence";
import { graphClone, newGraph, type GraphCommand, type GraphEntry } from "../domain/workflowGraph";
import { createGraphCommand, dispatchGraphCommand, type GraphCommandPorts } from "./workflowCommands";

afterEach(() => vi.unstubAllGlobals());

function commandPorts(overrides: Partial<GraphCommandPorts> = {}): GraphCommandPorts {
  return {
    persist: vi.fn(() => true),
    request: vi.fn(async () => ({})),
    readReceipt: vi.fn(async () => ({})),
    readDefinition: vi.fn(async () => { throw new Error("Unexpected definition read"); }),
    current: vi.fn(() => true),
    accept: vi.fn(),
    ...overrides,
  };
}

describe("legacy graph command retirement", () => {
  it.each([
    ["save", "/api/graph/definitions", (document: ReturnType<typeof newGraph>) => ({
      document, expected_revision: 0,
    })],
    ["create", "/api/graph/sessions", (document: ReturnType<typeof newGraph>) => ({
      workflow_definition_id: document.workflow_definition_id, definition_revision: 1,
    })],
    ["start", `/api/graph/sessions/${crypto.randomUUID()}/runs`, () => ({
      expected_revision: 1, inputs: { prompt: "frozen input" },
    })],
    ["session-archive", `/api/graph/sessions/${crypto.randomUUID()}/archive`, () => ({
      expected_revision: 2,
    })],
    ["session-delete", `/api/graph/sessions/${crypto.randomUUID()}/delete`, () => ({
      expected_revision: 2,
    })],
    ["data", `/api/graph/sessions/${crypto.randomUUID()}/data`, () => ({
      expected_revision: 2, expected_data_revision: 1, variables: { value: 3 },
    })],
    ["copy", `/api/graph/sessions/${crypto.randomUUID()}/copy`, (document: ReturnType<typeof newGraph>) => ({
      document, expected_session_revision: 2, expected_data_revision: 1,
      expected_definition_revision: 1, expected_head_revision: 1,
    })],
  ])("refuses a new %s command before persistence or transport", async (action, path, bodyFor) => {
    const document = newGraph("Legacy workflow");
    const entry: GraphEntry = { document, saved_revision: 1, session_id: crypto.randomUUID(), pending: null };
    const command = createGraphCommand(path, bodyFor(document), action);
    const ports = commandPorts();

    await expect(dispatchGraphCommand(entry, command, ports)).rejects.toMatchObject({ kind: "unavailable" });

    expect(entry.pending).toBeNull();
    expect(ports.persist).not.toHaveBeenCalled();
    expect(ports.request).not.toHaveBeenCalled();
    expect(ports.readReceipt).not.toHaveBeenCalled();
    expect(ports.accept).not.toHaveBeenCalled();
  });

  it("reconciles an exact old pending command by receipt read only, without accepting or clearing it", async () => {
    const document = newGraph("Frozen workflow");
    const entry: GraphEntry = { document, saved_revision: 0, session_id: null, pending: null };
    const command = createGraphCommand("/api/graph/definitions", {
      document: graphClone(document), expected_revision: 0,
    }, "save");
    entry.pending = graphClone(command);
    const before = graphClone(entry);
    const ports = commandPorts({ readReceipt: vi.fn(async () => graphClone(document)) });

    await expect(dispatchGraphCommand(entry, command, ports, true))
      .resolves.toEqual({ kind: "definition", document });

    expect(ports.readReceipt).toHaveBeenCalledOnce();
    expect(ports.readReceipt).toHaveBeenCalledWith(command.path, command.body);
    expect(ports.request).not.toHaveBeenCalled();
    expect(ports.persist).not.toHaveBeenCalled();
    expect(ports.accept).not.toHaveBeenCalled();
    expect(entry).toEqual(before);
  });

  it("rejects direct definition.save transport before fetch", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);

    await expect(sendGraphCommand("/api/graph/definitions", {
      document: newGraph("Retired save"), expected_revision: 0, idempotency_key: crypto.randomUUID(),
    })).rejects.toMatchObject({ kind: "unavailable", code: "graph_definition_save_retired" });

    expect(fetcher).not.toHaveBeenCalled();
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
