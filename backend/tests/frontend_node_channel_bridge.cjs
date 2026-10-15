const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const vm = require("node:vm");

const scope = { setTimeout, clearTimeout };
vm.runInNewContext(fs.readFileSync(
  path.join(__dirname, "../src/phase1_agent/static/frontend-node-client.js"), "utf8"), scope);
const lines = readline.createInterface({ input: process.stdin });
const replies = new Map();
let sequence = 0, keys = 0;
lines.on("line", line => {
  const reply = JSON.parse(line);
  const pending = replies.get(reply.id);
  assert.ok(pending, "reply has an outstanding exact request");
  replies.delete(reply.id);
  if (reply.error) pending.reject(Object.assign(new Error(reply.error.message), reply.error));
  else pending.resolve(reply.value);
});

const port = {
  request(request) {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      replies.set(id, { resolve, reject });
      process.stdout.write(JSON.stringify({ id, request }) + "\n");
    });
  },
};
const create = owner => new scope.FrontendNodeClient({
  owner, port, keyFactory: () => "bridge-command-" + (++keys),
});

(async () => {
  const owner = JSON.parse(process.argv[2]);
  let client = create(owner);
  const initial = await client.initialize();
  assert.equal(initial.input.text, "current B");
  assert.equal(initial.input.edit_sequence, 2);
  assert.equal(initial.run, null);
  assert.equal(initial.snapshot, null);
  client.update("new message C");
  if (process.argv[3] === "unknown") {
    await assert.rejects(client.flush());
    const original = client.view().pending.patch_input;
    assert.equal(client.view().status.patch_input, "unknown");
    client.dispose();
    client = create(owner);
    const reopened = await client.initialize();
    assert.equal(reopened.input.text, "new message C");
    assert.equal(reopened.pending.patch_input.parameters.idempotency_key,
      original.parameters.idempotency_key);
    await assert.rejects(client.flush());
    await client.reconcile("patch_input");
  } else {
    await client.flush();
  }
  assert.equal(client.view().input.text, "new message C");
  assert.equal(client.view().input.edit_sequence, 3);
  const started = await client.send();
  assert.equal(started.outcome, "succeeded");
  assert.equal(started.result.create_result.workflow_session_id,
    started.result.start_result.workflow_session_id);
  const next = started.result.next_owner;
  assert.equal(next.node_binding_id, owner.node_binding_id);
  client.dispose();

  const bound = create(next);
  const finished = await bound.initialize();
  assert.equal(finished.run.status, "succeeded");
  assert.equal(finished.snapshot.owner.workflow_session_id, next.session_id);
  assert.equal(finished.snapshot.entries.length, 1);
  assert.equal(finished.snapshot.entries[0].content.text, "new message C");
  finished.snapshot.entries[0].content.text = "local display edit";
  assert.equal(bound.view().snapshot.entries[0].content.text, "new message C");
  bound.dispose();
  process.stdout.write(JSON.stringify({ done: true, session_id: next.session_id }) + "\n");
  lines.close();
})().catch(failure => {
  process.stderr.write(failure.stack + "\n");
  process.exitCode = 1;
  lines.close();
});
