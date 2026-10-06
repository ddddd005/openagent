import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const scope: Record<string, any> = {};
runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
const { GraphChatClient, validateContent, displayText, displayEntryText, httpFailure, parseExternalInput, validateConsumer,
  validateHistory, validateRegistrations, commandEnvelope, validateApplication, supportedContent } = scope.GraphChat;
const workflow = "00000000-0000-4000-8000-000000000011";
const session = "00000000-0000-4000-8000-000000000012";
const node = "00000000-0000-4000-8000-000000000013";
const run = "00000000-0000-4000-8000-000000000014";
const chain = "00000000-0000-4000-8000-000000000015";
const key = "00000000-0000-4000-8000-000000000016";
function consumer(revision = 1) {
  return { schema_version: 1, kind: "workflow.consumer", workflow_definition_id: workflow,
    definition_revision: 1, workflow_session_id: session, session_revision: revision,
    status: "idle", can_submit: true, available_actions: [], inputs: [],
    nodes: [{ node_binding_id: node, label: "Text output", status: "idle", run_id: null, revision: null, diagnostic: null, budget: null }],
    outputs: [{ node_binding_id: node, port_id: "output", data_type: "TEXT", label: "Text output", is_output: true,
      status: "idle", availability: "unproduced", reason_code: null, run_id: null, chain_run_id: null, payload: null, source: null }],
    history: [], diagnostics: [] };
}
function receipt(command: any, revision = 2) {
  const parameters = command.parameters;
  const result = { schema_version: 1, kind: "workflow.consumer.receipt",
    receipt: { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
      session_revision: revision, chain_run_id: chain, status: "prepared", idempotency_key: parameters.idempotency_key,
      operation: command.operation === "consumer.session.create" ? "create" : command.operation === "consumer.run.start" ? "start" : "control" },
    consumer: { ...consumer(revision), status: "prepared", can_submit: false, available_actions: ["pause"] } };
  return { schema_version: 1, kind: "workflow.application-command", receipt: {
    schema_version: 1, kind: "workflow.application-receipt", operation: command.operation,
    operation_scope: "consumer", idempotency_key: parameters.idempotency_key, request_sha256: "1".repeat(64),
    authority: "service_receipt", target: parameters.session_id ? { session_id: parameters.session_id } : {},
    accepted: { ...result.receipt },
  }, result };
}
function receiptRead(command: any, revision = 2) {
  const value = receipt(command, revision);
  return { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
    reason_code: "receipt_matched", receipt: value.receipt, result: { receipt: value.result.receipt } };
}
function storage() {
  const values = new Map<string, string>();
  return { getItem: (name: string) => values.get(name) ?? null, setItem: vi.fn((name: string, value: string) => { values.set(name, value); }) };
}
function client(request: any, saved = storage()) {
  const value = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request, keyFactory: () => key });
  value.accept(consumer()); return { value, saved };
}
function application() {
  const descriptor = (name: string, kind: string) => ({ name, kind, scope: "consumer", required_fields: [],
    optional_fields: [], idempotency: kind === "command" ? "service_receipt" : "none",
    idempotency_key_format: kind === "command" ? "opaque" : null, http_status: 200 });
  return { schema_version: 1, kind: "workflow.application", scope: "consumer",
    boundary: { caller: "trusted_local", remote_plugin_authentication: false },
    commands: ["consumer.session.create", "consumer.run.start", "consumer.run.control"].map(name => descriptor(name, "command")),
    queries: ["consumer.definition", "consumer.sessions", "consumer.read", "consumer.actions", "output.history",
      "registration.list", "information.read", "output.artifact.read", "frontend.extensions.read"].map(name => descriptor(name, "query")) };
}
function publicBinding(ownerSession = session) {
  const reference = { source_id: "test.status", exact_version: "1.0.0" };
  return { kind: "information_binding", registration_ref: reference,
    package_id: "test", package_version: "1.0.0", discover_public: true, read_public: true, availability: "live",
    owner: { workflow_session_id: ownerSession, chain_run_id: chain, node_binding_id: node, node_run_id: run }, generation: 2,
    declaration: { source_ref: reference, component_id: "test.long", component_version: "1",
      channel_id: "status", format_id: "test.unfamiliar-status", format_version: 3, item_schema: {},
      discover_public: true, read_public: true, source_scope: "live_and_history", max_page_size: 2, max_page_bytes: 1000 } };
}
function informationPage(binding = publicBinding(), sourceScope = "live") {
  return { schema_version: 1, source_ref: binding.registration_ref, owner: binding.owner, generation: binding.generation,
    source_scope: sourceScope, format_id: binding.declaration.format_id, format_version: binding.declaration.format_version,
    items: [{ arbitrary: { progress: "provider-owned" } }], next_cursor: "provider-opaque", status: "ok" };
}
function acceptInformationHistory(value: any, ownerSession = session) {
  value.accept({ ...consumer(), history: [{ workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: ownerSession, chain_run_id: chain, status: "running", revision: 1, inherited: ownerSession !== session }] });
}
function displayConsumer() {
  const value: any = consumer();
  value.history = [{ workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
    chain_run_id: chain, status: "succeeded", revision: 1, inherited: false }];
  value.outputs[0] = { ...value.outputs[0], data_type: "FRONTEND_DISPLAY", data_schema_version: 1,
    availability: "produced", status: "succeeded", run_id: run, chain_run_id: chain,
    payload: { schema_version: 1, kind: "workflow.frontend-display", entries: [
      { entry_id: key, role: "assistant", source_ref: { scope: "artifact", output_id: key } }] },
    source: { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
      node_binding_id: node, port_id: "output", run_id: run, chain_run_id: chain } };
  return value;
}
function publicArtifact() {
  return { schema_version: 1, kind: "workflow.public-artifact", workflow_definition_id: workflow,
    definition_revision: 1, workflow_session_id: session, session_revision: 1, node_id: node, port_id: "output",
    run_id: run, reference: { scope: "artifact", output_id: key }, data_type: "TEXT", data_schema_version: 2,
    value: { schema_version: 2, kind: "workflow.text", text: "accepted postprocessed text" },
    producer: { workflow_session_id: session, chain_run_id: chain, node_binding_id: node, node_run_id: run } };
}
function frontendSelection() {
  return { schema_version: 1, kind: "workflow.consumer-frontend-extensions",
    workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session, session_revision: 1,
    node_id: node, port_id: "output", package_lock: [{ package_id: "workflow.frontend", version: "1.0.0" }],
    frontend_extensions: [] };
}

describe("optional graph chat consumer", () => {
  it("reads frozen UI declarations for the exact public root through the consumer scope", async () => {
    const selection = frontendSelection(), request = vi.fn().mockResolvedValue(selection), { value, saved } = client(request);
    value.application = application(); value.accept(displayConsumer());
    const output = value.consumer.outputs[0], before = JSON.stringify(value.consumer);
    const result = await value.readFrontendExtensions(output);
    expect(result).toEqual(selection); expect(Object.isFrozen(result)).toBe(true);
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ operation: "frontend.extensions.read", parameters: {
      session_id: session, workflow_definition_id: workflow, definition_revision: 1, node_id: node, port_id: "output" } });
    expect(request.mock.calls[0][0]).toBe("/api/graph/consumer/queries");
    expect(JSON.stringify(value.consumer)).toBe(before); expect(saved.setItem).not.toHaveBeenCalled();
    await expect(value.readFrontendExtensions({ ...output, port_id: "private" })).rejects.toThrow("来源");
    expect(request).toHaveBeenCalledTimes(1);
  });
  it.each(["session", "definition", "revision", "node", "port", "extra", "old-revision"])(
    "rejects a mismatched UI selection %s", async field => {
      const selection: any = frontendSelection();
      if (field === "session") selection.workflow_session_id = node;
      if (field === "definition") selection.workflow_definition_id = node;
      if (field === "revision") selection.definition_revision = 2;
      if (field === "node") selection.node_id = run;
      if (field === "port") selection.port_id = "private";
      if (field === "extra") selection.arbitrary_code = "exec";
      if (field === "old-revision") selection.session_revision = 0;
      const { value } = client(vi.fn().mockResolvedValue(selection)); value.accept(displayConsumer());
      await expect(value.readFrontendExtensions(value.consumer.outputs[0])).rejects.toThrow("归属");
    });
  it.each(["success", "failure"])("drops a late UI declaration %s after replacing the output", async mode => {
    let resolve: (value: any) => void = () => {}, reject: (value: any) => void = () => {};
    const request = vi.fn(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
    const { value } = client(request); value.accept(displayConsumer());
    const reading = value.readFrontendExtensions(value.consumer.outputs[0]), replacement = displayConsumer();
    replacement.outputs[0].run_id = node; replacement.outputs[0].source.run_id = node; value.accept(replacement);
    if (mode === "success") resolve(frontendSelection()); else reject(new Error("old selection"));
    expect(await reading).toBeNull(); expect(value.pending).toBeNull();
  });
  it("reads exactly one declared display reference through the consumer query without storing messages or changing execution", async () => {
    const request = vi.fn().mockResolvedValue(publicArtifact()), { value, saved } = client(request);
    value.application = application(); value.accept(displayConsumer());
    const before = JSON.stringify(value.consumer), output = value.consumer.outputs[0];
    expect(supportedContent("FRONTEND_DISPLAY", 1)).toBe(true);
    const result = await value.readDisplayEntry(output, output.payload.entries[0]);
    expect(displayText(result.value)).toBe("accepted postprocessed text");
    expect(request.mock.calls[0][0]).toBe("/api/graph/consumer/queries");
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ operation: "output.artifact.read", parameters: {
      session_id: session, workflow_definition_id: workflow, definition_revision: 1,
      node_id: node, port_id: "output", run_id: run, reference: { scope: "artifact", output_id: key } } });
    expect(JSON.stringify(value.consumer)).toBe(before); expect(value.pending).toBeNull();
    expect(saved.setItem).not.toHaveBeenCalled();
  });

  it.each(["duplicate", "role", "reference", "extra", "capacity"])("rejects an invalid display %s", field => {
    const view = displayConsumer(), payload = view.outputs[0].payload;
    if (field === "duplicate") payload.entries.push(payload.entries[0]);
    if (field === "role") payload.entries[0].role = "tool";
    if (field === "reference") payload.entries[0].source_ref = { scope: "object", object_key: "private" };
    if (field === "extra") payload.derivation = { view_ref: { scope: "artifact", output_id: node } };
    if (field === "capacity") payload.entries = Array(4096).fill(payload.entries[0]);
    expect(() => validateConsumer(view, workflow, session)).toThrow("展示");
  });

  it("refuses fabricated entries, roots, private views and unregistered queries before sending", async () => {
    const request = vi.fn(), { value } = client(request); value.accept(displayConsumer());
    const output = value.consumer.outputs[0], entry = output.payload.entries[0];
    await expect(value.readDisplayEntry(output, { ...entry, source_ref: { scope: "artifact", output_id: node } })).rejects.toThrow("声明");
    await expect(value.readDisplayEntry({ ...output, run_id: node }, entry)).rejects.toThrow("来源");
    const privateView = { ...output, data_type: "FRONTEND_VIEW", payload: { derivation: { view_ref: entry.source_ref } } };
    value.accept({ ...displayConsumer(), outputs: [privateView] });
    await expect(value.readDisplayEntry(privateView, entry)).rejects.toThrow("类型");
    value.accept(displayConsumer()); value.application = { ...application(), queries: [] };
    await expect(value.readDisplayEntry(output, entry)).rejects.toThrow("未登记");
    await expect(value.query("object.artifact.read", {})).rejects.toThrow("不受支持");
    expect(request).not.toHaveBeenCalled();
  });

  it.each(["workflow", "session", "revision", "node", "port", "run", "reference", "producer", "version"])(
    "rejects a mismatched public artifact %s", async field => {
      const result: any = publicArtifact();
      if (field === "workflow") result.workflow_definition_id = node;
      if (field === "session") result.workflow_session_id = node;
      if (field === "revision") result.definition_revision = 2;
      if (field === "node") result.node_id = run;
      if (field === "port") result.port_id = "private";
      if (field === "run") result.run_id = node;
      if (field === "reference") result.reference.output_id = node;
      if (field === "producer") result.producer.chain_run_id = node;
      if (field === "version") result.value.schema_version = 1;
      const { value } = client(vi.fn().mockResolvedValue(result)); value.accept(displayConsumer());
      const output = value.consumer.outputs[0];
      await expect(value.readDisplayEntry(output, output.payload.entries[0])).rejects.toThrow();
      expect(value.consumer.status).toBe("idle"); expect(value.pending).toBeNull();
    });

  it("preserves an inherited entry producer and leaves unfamiliar artifact bodies uninterpreted", async () => {
    const parent = "00000000-0000-4000-8000-000000000019", result: any = publicArtifact();
    result.producer.workflow_session_id = parent; result.data_type = "FUTURE_CONTENT"; result.data_schema_version = 8;
    result.value = ["registered unknown body"];
    const { value } = client(vi.fn().mockResolvedValue(result)), view = displayConsumer();
    view.history.push({ ...view.history[0], workflow_session_id: parent, inherited: true }); value.accept(view);
    const output = value.consumer.outputs[0], read = await value.readDisplayEntry(output, output.payload.entries[0]);
    expect(read.producer.workflow_session_id).toBe(parent); expect(supportedContent(read.data_type, read.data_schema_version)).toBe(false);
    expect(value.consumer.can_submit).toBe(true);
  });

  it("reads validated public historical display roots with their original run identity", async () => {
    const older = displayConsumer().outputs[0]; older.run_id = key; older.source.run_id = key;
    const artifact = { ...publicArtifact(), run_id: key };
    const request = vi.fn().mockResolvedValueOnce({ schema_version: 1, kind: "workflow.public-history",
      workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session, session_revision: 1,
      node_id: node, port_id: "output", outputs: [older] }).mockResolvedValueOnce(artifact);
    const { value } = client(request); value.accept(displayConsumer());
    const history = await value.readHistory(value.consumer.outputs[0]);
    expect(await value.readDisplayEntry(history.outputs[0], older.payload.entries[0])).toEqual(artifact);
    expect(JSON.parse(request.mock.calls[1][1].body).parameters.run_id).toBe(key);
  });

  it("discards late display successes and failures after replacing the source or selecting another session", async () => {
    const pending: Array<{ resolve: (v: any) => void; reject: (e: Error) => void }> = [];
    const request = vi.fn().mockImplementation(() => new Promise((resolve, reject) => { pending.push({ resolve, reject }); }));
    const { value } = client(request); value.accept(displayConsumer());
    const output = value.consumer.outputs[0], entry = output.payload.entries[0];
    const old = value.readDisplayEntry(output, entry), newer = value.readDisplayEntry(output, entry);
    pending[1]!.resolve(publicArtifact()); expect(await newer).not.toBeNull();
    pending[0]!.resolve(publicArtifact()); expect(await old).toBeNull();
    const replaced = value.readDisplayEntry(output, entry), replacement = displayConsumer();
    replacement.outputs[0].run_id = node; replacement.outputs[0].source.run_id = node; value.accept(replacement);
    pending[2]!.reject(new Error("old source failure")); expect(await replaced).toBeNull();
    value.accept(displayConsumer()); const late = value.readDisplayEntry(output, entry);
    request.mockResolvedValueOnce({ ...consumer(), workflow_session_id: node }); await value.selectSession(node);
    pending[3]!.resolve(publicArtifact()); expect(await late).toBeNull();
  });

  it("freezes the requested display source and reports a current public read failure without changing pending state", async () => {
    let finish: (v: any) => void = () => {};
    const request = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockRejectedValueOnce(new Error("output_artifact_not_public"));
    const { value } = client(request); value.accept(displayConsumer());
    const output = JSON.parse(JSON.stringify(value.consumer.outputs[0])), entry = output.payload.entries[0];
    const reading = value.readDisplayEntry(output, entry); entry.source_ref.output_id = node;
    finish(publicArtifact()); expect((await reading).reference.output_id).toBe(key);
    const current = value.consumer.outputs[0];
    await expect(value.readDisplayEntry(current, current.payload.entries[0])).rejects.toThrow("not_public");
    expect(value.pending).toBeNull();
  });

  it.each([
    ["TEXT", 1], ["PROMPT", 2], ["JSON", 2], ["FRONTEND_DISPLAY", 1], ["FRONTEND_VIEW", 1], ["FUTURE_CONTENT", 8],
  ])("leaves %s@%s display entry bodies unsupported instead of guessing their text", (type, version) => {
    const result = { ...publicArtifact(), data_type: type, data_schema_version: version,
      value: { schema_version: version, kind: "workflow.text", text: "must not appear" } };
    expect(displayEntryText(result)).toBe(`不支持的正文格式 · ${type}@${version}`);
    expect(displayEntryText(result)).not.toContain("must not appear");
    expect(displayEntryText(publicArtifact())).toBe("accepted postprocessed text");
  });

  it("clones and freezes observed display roots so a caller cannot replace their authorized reference", async () => {
    const observed = displayConsumer(), request = vi.fn().mockResolvedValue(publicArtifact()), { value } = client(request);
    value.accept(observed);
    observed.outputs[0].payload.entries[0].source_ref.output_id = node;
    expect(value.consumer.outputs[0].payload.entries[0].source_ref.output_id).toBe(key);
    expect(Object.isFrozen(value.consumer.outputs[0].payload.entries[0].source_ref)).toBe(true);
    await expect(value.readDisplayEntry(observed.outputs[0], observed.outputs[0].payload.entries[0])).rejects.toThrow("来源");
    const output = value.consumer.outputs[0];
    expect(await value.readDisplayEntry(output, output.payload.entries[0])).toEqual(publicArtifact());
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("keeps only the complete current business display list after successive rounds and reconstructs it after reopening", async () => {
    const saved = storage(), { value } = client(vi.fn(), saved), first = displayConsumer(), second = displayConsumer();
    value.accept(first);
    second.session_revision = 2;
    second.outputs[0].run_id = node; second.outputs[0].source.run_id = node;
    second.outputs[0].payload.entries.push({ entry_id: run, role: "assistant",
      source_ref: { scope: "artifact", output_id: run } });
    value.accept(second);
    expect(value.consumer.outputs[0].payload.entries.map((entry: any) => entry.entry_id)).toEqual([key, run]);
    const reopened = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved,
      request: vi.fn().mockResolvedValue(second), keyFactory: () => key });
    await reopened.refresh();
    expect(reopened.consumer.outputs[0].payload.entries).toEqual(value.consumer.outputs[0].payload.entries);
    expect(saved.setItem).not.toHaveBeenCalled(); expect(reopened.pending).toBeNull();
  });

  it("discards display reads after a source is revoked and then reappears in the same session revision", async () => {
    let finish: (v: any) => void = () => {};
    const { value } = client(vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; })));
    value.accept(displayConsumer());
    const output = value.consumer.outputs[0], pending = value.readDisplayEntry(output, output.payload.entries[0]);
    value.accept(consumer()); value.accept(displayConsumer());
    finish(publicArtifact()); expect(await pending).toBeNull();
  });

  it.each(["success", "failure"])("discards a late display %s after the current definition changes", async outcome => {
    let finish: (v: any) => void = () => {}, fail: (e: Error) => void = () => {};
    const { value } = client(vi.fn().mockImplementation(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; })));
    value.accept(displayConsumer());
    const output = value.consumer.outputs[0], pending = value.readDisplayEntry(output, output.payload.entries[0]);
    value.accept({ ...consumer(), definition_revision: 2 });
    if (outcome === "success") finish(publicArtifact()); else fail(new Error("old definition failure"));
    expect(await pending).toBeNull(); expect(value.consumer.definition_revision).toBe(2); expect(value.pending).toBeNull();
  });

  it("isolates concurrent historical reads, freezes returned sources and discards their stale failures", async () => {
    const pending: Array<{ resolve: (v: any) => void; reject: (e: Error) => void }> = [];
    const request = vi.fn().mockImplementation(() => new Promise((resolve, reject) => { pending.push({ resolve, reject }); }));
    const { value } = client(request); value.accept(displayConsumer());
    const port = value.consumer.outputs[0], response = { schema_version: 1, kind: "workflow.public-history",
      workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session, session_revision: 1,
      node_id: node, port_id: "output", outputs: [displayConsumer().outputs[0]] };
    const old = value.readHistory(port), newer = value.readHistory(port);
    pending[1]!.resolve(response); expect(await newer).toEqual(response);
    pending[0]!.reject(new Error("obsolete history failure")); expect(await old).toBeNull();
    response.outputs[0].payload.entries[0].source_ref.output_id = node;
    expect(value.publicHistory[0].payload.entries[0].source_ref.output_id).toBe(key);
    expect(Object.isFrozen(value.publicHistory[0])).toBe(true);
    const selected = value.readHistory(port);
    value.accept({ ...consumer(), definition_revision: 2 });
    pending[2]!.reject(new Error("old definition history failure")); expect(await selected).toBeNull();
  });

  it("rejects a historical display root whose type is forged and freezes query coordinates before dispatch", async () => {
    let finish: (v: any) => void = () => {};
    const request = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { value } = client(request); value.accept(displayConsumer());
    const port = JSON.parse(JSON.stringify(value.consumer.outputs[0])), reading = value.readHistory(port);
    port.node_binding_id = run; port.port_id = "forged";
    const output = displayConsumer().outputs[0]; output.data_type = "FRONTEND_VIEW";
    finish({ schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
      definition_revision: 1, workflow_session_id: session, session_revision: 1, node_id: node, port_id: "output", outputs: [output] });
    await expect(reading).rejects.toThrow("端口");
    expect(JSON.parse(request.mock.calls[0][1].body).parameters.node_id).toBe(node);
    expect(value.publicHistory).toEqual([]);
  });

  it.each(["success", "failure"])("discards a historical entry %s once its history snapshot is refreshed", async outcome => {
    let finish: (v: any) => void = () => {}, fail: (e: Error) => void = () => {};
    const older = displayConsumer().outputs[0]; older.run_id = key; older.source.run_id = key;
    const history = { schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
      definition_revision: 1, workflow_session_id: session, session_revision: 1, node_id: node, port_id: "output", outputs: [older] };
    const request = vi.fn().mockResolvedValueOnce(history)
      .mockImplementationOnce(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; }))
      .mockResolvedValueOnce(history);
    const { value } = client(request); value.accept(displayConsumer());
    await value.readHistory(value.consumer.outputs[0]);
    const reading = value.readDisplayEntry(older, older.payload.entries[0]);
    await value.readHistory(value.consumer.outputs[0]);
    if (outcome === "success") finish({ ...publicArtifact(), run_id: key }); else fail(new Error("obsolete history entry failure"));
    expect(await reading).toBeNull();
    expect(value.publicHistory).toHaveLength(1); expect(value.pending).toBeNull();
  });

  it("starts a zero-Agent fixed-text graph without a chat input or formal message", async () => {
    const request = vi.fn(async (_path, options) => receipt(JSON.parse(options.body)));
    const { value } = client(request);
    await value.command("start");
    expect(request.mock.calls[0][0]).toBe("/api/graph/consumer/commands");
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ operation: "consumer.run.start",
      parameters: { session_id: session, expected_revision: 1, idempotency_key: key, inputs: {} } });
    expect(value.consumer.status).toBe("prepared");
    expect(value.consumer.outputs[0].availability).toBe("unproduced");
    expect(value.consumer).not.toHaveProperty("visible_messages");
  });

  it("keeps original inputs and idempotency key across an unknown result and reload", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("unknown"))
      .mockImplementationOnce(async (_path, options) => receiptRead(JSON.parse(options.body)));
    const { value, saved } = client(request);
    await expect(value.command("start", { inputs: { text: "original", other: "" } })).rejects.toThrow("unknown");
    const recovered = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request, keyFactory: () => "different" });
    await recovered.command("start", { inputs: { text: "edited" } });
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    expect(request.mock.calls[1][0]).toBe("/api/graph/consumer/receipts/read");
    expect(recovered.pending).toBeNull();
  });

  it.each(["retry_acceptance", "retry_failed_node"])("reads an unknown %s receipt with the original action and session after reload", async action => {
    const request = vi.fn().mockRejectedValueOnce(new Error("lost acceptance receipt"))
      .mockImplementationOnce(async (_path, options) => {
        return receiptRead(JSON.parse(options.body), 3);
      });
    const { value, saved } = client(request);
    value.accept({ ...consumer(2), status: action === "retry_acceptance" ? "archive_failed" : "failed", can_submit: false,
      available_actions: [action, "close"] });
    await expect(value.command(action)).rejects.toThrow("lost acceptance receipt");
    const original = JSON.parse(request.mock.calls[0][1].body);
    expect(original).toEqual({ operation: "consumer.run.control",
      parameters: { session_id: session, action, expected_revision: 2, idempotency_key: key } });
    const recovered = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved,
      request, keyFactory: () => "different" });
    await recovered.command("start", { inputs: { text: "replacement" } });
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    expect(request.mock.calls[1][0]).toBe("/api/graph/consumer/receipts/read");
    expect(recovered.pending).toBeNull();
  });

  it("keeps a structured PROMPT input and its metadata unchanged during receipt-only reconciliation", async () => {
    const declaration = { name: "prompt", data_type: "PROMPT", required: true, node_ids: [node] };
    const payload = { schema_version: 1, kind: "workflow.prompt", stage: "materials", assembly: null, items: [{
      item_instance_id: node, text: "original {{macro}}", role: "assistant", placement: "middle", depth: 3,
      order: 2, enabled: true, purpose: "prompt", source: { kind: "configuration", revision: 2 }, protected: false, metadata: { name: "source" },
    }] };
    const request = vi.fn().mockRejectedValueOnce(new Error("unknown"))
      .mockImplementationOnce(async (_path, options) => receiptRead(JSON.parse(options.body)));
    const { value, saved } = client(request);
    value.accept({ ...consumer(), inputs: [declaration] });
    await expect(value.command("start", { inputs: { prompt: parseExternalInput("PROMPT", JSON.stringify(payload)) } })).rejects.toThrow("unknown");
    const recovered = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request, keyFactory: () => "different" });
    expect(recovered.pending.body.inputs.prompt).toEqual(payload);
    await recovered.command("start", { inputs: { prompt: "edited" } });
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    expect(request.mock.calls[1][0]).toBe("/api/graph/consumer/receipts/read");
    expect(JSON.parse(request.mock.calls[1][1].body).parameters.inputs.prompt).toEqual(payload);
  });

  it("blocks an invalid PROMPT envelope before persistence or submission", async () => {
    const request = vi.fn(), { value, saved } = client(request);
    value.accept({ ...consumer(), inputs: [{ name: "prompt", data_type: "PROMPT", required: true, node_ids: [node] }] });
    expect(() => parseExternalInput("PROMPT", "broken JSON")).toThrow("有效 JSON");
    await expect(value.command("start", { inputs: { prompt: { schema_version: 1, kind: "workflow.prompt",
      stage: "assembled", items: [], assembly: null } } })).rejects.toThrow("PROMPT");
    expect(request).not.toHaveBeenCalled();
    expect(saved.setItem).not.toHaveBeenCalled();
    expect(value.pending).toBeNull();
  });

  it("rejects an unrelated receipt and preserves the pending request", async () => {
    const request = vi.fn(async (_path, options) => {
      const value = receipt(JSON.parse(options.body)); value.receipt.idempotency_key = node; return value;
    });
    const { value } = client(request);
    await expect(value.command("start")).rejects.toThrow("不匹配");
    expect(value.pending.body.idempotency_key).toBe(key);
  });

  it("does not replace a newer control observation with a delayed refresh", async () => {
    let finish: (value: unknown) => void = () => {};
    const request = vi.fn(() => new Promise(resolve => { finish = resolve; }));
    const { value } = client(request);
    const refreshing = value.refresh();
    value.accept({ ...consumer(5), status: "running" });
    finish({ ...consumer(4), status: "paused" });
    await refreshing;
    expect(value.consumer.session_revision).toBe(5);
    expect(value.consumer.status).toBe("running");
  });

  it("rejects older same-revision node and chain progress but accepts a new run identity", () => {
    const { value } = client(vi.fn());
    const current = { ...consumer(5), nodes: [{ ...consumer().nodes[0], run_id: run, revision: 4 }], history: [{
      workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session, chain_run_id: chain,
      status: "running", revision: 6, inherited: false }] };
    expect(value.accept(current)).toBe(true);
    expect(value.accept({ ...current, nodes: [{ ...current.nodes[0], revision: 3 }] })).toBe(false);
    expect(value.accept({ ...current, history: [{ ...current.history[0], revision: 5 }] })).toBe(false);
    expect(value.accept({ ...current, nodes: [{ ...current.nodes[0], run_id: node, revision: 1 }] })).toBe(true);
  });

  it("decodes content families by their declared wire type rather than guessing text", () => {
    expect(validateContent({ schema_version: 1, kind: "workflow.text", text: "plain" }, "TEXT").text).toBe("plain");
    expect(validateContent({ schema_version: 1, kind: "workflow.json", value: null }, "JSON").value).toBeNull();
    expect(validateContent({ schema_version: 1, kind: "workflow.prompt", stage: "materials", items: [], assembly: null }, "PROMPT").stage).toBe("materials");
    expect(() => validateContent({ schema_version: 1, kind: "workflow.json", value: "looks textual" }, "TEXT")).toThrow("TEXT");
    expect(() => validateContent({ schema_version: 1, kind: "workflow.prompt", stage: "assembled", items: [], assembly: null }, "PROMPT")).toThrow("PROMPT");
  });
  it("accepts explicit v2 content envelopes and displays materials without changing them", () => {
    expect(displayText(validateContent({ schema_version: 2, kind: "workflow.text", text: "v2 text" }, "TEXT"))).toBe("v2 text");
    expect(validateContent({ schema_version: 2, kind: "workflow.json", value: { source: "v2" } }, "JSON").value).toEqual({ source: "v2" });
    const item = { item_instance_id: node, text: "enabled {{literal}}", role: "system", placement: "before",
      depth: null, order: 0, enabled: true, purpose: "prompt", source: { kind: "configuration" },
      protected: false, metadata: { business: "kept" } };
    const payload = { schema_version: 2, kind: "workflow.prompt", stage: "materials", assembly: null,
      items: [item, { ...item, item_instance_id: run, text: "disabled", enabled: false }] };
    const original = JSON.stringify(payload);
    expect(validateContent(payload, "PROMPT")).toEqual(payload);
    expect(displayText(payload)).toBe("enabled {{literal}}");
    expect(JSON.stringify(payload)).toBe(original);
    expect(() => validateContent({ ...payload, schema_version: 3 }, "PROMPT")).toThrow("封套");
  });
  it("uses actual v2 assembly messages for display and rejects a v1 assembly inside a v2 envelope", () => {
    const payload = { schema_version: 2, kind: "workflow.prompt", stage: "assembled", items: [{
      item_instance_id: node, text: "instruction", role: "system", placement: "before", depth: null,
      order: 0, enabled: true, purpose: "prompt", protected: false,
      source: { kind: "configuration" }, metadata: {},
    }], assembly: { schema_version: 2, kind: "workflow.prompt-assembly",
      messages: [{ role: "system", content: "instruction" }, { role: "user", content: "current question" }],
      manifest: { ordered_item_ids: [node], source_output_refs: [{ edge_id: node, output_id: run, order: 0 }],
        current_input_refs: [{ edge_id: chain, output_id: key, order: 0 }] },
      current_input: { role: "user", content: "current question" } } };
    expect(validateContent(payload, "PROMPT")).toEqual(payload);
    expect(displayText(payload)).toBe("instruction\n\ncurrent question");
    const wrong = { ...payload, assembly: { schema_version: 1, kind: "workflow.prompt-assembly",
      prompt: {}, current_root: {}, context_basis: [] } };
    expect(() => validateContent(wrong, "PROMPT")).toThrow("PROMPT");
    expect(() => validateContent({ ...payload, assembly: { ...payload.assembly, messages: [] } }, "PROMPT")).toThrow("PROMPT");
  });
  it("reads v2 produced output through the unchanged v1 consumer protocol and enforces an optional exact port version", () => {
    const view = consumer();
    view.history = [{ workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
      chain_run_id: chain, status: "succeeded", revision: 1, inherited: false }] as any;
    const output = { ...view.outputs[0], availability: "produced", status: "succeeded", run_id: run, chain_run_id: chain,
      payload: { schema_version: 2, kind: "workflow.text", text: "new package output" },
      source: { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
        node_binding_id: node, port_id: "output", run_id: run, chain_run_id: chain } };
    view.outputs = [output] as any;
    expect(validateConsumer(view, workflow, session).schema_version).toBe(1);
    expect(displayText(view.outputs[0].payload)).toBe("new package output");
    view.outputs = [{ ...output, data_schema_version: 2 }] as any;
    expect(validateConsumer(view, workflow, session).outputs[0].payload.schema_version).toBe(2);
    view.outputs = [{ ...output, data_schema_version: 1 }] as any;
    expect(() => validateConsumer(view, workflow, session)).toThrow("封套");
  });
  it("retains a v2 PROMPT external input during receipt-only reconciliation", async () => {
    const payload = { schema_version: 2, kind: "workflow.prompt", stage: "materials", assembly: null, items: [] };
    const declaration = { name: "prompt", data_type: "PROMPT", required: true, node_ids: [node] };
    const request = vi.fn().mockRejectedValueOnce(new Error("unknown"))
      .mockImplementationOnce(async (_path, options) => receiptRead(JSON.parse(options.body)));
    const { value, saved } = client(request); value.accept({ ...consumer(), inputs: [declaration] });
    await expect(value.command("start", { inputs: { prompt: parseExternalInput("PROMPT", JSON.stringify(payload)) } })).rejects.toThrow("unknown");
    const reopened = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request,
      keyFactory: () => "must-not-replace-original-key" });
    await reopened.command("start", { inputs: { prompt: "edited" } });
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    expect(request.mock.calls[1][0]).toBe("/api/graph/consumer/receipts/read");
    expect(JSON.parse(request.mock.calls[1][1].body).parameters.inputs.prompt).toEqual(payload);
  });

  it("shows a model reference summary without credential references or evidence", () => {
    const payload = { schema_version: 1, kind: "workflow.model-resource", binding: { node_id: node,
      provider: { provider_id: workflow, revision: 1, name: "Chat", credential_ref: "env:API_KEY" },
      credential_evidence: "1".repeat(64), parameters: { model: "test-chat" } } };
    validateContent(payload, "MODEL_RESOURCE");
    expect(JSON.parse(displayText(payload))).toEqual({ provider_id: workflow, revision: 1, name: "Chat", model: "test-chat" });
    expect(displayText(payload)).not.toMatch(/credential|API_KEY|11111111/);
  });

  it("shows a structured server rejection code and diagnostic rather than an object string", () => {
    const error = httpFailure({ error: { code: "stale_revision", message: "Session changed",
      diagnostic: { reason_code: "stale_revision", message: "Session changed" } } }, 409);
    expect(error.message).toBe("stale_revision · Session changed");
    expect(error.definite).toBe(true);
    expect(error.message).not.toContain("[object Object]");
  });

  it("keeps unresolved submission after a local write failure following a valid receipt", async () => {
    const saved = storage();
    const request = vi.fn(async (_path, options) => receipt(JSON.parse(options.body)));
    const { value } = client(request, saved);
    saved.setItem.mockImplementationOnce(() => {}).mockImplementationOnce(() => { throw new Error("storage full"); });
    await expect(value.command("start")).rejects.toThrow("storage full");
    expect(value.pending.body.idempotency_key).toBe(key);
  });

  it("does not display last-run payload on an unproduced current port", () => {
    const value = consumer();
    value.outputs[0].payload = { schema_version: 1, kind: "workflow.text", text: "old" } as any;
    expect(() => validateConsumer(value, workflow, session)).toThrow("不应携带结果");
  });

  it("keeps the current running source while its public port has no payload yet", () => {
    const value = consumer();
    value.history = [{ workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
      chain_run_id: chain, status: "running", revision: 2, inherited: false }] as any;
    value.outputs = [{ ...value.outputs[0], status: "running", run_id: run, chain_run_id: chain,
      source: { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
        node_binding_id: node, port_id: "output", run_id: run, chain_run_id: chain } }] as any;
    expect(validateConsumer(value, workflow, session).outputs[0].payload).toBeNull();
  });

  it("preserves original ownership of frozen inherited output and rejects foreign history", () => {
    const value = consumer();
    const parent = "00000000-0000-4000-8000-000000000019";
    value.history = [{ workflow_definition_id: parent, definition_revision: 3, workflow_session_id: parent,
      chain_run_id: chain, status: "succeeded", revision: 4, inherited: true }] as any;
    const output = { ...value.outputs[0], availability: "produced", status: "succeeded", run_id: run, chain_run_id: chain,
      payload: { schema_version: 1, kind: "workflow.text", text: "copied" }, source: { workflow_definition_id: parent,
        definition_revision: 3, workflow_session_id: parent, node_binding_id: parent, port_id: "output", run_id: run, chain_run_id: chain } };
    value.outputs = [output] as any;
    expect(validateConsumer(value, workflow, session).outputs[0].source.workflow_session_id).toBe(parent);
    const history = { schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
      definition_revision: 1, workflow_session_id: session, session_revision: 1, node_id: node, port_id: "output", outputs: [output] };
    expect(validateHistory(history, value, output).outputs).toHaveLength(1);
    history.outputs[0].source = { ...output.source, chain_run_id: parent };
    history.outputs[0].chain_run_id = parent;
    expect(() => validateHistory(history, value, output)).toThrow("冻结会话范围");
  });

  it("does not clear pending input or switch to another session before reconciliation", async () => {
    const { value } = client(vi.fn().mockRejectedValue(new Error("unknown")));
    await expect(value.command("start", { inputs: { text: "kept" } })).rejects.toThrow();
    await expect(value.selectSession(node)).rejects.toThrow("核实原请求");
    expect(value.sessionId).toBe(session);
    expect(value.pending.body.inputs.text).toBe("kept");
  });

  it("maps a previously frozen pending request to the same consumer authority without changing its identity", async () => {
    const saved = storage();
    const pending = { path: `/api/graph/sessions/${session}/consumer/runs`, action: "start",
      workflow_session_id: session, body: { expected_revision: 7, idempotency_key: key, inputs: { prompt: "frozen" } } };
    saved.setItem(`workflow-chat:v1:${workflow}`, JSON.stringify({ schema_version: 1, kind: "workflow.chat-client",
      workflow_definition_id: workflow, workflow_session_id: session, pending }));
    const request = vi.fn(async (_path, options) => receiptRead(JSON.parse(options.body), 8));
    const reopened = new GraphChatClient({ workflowId: workflow, sessionId: node, storage: saved, request, keyFactory: vi.fn() });
    const original = JSON.stringify(reopened.pending);
    await reopened.command("close", { expected_revision: 99 });
    expect(JSON.parse(original)).toEqual(pending);
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ operation: "consumer.run.start",
      parameters: { ...pending.body, session_id: session } });
    expect(request.mock.calls[0][0]).toBe("/api/graph/consumer/receipts/read");
    expect(reopened.sessionId).toBe(session);
  });

  it("rejects an unknown saved command without rewriting the original record or sending a request", () => {
    const saved = storage(), request = vi.fn();
    const raw = JSON.stringify({ schema_version: 1, kind: "workflow.chat-client", workflow_definition_id: workflow,
      workflow_session_id: session, pending: { action: "force_latest", workflow_session_id: session,
        path: `/api/graph/sessions/${session}/consumer/control`,
        body: { action: "force_latest", expected_revision: 1, idempotency_key: key } } });
    saved.setItem(`workflow-chat:v1:${workflow}`, raw);
    saved.setItem.mockClear();
    expect(() => new GraphChatClient({ workflowId: workflow, storage: saved, request, keyFactory: vi.fn() })).toThrow();
    expect(saved.getItem(`workflow-chat:v1:${workflow}`)).toBe(raw);
    expect(saved.setItem).not.toHaveBeenCalled();
    expect(request).not.toHaveBeenCalled();
  });

  it.each(["operation", "scope", "target", "accepted", "authority", "digest"])(
    "keeps pending when the application receipt has a mismatched %s", async field => {
      const request = vi.fn(async (_path, options) => {
        const value = receipt(JSON.parse(options.body));
        if (field === "operation") value.receipt.operation = "run.start";
        if (field === "scope") value.receipt.operation_scope = "management";
        if (field === "target") value.receipt.target = { session_id: node };
        if (field === "accepted") value.receipt.accepted.session_revision++;
        if (field === "authority") value.receipt.authority = "service_result";
        if (field === "digest") value.receipt.request_sha256 = "invalid";
        return value;
      });
      const { value } = client(request);
      await expect(value.command("start")).rejects.toThrow("回执");
      expect(value.pending.body.idempotency_key).toBe(key);
    });

  it("reconciles the stable original receipt separately from a later fresh consumer observation", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("unknown"))
      .mockImplementationOnce(async (_path, options) => receiptRead(JSON.parse(options.body), 2));
    const { value } = client(request);
    await expect(value.command("start")).rejects.toThrow("unknown");
    value.accept({ ...consumer(8), status: "succeeded", can_submit: true, available_actions: [] });
    const accepted = await value.command("close");
    expect(accepted.receipt.session_revision).toBe(2);
    expect(value.consumer.session_revision).toBe(8);
    expect(value.consumer.status).toBe("succeeded");
    expect(value.pending).toBeNull();
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    expect(request.mock.calls[1][0]).toBe("/api/graph/consumer/receipts/read");
  });

  it("does not dispatch when persistence fails before sending the named command", async () => {
    const request = vi.fn(), saved = storage(), { value } = client(request, saved);
    saved.setItem.mockImplementationOnce(() => { throw new Error("storage full"); });
    await expect(value.command("start")).rejects.toThrow("storage full");
    expect(request).not.toHaveBeenCalled();
    expect(value.pending).toBeNull();
  });

  it("discovers only consumer operations and uses the named definition, directory, history and refresh queries", async () => {
    const history = { schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
      definition_revision: 1, workflow_session_id: session, session_revision: 1,
      node_id: node, port_id: "output", outputs: [] };
    const request = vi.fn(async (path, options) => {
      if (path === "/api/graph/consumer/application") return application();
      const envelope = JSON.parse(options.body);
      if (envelope.operation === "consumer.definition") return { schema_version: 1, kind: "workflow.consumer-definition",
        workflow_definition_id: workflow, definition_revision: 1, name: "Zero Agent" };
      if (envelope.operation === "consumer.sessions") return [{ workflow_definition_id: workflow, definition_revision: 1,
        workflow_session_id: session, session_revision: 1, status: "idle" }];
      return envelope.operation === "output.history" ? history : consumer();
    });
    const { value } = client(request);
    expect((await value.discover()).scope).toBe("consumer");
    expect((await value.readDefinition()).name).toBe("Zero Agent");
    expect(await value.readSessions()).toHaveLength(1);
    expect((await value.readHistory(consumer().outputs[0])).outputs).toEqual([]);
    await value.refresh();
    expect(request.mock.calls.slice(1).every(([path]) => path === "/api/graph/consumer/queries")).toBe(true);
    await expect(value.query("session.read", { session_id: session })).rejects.toThrow("不受支持");
    expect(request).toHaveBeenCalledTimes(5);
    expect(() => validateApplication({ ...application(), scope: "management" })).toThrow("目录");
  });

  it("rejects a command if discovery does not grant it and retains the frozen request", async () => {
    const { value } = client(vi.fn());
    value.application = { ...application(), commands: [] };
    await expect(value.command("start")).rejects.toThrow("未登记");
    expect(value.pending.body.idempotency_key).toBe(key);
    expect(commandEnvelope(value.pending, workflow).operation).toBe("consumer.run.start");
  });

  it("reads an unfamiliar public format with exact owner and bounded provider pagination while leaving execution unchanged", async () => {
    const binding = publicBinding(), page = informationPage(binding);
    const request = vi.fn(async (_path, options) => JSON.parse(options.body).operation === "registration.list"
      ? { items: [binding], next_cursor: "directory-opaque" } : page);
    const { value, saved } = client(request); acceptInformationHistory(value);
    const before = JSON.stringify(value.consumer);
    const directory = await value.listRegistrations({ limit: 2 });
    expect(directory.next_cursor).toBe("directory-opaque");
    expect(await value.readInformation(directory.items[0])).toEqual(page);
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ operation: "information.read", parameters: {
      session_id: session, reference: binding.registration_ref, owner: binding.owner, generation: 2,
      source_scope: "live", limit: 2 } });
    page.status = "gap";
    const gap = await value.readInformation(directory.items[0], { cursor: "provider-opaque" });
    expect(gap.status).toBe("gap");
    page.status = "reset"; page.next_cursor = null as any;
    expect((await value.readInformation(directory.items[0], { cursor: "provider-opaque" })).status).toBe("reset");
    expect(JSON.stringify(value.consumer)).toBe(before);
    expect(value.pending).toBeNull();
    expect(saved.setItem).not.toHaveBeenCalled();
    expect(request.mock.calls.every(([path]) => path === "/api/graph/consumer/queries")).toBe(true);
    await expect(value.readInformation(directory.items[0], { limit: 3 })).rejects.toThrow("登记边界");
    expect(request).toHaveBeenCalledTimes(4);
  });

  it("retains the original owner when reading inherited history through a child session", async () => {
    const parent = "00000000-0000-4000-8000-000000000019", binding = publicBinding(parent);
    binding.availability = "history";
    const request = vi.fn(async (_path, options) => JSON.parse(options.body).operation === "registration.list"
      ? { items: [binding], next_cursor: null } : informationPage(binding, "history"));
    const { value } = client(request); acceptInformationHistory(value, parent);
    await value.listRegistrations();
    await value.readInformation(binding, { source_scope: "history" });
    const parameters = JSON.parse(request.mock.calls[1][1].body).parameters;
    expect(parameters.session_id).toBe(session);
    expect(parameters.owner.workflow_session_id).toBe(parent);
    expect(parameters.owner.node_run_id).toBe(run);
    expect(parameters.generation).toBe(2);
  });

  it("keeps discovery and read permissions separate and never falls back to management for private execution facts", async () => {
    const binding = publicBinding();
    binding.read_public = false; binding.declaration.read_public = false;
    binding.declaration.channel_id = "execution-facts";
    const request = vi.fn(async (_path: string) => ({ items: [binding], next_cursor: null }));
    const { value } = client(request); acceptInformationHistory(value);
    await value.listRegistrations();
    await expect(value.readInformation(binding)).rejects.toThrow("未授予");
    expect(request).toHaveBeenCalledTimes(1);
    const privateBinding = { ...binding, discover_public: false, declaration: { ...binding.declaration, discover_public: false } };
    expect(() => validateRegistrations({ items: [privateBinding], next_cursor: null })).toThrow("公开");
    expect(request.mock.calls[0][0]).toBe("/api/graph/consumer/queries");
  });

  it.each(["owner", "generation", "reference", "format", "scope", "page"])(
    "rejects mismatched information %s without changing the consumer or pending", async field => {
      const binding = publicBinding(), page: any = informationPage(binding);
      if (field === "owner") page.owner = { ...binding.owner, node_run_id: node };
      if (field === "generation") page.generation = 1;
      if (field === "reference") page.source_ref = { ...binding.registration_ref, exact_version: "other" };
      if (field === "format") page.format_version = 1;
      if (field === "scope") page.source_scope = "history";
      if (field === "page") page.items = [1, 2, 3];
      const request = vi.fn(async (_path, options) => JSON.parse(options.body).operation === "registration.list"
        ? { items: [binding], next_cursor: null } : page);
      const { value } = client(request); acceptInformationHistory(value);
      await value.listRegistrations();
      await expect(value.readInformation(binding)).rejects.toThrow("归属");
      expect(value.consumer.session_revision).toBe(1);
      expect(value.pending).toBeNull();
    });

  it("discards late directory and information pages after session selection or a later page request", async () => {
    const binding = publicBinding();
    let completeDirectory: (value: any) => void = () => {};
    const request = vi.fn().mockImplementationOnce(() => new Promise(resolve => { completeDirectory = resolve; }))
      .mockResolvedValueOnce({ ...consumer(), workflow_session_id: node });
    const { value } = client(request); acceptInformationHistory(value);
    const listing = value.listRegistrations();
    await value.selectSession(node);
    completeDirectory({ items: [binding], next_cursor: null });
    expect(await listing).toBeNull();
    expect(value.registrations).toEqual([]);

    const delayed: Array<(value: any) => void> = [];
    const readRequest = vi.fn().mockResolvedValueOnce({ items: [binding], next_cursor: null })
      .mockImplementation(() => new Promise(resolve => { delayed.push(resolve); }));
    const second = client(readRequest).value; acceptInformationHistory(second);
    await second.listRegistrations();
    const oldPage = second.readInformation(binding), newPage = second.readInformation(binding);
    delayed[1](informationPage(binding)); expect(await newPage).not.toBeNull();
    delayed[0](informationPage(binding)); expect(await oldPage).toBeNull();
    const latePage = second.readInformation(binding);
    second.invalidateInformation();
    delayed[2](informationPage(binding)); expect(await latePage).toBeNull();
  });

  it("requires re-discovery after the session observation changes instead of reading a guessed latest invocation", async () => {
    const binding = publicBinding(), request = vi.fn(async () => ({ items: [binding], next_cursor: null }));
    const { value } = client(request); acceptInformationHistory(value);
    await value.listRegistrations();
    value.accept({ ...consumer(2), history: value.consumer.history });
    await expect(value.readInformation(binding)).rejects.toThrow("重新列举");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("freezes source identity before an asynchronous provider read", async () => {
    const binding = publicBinding();
    let finish: (value: unknown) => void = () => {};
    const request = vi.fn().mockResolvedValueOnce({ items: [binding], next_cursor: null })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { value } = client(request); acceptInformationHistory(value);
    await value.listRegistrations();
    const page = informationPage(binding), original = JSON.parse(JSON.stringify(page));
    const reading = value.readInformation(binding);
    binding.owner.node_run_id = node; binding.generation = 9;
    binding.registration_ref.exact_version = "replacement"; binding.declaration.max_page_bytes = 1;
    finish(original);
    expect(await reading).toEqual(original);
  });

  it("discards failed stale information reads when their session or directory generation is no longer current", async () => {
    const binding = publicBinding();
    let rejectDirectory: (error: Error) => void = () => {}, rejectPage: (error: Error) => void = () => {};
    const request = vi.fn().mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectDirectory = reject; }))
      .mockResolvedValueOnce({ items: [binding], next_cursor: null })
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPage = reject; }));
    const { value } = client(request); acceptInformationHistory(value);
    const obsoleteDirectory = value.listRegistrations();
    value.invalidateInformation(); rejectDirectory(new Error("obsolete directory failure"));
    expect(await obsoleteDirectory).toBeNull();
    await value.listRegistrations();
    const obsoleteRead = value.readInformation(binding);
    value.invalidateInformation(); rejectPage(new Error("obsolete read failure"));
    expect(await obsoleteRead).toBeNull();
  });

  it("keeps valid unsupported public output formats available without interpreting them as text", () => {
    const value: any = consumer();
    value.history = [{ workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
      chain_run_id: chain, status: "succeeded", revision: 1, inherited: false }];
    value.outputs[0] = { ...value.outputs[0], data_type: "CUSTOM_VIEW", data_schema_version: 7,
      availability: "produced", run_id: run, chain_run_id: chain,
      payload: { content: "custom schema does not require a kind or schema_version" },
      source: { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
        node_binding_id: node, port_id: "output", run_id: run, chain_run_id: chain } };
    expect(validateConsumer(value, workflow, session).outputs).toHaveLength(1);
    expect(supportedContent("CUSTOM_VIEW", 7)).toBe(false);
    expect(() => validateContent(value.outputs[0].payload, "CUSTOM_VIEW")).toThrow();
    value.outputs[0].payload = ["arbitrary", "registered", "schema"];
    expect(validateConsumer(value, workflow, session).outputs[0].payload).toHaveLength(3);
  });
});
