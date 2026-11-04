import { describe, expect, it, vi } from "vitest";
import { createGraphCommand } from "./workflowCommands";
import { graphClone, newGraph } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { createWorkflowManagementRequests } from "./workflowManagementRequests";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
function managedFixture() {
  const document = newGraph("Frozen workflow"), workflowId = document.workflow_definition_id;
  const evidence = memoryRequestEvidence(), current = vi.fn(() => true);
  const session = { schema_version: 2, execution_model: "graph", workflow_definition_id: workflowId,
    workflow_session_id: crypto.randomUUID(), definition_revision: 1, revision: 1, data_revision: 0,
    head_revision: 1, status: "idle", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} } };
  const command = createGraphCommand("/api/graph/sessions",
    { workflow_definition_id: workflowId, definition_revision: 1 }, "create");
  const send = vi.fn(async () => graphClone(session)), readReceipt = vi.fn(async () => graphClone(session));
  const accept = vi.fn();
  const application = createWorkflowManagementRequests({
    evidence: evidence.ports, current, send, readReceipt, accept,
  });
  return { command, evidence, current, send, readReceipt, accept, channel: application.forWorkflow(workflowId) };
}

describe("async graph mutation custody", () => {
  it("awaits durable original storage and durable accepted-state storage before success", async () => {
    const before = deferred<boolean>(), after = deferred<boolean>(), reachedAfter = deferred<void>();
    const state = managedFixture(), write = state.evidence.ports.write, resolve = state.evidence.ports.resolve;
    state.evidence.ports.write = vi.fn(async request => {
      await before.promise; return await write(request);
    });
    state.evidence.ports.resolve = vi.fn(async request => {
      reachedAfter.resolve(); await after.promise; return await resolve(request);
    });
    let completed = false;
    const running = state.channel.dispatch(state.command)
      .then(value => { completed = true; return value; });
    await vi.waitFor(() => expect(state.evidence.ports.write).toHaveBeenCalledOnce());
    expect(state.send).not.toHaveBeenCalled();
    expect(state.evidence.ports.write).toHaveBeenCalledWith(expect.objectContaining({ request: state.command.body }));
    before.resolve(true); await reachedAfter.promise;
    expect(state.send).toHaveBeenCalledWith(state.command.path, state.command.body);
    expect(state.channel.pendingCommand.value).toEqual(state.command); expect(completed).toBe(false);
    after.resolve(true); await running;
    expect(completed).toBe(true); expect(state.channel.pending.value).toBeNull();
    expect(state.accept).toHaveBeenCalledOnce();
    expect([...state.evidence.records.values()][0]?.resolution).toBe("verified");
  });
  it("never dispatches after an asynchronous pre-save failure or closed-workflow fence", async () => {
    const gate = deferred<boolean>();
    const state = managedFixture();
    state.evidence.ports.write = vi.fn(async () => {
      await gate.promise; throw new WorkbenchApiError("unavailable", "storage failed");
    });
    const running = state.channel.dispatch(state.command);
    gate.resolve(false);
    await expect(running).rejects.toMatchObject({ kind: "unavailable" });
    expect(state.send).not.toHaveBeenCalled(); expect(state.channel.pending.value).toBeNull();
    const fence = deferred<boolean>();
    const closed = managedFixture(), write = closed.evidence.ports.write;
    closed.evidence.ports.write = vi.fn(async request => { await fence.promise; return await write(request); });
    const late = closed.channel.dispatch(closed.command);
    closed.current.mockReturnValue(false); fence.resolve(true);
    await expect(late).rejects.toMatchObject({ kind: "rejected" });
    expect(closed.send).not.toHaveBeenCalled();
    expect([...closed.evidence.records.values()][0]?.request).toEqual(closed.command.body);
  });
  it("keeps the original after accepted-state save failure and reconciles without mutation replay", async () => {
    const gate = deferred<boolean>(), reachedAfter = deferred<void>();
    const state = managedFixture(), resolve = state.evidence.ports.resolve;
    state.evidence.ports.resolve = vi.fn(async request => {
      reachedAfter.resolve();
      if (!await gate.promise) throw new WorkbenchApiError("unavailable", "resolution failed");
      return await resolve(request);
    });
    const running = state.channel.dispatch(state.command);
    await reachedAfter.promise; gate.resolve(false);
    await expect(running).rejects.toMatchObject({ kind: "unknown" });
    expect(state.channel.pendingCommand.value).toEqual(state.command); expect(state.accept).toHaveBeenCalledOnce();
    state.evidence.ports.resolve = vi.fn(resolve);
    expect(await state.channel.reconcile()).toBe(true);
    expect(state.channel.pending.value).toBeNull();
    expect(state.send).toHaveBeenCalledOnce();
    expect(state.readReceipt).toHaveBeenCalledWith(state.command.path, state.command.body);
  });
  it("awaits rejected-state persistence and retains the frozen original if that storage fails", async () => {
    const gate = deferred<boolean>(), reachedAfter = deferred<void>();
    let completed = false;
    const state = managedFixture();
    state.send.mockRejectedValue(new WorkbenchApiError("rejected", "stale", 409, "stale_revision"));
    state.evidence.ports.resolve = vi.fn(async () => {
      reachedAfter.resolve(); await gate.promise; throw new WorkbenchApiError("unavailable", "resolution failed");
    });
    const running = state.channel.dispatch(state.command)
      .catch(failure => { completed = true; return failure; });
    await reachedAfter.promise; expect(completed).toBe(false);
    gate.resolve(false);
    expect(await running).toMatchObject({ kind: "rejected", code: "stale_revision" });
    expect(state.channel.pendingCommand.value).toEqual(state.command);
    expect([...state.evidence.records.values()][0]?.resolution).toBe("pending");
  });
});
