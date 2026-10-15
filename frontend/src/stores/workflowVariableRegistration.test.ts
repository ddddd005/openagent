import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { workflowManagementQueries } from "../application/workflowManagement";
import { graphClone, isGraphDocument, type GraphSession } from "../domain/workflowGraph";
import { variableRegistrationEvent } from "../domain/workflowVariableRegistration";
import { frontendExecutionLock, frontendSession } from "../testUtils/frontendFixture";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { unregisteredVariable, variableCatalog } from "../testUtils/variableFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function setup() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  graph.catalog = graphClone(variableCatalog); graph.executionPackageLock = graphClone(frontendExecutionLock);
  const id = graph.createWorkflow();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  return { graph, id, evidence };
}
async function savedSession() {
  const f = await setup(), nodeId = f.graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
  const binding = f.graph.document!.object_bindings![0]!;
  const session = frontendSession(f.id); session.status = "idle";
  session.objects = { [binding.object_key]: { revision_id: crypto.randomUUID(), revision: 1,
    type_id: binding.type_id, schema_version: binding.schema_version, deleted: false,
    value: graphClone(unregisteredVariable), binding: graphClone(binding) } };
  f.graph.entries[f.id]!.saved_revision = 1;
  f.graph.entries[f.id]!.saved_document = graphClone(f.graph.document!);
  f.graph.entries[f.id]!.session_id = session.workflow_session_id;
  f.graph.views[session.workflow_session_id] = session;
  vi.spyOn(workflowManagementQueries, "events").mockImplementation(async () => ({
    schema_version: 1, kind: "workflow.event-bindings", workflow_definition_id: f.id,
    definition_revision: 1, workflow_session_id: session.workflow_session_id, session_revision: 1,
    can_submit: true, bindings: [variableRegistrationEvent(nodeId)],
  }));
  return { ...f, nodeId, binding, session };
}
function sessionResponse(session: GraphSession, operation: string, parameters: Record<string, unknown>) {
  const fields = ["workflow_definition_id", "definition_revision", "workflow_session_id", "revision",
    "data_revision", "head_revision", "status"] as const;
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result: session,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
      idempotency_key: parameters.idempotency_key, request_sha256: "a".repeat(64), authority: "service_receipt",
      target: { session_id: session.workflow_session_id },
      accepted: Object.fromEntries(fields.map(field => [field, session[field]])) } }));
}

describe("unified variable provider registration", () => {
  it("creates independent own items and self ACL without a session or implicit consumer keys", async () => {
    const { graph, id } = await setup();
    const first = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    const second = graph.addNode("tools.variable-register@2", { x: 200, y: 0 })!;
    const assign = graph.addNode("tools.variable-assign@1", { x: 400, y: 0 })!;
    const read = graph.addNode("tools.variable-to-content@1", { x: 600, y: 0 })!;
    const replace = graph.addNode("tools.variable-replace-all@1", { x: 800, y: 0 })!;
    const items = graph.document!.object_bindings!;
    expect(items).toHaveLength(2); expect(new Set(items.map(item => item.object_key)).size).toBe(2);
    for (const nodeId of [first, second]) {
      const item = items.find(row => row.registrar_node_id === nodeId)!;
      expect(item).toMatchObject({ type_id: "workflow.variable", schema_version: 1,
        scope: "shared", owner_node_id: null, readers: [nodeId], writers: [nodeId] });
      expect(item.object_key).toMatch(/^item\//);
      expect(graph.document!.nodes.find(node => node.node_binding_id === nodeId)!.config.object_key).toBe(item.object_key);
      expect(graph.document!.event_bindings).toContainEqual(variableRegistrationEvent(nodeId));
      expect(graph.registrationTarget(nodeId)?.canSubmit).toBe(false);
    }
    expect(graph.document!.nodes.find(node => node.node_binding_id === assign)!.config.object_key).toBe("");
    expect(graph.document!.nodes.find(node => node.node_binding_id === read)!.config.object_key).toBe("");
    expect(graph.document!.nodes.find(node => node.node_binding_id === replace)!.config.object_keys).toEqual([]);
    expect(items.every(item => !item.readers.includes(read) && !item.writers.includes(assign))).toBe(true);
    expect(graph.entries[id]!.session_id).toBeNull(); expect(graph.session).toBeNull();
    expect(Object.hasOwn(graph.document!, "execution_roots")).toBe(true);
    expect(isGraphDocument(graph.document)).toBe(true);
    expect(graph.catalog.find(row => row.component_id === "tools.variable-assign")!.execution_class).toBe("working");
  });

  it("copies only selected consumers onto the new provider key, ACL and exact registration event", async () => {
    const { graph } = await setup();
    const provider = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    const selectedReader = graph.addNode("tools.variable-to-content@1", { x: 200, y: 0 })!;
    const remainingReader = graph.addNode("tools.variable-to-content@1", { x: 400, y: 0 })!;
    const item = graphClone(graph.document!.object_bindings![0]!);
    item.readers.push(selectedReader, remainingReader);
    graph.setObjectBindings([item]);
    for (const reader of [selectedReader, remainingReader]) graph.patchNode(reader, { config: { object_key: item.object_key } });
    graph.selectedNodeIds = [provider, selectedReader]; graph.duplicateSelection();
    const copiedProvider = graph.document!.nodes.find(node => node.component_id === "tools.variable-register"
      && node.node_binding_id !== provider)!;
    const copyReader = graph.document!.nodes.find(node => graph.selectedNodeIds.includes(node.node_binding_id)
      && node.component_id === "tools.variable-to-content")!;
    const copyItem = graph.document!.object_bindings!.find(row => row.registrar_node_id === copiedProvider.node_binding_id)!;
    expect(copyItem.object_key).not.toBe(item.object_key);
    expect(copiedProvider.config.object_key).toBe(copyItem.object_key);
    expect(copyReader.config.object_key).toBe(copyItem.object_key);
    expect(copyItem.readers).toEqual([copiedProvider.node_binding_id, copyReader.node_binding_id]);
    expect(copyItem.writers).toEqual([copiedProvider.node_binding_id]);
    expect(graph.document!.object_bindings!.find(row => row.registrar_node_id === provider)).toEqual(item);
    expect(graph.document!.nodes.find(node => node.node_binding_id === remainingReader)!.config.object_key).toBe(item.object_key);
    expect(graph.document!.event_bindings).toEqual([variableRegistrationEvent(provider),
      variableRegistrationEvent(copiedProvider.node_binding_id)]);
    expect(Object.hasOwn(graph.document!, "execution_roots")).toBe(true);
    expect(isGraphDocument(graph.document)).toBe(true);
  });

  it("preserves its item on exact replacement, removes its event when retired and keeps other manual events", async () => {
    const { graph } = await setup(), provider = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    const reader = graph.addNode("tools.variable-to-content@1", { x: 200, y: 0 })!;
    const original = graphClone(graph.document!.object_bindings![0]!);
    const manual = { ...variableRegistrationEvent(reader), event_id: "manual.read", display_name: "Manual read" };
    graph.setEventBindings([...graph.document!.event_bindings!, manual]);
    graph.replaceNode(provider, "tools.variable-register@2");
    expect(graph.document!.object_bindings).toEqual([original]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === provider)!.config.object_key).toBe(original.object_key);
    expect(graph.document!.event_bindings!.filter(event => event.event_id === variableRegistrationEvent(provider).event_id)).toHaveLength(1);
    expect(Object.hasOwn(graph.document!, "execution_roots")).toBe(true);
    expect(isGraphDocument(graph.document)).toBe(true);
    graph.replaceNode(provider, "tools.variable-assign@1");
    expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.document!.event_bindings).toEqual([manual]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === provider)!.config.object_key).toBe("");
    const added = graph.addNode("tools.variable-register@2", { x: 400, y: 0 })!;
    graph.selectedNodeIds = [added]; graph.removeSelection();
    expect(graph.document!.event_bindings).toEqual([manual]);
  });

  it("preserves a denied copied consumer's source key instead of treating a matching type as authorization", async () => {
    const { graph } = await setup();
    const provider = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    const reader = graph.addNode("tools.variable-to-content@1", { x: 200, y: 0 })!;
    const key = graph.document!.object_bindings![0]!.object_key;
    graph.patchNode(reader, { config: { object_key: key } });
    graph.selectedNodeIds = [provider, reader]; graph.duplicateSelection();
    const copiedReader = graph.document!.nodes.find(node => graph.selectedNodeIds.includes(node.node_binding_id)
      && node.component_id === "tools.variable-to-content")!;
    const copiedProvider = graph.document!.nodes.find(node => graph.selectedNodeIds.includes(node.node_binding_id)
      && node.component_id === "tools.variable-register")!;
    const copiedBinding = graph.document!.object_bindings!.find(binding =>
      binding.registrar_node_id === copiedProvider.node_binding_id)!;
    expect(copiedReader.config.object_key).toBe(key);
    expect(copiedBinding.readers).toEqual([copiedProvider.node_binding_id]);
    expect(copiedBinding.writers).toEqual([copiedProvider.node_binding_id]);
    expect(graph.document!.object_bindings![0]!.readers).toEqual([provider]);
  });
});

describe("explicit variable registration interaction", () => {
  it("submits the exact saved provider event with empty payload and the original session/context", async () => {
    const f = await savedSession(), requests: { operation: string; parameters: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_path, init) => {
      const request = JSON.parse(String(init.body)); requests.push(request);
      return sessionResponse({ ...f.session, revision: 2 }, request.operation, request.parameters);
    }));
    expect(f.graph.registrationTarget(f.nodeId)?.state).toBe("未注册");
    expect(await f.graph.submitVariableRegistration(f.nodeId)).toBe(true);
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({ operation: "event.submit", parameters: {
      session_id: f.session.workflow_session_id, workflow_definition_id: f.id, definition_revision: 1,
      event_id: `tools.variable-register/${f.nodeId}`, event_schema_version: 1, payload: {}, expected_revision: 1,
    } });
    expect(f.evidence.ports.write).toHaveBeenCalledWith(expect.objectContaining({
      owner_kind: "workflow-management", owner_id: f.id, operation: "event.submit",
    }));
    expect(f.graph.views[f.session.workflow_session_id]!.revision).toBe(2);
  });

  it.each(["no-session", "unsaved", "running", "paused", "wrong-item", "wrong-event",
    "invalid-name", "null-initial", "numeric-string-initial"] as const)(
    "blocks %s without creating a session or writing evidence", async mode => {
      const f = await savedSession(), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
      if (mode === "no-session") f.graph.entries[f.id]!.session_id = null;
      if (mode === "unsaved") f.graph.document!.name = "unsaved";
      if (mode === "running" || mode === "paused") f.graph.views[f.session.workflow_session_id]!.status = mode;
      if (mode === "wrong-item") f.graph.document!.nodes[0]!.config.object_key = "another-item";
      if (mode === "wrong-event") f.graph.document!.event_bindings![0]!.target_node_ids = [crypto.randomUUID()];
      if (mode === "invalid-name") f.graph.document!.nodes[0]!.config.name = "1";
      if (mode === "null-initial") f.graph.document!.nodes[0]!.config.has_initial = true;
      if (mode === "numeric-string-initial")
        Object.assign(f.graph.document!.nodes[0]!.config, { has_initial: true, initial_value: 1 });
      if (["invalid-name", "null-initial", "numeric-string-initial"].includes(mode))
        f.graph.entries[f.id]!.saved_document = graphClone(f.graph.document!);
      expect(await f.graph.submitVariableRegistration(f.nodeId)).toBe(false);
      expect(fetcher).not.toHaveBeenCalled(); expect(f.evidence.ports.write).not.toHaveBeenCalled();
    });

  it("keeps unknown evidence bound to the original event and blocks new registration attempts", async () => {
    const f = await savedSession();
    const fetcher = vi.fn(async () => { throw new Error("response lost"); }); vi.stubGlobal("fetch", fetcher);
    expect(await f.graph.submitVariableRegistration(f.nodeId)).toBe(false);
    const original = graphClone(f.graph.pending!);
    expect(original.body).toMatchObject({ event_id: `tools.variable-register/${f.nodeId}`, payload: {} });
    expect(f.graph.locked).toBe(true);
    expect(await f.graph.submitVariableRegistration(f.nodeId)).toBe(false);
    expect(fetcher).toHaveBeenCalledOnce(); expect(f.graph.pending).toEqual(original);
    expect([...f.evidence.records.values()][0]).toMatchObject({
      owner_id: f.id, resolution: "pending", request: {
        session_id: f.session.workflow_session_id, event_id: `tools.variable-register/${f.nodeId}`,
      },
    });
  });

  it("accepts a late receipt on the original session after the user switches workflow", async () => {
    const f = await savedSession(), other = f.graph.createWorkflow()!;
    useWorkspaceStore().openWorkflow(f.id);
    let finish!: (value: Response) => void;
    let parameters!: Record<string, unknown>;
    vi.stubGlobal("fetch", vi.fn(async (_path, init) => {
      parameters = JSON.parse(String(init.body)).parameters;
      return new Promise<Response>(resolve => { finish = resolve; });
    }));
    const submitting = f.graph.submitVariableRegistration(f.nodeId);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    useWorkspaceStore().openWorkflow(other);
    finish(sessionResponse({ ...f.session, revision: 2 }, "event.submit", parameters));
    expect(await submitting).toBe(true);
    expect(useWorkspaceStore().activeWorkflowId).toBe(other);
    expect(f.graph.entries[other]!.session_id).toBeNull();
    expect(f.graph.views[f.session.workflow_session_id]!.revision).toBe(2);
    expect([...f.evidence.records.values()][0]!.owner_id).toBe(f.id);
  });
});
