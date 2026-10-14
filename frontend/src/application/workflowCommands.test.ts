import { describe, expect, it, vi } from "vitest";
import { createGraphCommand, dispatchGraphCommand, type GraphCommandPorts } from "./workflowCommands";
import { graphClone, newGraph, type GraphEntry } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
function fixture(overrides: Partial<GraphCommandPorts> = {}) {
  const document = newGraph("Frozen workflow");
  const entry: GraphEntry = { document, saved_revision: 0, session_id: null, pending: null };
  const command = createGraphCommand("/api/graph/definitions",
    { document: graphClone(document), expected_revision: 0 }, "save");
  const ports: GraphCommandPorts = { persist: vi.fn(() => true), request: vi.fn(async () => graphClone(document)),
    readReceipt: vi.fn(async () => graphClone(document)), readDefinition: vi.fn(),
    current: () => true, accept: vi.fn(receipt => {
      if (receipt.kind === "definition") {
        entry.saved_revision = receipt.document.revision;
        entry.saved_document = graphClone(receipt.document);
      }
    }), ...overrides };
  return { entry, command, ports };
}

describe("async graph mutation custody", () => {
  it("blocks a new rebind before persistence or transport", async () => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${crypto.randomUUID()}/rebind`,
      { definition_revision: 2, expected_revision: 1, expected_data_revision: 0,
        expected_head_revision: 1, mappings: [] }, "rebind");
    await expect(dispatchGraphCommand(state.entry, command, state.ports))
      .rejects.toMatchObject({ kind: "rejected" });
    expect(state.entry.pending).toBeNull();
    expect(state.ports.persist).not.toHaveBeenCalled();
    expect(state.ports.request).not.toHaveBeenCalled();
    expect(state.ports.readReceipt).not.toHaveBeenCalled();
  });

  it("reconciles an already-persisted rebind through its exact read-only receipt coordinates", async () => {
    const sessionId = crypto.randomUUID(), state = fixture();
    state.entry.saved_revision = 2;
    state.entry.document.revision = 2;
    state.entry.session_id = sessionId;
    const path = `/api/graph/sessions/${sessionId}/rebind`;
    const command = createGraphCommand(path, { definition_revision: 2, expected_revision: 3,
      expected_data_revision: 2, expected_head_revision: 1, mappings: [] }, "rebind");
    const original = graphClone(command);
    const result = { schema_version: 2, execution_model: "graph",
      workflow_definition_id: state.entry.document.workflow_definition_id,
      workflow_session_id: sessionId, definition_revision: 2, revision: 4, data_revision: 2,
      head_revision: 2, status: "idle", can_submit: true, nodes: [], chains: [], outputs: [],
      data: { revision: 2, values: {} } };
    state.entry.pending = graphClone(command);
    state.ports.readReceipt = vi.fn(async () => result);

    await dispatchGraphCommand(state.entry, command, state.ports, true);

    expect(state.ports.readReceipt).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).toHaveBeenCalledWith(path, original.body);
    expect(state.ports.request).not.toHaveBeenCalled();
    expect(state.entry.pending).toBeNull();
  });

  it.each(["candidate-select", "candidate-fork"])("blocks a new %s before persistence or transport", async action => {
    const state = fixture();
    const command = createGraphCommand(`/api/graph/sessions/${crypto.randomUUID()}/candidates/${action.endsWith("select") ? "select" : "fork"}`,
      { candidate_id: crypto.randomUUID(), expected_revision: 1, expected_data_revision: 0,
        expected_head_revision: 1 }, action);
    await expect(dispatchGraphCommand(state.entry, command, state.ports)).rejects.toMatchObject({ kind: "rejected" });
    expect(state.entry.pending).toBeNull();
    expect(state.ports.persist).not.toHaveBeenCalled();
    expect(state.ports.request).not.toHaveBeenCalled();
  });

  it("awaits durable original storage and durable accepted-state storage before success", async () => {
    const before = deferred<boolean>(), after = deferred<boolean>(), reachedAfter = deferred<void>();
    let saves = 0, completed = false;
    const state = fixture({ persist: vi.fn(() => {
      if (++saves === 1) return before.promise;
      reachedAfter.resolve(); return after.promise;
    }) });
    const running = dispatchGraphCommand(state.entry, state.command, state.ports)
      .then(value => { completed = true; return value; });
    expect(state.entry.pending).toEqual(state.command); expect(state.ports.request).not.toHaveBeenCalled();
    before.resolve(true); await reachedAfter.promise;
    expect(state.ports.request).toHaveBeenCalledWith(state.command.path, state.command.body);
    expect(state.entry.pending).toBeNull(); expect(completed).toBe(false);
    after.resolve(true); await running;
    expect(completed).toBe(true); expect(state.entry.saved_revision).toBe(1);
  });
  it("never dispatches after an asynchronous pre-save failure or closed-workflow fence", async () => {
    const gate = deferred<boolean>();
    const state = fixture({ persist: () => gate.promise });
    const running = dispatchGraphCommand(state.entry, state.command, state.ports);
    gate.resolve(false);
    await expect(running).rejects.toMatchObject({ kind: "unavailable" });
    expect(state.ports.request).not.toHaveBeenCalled(); expect(state.entry.pending).toBeNull();
    const fence = deferred<boolean>();
    let current = true;
    const closed = fixture({ persist: () => fence.promise, current: () => current });
    const late = dispatchGraphCommand(closed.entry, closed.command, closed.ports);
    current = false; fence.resolve(true);
    await expect(late).rejects.toMatchObject({ kind: "unknown" });
    expect(closed.ports.request).not.toHaveBeenCalled(); expect(closed.entry.pending).toEqual(closed.command);
  });
  it("keeps the original after accepted-state save failure and reconciles without mutation replay", async () => {
    const gate = deferred<boolean>(), reachedAfter = deferred<void>();
    let saves = 0;
    const state = fixture({ persist: () => {
      if (++saves === 1) return true;
      if (saves === 2) { reachedAfter.resolve(); return gate.promise; }
      return true;
    } });
    const running = dispatchGraphCommand(state.entry, state.command, state.ports);
    await reachedAfter.promise; gate.resolve(false);
    await expect(running).rejects.toMatchObject({ kind: "unknown" });
    expect(state.entry.pending).toEqual(state.command); expect(state.entry.saved_revision).toBe(1);
    await dispatchGraphCommand(state.entry, state.command, state.ports, true);
    expect(state.entry.pending).toBeNull();
    expect(state.ports.request).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).toHaveBeenCalledWith(state.command.path, state.command.body);
  });
  it("does not report a stale accepted completion if the workflow closes during final persistence", async () => {
    const gate = deferred<boolean>(), reachedAfter = deferred<void>();
    let saves = 0, current = true;
    const state = fixture({ current: () => current, persist: () => {
      if (++saves === 1) return true;
      reachedAfter.resolve(); return gate.promise;
    } });
    const running = dispatchGraphCommand(state.entry, state.command, state.ports);
    await reachedAfter.promise;
    current = false; gate.resolve(true);
    await expect(running).rejects.toMatchObject({ kind: "unknown" });
    expect(state.entry.pending).toBeNull();
    expect(state.ports.request).toHaveBeenCalledOnce();
  });
  it("awaits rejected-state persistence and retains the frozen original if that storage fails", async () => {
    const gate = deferred<boolean>(), reachedAfter = deferred<void>();
    let saves = 0, completed = false;
    const state = fixture({ persist: () => {
      if (++saves === 1) return true;
      reachedAfter.resolve(); return gate.promise;
    }, request: vi.fn(async () => { throw new WorkbenchApiError("rejected", "stale", 409, "stale_revision"); }) });
    const running = dispatchGraphCommand(state.entry, state.command, state.ports)
      .catch(failure => { completed = true; return failure; });
    await reachedAfter.promise; expect(completed).toBe(false);
    gate.resolve(false);
    expect(await running).toMatchObject({ kind: "rejected", code: "stale_revision" });
    expect(state.entry.pending).toEqual(state.command);
  });
});
