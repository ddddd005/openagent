const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const scope = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/phase1_agent/static/frontend-node-client.js"), "utf8"), scope);
const id = value => `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;
const copy = value => JSON.parse(JSON.stringify(value));
const owner = { workflow_definition_id: id(1), definition_revision: 1, node_binding_id: id(2), session_id: id(3) };
function fixture(handle) {
  const calls = [], receipts = new Map();
  const state = {
    schema_version: 1, kind: "workflow.frontend-node-state", owner,
    input: null, run: { revision: 8, status: "succeeded", can_start: true, can_pause: false,
      can_resume: false, can_close: false, can_fork: true, data_revision: 4, head_revision: 2, object_revision: 7 },
    snapshot: { schema_version: 2, kind: "workflow.frontend-node-snapshot",
      owner: { workflow_definition_id: id(1), definition_revision: 1, node_binding_id: id(2),
        workflow_session_id: id(3), object_key: "chat" }, basis: { revision: 7, revision_id: id(4) },
      chat: { rounds: [{ round_id: id(10), base_checkpoint_id: null, submitted_input: "original",
        candidates: [20, 21].map((candidate, index) => ({ candidate_id: id(candidate),
          messages: [{ message_id: id(30 + index), role: "assistant", text: "reply " + index }],
          result_checkpoint_id: id(40 + index) })), selected_candidate_id: id(21) }] } },
    pending: [],
  };
  let sequence = 0;
  const client = new scope.FrontendNodeClient({ owner, keyFactory: () => "chat-" + ++sequence,
    port: { request: async request => {
      calls.push(copy(request));
      if (request.operation === "state") return copy(state);
      if (["receipt", "accept_receipt"].includes(request.operation))
        return copy(receipts.get(request.parameters.original_request.parameters.idempotency_key));
      const body = request.operation === "fork_floor" ? {
        outcome: "succeeded", fork_result: { target_session_id: id(50), target_checkpoint_id: id(51),
          target_commit_id: id(52), target_workflow_ref_id: id(53) }, failure_phase: null, error: null,
        next_owner: { ...owner, session_id: id(50) },
      } : { outcome: "succeeded", control_result: {
        workflow_definition_id: id(1), definition_revision: 1, workflow_session_id: id(3),
        revision: 9, status: "succeeded", active_chain_run_id: null,
      }, failure_phase: null, error: null, next_owner: null };
      const value = { schema_version: 1, kind: "workflow.frontend-node-result", owner,
        operation: request.operation, idempotency_key: request.parameters.idempotency_key,
        outcome: body.outcome, result: body };
      receipts.set(request.parameters.idempotency_key, copy(value));
      if (handle) await handle(request, state, value);
      return copy(value);
    } } });
  return { client, state, calls, receipts };
}
test("logical snapshot reads inline candidate bodies and cannot be written by the client", async () => {
  const f = fixture();
  await f.client.initialize();
  assert.deepEqual(copy(f.client.view().snapshot), f.state.snapshot);
  f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text = "local";
  assert.equal(f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text, "reply 0");
  assert.deepEqual(f.calls.map(call => call.operation), ["state"]);
  assert.equal(typeof f.client.writeSnapshot, "undefined");
});
for (const [operation, method, arguments_] of [
  ["reroll", "reroll", [id(10)]], ["select_candidate", "selectCandidate", [id(10), id(20)]],
  ["fork_floor", "forkFloor", [id(10)]],
]) test(`${operation} submits exact node owner and four revision bases`, async () => {
  const f = fixture();
  await f.client.initialize();
  await f.client[method](...arguments_);
  assert.deepEqual(f.calls[1], { owner, operation, parameters: {
    round_id: id(10), ...(operation === "select_candidate" ? { candidate_id: id(20) } : {}),
    expected_session_revision: 8, expected_data_revision: 4, expected_head_revision: 2,
    expected_object_revision: 7, idempotency_key: "chat-1",
  } });
  assert.equal(f.client.view().snapshot.chat.rounds[0].selected_candidate_id, id(21));
});
test("candidate unknown reconciles the original request without redispatch", async () => {
  const f = fixture(async request => { if (request.operation === "select_candidate") throw new Error("lost"); });
  await f.client.initialize();
  await assert.rejects(f.client.selectCandidate(id(10), id(20)), /lost/);
  await assert.rejects(f.client.selectCandidate(id(10), id(21)), /unavailable/);
  await f.client.reconcile("select_candidate");
  assert.deepEqual(f.calls.map(call => call.operation), ["state", "select_candidate", "receipt", "accept_receipt"]);
  assert.equal(f.client.view().pending.select_candidate, null);
});
test("locked floors, pending candidates and stale object snapshots do not dispatch", async () => {
  const f = fixture();
  f.state.snapshot.chat.rounds.push({ round_id: id(11), base_checkpoint_id: id(41), submitted_input: "next",
    candidates: [], selected_candidate_id: null });
  await f.client.initialize();
  await assert.rejects(f.client.reroll(id(10)), /unavailable/);
  await assert.rejects(f.client.selectCandidate(id(10), id(20)), /unavailable/);
  f.state.snapshot.chat.rounds.pop();
  f.state.snapshot.chat.rounds[0].candidates[0].result_checkpoint_id = null;
  await f.client.refresh();
  await assert.rejects(f.client.selectCandidate(id(10), id(20)), /unavailable/);
  f.state.run.object_revision++;
  await f.client.refresh();
  await assert.rejects(f.client.forkFloor(id(10)), /unavailable/);
  assert.ok(f.calls.every(call => call.operation === "state"));
});
for (const mutate of [
  state => { state.snapshot.chat.rounds[0].selected_candidate_id = id(99); },
  state => { state.snapshot.chat.rounds[0].candidates[0].messages[0].source_ref = { scope: "artifact", output_id: id(99) }; },
  state => { state.snapshot.chat.rounds[0].candidates[0].messages[0].text = {}; },
  state => { state.snapshot.owner.workflow_session_id = id(99); },
]) test("untrusted logical state is rejected before becoming a display cache", async () => {
  const f = fixture(); mutate(f.state);
  await assert.rejects(f.client.initialize(), /state_mismatch/);
  assert.equal(f.client.view().snapshot, null);
});
