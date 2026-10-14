import { afterEach, describe, expect, it, vi } from "vitest";
import { createWorkflowConfigurationApplication, type WorkflowConfigurationApplicationPorts } from "./workflowConfiguration";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationDocument, GraphConfigurationResult,
  GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { memoryRequestEvidence, requestEvidenceCommandResponse } from "../testUtils/requestEvidence";
import type { WorkflowNodeConfigurationSubmission } from "../plugins/workflowFrontendSdk2";

afterEach(() => vi.unstubAllGlobals());

function source(): GraphConfigurationDocument {
  return { schema_version: 3, workflow_definition_id: crypto.randomUUID(), revision: 2, edit_sequence: 4,
    name: "Original", nodes: [{ node_binding_id: crypto.randomUUID(), component_id: "example.node",
      component_version: "1", title: "Node", position: { x: 0, y: 0 }, config: { text: "Before" },
      configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }], edges: [], object_bindings: [] };
}
function declaration(): FrontendExtension {
  return { schema_version: 3, host_protocol_version: 2, extension_id: "example.fields", kind: "field-editor",
    entrypoint: "example.fields", component_id: "example.node", component_version: "1",
    package_id: "example.package", package_version: "2.0.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "example.node", component_version: "1" } } };
}
function submission(document: GraphConfigurationDocument): WorkflowNodeConfigurationSubmission {
  return { workflowId: document.workflow_definition_id, nodeId: document.nodes[0].node_binding_id,
    componentId: "example.node", componentVersion: "1", extensionId: "example.fields",
    expectedEditSequence: document.edit_sequence, expectedConfig: graphClone(document.nodes[0].config),
    nextConfig: { text: "After" }, nextPolicy: graphClone(document.nodes[0].configuration_policy) };
}
function result(request: GraphConfigurationUpdateRequest, target?: string,
  adjustable = false): GraphConfigurationResult {
  const derived = target !== undefined && target !== request.workflow_definition_id;
  const revision = derived || request.expected_edit_sequence === 0 ? 1 : request.document.revision + Number(!adjustable);
  const editSequence = derived ? 1 : request.expected_edit_sequence + 1;
  return { schema_version: 1, kind: "workflow.configuration-result", outcome: derived ? "derived" : "saved",
    source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: target ?? request.workflow_definition_id,
    definition_revision: revision, edit_sequence: editSequence, source_has_effective_session: derived || adjustable,
    changed_categories: [adjustable ? "adjustable" : "fixed"],
    node_id_map: Object.fromEntries(request.document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document: { ...graphClone(request.document), workflow_definition_id: target ?? request.workflow_definition_id,
      revision, edit_sequence: editSequence } };
}
function ask(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  return { schema_version: 1, kind: "workflow.configuration-result", outcome: "confirmation_required",
    source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: request.workflow_definition_id,
    definition_revision: request.document.revision, edit_sequence: request.expected_edit_sequence,
    source_has_effective_session: true, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(request.document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null,
    document: { ...graphClone(request.document), edit_sequence: request.expected_edit_sequence } };
}
function fixture(overrides: Partial<WorkflowConfigurationApplicationPorts> = {}) {
  const document = source(), evidence = memoryRequestEvidence();
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => result(request));
  const readReceipt = vi.fn(async (request: GraphConfigurationUpdateRequest) => result(request));
  const ports: WorkflowConfigurationApplicationPorts = { evidence: evidence.ports, update, readReceipt,
    extensions: () => [{ declaration: declaration(), configurationFields: ["text"] }], ...overrides };
  const app = createWorkflowConfigurationApplication(ports);
  app.observe(document); app.select(document.workflow_definition_id);
  const entry = app.forWorkflow(document.workflow_definition_id);
  return { app, document, input: submission(document), entry, ports, evidence, update, readReceipt };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}
function response(request: GraphConfigurationUpdateRequest, actual: GraphConfigurationResult, readOnly = false) {
  const accepted = Object.fromEntries(["workflow_definition_id", "definition_revision", "outcome",
    "source_workflow_definition_id", "edit_sequence", "source_has_effective_session",
    "changed_categories", "node_id_map", "session_id"].map(key => [key, actual[key as keyof GraphConfigurationResult]]));
  const receipt = { schema_version: 1, kind: "workflow.application-receipt", operation: "definition.update",
    operation_scope: "management", idempotency_key: request.idempotency_key, request_sha256: "a".repeat(64),
    authority: "service_receipt", target: {}, accepted };
  return new Response(JSON.stringify(readOnly ? { schema_version: 1, kind: "workflow.application-receipt-read",
    outcome: "matched", reason_code: "receipt_matched", receipt, result: actual }
    : { schema_version: 1, kind: "workflow.application-command", receipt, result: actual }));
}

describe("uninstalled configuration application and SDK2", () => {
  it("persists the exact SDK request before dispatch, accepts official current and verifies retained evidence", async () => {
    const f = fixture(), order: string[] = [];
    const write = f.evidence.ports.write, resolve = f.evidence.ports.resolve;
    f.evidence.ports.write = vi.fn(async request => { order.push("evidence"); return write(request); });
    f.evidence.ports.resolve = vi.fn(async request => { order.push("verified"); return resolve(request); });
    f.update.mockImplementation(async request => { order.push("update"); return result(request); });
    await f.entry.initialize();
    const accepted = await f.app.sdk.submitNodeConfiguration(f.input);
    expect(order).toEqual(["evidence", "update", "verified"]);
    const saved = [...f.evidence.records.values()][0];
    expect(saved).toMatchObject({ operation: "definition.update", owner_kind: "workflow-configuration",
      owner_id: f.document.workflow_definition_id, resolution: "verified" });
    expect(saved.request).toEqual(f.update.mock.calls[0][0]);
    expect(f.app.current.value).toEqual(accepted.document);
    expect(f.entry.pending.value).toBeNull(); expect(f.entry.locked.value).toBe(false);
    expect(f.document.nodes[0].config.text).toBe("Before");
  });
  it("adds a formal derived directory target, preserves source and follows it without a session", async () => {
    const target = crypto.randomUUID(), f = fixture();
    f.update.mockImplementation(async request =>
      request.confirm_derivation ? result(request, target) : ask(request));
    f.app.select(f.document.workflow_definition_id, crypto.randomUUID());
    await f.entry.initialize();
    const question = await f.app.sdk.submitNodeConfiguration(f.input);
    expect(question.outcome).toBe("confirmation_required");
    const original = f.update.mock.calls[0]![0];
    expect(original).not.toHaveProperty("confirm_derivation");
    const accepted = await f.app.sdk.confirmDerivation(f.document.workflow_definition_id);
    const confirmed = f.update.mock.calls[1]![0];
    expect(f.update).toHaveBeenCalledTimes(2);
    expect(confirmed.confirm_derivation).toBe(true);
    expect(confirmed.idempotency_key).not.toBe(original.idempotency_key);
    expect(f.app.document(f.document.workflow_definition_id)).toEqual(f.document);
    expect(f.app.current.value).toEqual(accepted.document);
    expect(f.app.selection.value).toEqual({ workflowId: target, sessionId: null });
    expect(f.app.directory.value.map(row => row.workflowId)).toEqual([f.document.workflow_definition_id, target]);
  });
  it("creates a formal first configuration through the same evidence and complete-document submitter", async () => {
    const f = fixture(), newDocument = source();
    newDocument.revision = 1;
    Reflect.deleteProperty(newDocument, "edit_sequence");
    const request: GraphConfigurationUpdateRequest = { workflow_definition_id: newDocument.workflow_definition_id,
      expected_edit_sequence: 0, document: newDocument, idempotency_key: crypto.randomUUID() };
    const entry = f.app.forWorkflow(newDocument.workflow_definition_id);
    const accepted = await entry.submit(request);
    expect(accepted.definition_revision).toBe(1); expect(accepted.edit_sequence).toBe(1);
    expect(f.app.document(newDocument.workflow_definition_id)).toEqual(accepted.document);
    expect(f.app.current.value).toEqual(f.document);
    expect(entry.locked.value).toBe(false);
  });
  it("accepts unchanged SDK submissions without incrementing official revision or edit sequence", async () => {
    const f = fixture();
    f.input.nextConfig = graphClone(f.input.expectedConfig);
    f.update.mockImplementation(async request => ({ ...result(request), outcome: "unchanged",
      changed_categories: [], definition_revision: request.document.revision,
      edit_sequence: request.expected_edit_sequence,
      document: { ...graphClone(request.document), edit_sequence: request.expected_edit_sequence } }));
    await f.entry.initialize();
    const accepted = await f.app.sdk.submitNodeConfiguration(f.input);
    expect(accepted.outcome).toBe("unchanged"); expect(f.app.current.value).toEqual(f.document);
    expect(f.entry.locked.value).toBe(false);
  });
  it.each(["workflow", "session"] as const)("does not follow late derivation after a %s selection change", async switchKind => {
    const target = crypto.randomUUID(), delivered = deferred<GraphConfigurationResult>(), f = fixture();
    f.update.mockImplementationOnce(async request => ask(request));
    f.update.mockImplementationOnce(() => delivered.promise);
    await f.entry.initialize();
    const question = await f.app.sdk.submitNodeConfiguration(f.input);
    expect(question.outcome).toBe("confirmation_required");
    const original = f.update.mock.calls[0]![0];
    expect(original).not.toHaveProperty("confirm_derivation");
    const deriving = f.app.sdk.confirmDerivation(f.document.workflow_definition_id);
    await vi.waitFor(() => expect(f.update).toHaveBeenCalledTimes(2));
    const confirmed = f.update.mock.calls[1]![0];
    expect(confirmed.confirm_derivation).toBe(true);
    expect(confirmed.idempotency_key).not.toBe(original.idempotency_key);
    const selected = switchKind === "workflow" ? source() : f.document;
    if (switchKind === "workflow") f.app.observe(selected);
    const sessionId = crypto.randomUUID();
    f.app.select(selected.workflow_definition_id, sessionId);
    delivered.resolve(result(confirmed, target));
    await deriving;
    expect(f.app.selection.value).toEqual({ workflowId: selected.workflow_definition_id, sessionId });
    expect(f.app.current.value).toEqual(selected);
    expect(f.app.document(target)?.nodes[0].config.text).toBe("After");
    expect(f.app.document(f.document.workflow_definition_id)).toEqual(f.document);
    expect(f.app.directory.value.some(row => row.workflowId === target)).toBe(true);
  });
  it("accepts shared adjustable current after a session switch and preserves the newly selected session", async () => {
    const delivered = deferred<GraphConfigurationResult>(), f = fixture();
    f.update.mockImplementation(() => delivered.promise);
    await f.entry.initialize();
    const saving = f.app.sdk.submitNodeConfiguration(f.input);
    await vi.waitFor(() => expect(f.update).toHaveBeenCalledTimes(1));
    const sessionId = crypto.randomUUID();
    f.app.select(f.document.workflow_definition_id, sessionId);
    delivered.resolve(result(f.update.mock.calls[0][0], f.document.workflow_definition_id, true));
    await saving;
    expect(f.app.current.value?.nodes[0].config.text).toBe("After");
    expect(f.app.selection.value?.sessionId).toBe(sessionId);
    expect(f.app.current.value?.revision).toBe(f.document.revision);
  });
  it("locks only the unknown workflow, reopens its immutable evidence and reads the original receipt without replay", async () => {
    const f = fixture();
    f.update.mockRejectedValue(new WorkbenchApiError("unknown", "Response lost"));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "unknown" });
    const original = graphClone(f.update.mock.calls[0][0]);
    expect(f.entry.locked.value).toBe(true);
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "rejected" });
    const reopened = createWorkflowConfigurationApplication(f.ports), other = source();
    reopened.observe(other); reopened.select(other.workflow_definition_id);
    await reopened.forWorkflow(other.workflow_definition_id).initialize();
    expect(reopened.forWorkflow(other.workflow_definition_id).locked.value).toBe(false);
    const entry = reopened.forWorkflow(f.document.workflow_definition_id);
    await entry.initialize();
    expect(reopened.document(f.document.workflow_definition_id)).toBeNull();
    await entry.reconcile();
    expect(f.update).toHaveBeenCalledTimes(1);
    expect(f.readReceipt).toHaveBeenCalledExactlyOnceWith(original);
    expect(reopened.current.value).toEqual(other);
    expect(reopened.document(f.document.workflow_definition_id)?.nodes[0].config.text).toBe("After");
    expect(entry.locked.value).toBe(false);
    expect([...f.evidence.records.values()][0]).toMatchObject({ resolution: "verified", request: original });
  });
  it("exposes genuine CAS rejection and ends its evidence without applying the rejected edit", async () => {
    const f = fixture();
    f.update.mockRejectedValue(new WorkbenchApiError("rejected", "Current changed", 409, "stale_edit_sequence"));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ code: "stale_edit_sequence" });
    expect(f.entry.error.value).toBe("Current changed");
    expect(f.app.current.value).toEqual(f.document); expect(f.entry.locked.value).toBe(false);
    expect([...f.evidence.records.values()][0].resolution).toBe("verified");
  });
  it("keeps an idempotency conflict unresolved and never interprets it as rollback", async () => {
    const f = fixture();
    f.update.mockRejectedValue(new WorkbenchApiError("rejected", "Key differs", 409, "idempotency_conflict"));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ code: "idempotency_conflict" });
    expect(f.entry.locked.value).toBe(true); expect(f.evidence.ports.resolve).not.toHaveBeenCalled();
  });
  it("does not send a business request when evidence persistence definitively fails", async () => {
    const f = fixture();
    f.evidence.ports.write = vi.fn(async () => {
      throw new WorkbenchApiError("rejected", "Evidence capacity exceeded", 413, "request_evidence_capacity_exceeded");
    });
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ code: "request_evidence_capacity_exceeded" });
    expect(f.update).not.toHaveBeenCalled(); expect(f.app.current.value).toEqual(f.document);
    expect(f.entry.locked.value).toBe(false); expect(f.entry.error.value).toContain("配置未发送");
  });
  it("keeps uncertain evidence writes locked and only checks durable original evidence and original receipts", async () => {
    const f = fixture(), write = f.evidence.ports.write;
    f.evidence.ports.write = vi.fn(async request => {
      await write(request); throw new WorkbenchApiError("unknown", "Evidence acknowledgement lost");
    });
    f.readReceipt.mockRejectedValue(new WorkbenchApiError("unknown", "No business receipt"));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "unknown" });
    expect(f.entry.locked.value).toBe(true); expect(f.update).not.toHaveBeenCalled();
    await expect(f.entry.reconcile()).rejects.toMatchObject({ kind: "unknown" });
    expect(f.readReceipt).toHaveBeenCalledTimes(1); expect(f.update).not.toHaveBeenCalled();
    expect(f.entry.locked.value).toBe(true);
  });
  it("retains actual success and its lock when evidence resolution fails, then reconciles without another update", async () => {
    const f = fixture(), resolve = f.evidence.ports.resolve;
    f.evidence.ports.resolve = vi.fn(async () => { throw new Error("Evidence disk unavailable"); });
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toThrow("Evidence disk unavailable");
    expect(f.app.current.value?.nodes[0].config.text).toBe("After");
    expect(f.entry.lastResult.value?.outcome).toBe("saved");
    expect(f.entry.error.value).toContain("配置已正式保存"); expect(f.entry.locked.value).toBe(true);
    f.evidence.ports.resolve = resolve;
    await f.entry.reconcile();
    expect(f.update).toHaveBeenCalledTimes(1); expect(f.readReceipt).toHaveBeenCalledTimes(1);
    expect(f.entry.locked.value).toBe(false);
  });
  it("preserves original evidence after a mismatched result or a rejected receipt read", async () => {
    const f = fixture();
    f.update.mockImplementation(async request => {
      const value = result(request);
      value.document.nodes[0].config.text = "Different response";
      return value;
    });
    f.readReceipt.mockRejectedValue(new WorkbenchApiError("rejected", "Receipt missing", 404));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ code: "graph_configuration_receipt_invalid" });
    const pending = graphClone(f.entry.pending.value);
    await expect(f.entry.reconcile()).rejects.toMatchObject({ kind: "rejected" });
    expect(f.entry.pending.value).toEqual(pending); expect(f.entry.locked.value).toBe(true);
    expect(f.app.current.value).toEqual(f.document); expect(f.evidence.ports.resolve).not.toHaveBeenCalled();
  });
  it("does not replace a more recent formal current with an older reconciled success", async () => {
    const f = fixture();
    f.update.mockRejectedValue(new WorkbenchApiError("unknown", "Response lost"));
    await f.entry.initialize();
    await expect(f.app.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "unknown" });
    const newer = { ...graphClone(f.document), edit_sequence: 8, revision: 3, name: "Later official name" };
    f.app.observe(newer);
    await f.entry.reconcile();
    expect(f.app.current.value).toEqual(newer);
    expect(f.entry.lastResult.value?.edit_sequence).toBe(5);
    expect(f.entry.locked.value).toBe(false);
  });
  it("retains a validated write fact when local formal-view acceptance reports conflicting data", async () => {
    const delivered = deferred<GraphConfigurationResult>(), f = fixture();
    f.update.mockImplementation(() => delivered.promise);
    await f.entry.initialize();
    const saving = f.app.sdk.submitNodeConfiguration(f.input);
    await vi.waitFor(() => expect(f.update).toHaveBeenCalledTimes(1));
    f.app.observe({ ...graphClone(f.document), revision: 3, edit_sequence: 5, name: "Conflicting observation" });
    delivered.resolve(result(f.update.mock.calls[0][0]));
    await expect(saving).rejects.toMatchObject({ code: "graph_configuration_basis_mismatch" });
    expect(f.entry.lastResult.value?.document.nodes[0].config.text).toBe("After");
    expect(f.entry.error.value).toContain("配置正式结果已确认");
    expect(f.entry.locked.value).toBe(true); expect(f.evidence.ports.resolve).not.toHaveBeenCalled();
  });
  it("permits an explicit evidence-read retry without dispatching anything during initialization", async () => {
    const f = fixture(), read = f.evidence.ports.read;
    f.evidence.ports.read = vi.fn(async () => { throw new Error("Evidence read unavailable"); });
    await expect(f.entry.initialize()).rejects.toThrow("Evidence read unavailable");
    expect(f.entry.locked.value).toBe(true); expect(f.update).not.toHaveBeenCalled();
    f.evidence.ports.read = read;
    await f.entry.initialize();
    expect(f.entry.locked.value).toBe(false); expect(f.update).not.toHaveBeenCalled();
  });
  it.each(["saved", "response-lost"] as const)(
    "uses default evidence and formal adapters with SDK2 for %s, never browser drafts", async mode => {
    const document = source(), evidence = memoryRequestEvidence(), sent: GraphConfigurationUpdateRequest[] = [];
    const getItem = vi.fn(), setItem = vi.fn();
    vi.stubGlobal("localStorage", { getItem, setItem });
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      const command = JSON.parse(String(init.body)), parameters = command.parameters;
      if (command.operation === "request-evidence.read")
        return new Response(JSON.stringify(await evidence.ports.read(parameters.owner_kind, parameters.owner_id)));
      if (command.operation === "request-evidence.write")
        return requestEvidenceCommandResponse(command.operation, await evidence.ports.write(parameters));
      if (command.operation === "request-evidence.resolve")
        return requestEvidenceCommandResponse(command.operation, await evidence.ports.resolve(parameters));
      expect(command.operation).toBe("definition.update");
      if (path === "/api/graph/commands") {
        sent.push(graphClone(parameters));
        if (mode === "response-lost") throw new Error("Controlled response loss");
        return response(parameters, result(parameters));
      }
      expect(path).toBe("/api/graph/receipts/read");
      expect(parameters).toEqual(sent[0]);
      return response(parameters, result(parameters), true);
    }));
    const ports = { extensions: () => [{ declaration: declaration(), configurationFields: ["text"] }] };
    const first = createWorkflowConfigurationApplication(ports);
    first.observe(document); first.select(document.workflow_definition_id);
    await first.forWorkflow(document.workflow_definition_id).initialize();
    if (mode === "response-lost")
      await expect(first.sdk.submitNodeConfiguration(submission(document))).rejects.toMatchObject({ kind: "unknown" });
    else await expect(first.sdk.submitNodeConfiguration(submission(document))).resolves.toMatchObject({ outcome: "saved" });
    const reopened = createWorkflowConfigurationApplication(ports);
    reopened.observe(document); reopened.select(document.workflow_definition_id);
    if (mode === "response-lost") await reopened.forWorkflow(document.workflow_definition_id).reconcile();
    else {
      await reopened.forWorkflow(document.workflow_definition_id).initialize();
      reopened.observe(first.current.value!);
    }
    expect(sent).toHaveLength(1);
    expect(reopened.current.value?.nodes[0].config.text).toBe("After");
    expect([...evidence.records.values()][0]).toMatchObject({ resolution: "verified", request: sent[0] });
    expect(getItem).not.toHaveBeenCalled(); expect(setItem).not.toHaveBeenCalled();
  });
});
