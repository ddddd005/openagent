const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const scope = { URL, setTimeout, clearTimeout };
const staticPath = path.join(__dirname, "../src/phase1_agent/static");
for (const name of ["frontend-node-client.js", "frontend-node-host.js"])
  vm.runInNewContext(fs.readFileSync(path.join(staticPath, name), "utf8"), scope);
const { Host, parseOwnerPath, urlsForOwner } = scope.FrontendNodePage;
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const owner = { workflow_definition_id: id(1), definition_revision: 1, node_binding_id: id(2), session_id: null };
const origin = "http://127.0.0.1:5199", copy = value => JSON.parse(JSON.stringify(value));
const tick = async () => { for (let i = 0; i < 18; i++) await Promise.resolve(); };
function held() {
  let resolve;
  const promise = new Promise(ok => { resolve = ok; });
  return { promise, resolve };
}
function snapshot(binding, revision = 1) {
  return { schema_version: 1, kind: "workflow.frontend-node-snapshot", owner: {
    workflow_definition_id: binding.workflow_definition_id, definition_revision: binding.definition_revision,
    node_binding_id: binding.node_binding_id, workflow_session_id: binding.session_id, object_key: "chat",
  }, basis: { revision, revision_id: id(30 + revision) }, entries: [], source: null };
}
function result(envelope, body) {
  return { schema_version: 1, kind: "workflow.frontend-node-result", owner: copy(envelope.owner),
    operation: envelope.operation, idempotency_key: envelope.parameters.idempotency_key, outcome: body.outcome, result: body };
}
function patch(envelope, overrides = {}) {
  return result(envelope, { outcome: "saved", edit_sequence: envelope.parameters.expected_edit_sequence + 1,
    definition_revision: envelope.owner.definition_revision, text: envelope.parameters.text, next_owner: null, ...overrides });
}
function phase(binding, revision, status) {
  return { workflow_definition_id: binding.workflow_definition_id, definition_revision: binding.definition_revision,
    workflow_session_id: binding.session_id ?? id(3), revision, status, active_chain_run_id: status === "idle" ? null : id(40) };
}
function fixture(options = {}) {
  const calls = [], streams = [], changes = [], bindings = [], receipts = new Map();
  const state = new Map();
  const packet = binding => {
    const key = JSON.stringify(binding);
    if (!state.has(key)) state.set(key, { schema_version: 1, kind: "workflow.frontend-node-state", owner: copy(binding),
      input: { text: "formal", edit_sequence: 1, policy: "adjustable" },
      run: binding.session_id === null ? null : { revision: 4, status: "running", can_start: false, can_pause: true,
        can_resume: false, can_close: false, can_fork: false },
      snapshot: binding.session_id === null ? null : snapshot(binding), pending: [] });
    return state.get(key);
  };
  let sequence = 0;
  const fetcher = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url, method: init.method ?? "GET", body });
    if (options.fetch) {
      const answer = options.fetch(url, init, body, packet, receipts);
      if (answer !== undefined) return await answer;
    }
    const binding = body?.owner ?? parseOwnerPath(new URL(url).pathname
      .replace("/api/graph/frontend-nodes/", "/frontend-node/").replace(/\/entry$/, "/"));
    let value;
    if (url.endsWith("/entry")) value = { schema_version: 1, kind: "workflow.frontend-node-entry",
      owner: binding, ...copy(urlsForOwner(origin, binding)) };
    if (value) delete value.entry_url;
    else if (body.operation === "state") value = packet(binding);
    else if (["receipt", "accept_receipt"].includes(body.operation))
      value = receipts.get(body.parameters.original_request.parameters.idempotency_key);
    else {
      if (body.operation === "patch_input") {
        value = patch(body); const p = packet(binding);
        p.input.text = body.parameters.text; p.input.edit_sequence = value.result.edit_sequence;
      } else if (body.operation === "start") value = result(body, { outcome: "succeeded",
        create_result: binding.session_id === null ? phase(binding, 1, "idle") : null,
        start_result: phase(binding, 2, "prepared"), failure_phase: null, error: null,
        next_owner: binding.session_id === null ? { ...binding, session_id: id(3) } : null });
      else value = result(body, { outcome: "succeeded", control_result: phase(binding, 5, "running"),
        failure_phase: null, error: null, next_owner: null });
      receipts.set(body.parameters.idempotency_key, value);
    }
    return { ok: true, status: 200, json: async () => copy(value) };
  };
  const eventSourceFactory = url => {
    const events = {};
    const stream = { url, closed: false, close() { this.closed = true; },
      addEventListener(name, handler) { events[name] = handler; },
      emit(name, value) { events[name]?.({ data: JSON.stringify(value) }); },
      open() { this.onopen?.(); }, error() { this.onerror?.(); },
    };
    streams.push(stream); return stream;
  };
  const host = new Host({ origin, fetcher, eventSourceFactory, keyFactory: () => "key-" + ++sequence,
    onChange: value => changes.push(copy(value)), onBinding: (binding, url) => bindings.push({ owner: copy(binding), url }) });
  return { host, calls, streams, changes, bindings, packet, receipts };
}
const operations = f => f.calls.map(row => row.body?.operation).filter(Boolean);

test("page route accepts only exact canonical W/R/N/S identities", () => {
  const address = urlsForOwner(origin, owner).page_url;
  assert.deepEqual(copy(parseOwnerPath(new URL(address).pathname)), owner);
  for (const path of ["/", "/frontend-node/", new URL(address).pathname.replace("/1/", "/01/"),
    new URL(address).pathname.replace("/none/", "/invalid/"), new URL(address).pathname + "?other=1",
    new URL(address).pathname.replace(id(2), id(2).toUpperCase().replace("0000", "FFFF"))])
    assert.throws(() => parseOwnerPath(path), /invalid_binding/);
});

test("opening fetches only node entry and state; it does not create or start", async () => {
  const f = fixture(); await f.host.bind(owner);
  assert.deepEqual(operations(f), ["state"]);
  assert.ok(f.calls.every(call => call.url.startsWith(origin + "/api/graph/frontend-nodes/")));
  assert.equal(f.streams.length, 1); assert.equal(f.host.view().client.owner.session_id, null);
  f.streams[0].open(); assert.equal(f.host.view().connection, "connected");
  f.host.dispose(); assert.equal(f.streams[0].closed, true);
  assert.deepEqual(operations(f), ["state"]);
});

test("entry URL mismatch or another origin is refused before any client command", async () => {
  for (const field of ["owner", "page_url", "channel_url", "events_url"]) {
    const f = fixture({ fetch(url) {
      const value = { schema_version: 1, kind: "workflow.frontend-node-entry", owner: copy(owner),
        ...copy(urlsForOwner(origin, owner)) };
      delete value.entry_url;
      if (field === "owner") value.owner.node_binding_id = id(9);
      else value[field] = "https://other.example" + new URL(value[field]).pathname;
      return { ok: true, status: 200, json: async () => value };
    } });
    await assert.rejects(f.host.bind(owner), /entry_mismatch/);
    assert.deepEqual(operations(f), []); assert.equal(f.streams.length, 0);
  }
});

test("first successful start follows actual new session with a new read-only binding, once", async () => {
  const f = fixture(); await f.host.bind(owner);
  f.host.update("saved before start"); const started = await f.host.action("send");
  assert.equal(started.outcome, "succeeded");
  assert.deepEqual(operations(f), ["state", "patch_input", "start", "state"]);
  assert.equal(f.host.view().owner.session_id, id(3));
  assert.equal(f.streams[0].closed, true); assert.equal(f.streams.length, 2);
  assert.equal(f.bindings.at(-1).owner.session_id, id(3));
  f.host.dispose();
});

test("derived input waits for explicit binding continuation and never starts", async () => {
  const next = { ...owner, workflow_definition_id: id(9) };
  const f = fixture({ fetch(url, init, body) {
    if (body?.operation === "patch_input") return { ok: true, status: 200,
      json: async () => patch(body, { outcome: "derived", edit_sequence: 1, next_owner: next }) };
  } });
  await f.host.bind(owner); f.host.update("derive");
  await assert.rejects(f.host.action("send"), /input_blocked/);
  assert.equal(f.host.view().owner.workflow_definition_id, owner.workflow_definition_id);
  assert.equal(f.host.view().client.next_owner.workflow_definition_id, id(9));
  await f.host.continueBinding();
  assert.equal(f.host.view().owner.workflow_definition_id, id(9));
  assert.equal(operations(f).includes("start"), false); f.host.dispose();
});

test("fixed no-session save advancing revision reports saved-and-rebind for both flush and send", async () => {
  for (const action of ["flush", "send"]) {
    const next = { ...owner, definition_revision: 2 };
    const f = fixture({ fetch(url, init, body, packet) {
      if (body?.operation === "patch_input") {
        packet(body.owner).input = { text: body.parameters.text, edit_sequence: 2, policy: "fixed" };
        return { ok: true, status: 200, json: async () => patch(body, {
          definition_revision: 2, next_owner: next,
        }) };
      }
    } });
    f.packet(owner).input.policy = "fixed";
    await f.host.bind(owner); f.host.update("fixed saved at revision 2");
    await assert.rejects(f.host.action(action), /input_blocked/);
    assert.equal(f.host.view().client.status.patch_input, "saved");
    assert.equal(f.host.view().client.last_result.result.definition_revision, 2);
    assert.equal(f.host.view().error, "");
    assert.equal(f.host.view().notice, "输入已保存，请接续新绑定；本轮尚未启动。");
    assert.equal(f.host.view().owner.definition_revision, 1);
    assert.equal(f.host.view().client.next_owner.definition_revision, 2);
    assert.deepEqual(operations(f), ["state", "patch_input"]);
    f.host.dispose();
  }
});

test("snapshot push refreshes only actual state and deduplicates its exact basis", async () => {
  const binding = { ...owner, session_id: id(3) }, f = fixture();
  await f.host.bind(binding);
  const event = snapshot(binding, 2);
  f.packet(binding).snapshot = snapshot(binding, 2);
  f.streams[0].emit("snapshot", event); await tick();
  assert.equal(f.host.view().client.snapshot.basis.revision, 2);
  f.streams[0].emit("snapshot", event); await tick();
  assert.deepEqual(operations(f), ["state", "state"]);
  assert.ok(operations(f).every(name => name === "state"));
  f.host.dispose();
});

test("wrong snapshot/state event owner closes the connection without fetching or changing data", async () => {
  for (const name of ["snapshot", "state_changed"]) {
    const binding = { ...owner, session_id: id(3) }, f = fixture();
    await f.host.bind(binding);
    const event = name === "snapshot" ? snapshot(binding) : {
      schema_version: 1, kind: "workflow.frontend-node-state-changed", owner: copy(binding),
    };
    event.owner.node_binding_id = id(9);
    f.streams[0].emit(name, event); await tick();
    assert.equal(f.streams[0].closed, true); assert.equal(f.host.view().connection, "disconnected");
    assert.deepEqual(operations(f), ["state"]); f.host.dispose();
  }
});

test("state_changed refresh coalesces an in-flight read without performing a business command", async () => {
  const binding = { ...owner, session_id: id(3) }, wait = held(); let reads = 0;
  const f = fixture({ fetch(url, init, body, packet) {
    if (body?.operation === "state" && ++reads === 2)
      return wait.promise.then(() => ({ ok: true, status: 200, json: async () => copy(packet(binding)) }));
  } });
  await f.host.bind(binding);
  const event = { schema_version: 1, kind: "workflow.frontend-node-state-changed", owner: binding };
  for (let i = 0; i < 8; i++) f.streams[0].emit("state_changed", event);
  await tick(); assert.equal(reads, 2);
  wait.resolve(); await tick(); assert.equal(reads, 3);
  assert.ok(operations(f).every(operation => operation === "state")); f.host.dispose();
});

test("SSE disconnect closes automatic reconnection; manual refresh explicitly reconnects", async () => {
  const f = fixture(); await f.host.bind(owner); f.streams[0].open(); f.streams[0].error();
  await tick(); assert.equal(f.streams[0].closed, true); assert.equal(f.streams.length, 1);
  assert.equal(f.host.view().connection, "disconnected");
  await f.host.refresh(); assert.equal(f.streams.length, 2);
  assert.deepEqual(operations(f), ["state", "state"]); f.host.dispose();
});

test("saved then 409 projection failure retains unknown original and only receipt/accept follows", async () => {
  const f = fixture({ fetch(url, init, body, packet, receipts) {
    if (body?.operation === "patch_input") {
      const saved = patch(body); receipts.set(body.parameters.idempotency_key, saved);
      packet(body.owner).input = { text: body.parameters.text, edit_sequence: 2, policy: "adjustable" };
      return { ok: false, status: 409, json: async () => ({ error: {
        code: "conflict", reason_code: "frontend_node_result_mismatch", message: "Projection failed",
        diagnostics: [{ code: "frontend_node_result_mismatch" }],
      } }) };
    }
  } });
  await f.host.bind(owner); f.host.update("actually saved");
  await assert.rejects(f.host.action("flush"), /Projection failed/);
  assert.equal(f.host.view().client.status.patch_input, "unknown");
  await f.host.action("reconcile", "patch_input");
  assert.deepEqual(operations(f), ["state", "patch_input", "receipt", "accept_receipt"]);
  assert.equal(f.host.view().client.pending.patch_input, null);
  f.host.dispose();
});

test("safe diagnosed stale rejection is definite, while conflict, 5xx and malformed errors remain unknown", async () => {
  for (const [status, code, diagnosed, expected] of [
    [409, "stale_edit_sequence", true, "rejected"],
    [409, "request_evidence_pending", true, "unknown"],
    [409, "request_evidence_conflict", true, "unknown"],
    [409, "idempotency_conflict", true, "unknown"],
    [400, "invalid_request", false, "unknown"],
    [500, "internal_error", true, "unknown"],
  ]) {
    const f = fixture({ fetch(url, init, body) {
      if (body?.operation === "patch_input") return { ok: false, status, json: async () => ({ error: {
        code, message: "Denied", ...(diagnosed ? { diagnostics: [{ code }] } : {}),
      } }) };
    } });
    await f.host.bind(owner); f.host.update("not saved");
    await assert.rejects(f.host.action("flush"));
    assert.equal(f.host.view().client.status.patch_input, expected);
    assert.equal(operations(f).filter(name => name === "patch_input").length, 1); f.host.dispose();
  }
});

test("closing and changing exact bindings prevent late requests/events from updating the new view", async () => {
  const first = held(), f = fixture({ fetch(url) {
    if (url === urlsForOwner(origin, owner).entry_url) return first.promise;
  } });
  const waiting = f.host.bind(owner), other = { ...owner, node_binding_id: id(9) };
  await f.host.bind(other);
  first.resolve({ ok: true, status: 200, json: async () => ({}) }); await waiting;
  assert.equal(f.host.view().owner.node_binding_id, id(9));
  const count = f.changes.length; f.host.dispose();
  f.streams[0].emit("state_changed", { schema_version: 1,
    kind: "workflow.frontend-node-state-changed", owner: other });
  await tick(); assert.equal(f.changes.length, count);
  assert.deepEqual(operations(f), ["state"]);
});
