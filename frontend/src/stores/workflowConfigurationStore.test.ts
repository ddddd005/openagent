import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { graphClone, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock, frontendSession } from "../testUtils/frontendFixture";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import type { GraphRequestAbandonRequest } from "../adapters/workflowConfigurationApi";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

const nodeType: GraphNodeType = {
  component_id: "sample.node", component_version: "1", display_name: "Sample", category: "Test",
  default_config: { text: "before" }, config_schema: { type: "object", properties: { text: { type: "string" } } },
  inputs: [], outputs: [], executable: true, is_output: false,
};
const extension: FrontendExtension = {
  schema_version: 3, host_protocol_version: 2, extension_id: "sample.node-fields", kind: "field-editor",
  entrypoint: "sample.node-fields", component_id: "sample.node", component_version: "1",
  package_id: "sample", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "node-fields",
    target: { component_id: "sample.node", component_version: "1" } },
};
function accepted(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  const revision = request.expected_edit_sequence === 0 ? 1 : request.document.revision + 1;
  const editSequence = request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), revision, edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
    edit_sequence: editSequence, source_has_effective_session: false, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

const promptType: GraphNodeType = {
  component_id: "prompts.item", component_version: "3", display_name: "Prompt", category: "Prompt",
  default_config: { id: crypto.randomUUID(), text: "before" },
  config_schema: { type: "object", properties: { id: { type: "string" }, text: { type: "string" } } },
  inputs: [], outputs: [], executable: true, is_output: false,
  adjustable_configuration: { schema_version: 1, policy: "prompt",
    fields: ["text", "presentation", "metadata", "lifecycle", "compaction"] },
};
function configurationResult(request: GraphConfigurationUpdateRequest, options: {
  outcome: GraphConfigurationResult["outcome"];
  categories: GraphConfigurationResult["changed_categories"];
  target?: string;
  hasSession?: boolean;
}): GraphConfigurationResult {
  const derived = options.outcome === "derived";
  const target = options.target ?? (derived ? crypto.randomUUID() : request.workflow_definition_id);
  const revision = derived ? 1 : request.document.revision
    + Number(options.outcome === "saved" && options.categories.includes("fixed"));
  const editSequence = options.outcome === "confirmation_required"
    ? request.expected_edit_sequence : derived ? 1 : request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), workflow_definition_id: target, revision,
    edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: options.outcome,
    source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: target,
    definition_revision: revision, edit_sequence: editSequence,
    source_has_effective_session: options.hasSession ?? false, changed_categories: options.categories,
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}
function idleSession(document: ReturnType<typeof promptDocument>, sessionId: string): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sessionId,
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    nodes: document.nodes.map(node => ({ node_binding_id: node.node_binding_id, label: node.title,
      status: "idle", run_id: null, revision: null, outputs: {}, diagnostic: null })),
    chains: [], outputs: [], data: { revision: 0, values: {} } };
}
function promptDocument(workflowId: string, mode: "fixed" | "adjustable" = "fixed") {
  return {
    schema_version: 3 as const, workflow_definition_id: workflowId, revision: 4, edit_sequence: 7,
    name: "Prompt configuration",
    nodes: [{ node_binding_id: crypto.randomUUID(), component_id: "prompts.item", component_version: "3",
      title: "Prompt", position: { x: 0, y: 0 }, config: graphClone(promptType.default_config),
      configuration_policy: { schema_version: 1 as const, prompt_mode: mode } }],
    edges: [], object_bindings: [],
  };
}
function port(port_id: string, data_type: string, data_schema_version = 1, required = true) {
  return { port_id, data_type, data_schema_version, required, multiple: false };
}
const currentNodeTypes: GraphNodeType[] = [
  {
    component_id: "agents.execute", component_version: "4", display_name: "Agent execution", category: "Agent",
    default_config: {}, config_schema: { type: "object", properties: {}, additionalProperties: false },
    inputs: [port("prompt", "PROMPT", 6), port("model", "MODEL_BINDING", 2),
      port("compaction_policy", "CONTEXT_COMPACTION_POLICY", 1, false)],
    outputs: [port("result", "TEXT", 2), port("context", "AGENT_CONTEXT_UPDATE")],
    capabilities: ["models:call", "artifacts:read"], input_storage: "references",
    executable: true, is_output: false,
  },
  {
    component_id: "context.output", component_version: "4", display_name: "Native context output", category: "Context",
    default_config: { object_key: "context", agent_node_id: "00000000-0000-4000-8000-000000000000" },
    config_schema: { type: "object", properties: { object_key: { type: "string" }, agent_node_id: { type: "string" } },
      required: ["object_key", "agent_node_id"], additionalProperties: false },
    inputs: [], outputs: [port("output", "CONTEXT_VIEW", 4)],
    capabilities: ["objects:read", "context:read"], input_storage: "references",
    object_accesses: [{ config_field: "object_key", type_id: "workflow.effective-context",
      schema_version: 4, access: "read", multiple: false }],
    config_node_references: [{ config_field: "agent_node_id", multiple: false,
      target_types: [{ component_id: "agents.execute", component_version: "4" },
        { component_id: "agents.execute", component_version: "8" }] }],
    executable: true, is_output: false,
  },
  {
    component_id: "tools.output", component_version: "1", display_name: "Output", category: "Tools",
    default_config: { mode: "text" },
    config_schema: { type: "object", properties: { mode: { enum: ["text", "prompt", "json"] } },
      required: ["mode"], additionalProperties: false },
    inputs: [], outputs: [], port_modes: Object.fromEntries(
      [["text", "TEXT"], ["prompt", "PROMPT"], ["json", "JSON"]].map(([mode, dataType]) => [mode, {
        inputs: [port("input", dataType!, 2)],
        outputs: [port("output", dataType!, 2)],
      }])),
    input_storage: "references", executable: true, is_output: true,
  },
  {
    component_id: "tools.text", component_version: "1", display_name: "Text", category: "Tools",
    default_config: { text: "" },
    config_schema: { type: "object", properties: { text: { type: "string" } },
      required: ["text"], additionalProperties: false },
    inputs: [], outputs: [port("output", "TEXT", 2)],
    input_storage: "references", executable: true, is_output: false,
  },
];

let activePinia: Pinia | null = null;
beforeEach(() => { activePinia = createPinia(); setActivePinia(activePinia); });
afterEach(() => {
  if (activePinia) disposePinia(activePinia);
  activePinia = null;
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("workflow configuration store wiring", () => {
  it("creates and edits the active graph through one evidence-protected schema 3 application", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    const requests: GraphConfigurationUpdateRequest[] = [];
    let pending: ReturnType<typeof deferred<GraphConfigurationResult>> | null = null;
    const application = createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [{ declaration: extension, configurationFields: ["text"] }],
      update: vi.fn(async request => {
        requests.push(graphClone(request));
        return pending?.promise ?? accepted(request);
      }),
      readReceipt: vi.fn(async request => accepted(request)),
    });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    graph.packageLock = [{ package_id: "sample", version: "1.0.0" }];

    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.entries[workflowId]?.saved_revision).toBe(1));
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 20, y: 30 }))!;
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[0]).toMatchObject({
      workflow_definition_id: workflowId, expected_edit_sequence: 0, document: { schema_version: 3 },
    });
    expect(requests[1]!.document.nodes[0]).toMatchObject({
      node_binding_id: nodeId, configuration_policy: { schema_version: 1, prompt_mode: "fixed" },
    });

    const specialized = await graph.patchNodeConfiguration({
      workflowId, nodeId, componentId: "sample.node", componentVersion: "1",
      expectedConfig: { text: "before" }, patch: { text: "via SDK2" },
      extensionId: extension.extension_id, lifecycle: "workbench",
    });
    expect(specialized).toMatchObject({ workflowId, nodeId, outcome: "saved" });
    expect(requests[2]!.document.nodes[0]!.config).toEqual({ text: "via SDK2" });
    expect(requests[2]!.workflow_definition_id).toBe(requests[1]!.workflow_definition_id);

    pending = deferred<GraphConfigurationResult>();
    const saving = graph.patchNode(nodeId, { config: { text: "ordinary edit" } });
    expect(graph.editorLocked).toBe(true);
    // A second edit entry must be refused with a reason, not silently return null.
    const blocked = await graph.patchNode(nodeId, { config: { text: "blocked while submitting" } });
    expect(blocked).toMatchObject({ outcome: "failed" });
    expect((blocked as { error: Error }).error.message).toContain("工作流配置正在提交");
    expect(useWorkbenchNoticesStore().notices).toEqual([
      expect.objectContaining({ kind: "rejected", reason: expect.stringContaining("工作流配置正在提交") }),
    ]);
    const archive = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", archive);
    // A management write must not interleave with the configuration submit,
    // even though the buttons are only disabled in the UI.
    expect(await graph.manageWorkflow(workflowId, "archive")).toBe(false);
    expect(archive).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(requests).toHaveLength(4));
    expect(graph.configurationPendingRequests()).toEqual([]);
    expect(application.forWorkflow(workflowId).progress.value).toBe("request-in-flight");
    pending.resolve(accepted(requests[3]!));
    expect(await saving).toMatchObject({ outcome: "accepted" });
    await vi.waitFor(() => expect(graph.document?.nodes[0]?.config.text).toBe("ordinary edit"));
    expect(graph.active?.saved_document?.nodes[0]?.config.text).toBe("ordinary edit");
    expect(graph.locked).toBe(false);
    pending = null;
    for (let step = 1; step <= 3; step++) {
      expect(await graph.moveNodes([{ id: nodeId, position: { x: 20 + step * 10, y: 30 } }]))
        .toMatchObject({ outcome: "accepted" });
      expect(graph.locked).toBe(false);
      expect(requests).toHaveLength(4 + step);
      expect(graph.document?.nodes[0]?.position).toEqual({ x: 20 + step * 10, y: 30 });
      expect(graph.configurationPendingRequests()).toEqual([]);
    }
    // A save accepted before evidence closing failed still projects the real
    // written body; the lock and original evidence remain until reconcile.
    const resolveEvidence = evidence.ports.resolve;
    evidence.ports.resolve = vi.fn(async () => { throw new Error("Evidence disk unavailable"); });
    expect(await graph.patchNode(nodeId, { config: { text: "evidence failure" } }))
      .toMatchObject({ outcome: "failed" });
    await vi.waitFor(() => expect(graph.document?.nodes[0]?.config.text).toBe("evidence failure"));
    expect(graph.editorLocked).toBe(true);
    expect(graph.configurationPendingRequests()).toHaveLength(1);
    expect(application.forWorkflow(workflowId).progress.value).toBe("closure-failed");
    evidence.ports.resolve = resolveEvidence;
    await graph.reconcileConfiguration(workflowId);
    expect(graph.configurationPendingRequests()).toEqual([]);
    expect(graph.document?.nodes[0]?.config.text).toBe("evidence failure");
    expect([...evidence.records.values()].every(row => row.operation === "definition.update"
      && row.owner_kind === "workflow-configuration" && row.resolution === "verified")).toBe(true);
  });

  it("changes selection, document and removal only after the awaited formal result", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    const requests: GraphConfigurationUpdateRequest[] = [];
    let gate: ReturnType<typeof deferred<GraphConfigurationResult>> | null = null;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
      requests.push(graphClone(request));
      return gate?.promise ?? accepted(request);
    });
    const application = createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)),
    });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];

    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;

    // A move that is still in flight must not be applied to the formal document yet.
    gate = deferred<GraphConfigurationResult>();
    const moving = graph.moveNodes([{ id: nodeId, position: { x: 5, y: 5 } }]);
    expect(graph.document!.nodes[0]!.position).toEqual({ x: 0, y: 0 });
    expect(graph.locked).toBe(true);
    // A second edit entry is refused with a reason instead of a silent null.
    const overlapping = await graph.moveNodes([{ id: nodeId, position: { x: 9, y: 9 } }]);
    expect(overlapping).toMatchObject({ outcome: "failed" });
    expect((overlapping as { error: Error }).error.message).toContain("工作流配置正在提交");
    // An add entry started while the lock was held neither submits nor changes selection.
    expect(graph.selectedNodeIds).toEqual([nodeId]);
    const pendingAdd = graph.addNode("sample.node@1", { x: 10, y: 0 });
    expect(graph.selectedNodeIds).toEqual([nodeId]);
    expect(await pendingAdd).toBeNull();
    expect(graph.document!.nodes).toHaveLength(1);
    gate.resolve(accepted(requests.at(-1)!));
    expect(await moving).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes[0]!.position).toEqual({ x: 5, y: 5 });

    // An accepted add selects the node, but only after the formal acceptance.
    gate = null;
    const added = await graph.addNode("sample.node@1", { x: 30, y: 0 });
    expect(added).toBeTruthy();
    expect(graph.selectedNodeIds).toEqual([added]);

    // Removal keeps the node and the selection until the result is accepted.
    gate = deferred<GraphConfigurationResult>();
    const beforeRemoval = requests.length;
    const removing = graph.removeSelection();
    await vi.waitFor(() => expect(requests.length).toBe(beforeRemoval + 1));
    expect(graph.selectedNodeIds).toEqual([added]);
    expect(graph.document!.nodes).toHaveLength(2);
    gate.resolve(accepted(requests.at(-1)!));
    expect(await removing).toMatchObject({ outcome: "accepted" });
    expect(graph.selectedNodeIds).toEqual([]);
    expect(graph.document!.nodes).toHaveLength(1);

    // A definite rejection does not project the rejected body or advance history.
    gate = null;
    const before = graphClone(graph.document!), history = graphClone(graph.history[workflowId]);
    update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "Configuration CAS conflict", 409, "stale_edit_sequence"));
    expect(await graph.moveNodes([{ id: nodeId, position: { x: 99, y: 99 } }]))
      .toMatchObject({ outcome: "failed" });
    expect(graph.document).toEqual(before);
    expect(graph.history[workflowId]).toEqual(history);
    expect(graph.locked).toBe(false);
  });

  it("keeps a definite refusal as a persistent failed proposal until handled", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request));
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)) });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;

    update.mockRejectedValueOnce(new WorkbenchApiError("rejected",
      "工作流正在修改，请等待当前请求结束后按原基线重试", 409, "workflow_modification_busy"));
    expect(await graph.patchNode(nodeId, { config: { text: "rejected proposal" } }))
      .toMatchObject({ outcome: "failed" });
    const rejected = graph.activeRejectedEdit!;
    expect(rejected).toMatchObject({ code: "workflow_modification_busy", kind: "rejected" });
    expect(rejected.reason).toContain("工作流正在修改");
    expect(rejected.request.document.nodes[0]!.config.text).toBe("rejected proposal");
    expect(rejected.request.expected_edit_sequence).toBe(2);
    expect(graph.document!.nodes[0]!.config.text).toBe("before");
    expect(graph.locked).toBe(false);

    // A new edit uses the current formal baseline; acceptance clears the old failure.
    expect(await graph.patchNode(nodeId, { config: { text: "second proposal" } }))
      .toMatchObject({ outcome: "accepted" });
    expect(graph.activeRejectedEdit).toBeNull();
    expect(graph.document!.nodes[0]!.config.text).toBe("second proposal");

    // Dismissal closes a retained failure without resubmitting anything.
    update.mockRejectedValueOnce(new WorkbenchApiError("rejected", "再次被拒绝", 409, "workflow_modification_busy"));
    await graph.patchNode(nodeId, { config: { text: "third proposal" } });
    expect(graph.activeRejectedEdit).not.toBeNull();
    const calls = update.mock.calls.length;
    graph.dismissRejectedEdit();
    expect(graph.activeRejectedEdit).toBeNull();
    expect(update.mock.calls.length).toBe(calls);
  });

  it("terminates an unknown request through arbitration without resending the update", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    let fail = false;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
      if (!fail) return accepted(request);
      throw new WorkbenchApiError("unknown", "Configuration response lost");
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)) });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    fail = true;
    expect(await graph.patchNode(nodeId, { config: { text: "lost edit" } }))
      .toMatchObject({ outcome: "failed" });
    expect(graph.locked).toBe(true);
    const pendingKey = application.forWorkflow(workflowId).pending.value!.idempotency_key;
    const record = [...evidence.records.values()].find(row => row.idempotency_key === pendingKey)!;
    expect(record).toBeDefined();
    const arbitration: GraphRequestAbandonRequest[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe("/api/graph/commands");
      const body = JSON.parse(String(init.body));
      expect(body.operation).toBe("request.abandon");
      arbitration.push(body.parameters);
      return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result: {
        schema_version: 1, kind: "workflow.request-arbitration", outcome: "abandoned",
        operation: "definition.update", idempotency_key: body.parameters.target_idempotency_key,
        request_digest: body.parameters.target_request_digest, workflow_definition_id: workflowId,
        evidence_resolution: "archived", result: null } }));
    }));
    expect(await graph.abandonConfiguration(workflowId)).toBe(true);
    expect(update).toHaveBeenCalledTimes(3);
    expect(arbitration).toHaveLength(1);
    expect(arbitration[0]).toMatchObject({ target_operation: "definition.update",
      target_idempotency_key: record.idempotency_key, target_request_digest: record.request_digest });
    expect(graph.locked).toBe(false);
    expect(graph.configurationPendingRequests()).toEqual([]);
    expect(application.forWorkflow(workflowId).progress.value).toBe("abandoned");
    expect(useWorkbenchNoticesStore().notices.some(row => row.title === "原请求已终止")).toBe(true);
  });

  it("accepts a committed arbitration whose evidence was already archived (R7)", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    let fail = false;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
      if (!fail) return accepted(request);
      throw new WorkbenchApiError("unknown", "Configuration response lost");
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)) });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    fail = true;
    await graph.patchNode(nodeId, { config: { text: "lost edit" } });
    const pendingKey = application.forWorkflow(workflowId).pending.value!.idempotency_key;
    const record = [...evidence.records.values()].find(row => row.idempotency_key === pendingKey)!;
    const committed = accepted(record.request as unknown as GraphConfigurationUpdateRequest);
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      expect(path).toBe("/api/graph/commands"); expect(body.operation).toBe("request.abandon");
      return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result: {
        schema_version: 1, kind: "workflow.request-arbitration", outcome: "committed",
        operation: "definition.update", idempotency_key: body.parameters.target_idempotency_key,
        request_digest: body.parameters.target_request_digest, workflow_definition_id: workflowId,
        evidence_resolution: "archived", result: committed } }));
    }));
    const abandonResult = await graph.abandonConfiguration(workflowId);
    expect(abandonResult).toBe(true);
    expect(update).toHaveBeenCalledTimes(3);
    expect(graph.locked).toBe(false);
    expect(graph.document!.nodes[0]!.config.text).toBe("lost edit");
    expect(application.forWorkflow(workflowId).progress.value).toBe("completed");
  });

  it("keeps the request blocked when arbitration is unavailable", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    let fail = false;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
      if (!fail) return accepted(request);
      throw new WorkbenchApiError("unknown", "Configuration response lost");
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)) });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    fail = true;
    await graph.patchNode(nodeId, { config: { text: "lost edit" } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: { reason_code: "request_arbitration_unavailable" } }), { status: 409 })));
    expect(await graph.abandonConfiguration(workflowId)).toBe(false);
    expect(graph.locked).toBe(true);
    expect(graph.configurationPendingRequests()).toHaveLength(1);
    expect(useWorkbenchNoticesStore().notices.some(row => row.title === "结果未决")).toBe(true);
  });

  it("refuses a derivation confirm whose original proposal baseline changed", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), workflowId = await graph.createWorkflow();
    const document = promptDocument(workflowId), sessionId = crypto.randomUUID();
    graph.catalog = [graphClone(promptType)];
    graph.entries[workflowId]!.document = graphClone(document);
    graph.entries[workflowId]!.saved_document = graphClone(document);
    graph.entries[workflowId]!.saved_revision = document.revision;
    graph.entries[workflowId]!.session_id = sessionId;
    graph.views[sessionId] = idleSession(document, sessionId);
    workspace.markWorkflowSaved(workflowId, document.name);
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) =>
      configurationResult(request, { outcome: "confirmation_required", categories: ["fixed"], hasSession: true }));
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update });
    graph.installWorkflowConfigurationApplication(application, { seed: true });
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    expect(await graph.patchNodeConfigurationPolicy(document.nodes[0]!.node_binding_id, "adjustable"))
      .toMatchObject({ outcome: "confirmation-required" });
    const acceptedDocument = application.accepted(workflowId)!;
    const newer = { ...acceptedDocument, edit_sequence: acceptedDocument.edit_sequence + 1,
      revision: acceptedDocument.revision + 1 };
    application.observe(newer);
    expect(await graph.confirmDerivation()).toBe(false);
    expect(graph.activeDerivationAsk).toBeNull();
    expect(update).toHaveBeenCalledTimes(1);
    expect(useWorkbenchNoticesStore().notices.some(row =>
      row.reason.includes("基線") || row.reason.includes("基线"))).toBe(true);
  });

  it("re-reads the formal current after a closure failure without resubmitting", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request));
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update,
      readReceipt: vi.fn(async (request: GraphConfigurationUpdateRequest) => accepted(request)) });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    const resolve = evidence.ports.resolve;
    evidence.ports.resolve = vi.fn(async () => { throw new Error("Evidence disk unavailable"); });
    expect(await graph.patchNode(nodeId, { config: { text: "confirmed but not closed" } }))
      .toMatchObject({ outcome: "failed" });
    expect(application.forWorkflow(workflowId).progress.value).toBe("closure-failed");
    expect(graph.locked).toBe(true);
    const updateCalls = update.mock.calls.length;

    // Read-only formal read repairs the local projection; it never resubmits the save.
    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe(`/api/graph/definitions/${workflowId}`);
      return new Response(JSON.stringify(application.accepted(workflowId)));
    }));
    expect(await graph.reloadAcceptedConfiguration(workflowId)).toBe(true);
    expect(update.mock.calls.length).toBe(updateCalls);
    // The original evidence is still open: editing stays blocked until it closes.
    expect(graph.locked).toBe(true);
    expect(graph.configurationPendingRequests()).toHaveLength(1);

    evidence.ports.resolve = resolve;
    expect(await graph.reconcileConfiguration(workflowId)).toBe(true);
    expect(graph.locked).toBe(false);
    expect(graph.configurationPendingRequests()).toEqual([]);
    expect(update.mock.calls.length).toBe(updateCalls);
    expect(graph.document!.nodes[0]!.config.text).toBe("confirmed but not closed");
  });

  it.each(["archive", "delete"] as const)(
    "rejects a direct definition %s while a configuration request is unknown", async action => {
      const evidence = memoryRequestEvidence();
      vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
      vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
      vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
      const graph = useWorkflowGraphStore();
      let calls = 0;
      const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
        calls++;
        if (calls === 1) return accepted(request);
        throw new WorkbenchApiError("unknown", "Configuration response lost");
      });
      const application = createWorkflowConfigurationApplication({
        evidence: evidence.ports, extensions: () => [], update,
      });
      graph.installWorkflowConfigurationApplication(application);
      graph.catalog = [graphClone(nodeType)];
      const workflowId = await graph.createWorkflow();
      await vi.waitFor(() => expect(graph.entries[workflowId]?.saved_revision).toBe(1));
      await vi.waitFor(() => expect(graph.locked).toBe(false));
      expect(await graph.renameWorkflow("unknown configuration")).toMatchObject({ outcome: "failed" });
      await vi.waitFor(() => expect(application.forWorkflow(workflowId).pending.value).not.toBeNull());
      expect(application.forWorkflow(workflowId).locked.value).toBe(true);
      const records = evidence.records.size;
      const fetch = vi.fn(async () => new Response("{}"));
      vi.stubGlobal("fetch", fetch);

      await expect(graph.management.commands.manageDefinition({ workflowId }, action))
        .rejects.toMatchObject({ code: "graph_configuration_pending" });
      expect(fetch).not.toHaveBeenCalled();
      expect(evidence.records.size).toBe(records);
      expect(graph.entries[workflowId]).toBeDefined();
      expect(application.forWorkflow(workflowId).pending.value).not.toBeNull();
    });

  it("rejects a direct session delete while a configuration request is unknown", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    let calls = 0;
    const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
      calls++;
      if (calls === 1) return accepted(request);
      throw new WorkbenchApiError("unknown", "Configuration response lost");
    });
    const application = createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [], update,
    });
    graph.installWorkflowConfigurationApplication(application);
    graph.catalog = [graphClone(nodeType)];
    const workflowId = await graph.createWorkflow();
    await vi.waitFor(() => expect(graph.entries[workflowId]?.saved_revision).toBe(1));
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    expect(await graph.renameWorkflow("unknown configuration")).toMatchObject({ outcome: "failed" });
    await vi.waitFor(() => expect(application.forWorkflow(workflowId).pending.value).not.toBeNull());

    const sessionId = crypto.randomUUID();
    const view = { schema_version: 2 as const, execution_model: "graph" as const,
      workflow_session_id: sessionId, workflow_definition_id: workflowId, definition_revision: 1,
      revision: 1, data_revision: 0, head_revision: 1, status: "idle" as const, can_submit: true,
      nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} } };
    const paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async path => {
      paths.push(path);
      return new Response(JSON.stringify(view));
    }));
    const records = evidence.records.size;
    await expect(graph.management.commands.manageSession({ workflowId }, sessionId, "delete"))
      .rejects.toMatchObject({ code: "graph_configuration_pending" });
    expect(paths).toEqual([`/api/graph/sessions/${sessionId}`]);
    expect(evidence.records.size).toBe(records);
    expect(graph.entries[workflowId]).toBeDefined();
  });

  it("adds current context, Agent and output contracts through formal schema 3 without creating a session or item value", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore();
    const pinnedLock = [
      { package_id: "workflow.content", version: "1.0.0" },
      { package_id: "workflow.tools", version: "0.9.0" },
    ];
    const executionLock = [
      { package_id: "workflow.content", version: "1.0.0" },
      { package_id: "workflow.tools", version: "1.0.0" },
      { package_id: "workflow.context", version: "1.0.0" },
      { package_id: "workflow.agents", version: "1.0.0" },
      { package_id: "workflow.models", version: "1.0.0" },
      { package_id: "workflow.prompts", version: "1.0.0" },
    ];
    graph.executionPackageLock = graphClone(pinnedLock);
    graph.catalog = graphClone(currentNodeTypes);
    const requests: GraphConfigurationUpdateRequest[] = [];
    let storedDocument: GraphConfigurationResult["document"] | null = null;
    const application = createWorkflowConfigurationApplication({
      evidence: evidence.ports, extensions: () => [],
      update: vi.fn(async request => {
        requests.push(graphClone(request));
        const result = accepted(request);
        storedDocument = graphClone(result.document);
        return result;
      }),
      readReceipt: vi.fn(async request => accepted(request)),
    });
    graph.installWorkflowConfigurationApplication(application);

    const workflowId = await graph.createWorkflow();
    const waitForUpdate = async (count: number) => {
      await vi.waitFor(() => expect(requests).toHaveLength(count));
      await vi.waitFor(() => expect(graph.locked).toBe(false));
    };
    const add = async (component: string, index: number) => {
      const id = await graph.addNode(component, { x: index * 250, y: 0 });
      expect(id).toBeTruthy();
      return id!;
    };
    await waitForUpdate(1);
    expect(graph.document?.schema_version).toBe(3);
    expect(graph.entries[workflowId]!.session_id).toBeNull();

    graph.executionPackageLock = graphClone(executionLock);
    const agentId = await add("agents.execute@4", 1);
    await waitForUpdate(2);
    const contextId = await add("context.output@4", 2);
    await waitForUpdate(3);
    const outputId = await add("tools.output@1", 3);
    await waitForUpdate(4);
    const textId = await add("tools.text@1", 4);
    await waitForUpdate(5);
    expect(await graph.connect({ source_node_id: textId, source_port_id: "output",
      target_node_id: outputId, target_port_id: "input" })).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(6);
    const retainedEdge = graphClone(graph.document!.edges[0]!);
    // Q05-3: the edge targets the old "input" port which the new component lacks, so the
    // replacement is blocked (no submission) until the user disconnects it explicitly.
    expect(await graph.replaceNode(outputId, "agents.execute@4")).toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock).toMatchObject({ action: "replace-node",
      items: [expect.objectContaining({ kind: "edge-port", edgeId: retainedEdge.edge_id })] });
    graph.dismissEditBlock();
    graph.selectEdge(retainedEdge.edge_id);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(7);
    expect(await graph.replaceNode(outputId, "agents.execute@4")).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(8);

    expect(requests[0]!.document.package_lock).toEqual(pinnedLock);
    expect(requests.slice(1).every(request => request.document.schema_version === 3)).toBe(true);
    const expectedLock = [
      ...pinnedLock,
      ...executionLock.filter(item => !pinnedLock.some(existing => existing.package_id === item.package_id)),
    ];
    expect(requests[1]!.document.package_lock).toEqual(expectedLock);
    expect(requests[1]!.document.package_lock!.find(item => item.package_id === "workflow.tools")?.version)
      .toBe("0.9.0");
    for (const request of requests.slice(1))
      expect(request.document.nodes.every(node => node.configuration_policy
        && node.configuration_policy.prompt_mode === "fixed")).toBe(true);
    expect(requests[1]!.document.nodes.find(node => node.node_binding_id === agentId))
      .toMatchObject({ component_id: "agents.execute", component_version: "4" });
    expect(requests[2]!.document.nodes.find(node => node.node_binding_id === contextId))
      .toMatchObject({ component_id: "context.output", component_version: "4" });
    expect(requests[3]!.document.nodes.find(node => node.node_binding_id === outputId))
      .toMatchObject({ component_id: "tools.output", component_version: "1" });
    expect(requests[6]!.document.edges).toEqual([]);
    expect(requests[7]!.document.nodes.find(node => node.node_binding_id === outputId))
      .toMatchObject({ component_id: "agents.execute", component_version: "4",
        configuration_policy: { schema_version: 1, prompt_mode: "fixed" } });
    expect(requests[7]!.document.edges).toEqual([]);

    expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.entries[workflowId]!.session_id).toBeNull();
    expect(graph.session).toBeNull();
    expect(graph.views).toEqual({});

    const contextBinding = { object_key: "context", type_id: "workflow.effective-context", schema_version: 4,
      scope: "shared" as const, owner_node_id: null, readers: [contextId], writers: [] };
    expect(await graph.setControlDependencies(outputId, [agentId])).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(9);
    expect(requests[8]!.document.control_edges).toMatchObject([
      { source_node_id: agentId, target_node_id: outputId },
    ]);
    expect(await graph.setObjectBindings([contextBinding])).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(10);
    expect(requests[9]!.document.object_bindings).toEqual([contextBinding]);
    const event = { event_id: "test.output", schema_version: 1, display_name: "Test output",
      audience: "consumer" as const, target_node_ids: [outputId], payload_schema: { type: "object" as const } };
    expect(await graph.setEventBindings([event])).toMatchObject({ outcome: "accepted" });
    await waitForUpdate(11);
    expect(requests[10]!.document.event_bindings).toEqual([event]);
    expect(requests.slice(8).every(request => request.document.schema_version === 3)).toBe(true);
    expect(requests.slice(8).every(request => request.document.package_lock?.find(
      item => item.package_id === "workflow.tools")?.version === "0.9.0")).toBe(true);

    const countBeforeInvalidEdits = requests.length;
    const missingNodeId = crypto.randomUUID();
    expect(await graph.setControlDependencies(outputId, [missingNodeId])).toMatchObject({ outcome: "no-change" });
    expect(await graph.replaceNode(missingNodeId, "tools.output@1")).toMatchObject({ outcome: "no-change" });
    expect(await graph.setObjectBindings([{ ...contextBinding, readers: [missingNodeId] }])).toBeNull();
    expect(await graph.setObjectBindings([{ ...contextBinding, scope: "private", owner_node_id: contextId,
      readers: [contextId], writers: [outputId] }])).toBeNull();
    expect(await graph.setEventBindings([{ ...event, target_node_ids: [missingNodeId] }]))
      .toMatchObject({ outcome: "failed" });
    expect(requests).toHaveLength(countBeforeInvalidEdits);

    stubGraphApplicationFetch(async path => {
      if (path !== "/api/graph/definitions" || !storedDocument)
        throw new Error(`Unexpected configuration read: ${path}`);
      return new Response(JSON.stringify([{ document: storedDocument, archived: false }]));
    });
    expect(await graph.loadDefinitions()).toBe(true);
    expect(graph.document).toEqual(storedDocument);
    expect(application.document(workflowId)).toEqual(storedDocument);
    expect(graph.entries[workflowId]!.session_id).toBeNull();
    expect(graph.session).toBeNull();
    expect(graph.document!.object_bindings).toEqual([contextBinding]);
  });

  it.each(["component version 2", "another component", "schema version 2"] as const)(
    "does not expose the prompt policy control to %s", async invalidTarget => {
      const evidence = memoryRequestEvidence();
      vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
      vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
      vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
      const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
      const workflowId = await graph.createWorkflow();
      const document = promptDocument(workflowId);
      if (invalidTarget === "component version 2") document.nodes[0]!.component_version = "2";
      else if (invalidTarget === "another component") document.nodes[0]!.component_id = "prompts.group";
      const entryDocument = invalidTarget === "schema version 2"
        ? { ...graphClone(document), schema_version: 2 as const } : graphClone(document);
      graph.entries[workflowId]!.document = entryDocument;
      graph.entries[workflowId]!.saved_document = graphClone(entryDocument);
      graph.entries[workflowId]!.saved_revision = document.revision;
      workspace.markWorkflowSaved(workflowId, document.name);
      const requests: GraphConfigurationUpdateRequest[] = [];
      const application = createWorkflowConfigurationApplication({
        evidence: evidence.ports, extensions: () => [],
        update: vi.fn(async request => {
          requests.push(graphClone(request));
          return configurationResult(request, { outcome: "saved", categories: ["fixed"] });
        }),
      });
      graph.installWorkflowConfigurationApplication(application, { seed: true });
      await vi.waitFor(() => expect(graph.locked).toBe(false));

      expect(await graph.patchNodeConfigurationPolicy(document.nodes[0]!.node_binding_id, "adjustable"))
        .toMatchObject({ outcome: "no-change" });
      expect(graph.document!.nodes[0]!.configuration_policy).toEqual({ schema_version: 1, prompt_mode: "fixed" });
      expect(requests).toEqual([]);
    });

  it("asks to derive for a fixed policy change with an effective session and cancellation preserves source", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), workflowId = await graph.createWorkflow();
    const document = promptDocument(workflowId);
    const sessionId = crypto.randomUUID();
    graph.catalog = [graphClone(promptType)];
    graph.entries[workflowId]!.document = graphClone(document);
    graph.entries[workflowId]!.saved_document = graphClone(document);
    graph.entries[workflowId]!.saved_revision = document.revision;
    graph.entries[workflowId]!.session_id = sessionId;
    graph.views[sessionId] = idleSession(document, sessionId);
    workspace.markWorkflowSaved(workflowId, document.name);
    const requests: GraphConfigurationUpdateRequest[] = [];
    const update = vi.fn(async request => {
      requests.push(graphClone(request));
      return configurationResult(request, {
        outcome: "confirmation_required", categories: ["fixed"], hasSession: true,
      });
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update });
    graph.installWorkflowConfigurationApplication(application, { seed: true });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("window", { confirm });
    await vi.waitFor(() => expect(graph.locked).toBe(false));

    expect(await graph.patchNodeConfigurationPolicy(document.nodes[0]!.node_binding_id, "adjustable"))
      .toMatchObject({ outcome: "confirmation-required" });
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    expect(graph.editorLocked).toBe(false);

    // The ask is scoped to the workflow, not a global blocking confirm().
    expect(confirm).not.toHaveBeenCalled();
    expect(graph.activeDerivationAsk).not.toBeNull();
    expect(graph.declineDerivation()).toBe(true);
    expect(graph.activeDerivationAsk).toBeNull();
    expect(requests[0]!.document.nodes[0]!.configuration_policy).toEqual({
      schema_version: 1, prompt_mode: "adjustable",
    });
    expect(requests[0]).not.toHaveProperty("confirm_derivation");
    expect(application.forWorkflow(workflowId).confirmation.value).toBeNull();
    expect(workspace.activeWorkflowId).toBe(workflowId);
    expect(graph.entries[workflowId]!.document).toEqual(document);
    expect(application.document(workflowId)).toEqual(document);
    expect(Object.keys(graph.entries)).toEqual([workflowId]);
    expect([...evidence.records.values()][0]).toMatchObject({ resolution: "verified", request: requests[0] });
  });

  it("confirms exactly one derived prompt target without carrying session history and keeps the source", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), workflowId = await graph.createWorkflow();
    const document = promptDocument(workflowId);
    const sessionId = crypto.randomUUID(), targetId = crypto.randomUUID();
    graph.catalog = [graphClone(promptType)];
    graph.entries[workflowId]!.document = graphClone(document);
    graph.entries[workflowId]!.saved_document = graphClone(document);
    graph.entries[workflowId]!.saved_revision = document.revision;
    graph.entries[workflowId]!.session_id = sessionId;
    graph.views[sessionId] = idleSession(document, sessionId);
    workspace.markWorkflowSaved(workflowId, document.name);
    const requests: GraphConfigurationUpdateRequest[] = [];
    const update = vi.fn(async request => {
      requests.push(graphClone(request));
      return request.confirm_derivation
        ? configurationResult(request, { outcome: "derived", categories: ["fixed"], target: targetId, hasSession: true })
        : configurationResult(request, {
          outcome: "confirmation_required", categories: ["fixed"], hasSession: true,
        });
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update });
    graph.installWorkflowConfigurationApplication(application, { seed: true });
    const confirm = vi.fn(() => true);
    vi.stubGlobal("window", { confirm });
    await vi.waitFor(() => expect(graph.locked).toBe(false));

    expect(await graph.patchNodeConfigurationPolicy(document.nodes[0]!.node_binding_id, "adjustable"))
      .toMatchObject({ outcome: "confirmation-required" });
    expect(graph.activeDerivationAsk).not.toBeNull();
    expect(confirm).not.toHaveBeenCalled();
    expect(await graph.confirmDerivation()).toBe(true);
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(workspace.activeWorkflowId).toBe(targetId));

    expect(requests[0]).not.toHaveProperty("confirm_derivation");
    expect(requests[1]!.confirm_derivation).toBe(true);
    expect(requests[1]!.idempotency_key).not.toBe(requests[0]!.idempotency_key);
    expect(requests[0]!.document.nodes[0]!.configuration_policy.prompt_mode).toBe("adjustable");
    expect(requests[1]!.document.nodes[0]!.node_binding_id).toBe(document.nodes[0]!.node_binding_id);
    expect(graph.entries[workflowId]!.document).toEqual(document);
    expect(graph.entries[workflowId]!.session_id).toBe(sessionId);
    expect(graph.entries[targetId]!.document).toMatchObject({
      workflow_definition_id: targetId, revision: 1, edit_sequence: 1,
      nodes: [{ node_binding_id: document.nodes[0]!.node_binding_id,
        configuration_policy: { schema_version: 1, prompt_mode: "adjustable" } }],
    });
    expect(graph.entries[targetId]!.session_id).toBeNull();
    expect(graph.entries[targetId]!.session_document).toBeUndefined();
    expect(graph.history[targetId]).toEqual({ past: [], future: [] });
    expect(graph.sessionRows[targetId]).toBeUndefined();
    expect(graph.views[sessionId]).toEqual(idleSession(document, sessionId));
    expect(Object.keys(graph.entries).sort()).toEqual([workflowId, targetId].sort());
  });

  it.each([false, true])(
    "keeps provider duplication configuration-only when derivation is %s",
    async confirmDerivation => {
      const evidence = memoryRequestEvidence();
      vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
      vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
      vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
      const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
      graph.catalog = graphClone(frontendCatalog);
      graph.packageLock = graphClone(frontendProjectLock);
      graph.executionPackageLock = graphClone(frontendExecutionLock);
      const workflowId = await graph.createWorkflow();
      await vi.waitFor(() => expect(graph.locked).toBe(false));
      const providerId = (await graph.addNode("frontend.node@2", { x: 0, y: 0 }))!;
      expect(providerId).toBeTruthy();
      const session = frontendSession(workflowId);
      const sessionId = session.workflow_session_id;
      const original = graphClone(graph.entries[workflowId]!.document);
      const saved = {
        ...original, schema_version: 3 as const, edit_sequence: 1,
        nodes: original.nodes.map(node => ({ ...node,
          configuration_policy: { schema_version: 1 as const, prompt_mode: "fixed" as const } })),
      };
      graph.entries[workflowId]!.document = graphClone(saved);
      graph.entries[workflowId]!.saved_document = graphClone(saved);
      graph.entries[workflowId]!.saved_revision = 1;
      graph.entries[workflowId]!.session_id = sessionId;
      graph.views[sessionId] = session;
      workspace.markWorkflowSaved(workflowId, saved.name);
      const targetId = crypto.randomUUID();
      const requests: GraphConfigurationUpdateRequest[] = [];
      const update = vi.fn(async request => {
        requests.push(graphClone(request));
        return request.confirm_derivation
          ? configurationResult(request, { outcome: "derived", categories: ["fixed"],
            target: targetId, hasSession: true })
          : configurationResult(request, { outcome: "confirmation_required",
            categories: ["fixed"], hasSession: true });
      });
      const application = createWorkflowConfigurationApplication({
        evidence: evidence.ports, extensions: () => [], update,
        readReceipt: async request => configurationResult(request, {
          outcome: "confirmation_required", categories: ["fixed"], hasSession: true,
        }),
      });
      graph.installWorkflowConfigurationApplication(application, { seed: true });
      const confirm = vi.fn(() => confirmDerivation);
      vi.stubGlobal("window", { confirm });
      await vi.waitFor(() => expect(application.forWorkflow(workflowId).initialized.value).toBe(true));

      graph.selectedNodeIds = [providerId];
      expect(await graph.duplicateSelection()).toMatchObject({ outcome: "confirmation-required" });
      expect(update).toHaveBeenCalledTimes(1);
      expect(confirm).not.toHaveBeenCalled();
      expect(graph.activeDerivationAsk).not.toBeNull();
      if (confirmDerivation) {
        expect(await graph.confirmDerivation()).toBe(true);
        await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(2));
      } else {
        expect(graph.declineDerivation()).toBe(true);
        expect(graph.activeDerivationAsk).toBeNull();
      }
      await vi.waitFor(() => expect(graph.editorLocked).toBe(false));

      const registrationRequests = requests.map(request => request.document.object_bindings
        .filter(binding => binding.registrar_node_id
          && request.document.nodes.find(node => node.node_binding_id === binding.registrar_node_id)
            ?.component_id === "frontend.node"));
      expect(registrationRequests[0]).toHaveLength(2);
      expect(new Set(registrationRequests[0].map(binding => binding.object_key)).size).toBe(2);
      expect(registrationRequests[0].find(binding => binding.registrar_node_id === providerId)).toEqual(
        saved.object_bindings![0]);

      if (confirmDerivation) {
        expect(requests[1]!.confirm_derivation).toBe(true);
        expect(requests[1]!.document.object_bindings).toEqual(requests[0]!.document.object_bindings);
        expect(graph.entries[workflowId]!.document).toEqual(saved);
        expect(graph.entries[workflowId]!.session_id).toBe(sessionId);
        expect(graph.views[sessionId]).toEqual(session);
        expect(graph.entries[targetId]!.session_id).toBeNull();
        expect(graph.entries[targetId]!.session_document).toBeUndefined();
        expect(graph.entries[targetId]!.document.object_bindings).toEqual(requests[1]!.document.object_bindings);
        expect(graph.views[sessionId]).toEqual(session);
        expect(graph.views).toEqual({ [sessionId]: session });
      } else {
        expect(requests).toHaveLength(1);
        expect(graph.entries[workflowId]!.document).toEqual(saved);
        expect(graph.entries[workflowId]!.session_id).toBe(sessionId);
        expect(graph.entries[workflowId]!.saved_document).toEqual(saved);
        expect(Object.keys(graph.entries)).toEqual([workflowId]);
        expect(graph.views).toEqual({ [sessionId]: session });
        expect(application.forWorkflow(workflowId).confirmation.value).toBeNull();
        expect([...evidence.records.values()]).toMatchObject([{ resolution: "verified", request: requests[0] }]);
      }
    },
  );

  it("saves adjustable prompt text in place without advancing the fixed revision", async () => {
    const evidence = memoryRequestEvidence();
    vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
    vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), workflowId = await graph.createWorkflow();
    const document = promptDocument(workflowId, "adjustable"), sessionId = crypto.randomUUID();
    graph.catalog = [graphClone(promptType)];
    graph.entries[workflowId]!.document = graphClone(document);
    graph.entries[workflowId]!.saved_document = graphClone(document);
    graph.entries[workflowId]!.saved_revision = document.revision;
    graph.entries[workflowId]!.session_id = sessionId;
    graph.views[sessionId] = idleSession(document, sessionId);
    workspace.markWorkflowSaved(workflowId, document.name);
    const requests: GraphConfigurationUpdateRequest[] = [];
    const update = vi.fn(async request => {
      requests.push(graphClone(request));
      return configurationResult(request, { outcome: "saved", categories: ["adjustable"], hasSession: true });
    });
    const application = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [], update });
    graph.installWorkflowConfigurationApplication(application, { seed: true });
    await vi.waitFor(() => expect(graph.locked).toBe(false));

    expect(await graph.patchNode(document.nodes[0]!.node_binding_id, {
      config: { ...document.nodes[0]!.config, text: "updated prompt body" },
    })).toMatchObject({ outcome: "accepted" });
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(graph.entries[workflowId]!.document.nodes[0]!.config.text)
      .toBe("updated prompt body"));

    expect(requests[0]!.workflow_definition_id).toBe(workflowId);
    expect(requests[0]!.document.nodes[0]!.configuration_policy.prompt_mode).toBe("adjustable");
    expect(requests[0]!.document.nodes[0]!.config.text).toBe("updated prompt body");
    expect(graph.entries[workflowId]!.document.revision).toBe(document.revision);
    expect(graph.entries[workflowId]!.saved_revision).toBe(document.revision);
    expect(graph.entries[workflowId]!.session_id).toBe(sessionId);
    expect(workspace.activeWorkflowId).toBe(workflowId);
  });
});
