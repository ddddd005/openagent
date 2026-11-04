import { afterEach, describe, expect, it, vi } from "vitest";
import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "../adapters/workflowApplicationApi";
import {
  graphClone, newGraph, type GraphEntry, type GraphSession,
} from "../domain/workflowGraph";
import { createWorkflowManagementRequests, type WorkflowManagementRequestPorts } from "./workflowManagementRequests";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { createGraphCommand } from "./workflowCommands";
import { WorkbenchApiError } from "../adapters/workbenchApi";

type StartedSession = GraphSession & { active_chain_run_id: string };

function fixture() {
  const document = newGraph("Plugin workflow");
  const session: StartedSession = {
    schema_version: 2, execution_model: "graph",
    workflow_definition_id: document.workflow_definition_id,
    workflow_session_id: crypto.randomUUID(),
    definition_revision: 1, revision: 2, data_revision: 0, head_revision: 1,
    status: "running", can_submit: false,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} },
    active_chain_run_id: crypto.randomUUID(),
  };
  const entry: GraphEntry = {
    document, saved_document: graphClone(document), saved_revision: 1,
    session_id: session.workflow_session_id,
  };
  const command = createGraphCommand(`/api/graph/sessions/${session.workflow_session_id}/runs`, {
    expected_revision: 1, inputs: { event: { type: "plugin.input", payload: ["original"] } },
  }, "start");
  return { document, entry, session, command };
}

function envelope(session: StartedSession, key: unknown) {
  return {
    schema_version: 1, kind: "workflow.application-command",
    receipt: {
      schema_version: 1, kind: "workflow.application-receipt",
      operation: "run.start", operation_scope: "management",
      idempotency_key: key, request_sha256: "a".repeat(64),
      authority: "service_receipt", target: { session_id: session.workflow_session_id },
      accepted: {
        workflow_definition_id: session.workflow_definition_id,
        definition_revision: session.definition_revision,
        workflow_session_id: session.workflow_session_id,
        revision: session.revision, data_revision: session.data_revision,
        head_revision: session.head_revision, status: session.status,
        active_chain_run_id: session.active_chain_run_id,
      },
    },
    result: graphClone(session),
  };
}

function managed(state: ReturnType<typeof fixture>, overrides: Partial<WorkflowManagementRequestPorts> = {}) {
  const evidence = memoryRequestEvidence(), accept = vi.fn();
  const application = createWorkflowManagementRequests({
    evidence: evidence.ports, send: sendGraphCommand, readReceipt: readGraphReceipt,
    current: () => true, accept, ...overrides,
  });
  return { evidence, accept, channel: application.forWorkflow(state.document.workflow_definition_id) };
}

const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => { vi.unstubAllGlobals(); });

describe("workflow client transport boundary", () => {
  it("reads the frozen outbox after an unknown submission without dispatching another command", async () => {
    const state = fixture(), { session, command } = state;
    const original = graphClone(command), sent: unknown[] = [], persisted: unknown[] = [];
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe(attempts === 0 ? "/api/graph/commands" : "/api/graph/receipts/read");
      expect(init.method).toBe("POST");
      sent.push(JSON.parse(String(init.body)));
      if (++attempts === 1) throw new Error("Response lost after submission");
      const value = envelope(session, original.body.idempotency_key);
      return response({ schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
        reason_code: "receipt_matched", receipt: value.receipt, result: value.result });
    }));
    const boundary = managed(state), write = boundary.evidence.ports.write;
    boundary.evidence.ports.write = vi.fn(async request => {
      persisted.push(graphClone(request.request)); return await write(request);
    });
    await expect(boundary.channel.dispatch(command)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.channel.pendingCommand.value).toEqual(original);
    expect(persisted[0]).toEqual({ ...original.body, session_id: session.workflow_session_id });
    command.body.inputs = { event: { payload: ["later caller mutation"] } };
    command.body.idempotency_key = crypto.randomUUID();
    expect(await boundary.channel.reconcile()).toBe(true);
    const expected = {
      operation: "run.start",
      parameters: { ...original.body, session_id: session.workflow_session_id },
    };
    expect(sent).toEqual([expected, expected]);
    expect(boundary.accept).toHaveBeenCalledTimes(1);
    expect(boundary.channel.pending.value).toBeNull();
    expect([...boundary.evidence.records.values()][0]?.request).toEqual(expected.parameters);
  });

  it("does not expose an empty service result as an accepted command", async () => {
    const { session, command } = fixture();
    const original = envelope(session, command.body.idempotency_key);
    const invalid = {
      ...original, result: {}, receipt: { ...original.receipt, accepted: {} },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response(invalid)));
    await expect(sendGraphCommand(command.path, command.body)).rejects.toMatchObject({ kind: "unknown" });
  });

  it.each([
    ["operation", (value: ReturnType<typeof envelope>) => { value.receipt.operation = "run.control"; }],
    ["key", (value: ReturnType<typeof envelope>) => { value.receipt.idempotency_key = "other-operation"; }],
    ["target", (value: ReturnType<typeof envelope>) => { value.receipt.target.session_id = crypto.randomUUID(); }],
    ["authority", (value: ReturnType<typeof envelope>) => { value.receipt.authority = "service_result"; }],
    ["accepted result", (value: ReturnType<typeof envelope>) => { value.receipt.accepted.revision++; }],
    ["digest format", (value: ReturnType<typeof envelope>) => { value.receipt.request_sha256 = "not-a-digest"; }],
  ])("retains the original request when the %s evidence disagrees", async (_label, change) => {
    const state = fixture(), { session, command } = state;
    const result = envelope(session, command.body.idempotency_key);
    change(result);
    vi.stubGlobal("fetch", vi.fn(async () => response(result)));
    const boundary = managed(state);
    await expect(boundary.channel.dispatch(command)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.accept).not.toHaveBeenCalled();
    expect(boundary.channel.pendingCommand.value).toEqual(command);
    expect(boundary.evidence.ports.resolve).not.toHaveBeenCalled();
  });

  it("retains the exact request after receipt acceptance if local confirmation cannot be saved", async () => {
    const state = fixture(), { session, command } = state;
    vi.stubGlobal("fetch", vi.fn(async () => response(envelope(session, command.body.idempotency_key))));
    const boundary = managed(state);
    vi.mocked(boundary.evidence.ports.resolve).mockRejectedValue(new Error("confirmation unavailable"));
    await expect(boundary.channel.dispatch(command)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.accept).toHaveBeenCalledTimes(1);
    expect(boundary.channel.pendingCommand.value).toEqual(command);
    expect([...boundary.evidence.records.values()][0]?.resolution).toBe("pending");
  });

  it.each([
    ["/api/graph/sessions/not-a-uuid/runs", { expected_revision: 1, idempotency_key: "original" }],
    ["/api/graph/sessions/other/runs", { expected_revision: 1, idempotency_key: "original" }],
    ["/api/graph/sessions", { session_id: crypto.randomUUID(), idempotency_key: "original" }],
    [`/api/graph/sessions/${crypto.randomUUID()}/runs`, {
      session_id: crypto.randomUUID(), expected_revision: 1, idempotency_key: "original",
    }],
  ])("leaves malformed frozen coordinates unresolved before network dispatch: %s", async (path, body) => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const original = graphClone(body);
    await expect(sendGraphCommand(path, body)).rejects.toMatchObject({ kind: "unknown" });
    expect(body).toEqual(original);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("treats POST queries as reads and keeps exact owner, generation and opaque cursor coordinates", async () => {
    const parameters = {
      session_id: crypto.randomUUID(),
      reference: { source_id: "plugin.ordinary-source", exact_version: "7" },
      owner: {
        workflow_session_id: crypto.randomUUID(), chain_run_id: crypto.randomUUID(),
        node_binding_id: crypto.randomUUID(), node_run_id: crypto.randomUUID(),
      },
      generation: 4, source_scope: "history", limit: 8, cursor: "provider:opaque-position",
    };
    const original = graphClone(parameters), sent: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe("/api/graph/queries");
      expect(init.method).toBe("POST");
      sent.push(JSON.parse(String(init.body)));
      throw new Error("Read disconnected");
    }));
    await expect(graphApplicationQuery("information.read", parameters))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(sent).toEqual([{ operation: "information.read", parameters: original }]);
    expect(parameters).toEqual(original);
  });

  it("clears an initial definitive rejection but keeps an initial identity conflict unresolved", async () => {
    for (const code of ["stale_revision", "idempotency_conflict"]) {
      const state = fixture(), readReceipt = vi.fn();
      const boundary = managed(state, { send: async () => {
        throw new WorkbenchApiError("rejected", code, 409, code);
      }, readReceipt });
      await expect(boundary.channel.dispatch(state.command)).rejects.toMatchObject({ code });
      expect(boundary.channel.pendingCommand.value).toEqual(code === "stale_revision" ? null : state.command);
      expect(readReceipt).not.toHaveBeenCalled();
      expect([...boundary.evidence.records.values()][0]?.resolution)
        .toBe(code === "stale_revision" ? "verified" : "pending");
    }
  });
});
