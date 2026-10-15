const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const [origin, encodedOwner, mode] = process.argv.slice(2);
const owner = JSON.parse(encodedOwner);
const scope = { setTimeout, clearTimeout, URL };
for (const name of ["frontend-node-client.js", "frontend-node-host.js"]) {
  vm.runInNewContext(fs.readFileSync(
    path.join(__dirname, "../src/phase1_agent/static", name), "utf8"), scope);
}
const trace = [];
let dropped = false, sequence = 0;
const fetcher = async (url, options) => {
  const packet = options.body ? JSON.parse(options.body) : null;
  if (packet) trace.push(packet);
  const response = await fetch(url, {
    ...options, headers: { ...options.headers, Origin: origin },
  });
  if (mode === "lost" && packet?.operation === "patch_input" && !dropped) {
    dropped = true;
    assert.equal(response.status, 200);
    await response.arrayBuffer();
    throw new TypeError("Simulated lost response after actual native save");
  }
  return response;
};
const streams = [];
const host = new scope.FrontendNodePage.Host({
  origin, fetcher, keyFactory: () => "http-client-" + (++sequence),
  eventSourceFactory(url) {
    assert.equal(new URL(url).origin, origin);
    const stream = { close() { this.closed = true; }, addEventListener() {} };
    streams.push(stream);
    return stream;
  },
});

(async () => {
  await host.bind(owner);
  assert.equal(host.view().client.input.text, "message 0");
  host.update("actual HTTP message C");
  await assert.rejects(host.action("flush"));
  assert.equal(host.view().client.status.patch_input, "unknown");
  const original = host.view().client.pending.patch_input;
  assert.equal(original.parameters.text, "actual HTTP message C");
  assert.equal(trace.filter(packet => packet.operation === "patch_input").length, 1);
  await host.action("reconcile", "patch_input");
  const operations = trace.map(packet => packet.operation);
  assert.deepEqual(operations, ["state", "patch_input", "receipt", "accept_receipt"]);
  for (const packet of trace.slice(2)) {
    assert.equal(packet.parameters.operation, "patch_input");
    assert.deepEqual(packet.parameters.original_request, {
      owner: JSON.parse(JSON.stringify(original.owner)), parameters: JSON.parse(JSON.stringify(original.parameters)),
    });
  }
  assert.equal(host.view().client.pending.patch_input, null);
  assert.equal(host.view().client.input.text, "actual HTTP message C");
  const started = await host.action("send");
  assert.equal(started.outcome, "succeeded");
  const next = host.view().owner;
  assert.equal(next.session_id, started.result.start_result.workflow_session_id);
  assert.equal(next.node_binding_id, owner.node_binding_id);
  for (let index = 0; index < 50 && host.view().client.run.status !== "succeeded"; index++) {
    await new Promise(resolve => setTimeout(resolve, 10));
    await host.refresh();
  }
  const finished = host.view().client;
  assert.equal(finished.run.status, "succeeded");
  assert.equal(finished.snapshot.chat.rounds.length, 1);
  assert.equal(finished.snapshot.chat.rounds[0].submitted_input, "actual HTTP message C");
  assert.equal(trace.filter(packet => packet.operation === "patch_input").length, 1);
  assert.equal(trace.filter(packet => packet.operation === "start").length, 1);
  host.dispose();
  assert.ok(streams.every(stream => stream.closed));
  process.stdout.write(JSON.stringify({ done: true, owner: next,
    trace: trace.map(packet => packet.operation) }) + "\n");
})().catch(failure => {
  host.dispose();
  process.stderr.write(failure.stack + "\n");
  process.exitCode = 1;
});
