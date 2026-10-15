const assert = require("node:assert/strict");
const vm = require("node:vm");
const [origin, encodedOwner] = process.argv.slice(2);
const owner = JSON.parse(encodedOwner);
const copy = value => JSON.parse(JSON.stringify(value));
const trace = [], streams = [];
let sequence = 0, loseReroll = true, host;
const coordinates = value => [value.workflow_definition_id, value.definition_revision,
  value.node_binding_id, value.session_id ?? "none"].join("/");
function channel(binding) { return `${origin}/api/graph/frontend-nodes/${coordinates(binding)}/channel`; }
async function fetcher(url, options) {
  const packet = options.body ? JSON.parse(options.body) : null;
  if (packet) trace.push(copy(packet));
  const response = await fetch(url, { ...options, headers: {
    ...(packet ? { "Content-Type": "application/json" } : {}), ...options.headers, Origin: origin,
  } });
  if (packet?.operation === "reroll" && loseReroll) {
    loseReroll = false;
    assert.equal(response.status, 200);
    await response.arrayBuffer();
    throw new TypeError("Offline test lost the response after real dispatch");
  }
  return response;
}
async function settled() {
  for (let index = 0; index < 100; index++) {
    await host.refresh();
    const value = host.view().client;
    if (value.run.status === "succeeded" && value.snapshot?.chat.rounds.at(-1)?.candidates.at(-1)?.result_checkpoint_id)
      return value;
    if (["failed", "archive_failed", "closed"].includes(value.run.status))
      throw new Error("Unexpected run status " + value.run.status);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error("Offline HTTP workflow did not settle");
}
async function receipt(original, expected) {
  const response = await fetcher(channel(original.owner), { method: "POST", body: JSON.stringify({
    owner: original.owner, operation: "receipt", parameters: { operation: original.operation,
      original_request: { owner: original.owner, parameters: original.parameters } },
  }) });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), copy(expected));
}
(async () => {
  const scope = { setTimeout, clearTimeout, URL };
  for (const name of ["frontend-node-client.js", "frontend-node-host.js"]) {
    const response = await fetch(`${origin}/static/${name}`);
    assert.equal(response.status, 200);
    vm.runInNewContext(await response.text(), scope);
  }
  const page = await fetch(`${origin}/frontend-node/${coordinates(owner)}/`);
  assert.equal(page.status, 200); assert.match(await page.text(), /frontend-node-entry\.js/);
  for (const name of ["frontend-node-entry.js", "frontend-node.css"]) {
    const response = await fetch(`${origin}/static/${name}`);
    assert.equal(response.status, 200); assert.ok((await response.text()).length > 100);
  }
  host = new scope.FrontendNodePage.Host({
    origin, fetcher, keyFactory: () => "http-chat-" + (++sequence),
    eventSourceFactory(url) {
      assert.equal(new URL(url).origin, origin);
      const stream = { addEventListener() {}, close() { this.closed = true; } };
      streams.push(stream); return stream;
    },
  });
  await host.bind(owner);
  assert.equal(host.view().client.snapshot.schema_version, 2);
  assert.equal(host.view().client.snapshot.chat.rounds.length, 0);
  host.update("HTTP original");
  await host.action("flush");
  await host.action("send");
  let value = await settled();
  let floor = value.snapshot.chat.rounds[0];
  assert.equal(floor.submitted_input, "HTTP original");
  assert.equal(floor.candidates[0].messages[0].text, "HTTP original:1");
  const roundId = floor.round_id;
  await assert.rejects(host.action("reroll", roundId));
  const unknown = copy(host.view().client.pending.reroll);
  assert.equal(unknown.parameters.round_id, roundId);
  await settled();
  await host.action("reconcile", "reroll");
  assert.equal(host.view().client.pending.reroll, null);
  await host.action("reroll", roundId);
  value = await settled(); floor = value.snapshot.chat.rounds[0];
  assert.equal(value.snapshot.chat.rounds.length, 1);
  assert.equal(floor.candidates.length, 3);
  assert.deepEqual(copy(floor.candidates.map(candidate => candidate.messages[0].text)),
    ["HTTP original:1", "HTTP original:2", "HTTP original:3"]);
  assert.equal(new Set(floor.candidates.map(candidate => candidate.result_checkpoint_id)).size, 3);
  assert.ok(floor.candidates.every(candidate => candidate.result_checkpoint_id));
  const first = floor.candidates[0].candidate_id, second = floor.candidates[1].candidate_id;
  const selected = await host.action("selectCandidate", { round_id: roundId, candidate_id: first });
  const selectRequest = trace.filter(packet => packet.operation === "select_candidate").at(-1);
  await receipt(selectRequest, selected);
  assert.equal(host.view().client.snapshot.chat.rounds[0].selected_candidate_id, first);
  host.update("HTTP next");
  await host.action("flush");
  await host.action("send");
  value = await settled();
  assert.equal(value.snapshot.chat.rounds.length, 2);
  assert.equal(value.snapshot.chat.rounds[1].candidates[0].messages[0].text, "HTTP next:5");
  assert.equal(value.snapshot.chat.rounds[0].candidates.length, 3);
  await assert.rejects(host.action("selectCandidate", { round_id: roundId, candidate_id: second }), /unavailable/);
  const forked = await host.action("forkFloor", roundId);
  assert.equal(forked.outcome, "succeeded");
  const forkRequest = trace.filter(packet => packet.operation === "fork_floor").at(-1);
  await receipt(forkRequest, forked);
  const next = copy(host.view().client.next_owner);
  assert.notEqual(next.session_id, owner.session_id);
  assert.equal(host.view().owner.session_id, owner.session_id);
  await host.continueBinding();
  assert.equal(host.view().owner.session_id, next.session_id);
  const target = host.view().client.snapshot.chat.rounds;
  assert.equal(target.length, 1); assert.equal(target[0].candidates.length, 3);
  assert.deepEqual(copy(target[0].candidates.map(candidate => candidate.candidate_id)),
    copy(floor.candidates.map(candidate => candidate.candidate_id)));
  assert.ok(target[0].candidates.every(candidate => candidate.result_checkpoint_id
    && !floor.candidates.some(source => source.result_checkpoint_id === candidate.result_checkpoint_id)));
  await host.action("selectCandidate", { round_id: roundId, candidate_id: second });
  assert.equal(host.view().client.snapshot.chat.rounds[0].selected_candidate_id, second);
  assert.equal(trace.filter(packet => packet.operation === "reroll").length, 2);
  const reconciled = trace.filter(packet => ["receipt", "accept_receipt"].includes(packet.operation)
    && packet.parameters.operation === "reroll");
  assert.deepEqual(reconciled.map(packet => packet.operation), ["receipt", "accept_receipt"]);
  assert.ok(reconciled.every(packet => JSON.stringify(packet.parameters.original_request)
    === JSON.stringify({ owner: unknown.owner, parameters: unknown.parameters })));
  assert.ok(trace.every(packet => packet.owner.node_binding_id === owner.node_binding_id));
  host.dispose(); assert.ok(streams.every(stream => stream.closed));
  process.stdout.write(JSON.stringify({ done: true, source_session_id: owner.session_id,
    target_session_id: next.session_id, round_id: roundId, second_candidate_id: second,
    trace: trace.map(packet => packet.operation) }) + "\n");
})().catch(failure => {
  host?.dispose(); process.stderr.write(failure.stack + "\n"); process.exitCode = 1;
});
