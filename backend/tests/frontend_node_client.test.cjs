const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../src/phase1_agent/static/frontend-node-client.js"), "utf8");
const scope = {};
vm.runInNewContext(source, scope);
const Client = scope.FrontendNodeClient;
const id = value => `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;
const binding = { workflow_definition_id: id(1), definition_revision: 1, node_binding_id: id(2), session_id: null };
const copy = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 12; index++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
function clock() {
  let now = 0, sequence = 0;
  const tasks = new Map();
  return {
    timers: {
      setTimeout(callback, milliseconds) {
        const handle = ++sequence;
        tasks.set(handle, { callback, at: now + milliseconds }); return handle;
      },
      clearTimeout(handle) { tasks.delete(handle); },
    },
    async advance(milliseconds) {
      const end = now + milliseconds;
      while (true) {
        const ready = [...tasks].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!ready) break;
        now = ready[1].at; tasks.delete(ready[0]); ready[1].callback(); await settle();
      }
      now = end; await settle();
    },
    count() { return tasks.size; },
  };
}
function snapshot(owner) {
  return {
    schema_version: 2, kind: "workflow.frontend-node-snapshot",
    owner: { workflow_definition_id: owner.workflow_definition_id, definition_revision: owner.definition_revision,
      node_binding_id: owner.node_binding_id, workflow_session_id: owner.session_id, object_key: "chat" },
    basis: { revision: 1, revision_id: id(30) }, chat: { rounds: [{
      round_id: id(31), base_checkpoint_id: null, submitted_input: "display-only",
      candidates: [{ candidate_id: id(32), result_checkpoint_id: id(34),
        messages: [{ message_id: id(33), role: "assistant", text: "display-only" }] }],
      selected_candidate_id: id(32),
    }] },
  };
}
function envelopeResult(request, result) {
  return { schema_version: 1, kind: "workflow.frontend-node-result", owner: copy(request.owner),
    operation: request.operation, idempotency_key: request.parameters.idempotency_key,
    outcome: result.outcome, result: copy(result) };
}
function patchResult(request, outcome = "saved", overrides = {}) {
  return envelopeResult(request, {
    outcome, edit_sequence: request.parameters.expected_edit_sequence + (outcome === "saved" ? 1 : 0),
    definition_revision: request.owner.definition_revision, text: request.parameters.text, next_owner: null,
    ...overrides,
  });
}
function phase(owner, session = owner.session_id ?? id(3), revision = 1, status = "idle") {
  return {
    workflow_definition_id: owner.workflow_definition_id, definition_revision: owner.definition_revision,
    workflow_session_id: session, revision, status, active_chain_run_id: status === "idle" ? null : id(40),
  };
}
function startResult(request) {
  const created = request.owner.session_id === null ? phase(request.owner) : null;
  const started = phase(request.owner, created?.workflow_session_id, 2, "prepared");
  return envelopeResult(request, {
    outcome: "succeeded", create_result: created, start_result: started, failure_phase: null, error: null,
    next_owner: created ? { ...request.owner, session_id: created.workflow_session_id } : null,
  });
}
function pauseResult(request) {
  return envelopeResult(request, { outcome: "succeeded", control_result: phase(
    request.owner, request.owner.session_id, request.parameters.expected_session_revision + 1, "running"),
  failure_phase: null, error: null, next_owner: null });
}
function pending(request) {
  return { schema_version: 1, kind: "workflow.request-evidence", operation: {
    patch_input: "frontend-node.patch-input", start: "frontend-node.start", pause: "frontend-node.pause",
    resume: "frontend-node.resume", close: "frontend-node.close", fork_checkpoint: "frontend-node.fork-checkpoint",
  }[request.operation],
    idempotency_key: request.parameters.idempotency_key, owner_kind: "frontend-node-input", owner_id: "exact",
    request: { owner: copy(request.owner), parameters: copy(request.parameters) },
    request_digest: "digest:original", resolution: "pending" };
}
function fixture(options = {}) {
  const owner = copy(options.owner ?? binding), time = clock(), calls = [], changes = [], receipts = new Map();
  const state = {
    schema_version: 1, kind: "workflow.frontend-node-state", owner,
    input: options.input === null ? null : { text: "formal", edit_sequence: 1, policy: options.policy ?? "adjustable" },
    run: owner.session_id === null ? null : { revision: 8, status: "running", can_start: false, can_pause: true,
      can_resume: false, can_close: false, can_fork: false },
    snapshot: owner.session_id === null ? null : snapshot(owner), pending: options.pending ?? [],
  };
  let keySequence = 0;
  const request = async envelope => {
    calls.push(copy(envelope));
    if (options.handle) {
      const handled = options.handle(envelope, state, receipts);
      if (handled !== undefined) return copy(await handled);
    }
    if (envelope.operation === "state") return copy(state);
    if (["receipt", "accept_receipt"].includes(envelope.operation)) {
      const original = { owner: envelope.parameters.original_request.owner,
        operation: envelope.parameters.operation, parameters: envelope.parameters.original_request.parameters };
      return copy(receipts.get(original.parameters.idempotency_key) ?? patchResult(original, "unknown", {
        text: null, edit_sequence: null,
      }));
    }
    let result;
    if (envelope.operation === "patch_input") {
      result = patchResult(envelope);
      state.input.text = envelope.parameters.text; state.input.edit_sequence = result.result.edit_sequence;
    } else result = envelope.operation === "start" ? startResult(envelope) : pauseResult(envelope);
    receipts.set(envelope.parameters.idempotency_key, copy(result));
    return copy(result);
  };
  const client = new Client({ owner, port: { request }, keyFactory: () => "request-" + ++keySequence,
    timers: time.timers, onChange: value => changes.push(copy(value)) });
  return { client, state, time, calls, changes, receipts };
}
const commands = fixture => fixture.calls.filter(call => ["patch_input", "start", "pause"].includes(call.operation));

test("client has only an injected exact-node port and opening reads without creating", async () => {
  const f = fixture();
  assert.equal(f.calls.length, 0);
  const result = await f.client.initialize();
  assert.equal(result.text, "formal");
  assert.deepEqual(f.calls, [{ owner: binding, operation: "state", parameters: {} }]);
  assert.equal(commands(f).length, 0);
  assert.equal(typeof f.client.writeSnapshot, "undefined");
  for (const forbidden of ["fetch(", "localStorage", "URLSearchParams", "GraphChat", "createDraftBuffer",
    "consumer.commands", "workflowConfiguration"]) assert.ok(!source.includes(forbidden), forbidden);
});

test("resume and close submit only their original bound revision and reconcile without dispatch", async () => {
  for (const operation of ["resume", "close"]) {
    let lose = true;
    const f = fixture({ owner: { ...binding, session_id: id(3) }, handle(envelope, state, receipts) {
      if (envelope.operation !== operation) return;
      const saved = pauseResult(envelope);
      receipts.set(envelope.parameters.idempotency_key, saved);
      if (lose) { lose = false; throw new Error("lost response"); }
      return saved;
    } });
    f.state.run = { revision: 8, status: "paused", can_start: false, can_pause: false,
      can_resume: true, can_close: true, can_fork: false };
    await f.client.initialize();
    await assert.rejects(f.client[operation](), /lost response/);
    const original = copy(f.client.view().pending[operation]);
    assert.deepEqual(original.parameters, { expected_session_revision: 8, idempotency_key: "request-1" });
    await assert.rejects(f.client[operation](), /unavailable/);
    await f.client.reconcile(operation);
    assert.equal(f.calls.filter(call => call.operation === operation).length, 1);
    assert.equal(f.client.view().pending[operation], null);
    assert.equal(f.client.view().last_result.operation, operation);
  }
});

function checkpointFixture(options = {}) {
  const basis = { revision: 8, data_revision: 0, head_revision: 3 };
  const points = [{ checkpoint_id: id(70), parent_checkpoint_id: null },
    { checkpoint_id: id(71), parent_checkpoint_id: id(70) }];
  const f = fixture({ owner: { ...binding, session_id: id(3) }, handle(envelope, state, receipts) {
    if (options.handle) {
      const result = options.handle(envelope, state, receipts);
      if (result !== undefined) return result;
    }
    if (envelope.operation === "checkpoints") return { schema_version: 1,
      kind: "workflow.frontend-node-checkpoints", owner: envelope.owner, result: { items: points, basis } };
    if (envelope.operation === "checkpoint") return { schema_version: 1,
      kind: "workflow.frontend-node-checkpoint", owner: envelope.owner, result: {
        schema_version: 3, checkpoint_id: envelope.parameters.checkpoint_id,
        workflow_definition_id: envelope.owner.workflow_definition_id,
        workflow_session_id: envelope.owner.session_id, fixed_definition_revision: envelope.owner.definition_revision,
        parent_checkpoint_id: null,
      } };
    if (envelope.operation === "fork_checkpoint") {
      const result = envelopeResult(envelope, { outcome: "succeeded", fork_result: {
        target_session_id: id(80), target_checkpoint_id: id(81), target_commit_id: id(82), target_workflow_ref_id: id(83),
      }, failure_phase: null, error: null, next_owner: { ...envelope.owner, session_id: id(80) } });
      receipts.set(envelope.parameters.idempotency_key, result); return result;
    }
  } });
  f.state.run = { revision: 8, status: "succeeded", can_start: true, can_pause: false,
    can_resume: false, can_close: false, can_fork: true };
  return { ...f, basis, points };
}

test("checkpoint reads stay on the exact node and fork uses the observed three guards", async () => {
  const f = checkpointFixture();
  await f.client.initialize();
  await f.client.listCheckpoints();
  await f.client.readCheckpoint(id(70));
  assert.equal(f.client.view().checkpoint.checkpoint_id, id(70));
  f.state.run.revision = 9;
  await f.client.refresh();
  await f.client.forkCheckpoint(id(70));
  const sent = f.calls.find(call => call.operation === "fork_checkpoint");
  assert.deepEqual(sent.parameters, { checkpoint_id: id(70), expected_session_revision: 8,
    expected_data_revision: 0, expected_head_revision: 3, idempotency_key: "request-1" });
  assert.deepEqual(copy(f.client.view().owner), { ...binding, session_id: id(3) });
  assert.deepEqual(copy(f.client.view().next_owner), { ...binding, session_id: id(80) });
  assert.equal(f.calls.filter(call => call.operation === "start").length, 0);
});

test("checkpoint foreign ownership or malformed lineage never supplies a fork basis", async () => {
  for (const foreign of [true, false]) {
    const f = checkpointFixture({ handle(envelope) {
      if (envelope.operation !== "checkpoints") return;
      return { schema_version: 1, kind: "workflow.frontend-node-checkpoints",
        owner: foreign ? { ...envelope.owner, session_id: id(99) } : envelope.owner,
        result: { items: [{ checkpoint_id: id(70), parent_checkpoint_id: id(69) }],
          basis: { revision: 8, data_revision: 0, head_revision: 3 } } };
    } });
    await f.client.initialize();
    await assert.rejects(f.client.listCheckpoints(), /checkpoints_mismatch/);
    await assert.rejects(f.client.forkCheckpoint(id(70)), /fork_unavailable/);
    assert.equal(f.client.view().checkpoints, null);
    assert.equal(f.calls.filter(call => call.operation === "fork_checkpoint").length, 0);
  }
});

test("a forged fork target leaves the original request pending for read-only verification", async () => {
  const f = checkpointFixture({ handle(envelope) {
    if (envelope.operation !== "fork_checkpoint") return;
    return envelopeResult(envelope, { outcome: "succeeded", fork_result: {
      target_session_id: id(80), target_checkpoint_id: id(81), target_commit_id: id(82), target_workflow_ref_id: id(83),
    }, failure_phase: null, error: null, next_owner: { ...envelope.owner, session_id: id(99) } });
  } });
  await f.client.initialize(); await f.client.listCheckpoints();
  await assert.rejects(f.client.forkCheckpoint(id(70)), /result_mismatch/);
  assert.equal(f.client.view().status.fork_checkpoint, "unknown");
  assert.equal(f.client.view().next_owner, null);
  assert.ok(f.client.view().pending.fork_checkpoint);
  await assert.rejects(f.client.forkCheckpoint(id(70)), /fork_unavailable/);
});

test("a lost fork result is verified once without creating another target or starting it", async () => {
  const f = checkpointFixture({ handle(envelope, state, receipts) {
    if (envelope.operation !== "fork_checkpoint") return;
    receipts.set(envelope.parameters.idempotency_key, envelopeResult(envelope, {
      outcome: "succeeded", fork_result: { target_session_id: id(80), target_checkpoint_id: id(81),
        target_commit_id: id(82), target_workflow_ref_id: id(83) },
      failure_phase: null, error: null, next_owner: { ...envelope.owner, session_id: id(80) },
    }));
    throw new Error("lost fork response");
  } });
  await f.client.initialize(); await f.client.listCheckpoints();
  await assert.rejects(f.client.forkCheckpoint(id(70)), /lost fork response/);
  const original = copy(f.client.view().pending.fork_checkpoint);
  assert.ok(original);
  const verified = await f.client.reconcile("fork_checkpoint");
  assert.equal(verified.outcome, "succeeded");
  assert.deepEqual(copy(f.client.view().next_owner), { ...binding, session_id: id(80) });
  assert.deepEqual(f.calls.slice(-2).map(row => row.operation), ["receipt", "accept_receipt"]);
  assert.deepEqual(f.calls.at(-2).parameters.original_request, {
    owner: original.owner, parameters: original.parameters,
  });
  assert.equal(f.calls.filter(call => call.operation === "fork_checkpoint").length, 1);
  assert.equal(f.calls.filter(call => call.operation === "start").length, 0);
});

test("late checkpoint reads from a disposed binding do not change its display", async () => {
  const waiting = deferred();
  const f = checkpointFixture({ handle(envelope) {
    if (envelope.operation === "checkpoints") return waiting.promise;
  } });
  await f.client.initialize();
  const reading = f.client.listCheckpoints();
  f.client.dispose();
  waiting.resolve({ invalid: true });
  assert.equal(await reading, null);
  assert.equal(f.client.view().checkpoints, null);
});

test("private 200ms input update coalesces edits and formal receipt advances its basis", async () => {
  const f = fixture(); await f.client.initialize();
  for (let index = 0; index < 50; index++) f.client.update("latest-" + index);
  await f.time.advance(199); assert.equal(commands(f).length, 0);
  await f.time.advance(1);
  assert.deepEqual(commands(f).map(row => row.parameters.text), ["latest-49"]);
  assert.equal(f.client.view().input.edit_sequence, 2);
  assert.equal(f.client.view().dirty, false);
});

test("saving while editing retains newest text and flush waits for its formal receipt", async () => {
  const held = deferred();
  const f = fixture({ handle(request) {
    if (request.operation === "patch_input" && request.parameters.text === "A") return held.promise;
  } });
  await f.client.initialize(); f.client.update("A"); await f.time.advance(200);
  f.client.update("B"); f.client.update("C");
  let flushed = false;
  const flush = f.client.flush().then(() => { flushed = true; });
  await settle(); assert.equal(flushed, false);
  held.resolve(patchResult(commands(f)[0])); await flush;
  assert.deepEqual(commands(f).map(row => [row.parameters.text, row.parameters.expected_edit_sequence]), [["A", 1], ["C", 2]]);
  assert.equal(f.client.view().text, "C");
  assert.equal(f.client.view().input.text, "C");
  assert.equal(f.client.view().dirty, false);
});

test("ordinary in-flight edits retain their own private 200ms update time", async () => {
  const held = deferred();
  const f = fixture({ handle(request) {
    if (request.operation === "patch_input" && request.parameters.text === "A") return held.promise;
  } });
  await f.client.initialize(); f.client.update("A"); await f.time.advance(200);
  await f.time.advance(50); f.client.update("B");
  held.resolve(patchResult(commands(f)[0])); await settle();
  await f.time.advance(199); assert.equal(commands(f).length, 1);
  await f.time.advance(1);
  assert.deepEqual(commands(f).map(row => row.parameters.text), ["A", "B"]);
  assert.equal(f.client.view().dirty, false);
});

test("immediate send cancels timer and starts only after latest successful save", async () => {
  const held = deferred();
  const f = fixture({ handle(request) {
    if (request.operation === "patch_input") return held.promise;
  } });
  await f.client.initialize(); f.client.update("send me");
  const sending = f.client.send(); await settle();
  assert.equal(f.time.count(), 0);
  assert.deepEqual(commands(f).map(row => row.operation), ["patch_input"]);
  assert.throws(() => f.client.update("edit during send"), /input_locked/);
  held.resolve(patchResult(commands(f)[0]));
  const accepted = await sending;
  assert.deepEqual(commands(f).map(row => row.operation), ["patch_input", "start"]);
  assert.equal(accepted.result.start_result.status, "prepared");
  assert.equal(f.client.view().next_owner.session_id, id(3));
  assert.equal(f.client.view().sending, false);
  assert.throws(() => f.client.update("after rebind"), /input_locked/);
});

test("unknown input never auto-retries and explicit receipt acceptance is required", async () => {
  let accepted = null;
  const f = fixture({ handle(request, state, receipts) {
    if (request.operation === "patch_input") {
      accepted = patchResult(request); receipts.set(request.parameters.idempotency_key, accepted);
      return Promise.reject(new Error("lost response"));
    }
  } });
  await f.client.initialize(); f.client.update("unknown");
  await assert.rejects(f.client.flush(), /lost response/);
  f.client.update("newer"); await f.time.advance(1000);
  await assert.rejects(f.client.send(), /input_blocked/);
  assert.equal(commands(f).length, 1);
  const original = f.client.view().pending.patch_input;
  await f.client.reconcile("patch_input");
  assert.deepEqual(f.calls.slice(-2).map(row => row.operation), ["receipt", "accept_receipt"]);
  for (const call of f.calls.slice(-2)) assert.deepEqual(call.parameters, {
    operation: "patch_input", original_request: { owner: binding, parameters: copy(original.parameters) },
  });
  assert.equal(f.client.view().pending.patch_input, null);
  assert.equal(f.client.view().text, "newer");
  assert.equal(f.client.view().dirty, true);
  assert.equal(f.client.view().input.text, "unknown");
  assert.equal(commands(f).length, 1);
});

test("lost receipt-acceptance reply retains original evidence and retries only read and acknowledgment", async () => {
  let acknowledgments = 0;
  const f = fixture({ handle(request, state, receipts) {
    if (request.operation === "patch_input") {
      receipts.set(request.parameters.idempotency_key, patchResult(request));
      return Promise.reject(new Error("lost command"));
    }
    if (request.operation === "accept_receipt" && ++acknowledgments === 1)
      return Promise.reject(new Error("lost acknowledgment"));
  } });
  await f.client.initialize(); f.client.update("original");
  await assert.rejects(f.client.flush(), /lost command/);
  const original = f.client.view().pending.patch_input;
  await assert.rejects(f.client.reconcile("patch_input"), /lost acknowledgment/);
  assert.deepEqual(copy(f.client.view().pending.patch_input), copy(original));
  await f.client.reconcile("patch_input");
  assert.deepEqual(f.calls.slice(2).map(row => row.operation),
    ["receipt", "accept_receipt", "receipt", "accept_receipt"]);
  assert.equal(commands(f).length, 1);
  assert.equal(f.client.view().dirty, false);
});

test("unresolved receipt stays pending and does not acknowledge or resend", async () => {
  const f = fixture({ handle(request) {
    if (request.operation === "patch_input") return Promise.reject(new Error("lost"));
  } });
  await f.client.initialize(); f.client.update("unknown");
  await assert.rejects(f.client.flush());
  await assert.rejects(f.client.reconcile("patch_input"), /request_unknown/);
  assert.equal(f.calls.at(-1).operation, "receipt");
  assert.ok(f.client.view().pending.patch_input);
  assert.equal(commands(f).length, 1);
});

test("a different receipt acknowledgment cannot clear the captured original pending request", async () => {
  const f = fixture({ handle(request, state, receipts) {
    if (request.operation === "patch_input") {
      receipts.set(request.parameters.idempotency_key, patchResult(request));
      return Promise.reject(new Error("lost"));
    }
    if (request.operation === "accept_receipt") {
      const original = { owner: request.owner, operation: request.parameters.operation,
        parameters: request.parameters.original_request.parameters };
      return patchResult(original, "saved", { edit_sequence: 99 });
    }
  } });
  await f.client.initialize(); f.client.update("captured"); await assert.rejects(f.client.flush());
  const original = copy(f.client.view().pending.patch_input);
  await assert.rejects(f.client.reconcile("patch_input"), /result_mismatch/);
  assert.deepEqual(copy(f.client.view().pending.patch_input), original);
  assert.equal(f.client.view().input.edit_sequence, 1);
  assert.equal(commands(f).length, 1);
});

test("definite rejection and conflicts block send without resnapshotting or changing original request", async () => {
  for (const conflict of [false, true]) {
    const f = fixture({ handle(request) {
      if (request.operation === "patch_input") return Promise.reject(Object.assign(new Error("rejected"), {
        definite: true, code: conflict ? "idempotency_conflict" : "stale_edit_sequence",
      }));
    } });
    await f.client.initialize(); f.client.update("rejected");
    await assert.rejects(f.client.flush());
    await assert.rejects(f.client.send());
    await f.time.advance(1000);
    assert.equal(commands(f).length, 1);
    assert.equal(f.calls.filter(row => row.operation === "state").length, 1);
    assert.equal(!!f.client.view().pending.patch_input, conflict);
  }
});

test("confirmation is not a saved value, explicit consent uses original text/basis and a fresh key", async () => {
  const f = fixture({ policy: "fixed", handle(request) {
    if (request.operation === "patch_input" && !request.parameters.confirm_derivation)
      return patchResult(request, "confirmation_required");
    if (request.operation === "patch_input") return patchResult(request, "derived", {
      edit_sequence: 1, definition_revision: 1,
      next_owner: { ...binding, workflow_definition_id: id(9), session_id: null },
    });
  } });
  await f.client.initialize(); f.client.update("fixed change");
  await assert.rejects(f.client.send(), /input_blocked/);
  assert.equal(f.client.view().input.text, "formal");
  const ask = commands(f)[0];
  const result = await f.client.confirm(), confirmation = commands(f)[1];
  assert.equal(confirmation.parameters.confirm_derivation, true);
  assert.equal(confirmation.parameters.text, ask.parameters.text);
  assert.equal(confirmation.parameters.expected_edit_sequence, ask.parameters.expected_edit_sequence);
  assert.notEqual(confirmation.parameters.idempotency_key, ask.parameters.idempotency_key);
  assert.equal(result.result.outcome, "derived");
  assert.equal(f.client.view().next_owner.workflow_definition_id, id(9));
  assert.equal(commands(f).filter(row => row.operation === "start").length, 0);
});

test("declining a fixed question performs no write and an edit invalidates the older question", async () => {
  const f = fixture({ policy: "fixed", handle(request) {
    if (request.operation === "patch_input") return patchResult(request, "confirmation_required");
  } });
  await f.client.initialize(); f.client.update("A"); await f.client.flush().catch(() => {});
  f.client.decline(); assert.equal(commands(f).length, 1);
  await assert.rejects(f.client.send());
  await f.client.refresh(); f.client.update("B"); await f.client.flush().catch(() => {});
  f.client.update("C");
  await assert.rejects(f.client.confirm(), /confirmation_unavailable/);
  assert.equal(commands(f).filter(row => row.parameters.confirm_derivation).length, 0);
});

test("fixed save without a session requires a new exact revision binding, not an old-owner start", async () => {
  const f = fixture({ policy: "fixed", handle(request) {
    if (request.operation === "patch_input") return patchResult(request, "saved", {
      definition_revision: 2, next_owner: { ...binding, definition_revision: 2 },
    });
  } });
  await f.client.initialize(); f.client.update("fixed");
  await assert.rejects(f.client.send(), /input_blocked/);
  assert.equal(f.client.view().next_owner.definition_revision, 2);
  assert.deepEqual(commands(f).map(row => row.operation), ["patch_input"]);
});

test("initial pending input recovers only original evidence and does not block running pause", async () => {
  const owner = { ...binding, session_id: id(3) };
  const original = { owner, operation: "patch_input", parameters: {
    text: "original unknown", expected_edit_sequence: 1, idempotency_key: "old-request",
  } };
  const f = fixture({ owner, pending: [pending(original)] });
  await f.client.initialize();
  assert.equal(f.client.view().text, "original unknown");
  assert.equal(f.client.view().status.patch_input, "unknown");
  await assert.rejects(f.client.send(), /input_blocked/);
  const paused = await f.client.pause();
  assert.equal(paused.result.control_result.status, "running");
  assert.equal(f.client.view().run.status, "running");
  assert.deepEqual(commands(f).map(row => row.operation), ["pause"]);
  assert.deepEqual(copy(f.client.view().pending.patch_input), copy(original));
});

test("actual create success with start failure remains partial, pending, and never creates again", async () => {
  const f = fixture({ handle(request) {
    if (request.operation === "start") return envelopeResult(request, {
      outcome: "failed", create_result: phase(binding), start_result: null, failure_phase: "start",
      error: { reason_code: "start_rejected" }, next_owner: { ...binding, session_id: id(3) },
    });
  } });
  await f.client.initialize();
  const failed = await f.client.send();
  assert.equal(failed.result.create_result.workflow_session_id, id(3));
  assert.equal(f.client.view().status.start, "failed");
  assert.equal(f.client.view().pending.start, null);
  await assert.rejects(f.client.send(), /start_blocked/);
  assert.equal(commands(f).length, 1);
});

test("known existing-session start rejection ends that request but does not retry it", async () => {
  const owner = { ...binding, session_id: id(3) };
  const f = fixture({ owner, handle(request) {
    if (request.operation === "start") return envelopeResult(request, {
      outcome: "failed", create_result: null, start_result: null, failure_phase: "start",
      error: { reason_code: "start_rejected" }, next_owner: null,
    });
  } });
  f.state.run = { revision: 8, status: "idle", can_start: true, can_pause: false,
    can_resume: false, can_close: false, can_fork: true };
  await f.client.initialize();
  const failed = await f.client.send();
  assert.equal(failed.result.outcome, "failed");
  assert.equal(f.client.view().pending.start, null);
  assert.equal(f.client.view().status.start, "failed");
  await f.time.advance(1000); assert.equal(commands(f).length, 1);
  await f.client.send();
  assert.equal(commands(f).length, 2);
  assert.notEqual(commands(f)[0].parameters.idempotency_key, commands(f)[1].parameters.idempotency_key);
});

test("original fixed false confirmation evidence is preserved exactly while reconciling", async () => {
  const original = { owner: binding, operation: "patch_input", parameters: {
    text: "original", expected_edit_sequence: 1, idempotency_key: "false-confirm", confirm_derivation: false,
  } };
  const f = fixture({ policy: "fixed", pending: [pending(original)] });
  await f.client.initialize();
  assert.equal(f.client.view().pending.patch_input.parameters.confirm_derivation, false);
  await assert.rejects(f.client.reconcile("patch_input"), /request_unknown/);
  assert.equal(f.calls.at(-1).parameters.original_request.parameters.confirm_derivation, false);
  assert.equal(commands(f).length, 0);
});

test("snapshot is backend-provided display only and all returned values are independent copies", async () => {
  const f = fixture({ owner: { ...binding, session_id: id(3) } });
  await f.client.initialize();
  const first = f.client.view(); first.snapshot.chat.rounds[0].candidates[0].messages[0].text = "edited";
  first.owner.node_binding_id = id(8);
  assert.equal(f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text, "display-only");
  assert.equal(f.client.view().owner.node_binding_id, id(2));
  f.state.snapshot.chat.rounds[0].candidates[0].messages[0].text = "backend refreshed";
  assert.equal(f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text, "display-only");
  await f.client.refresh();
  assert.equal(f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text, "backend refreshed");
  assert.equal(commands(f).length, 0);
});

test("a dirty refresh cannot rebase the input save but may update run and display", async () => {
  const f = fixture({ owner: { ...binding, session_id: id(3) } });
  await f.client.initialize(); f.client.update("my dirty text");
  f.state.input = { ...f.state.input, text: "another writer", edit_sequence: 7 };
  f.state.run.revision = 20; f.state.snapshot.chat.rounds[0].candidates[0].messages[0].text = "new display";
  await f.client.refresh();
  assert.equal(f.client.view().text, "my dirty text");
  assert.equal(f.client.view().input.edit_sequence, 1);
  assert.equal(f.client.view().run.revision, 20);
  assert.equal(f.client.view().snapshot.chat.rounds[0].candidates[0].messages[0].text, "new display");
  await f.client.flush();
  assert.equal(commands(f)[0].parameters.expected_edit_sequence, 1);
});

test("legacy reference-display snapshots are rejected without upgrading or guessing", async () => {
  const f = fixture({ owner: { ...binding, session_id: id(3) } });
  f.state.snapshot = { schema_version: 1, kind: "workflow.frontend-node-snapshot",
    owner: { workflow_definition_id: binding.workflow_definition_id, definition_revision: binding.definition_revision,
      node_binding_id: binding.node_binding_id, workflow_session_id: id(3), object_key: "chat" },
    basis: { revision: 1, revision_id: id(30) }, entries: [], source: null };
  await assert.rejects(f.client.initialize(), /state_mismatch/);
  assert.equal(f.client.view().initialized, false);
  assert.equal(commands(f).length, 0);
});

test("dispose cancels pending timer without save/control and late results cannot notify another owner", async () => {
  const held = deferred();
  const old = fixture({ handle(request) {
    if (request.operation === "patch_input") return held.promise;
  } });
  const newer = fixture({ owner: { ...binding, node_binding_id: id(8) } });
  await old.client.initialize(); old.client.update("old");
  await old.time.advance(200); old.client.update("not sent");
  const count = old.changes.length;
  old.client.dispose(); await newer.client.initialize();
  held.resolve(patchResult(commands(old)[0])); await settle(); await old.time.advance(1000);
  assert.equal(old.changes.length, count);
  assert.equal(commands(old).length, 1);
  assert.equal(newer.client.view().text, "formal");
  assert.equal(newer.client.view().owner.node_binding_id, id(8));
  assert.equal(commands(newer).length, 0);
});

test("late state read cannot replace a newer read or notify after closing", async () => {
  const held = deferred(); let reads = 0;
  const f = fixture({ handle(request) {
    if (request.operation === "state" && ++reads === 1) return held.promise;
  } });
  const old = f.client.initialize();
  f.state.input.text = "new state"; await f.client.refresh();
  held.resolve({ ...f.state, input: { ...f.state.input, text: "stale state" } }); await old;
  assert.equal(f.client.view().text, "new state");
});

test("a late pre-save state cannot roll back the formally accepted input basis", async () => {
  const held = deferred(); let reads = 0;
  const f = fixture({ handle(request) {
    if (request.operation === "state" && ++reads === 2) return held.promise;
  } });
  await f.client.initialize();
  const before = copy(f.state), refreshing = f.client.refresh();
  f.client.update("saved while reading"); await f.client.flush();
  held.resolve(before); await refreshing;
  assert.equal(f.client.view().input.edit_sequence, 2);
  assert.equal(f.client.view().input.text, "saved while reading");
  assert.equal(f.client.view().text, "saved while reading");
  assert.equal(f.client.view().errors.patch_input, "");
});

test("malformed or other-owner results retain original pending and never start", async () => {
  const mutations = [
    value => { value.owner.node_binding_id = id(8); },
    value => { value.idempotency_key = "other-key"; },
    value => { value.result.text = "other text"; },
    value => { value.result.edit_sequence = 99; },
    value => { value.result.next_owner = { ...binding, node_binding_id: id(8) }; },
  ];
  for (const mutate of mutations) {
    const f = fixture({ handle(request) {
      if (request.operation === "patch_input") {
        const result = patchResult(request); mutate(result); return result;
      }
    } });
    await f.client.initialize(); f.client.update("captured");
    await assert.rejects(f.client.send(), /result_mismatch/);
    assert.equal(f.client.view().input.text, "formal");
    assert.ok(f.client.view().pending.patch_input);
    assert.deepEqual(commands(f).map(row => row.operation), ["patch_input"]);
  }
});

test("wrong-session or malformed native phases cannot rebind a client", async () => {
  const mutations = [
    value => { value.result.create_result = null; },
    value => { value.result.start_result.workflow_session_id = id(90); },
    value => { value.result.create_result.workflow_definition_id = id(90); },
    value => { value.result.start_result.revision = true; },
    value => { value.result.next_owner.node_binding_id = id(90); },
  ];
  for (const mutate of mutations) {
    const f = fixture({ handle(request) {
      if (request.operation === "start") {
        const value = startResult(request); mutate(value); return value;
      }
    } });
    await f.client.initialize();
    await assert.rejects(f.client.send(), /result_mismatch/);
    assert.equal(f.client.view().next_owner, null);
    assert.ok(f.client.view().pending.start);
    assert.equal(commands(f).length, 1);
  }
});

test("state and original evidence must belong to this exact owner", async () => {
  const original = { owner: { ...binding, node_binding_id: id(90) }, operation: "patch_input",
    parameters: { text: "spoof", expected_edit_sequence: 0, idempotency_key: "spoof" } };
  const f = fixture({ pending: [pending(original)] });
  await assert.rejects(f.client.initialize(), /pending_mismatch/);
  assert.equal(f.client.view().initialized, false);
  assert.equal(f.client.view().input, null);
  assert.equal(commands(f).length, 0);
  const other = fixture();
  other.state.owner.node_binding_id = id(90);
  await assert.rejects(other.client.initialize(), /state_mismatch/);
  assert.equal(other.client.view().initialized, false);
});

test("unbound input has no composer saves but ordinary graph start remains possible", async () => {
  const f = fixture({ input: null }); await f.client.initialize();
  assert.throws(() => f.client.update("not bound"), /input_unavailable/);
  await assert.rejects(f.client.flush(), /input_blocked/);
  const result = await f.client.send();
  assert.equal(result.outcome, "succeeded");
  assert.deepEqual(commands(f).map(row => row.operation), ["start"]);
});
