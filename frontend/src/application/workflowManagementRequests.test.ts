import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphCommandOperation } from "../adapters/workflowApplicationApi";
import { graphClone } from "../domain/workflowGraph";
import { memoryRequestEvidence, requestEvidenceCommandResponse } from "../testUtils/requestEvidence";
import { createGraphCommand } from "./workflowCommands";
import { createWorkflowManagementRequests } from "./workflowManagementRequests";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
function fixture() {
  const workflowId = crypto.randomUUID(), sessionId = crypto.randomUUID();
  const evidence = memoryRequestEvidence();
  const session = { schema_version: 2, execution_model: "graph", workflow_definition_id: workflowId,
    workflow_session_id: sessionId, definition_revision: 1, revision: 1, data_revision: 0,
    head_revision: 1, status: "idle", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} } };
  const command = createGraphCommand("/api/graph/sessions",
    { workflow_definition_id: workflowId, definition_revision: 1 }, "create");
  const send = vi.fn(async () => graphClone(session)), readReceipt = vi.fn(async () => graphClone(session));
  const accept = vi.fn(), current = vi.fn(() => true);
  const ports = { evidence: evidence.ports, send, readReceipt, accept, current };
  const app = createWorkflowManagementRequests(ports);
  return { workflowId, sessionId, session, command, evidence, ports, app, entry: app.forWorkflow(workflowId) };
}

describe("formal management request custody", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the installed application transport with independent evidence rather than a workbench draft", async () => {
    const state = fixture(), observed: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_path, init) => {
      const { operation, parameters } = JSON.parse(String(init.body));
      observed.push(operation);
      if (operation === "request-evidence.read")
        return new Response(JSON.stringify(await state.evidence.ports.read(parameters.owner_kind, parameters.owner_id)));
      if (operation === "request-evidence.write")
        return requestEvidenceCommandResponse(operation, await state.evidence.ports.write(parameters));
      if (operation === "request-evidence.resolve")
        return requestEvidenceCommandResponse(operation, await state.evidence.ports.resolve(parameters));
      expect(operation).toBe("session.create");
      expect(parameters).toEqual(state.command.body);
      const accepted = Object.fromEntries(["workflow_definition_id", "definition_revision",
        "workflow_session_id", "revision", "data_revision", "head_revision", "status"]
        .map(field => [field, state.session[field as keyof typeof state.session]]));
      return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command",
        result: state.session, receipt: { schema_version: 1, kind: "workflow.application-receipt",
          operation, operation_scope: "management", idempotency_key: parameters.idempotency_key,
          request_sha256: "a".repeat(64), authority: "service_receipt", target: {}, accepted } }));
    }));
    const entry = createWorkflowManagementRequests({ accept: state.ports.accept }).forWorkflow(state.workflowId);
    expect(await entry.dispatch(state.command)).toMatchObject({ kind: "session", session: state.session });
    expect(observed).toEqual(["request-evidence.read", "request-evidence.write", "request-evidence.read",
      "session.create", "request-evidence.read", "request-evidence.read", "request-evidence.read", "request-evidence.resolve"]);
    expect(state.ports.accept).toHaveBeenCalledOnce();
  });

  it("persists exact business parameters before transport and resolves only the original evidence", async () => {
    const state = fixture(), written = state.evidence.ports.write;
    const gate = deferred<void>();
    state.ports.evidence.write = vi.fn(async request => { await gate.promise; return written(request); });
    const running = state.entry.dispatch(state.command);
    await vi.waitFor(() => expect(state.ports.evidence.write).toHaveBeenCalledOnce());
    expect(state.ports.send).not.toHaveBeenCalled();
    gate.resolve();
    const result = await running;
    expect(result).toMatchObject({ kind: "session", session: state.session });
    expect(state.ports.evidence.write).toHaveBeenCalledWith({
      operation: "session.create", idempotency_key: state.command.body.idempotency_key,
      owner_kind: "workflow-management", owner_id: state.workflowId, request: state.command.body,
    });
    expect(state.ports.accept).toHaveBeenCalledWith(state.workflowId, result, state.command);
    expect(state.entry.pending.value).toBeNull();
    expect([...state.evidence.records.values()][0].resolution).toBe("verified");
  });

  it("does not send if original evidence storage fails", async () => {
    const state = fixture();
    state.ports.evidence.write = vi.fn(async () => { throw new WorkbenchApiError("unavailable", "storage unavailable"); });
    await expect(state.entry.dispatch(state.command)).rejects.toMatchObject({ kind: "unavailable" });
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(state.ports.accept).not.toHaveBeenCalled();
  });

  it("does not construct a new extend-budget control request", async () => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${state.sessionId}/control`, {
      action: "extend_budget", expected_revision: 1, add_model_requests: 1, add_model_attempts: 4,
    }, "extend_budget");

    await expect(state.entry.dispatch(command)).rejects.toMatchObject({ kind: "rejected" });

    expect(state.ports.evidence.write).not.toHaveBeenCalled();
    expect(state.ports.send).not.toHaveBeenCalled();
  });

  it("keeps an original unknown extend-budget request receipt-readable without replay", async () => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${state.sessionId}/control`, {
      action: "extend_budget", expected_revision: 1, add_model_requests: 1, add_model_attempts: 4,
    }, "extend_budget");
    const original = graphCommandOperation(command.path, command.body);
    await state.evidence.ports.write({ operation: original.operation,
      idempotency_key: String(original.parameters.idempotency_key), owner_kind: "workflow-management",
      owner_id: state.workflowId, request: graphClone(original.parameters) });
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);

    await entry.initialize();
    expect(entry.pendingCommand.value).toEqual(command);
    expect(await entry.reconcile()).toBe(true);

    expect(state.ports.readReceipt).toHaveBeenCalledWith(command.path, command.body);
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(entry.pending.value).toBeNull();
  });

  it("rejects new session data updates before storing evidence or sending", async () => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${state.sessionId}/data`, {
      expected_revision: 1, expected_data_revision: 0, variables: { legacy: 7 },
    }, "data");

    await expect(state.entry.dispatch(command)).rejects.toMatchObject({ kind: "rejected" });

    expect(state.ports.evidence.write).not.toHaveBeenCalled();
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(state.entry.pending.value).toBeNull();
    expect([...state.evidence.records.values()]).toEqual([]);
  });

  it("keeps an original session data update receipt-readable without replay", async () => {
    const state = fixture();
    const path = `/api/graph/sessions/${state.sessionId}/data`;
    const body = { expected_revision: 1, expected_data_revision: 0, variables: { legacy: 7 },
      idempotency_key: crypto.randomUUID() };
    const operation = graphCommandOperation(path, body);
    await state.evidence.ports.write({ operation: operation.operation,
      idempotency_key: body.idempotency_key, owner_kind: "workflow-management",
      owner_id: state.workflowId, request: graphClone(operation.parameters) });
    const accepted = { ...state.session, revision: 2, data_revision: 1,
      data: { revision: 1, values: { legacy: 7 } } };
    state.ports.readReceipt.mockResolvedValue(accepted);
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);

    await entry.initialize();
    expect(entry.pendingCommand.value).toEqual({ path, body, action: "data" });
    expect(await entry.reconcile()).toBe(true);

    expect(state.ports.readReceipt).toHaveBeenCalledWith(path, body);
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(state.ports.accept).toHaveBeenCalledOnce();
    expect(entry.pending.value).toBeNull();
    expect([...state.evidence.records.values()][0].resolution).toBe("verified");
  });

  it("reopens unresolved evidence without sending and reconciles the original key", async () => {
    const state = fixture();
    state.ports.send = vi.fn(async () => { throw new WorkbenchApiError("unknown", "unknown result"); });
    const app = createWorkflowManagementRequests(state.ports), entry = app.forWorkflow(state.workflowId);
    await expect(entry.dispatch(state.command)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pendingCommand.value).toEqual(state.command);
    const reopened = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    await reopened.initialize();
    expect(reopened.pendingCommand.value).toEqual(state.command);
    expect(state.ports.send).toHaveBeenCalledOnce();
    expect(await reopened.reconcile()).toBe(true);
    expect(state.ports.readReceipt).toHaveBeenCalledWith(state.command.path, state.command.body);
    expect(state.ports.send).toHaveBeenCalledOnce();
    expect(reopened.pending.value).toBeNull();
  });

  it("keeps pending after mismatched or unresolved original receipt reads", async () => {
    const state = fixture();
    await state.evidence.ports.write({ operation: "session.create",
      idempotency_key: String(state.command.body.idempotency_key),
      owner_kind: "workflow-management", owner_id: state.workflowId, request: graphClone(state.command.body) });
    state.ports.readReceipt = vi.fn(async () => ({ ...state.session, workflow_definition_id: crypto.randomUUID() }));
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    expect(await entry.reconcile()).toBe(false);
    expect(entry.pending.value).not.toBeNull();
    expect(state.ports.accept).not.toHaveBeenCalled();
    expect(state.ports.evidence.resolve).not.toHaveBeenCalled();
    expect(state.ports.send).not.toHaveBeenCalled();
  });

  it("never replays a conflicting business request", async () => {
    const state = fixture();
    state.ports.send = vi.fn(async () => { throw new WorkbenchApiError("rejected", "key differs", 409, "idempotency_conflict"); });
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    await expect(entry.dispatch(state.command)).rejects.toMatchObject({ code: "idempotency_conflict" });
    expect(entry.pending.value).not.toBeNull();
    expect(state.ports.evidence.resolve).not.toHaveBeenCalled();
  });

  it("resolves a known business rejection without changing the current view", async () => {
    const state = fixture();
    state.ports.send = vi.fn(async () => { throw new WorkbenchApiError("rejected", "stale", 409, "stale_revision"); });
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    await expect(entry.dispatch(state.command)).rejects.toMatchObject({ code: "stale_revision" });
    expect(entry.pending.value).toBeNull();
    expect(state.ports.accept).not.toHaveBeenCalled();
  });

  it("reports evidence resolution failure and keeps original parameters", async () => {
    const state = fixture();
    state.ports.evidence.resolve = vi.fn(async () => { throw new WorkbenchApiError("unavailable", "resolution unavailable"); });
    await expect(state.entry.dispatch(state.command)).rejects.toMatchObject({ kind: "unknown" });
    expect(state.ports.accept).toHaveBeenCalledOnce();
    expect(state.entry.pendingCommand.value).toEqual(state.command);
    expect(state.ports.send).toHaveBeenCalledOnce();
  });

  it("does not mistake rejected local view acceptance for a rejected business command", async () => {
    const state = fixture();
    state.ports.accept = vi.fn(() => { throw new WorkbenchApiError("rejected", "view changed"); });
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    await expect(entry.dispatch(state.command)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.lastReceipt.value).toMatchObject({ kind: "session", session: state.session });
    expect(entry.pendingCommand.value).toEqual(state.command);
    expect(state.ports.evidence.resolve).not.toHaveBeenCalled();
    expect(state.ports.send).toHaveBeenCalledOnce();
    state.ports.accept.mockImplementation(() => {});
    expect(await entry.reconcile()).toBe(true);
    expect(state.ports.send).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).toHaveBeenCalledOnce();
    expect(entry.pending.value).toBeNull();
  });

  it("does not submit if the original workflow closes while evidence is being saved", async () => {
    const state = fixture(), written = state.evidence.ports.write;
    state.ports.evidence.write = vi.fn(async request => {
      const result = await written(request);
      state.ports.current.mockReturnValue(false);
      return result;
    });
    await expect(state.entry.dispatch(state.command)).rejects.toMatchObject({ kind: "rejected" });
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(state.ports.accept).not.toHaveBeenCalled();
    expect(state.entry.pending.value).toBeNull();
  });

  it.each(["session.copy", "candidate.fork", "session.rebind"])("does not reopen %s", async operation => {
    const state = fixture();
    const suffix = operation === "session.copy" ? "copy" : operation === "candidate.fork" ? "candidates/fork" : "rebind";
    const command = createGraphCommand(`/api/graph/sessions/${state.sessionId}/${suffix}`, {}, "copy");
    await expect(state.entry.dispatch(command)).rejects.toBeInstanceOf(WorkbenchApiError);
    expect(state.ports.send).not.toHaveBeenCalled();
    expect(state.ports.evidence.write).not.toHaveBeenCalled();
  });

  it("rejects an operation whose workflow target differs from the evidence owner", async () => {
    const state = fixture(), other = crypto.randomUUID();
    const command = createGraphCommand(`/api/graph/definitions/${other}/delete`, { expected_revision: 1 }, "definition-delete");
    await expect(state.entry.dispatch(command)).rejects.toMatchObject({ kind: "unavailable" });
    expect(state.ports.evidence.write).not.toHaveBeenCalled();
    expect(state.ports.send).not.toHaveBeenCalled();
  });

  it("accepts archive evidence for the original nonselected session", async () => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${state.sessionId}/archive`,
      { expected_revision: 3 }, "session-archive");
    command.source_session_id = state.sessionId;
    const result = { schema_version: 1, kind: "workflow.session-management",
      workflow_definition_id: state.workflowId, workflow_session_id: state.sessionId,
      revision: 3, archived: true, deleted: false };
    state.ports.send = vi.fn(async () => result as unknown as typeof state.session);
    const entry = createWorkflowManagementRequests(state.ports).forWorkflow(state.workflowId);
    expect(await entry.dispatch(command)).toEqual({ kind: "management", result });
    expect(state.ports.accept).toHaveBeenCalledWith(state.workflowId, { kind: "management", result }, command);
  });

  it("keeps independent workflow channels separate", async () => {
    const state = fixture(), other = crypto.randomUUID();
    const second = state.app.forWorkflow(other);
    await second.initialize();
    expect(second.pending.value).toBeNull();
    await state.entry.dispatch(state.command);
    expect(second.pending.value).toBeNull();
    expect(state.ports.accept.mock.calls[0][0]).toBe(state.workflowId);
  });
});
