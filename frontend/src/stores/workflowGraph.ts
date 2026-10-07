import { computed, onScopeDispose, ref } from "vue";
import { defineStore } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { readGraphReceipt, sendGraphCommand } from "../adapters/workflowApplicationApi";
import { createGraphCommand as command, dispatchGraphCommand } from "../application/workflowCommands";
import { createWorkflowManagement, workflowManagementQueries } from "../application/workflowManagement";
import { createWorkflowEditing } from "../application/workflowEditing";
import { editFrontendSource, wireFrontendDisplay, type FrontendWiringRequest } from "../domain/frontendWiring";
import { newerGraphObservation } from "../domain/graphObservation";
import { graphClone, graphObject, graphUuid, isGraphDocument, newGraph,
  isGraphObjectBindings, isGraphEventBindings, isGraphJsonValue, graphSignature, upgradeGraph,
  type GraphCommand, type GraphDocument, type GraphEdge, type GraphEntry, type GraphNode,
  type GraphNodeType, type GraphSession, type GraphCandidates, type GraphDataType } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import type { FrontendExtension } from "../domain/frontendExtensions";
import type { WorkflowNodeConfigurationRequest, WorkflowNodeConfigurationResult } from "../plugins/workflowFrontendSdk";
import { createSerialAgentDemo, type SerialAgentModelConfig } from "../domain/serialAgentDemo";

export const useWorkflowGraphStore = defineStore("workflow-graph", () => {
  const workspace = useWorkspaceStore();
  const notices = useWorkbenchNoticesStore();
  const entries = ref<Record<string, GraphEntry>>({});
  const catalog = ref<GraphNodeType[]>([]);
  const frontendExtensions = ref<FrontendExtension[]>([]);
  const packageLock = ref<NonNullable<GraphDocument["package_lock"]>>([]);
  const observedExecutionPackageLock = ref<NonNullable<GraphDocument["package_lock"]> | null>(null);
  const executionPackageLock = computed({
    get: () => observedExecutionPackageLock.value ?? packageLock.value,
    set: (value: NonNullable<GraphDocument["package_lock"]>) => { observedExecutionPackageLock.value = value; },
  });
  const dataTypes = ref<GraphDataType[]>([]);
  const catalogLoading = ref(false);
  const catalogError = ref<string | null>(null);
  const sessions = ref<Record<string, GraphSession[]>>({});
  const views = ref<Record<string, GraphSession>>({});
  const candidates = ref<Record<string, GraphCandidates>>({});
  const history = ref<Record<string, { past: GraphDocument[]; future: GraphDocument[] }>>({});
  const busy = ref<string | null>(null);
  const applicationBusy = ref(false);
  const selectingSessionFor = ref<string | null>(null);
  const selectedNodeIds = ref<string[]>([]);
  const selectedEdgeId = ref<string | null>(null);
  const active = computed(() => entries.value[workspace.activeWorkflowId]);
  const document = computed(() => active.value?.document ?? null);
  const session = computed(() => active.value?.session_id ? views.value[active.value.session_id] ?? null : null);
  const pending = computed(() => active.value?.pending ?? null);
  const mutationLocked = computed(() => !!busy.value || applicationBusy.value || !!pending.value || !!workspace.activeWorkflow.copyPending
    || workspace.workflows.some(row => row.copyPending && row.sourceId === workspace.activeWorkflowId));
  const locked = computed(() => mutationLocked.value || selectingSessionFor.value === workspace.activeWorkflowId);
  const primaryAction = computed(() => ["running", "prepared", "pausing"].includes(session.value?.status ?? "") ? "pause"
    : session.value?.available_actions?.includes("resume") || session.value?.status === "paused" ? "resume" : "start");
  const statusLabel = computed(() => busy.value ? "提交中" : primaryAction.value === "pause" ? "暂停"
    : primaryAction.value === "resume" ? "继续" : "启动");
  const canUndo = computed(() => !!history.value[workspace.activeWorkflowId]?.past.length && !locked.value);
  const canRedo = computed(() => !!history.value[workspace.activeWorkflowId]?.future.length && !locked.value);
  let persistence: (() => boolean) | null = null;
  let alive = true;
  let viewGeneration = 0;
  let sessionSelectionGeneration = 0;
  let mutationGeneration = 0;
  const refreshGenerations = new Map<string, number>();
  const candidateGenerations = new Map<string, number>();
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  onScopeDispose(() => { alive = false; frontendExtensions.value = []; viewGeneration++; sessionSelectionGeneration++; if (pollTimer) clearTimeout(pollTimer); });
  const isGeneric = (id: string) => !!entries.value[id];
  function setPersistenceGuard(guard: () => boolean) { persistence = guard; }
  function storeSnapshot(): { schema_version: 1; entries: Record<string, GraphEntry> } {
    return graphClone({ schema_version: 1, entries: entries.value });
  }
  function restoreSnapshot(value: unknown) {
    if (!graphObject(value) || value.schema_version !== 1 || !graphObject(value.entries)) return false;
    const restoredEntries = value.entries;
    for (const [id, entry] of Object.entries(restoredEntries)) {
      if (!graphObject(entry) || !isGraphDocument(entry.document)
        || !Number.isSafeInteger(entry.saved_revision) || Number(entry.saved_revision) < 0
        || entry.session_id !== null && !graphUuid(entry.session_id)
        || entry.saved_document !== undefined && !isGraphDocument(entry.saved_document)
        || entry.external_inputs !== undefined && !graphObject(entry.external_inputs)
        || entry.pending !== null && (!graphObject(entry.pending) || typeof entry.pending.path !== "string"
          || !/^\/api\/graph\/(definitions|sessions(?:\/|$))/.test(entry.pending.path)
          || !graphObject(entry.pending.body) || !graphUuid(entry.pending.body.idempotency_key))) return false;
      if (entry.saved_document && (entry.saved_document as GraphDocument).workflow_definition_id !== entry.document.workflow_definition_id) return false;
      if (entry.rejected_copy !== undefined) {
        const rejected = entry.rejected_copy;
        const sourceId = workspace.workflows.find(row => row.id === id)?.sourceId;
        const source = sourceId ? restoredEntries[sourceId] : null;
        if (!graphObject(rejected) || rejected.action !== "copy" || !graphObject(rejected.body)
          || !graphUuid(rejected.body.idempotency_key) || !graphObject(source)
          || rejected.path !== `/api/graph/sessions/${source.session_id}/copy`
          || !isGraphDocument(rejected.body.document)
          || rejected.body.document.workflow_definition_id !== entry.document.workflow_definition_id) return false;
      }
      if (entry.pending !== null) {
        const request = entry.pending as unknown as GraphCommand;
        const body = request.body; const root = entry.document.workflow_definition_id;
        if (request.action === "save") {
          if (request.path !== "/api/graph/definitions" || !isGraphDocument(body.document)
            || body.document.workflow_definition_id !== root) return false;
        } else if (request.action === "create") {
          if (request.path !== "/api/graph/sessions" || body.workflow_definition_id !== root) return false;
        } else if (request.action === "copy") {
          const sourceId = workspace.workflows.find(row => row.id === id)?.sourceId;
          const source = sourceId ? restoredEntries[sourceId] : null;
          if (!graphObject(source) || request.path !== `/api/graph/sessions/${source.session_id}/copy`
            || !isGraphDocument(body.document) || body.document.workflow_definition_id !== root) return false;
        } else if (["candidate-select", "candidate-fork"].includes(request.action)) {
          const suffix = request.action === "candidate-select" ? "select" : "fork";
          if (!graphUuid(request.source_session_id) || !graphUuid(request.expected_chain_run_id)
            || !graphUuid(request.expected_definition_id) || !Number.isSafeInteger(request.expected_definition_revision)
            || Number(request.expected_definition_revision) < 1
            || request.path !== `/api/graph/sessions/${request.source_session_id}/candidates/${suffix}`
            || request.action === "candidate-select" && request.source_session_id !== entry.session_id
            || !graphUuid(body.candidate_id) || !Number.isSafeInteger(body.expected_revision) || Number(body.expected_revision) < 1
            || !Number.isSafeInteger(body.expected_data_revision) || Number(body.expected_data_revision) < 0
            || !Number.isSafeInteger(body.expected_head_revision) || Number(body.expected_head_revision) < 1
            || Object.keys(body).length !== 5) return false;
        } else {
          const suffix = request.action === "start" ? "runs" : request.action === "rebind" ? "rebind"
            : request.action === "event" ? "events/submit"
            : request.action === "data" ? "data" : ["pause", "resume", "close", "extend_budget", "retry_archive", "retry_acceptance", "retry_failed_node"].includes(request.action) ? "control" : null;
          if (!suffix || !entry.session_id || request.path !== `/api/graph/sessions/${entry.session_id}/${suffix}`) return false;
          if (request.action === "event" && (Object.keys(body).length !== 7
            || body.workflow_definition_id !== root || !Number.isSafeInteger(body.definition_revision)
            || Number(body.definition_revision) < 1 || typeof body.event_id !== "string" || !body.event_id
            || body.event_id.length > 128 || body.event_id.trim() !== body.event_id
            || !Number.isSafeInteger(body.event_schema_version) || Number(body.event_schema_version) < 1
            || !Number.isSafeInteger(body.expected_revision) || Number(body.expected_revision) < 1
            || !graphObject(body.payload) || !isGraphJsonValue(body.payload) || Object.hasOwn(body.payload, "_workflow_frozen_resources"))) return false;
        }
      }
    }
    entries.value = graphClone(value.entries) as unknown as Record<string, GraphEntry>;
    for (const id of Object.keys(entries.value)) history.value[id] = { past: [], future: [] };
    return true;
  }
  function createWorkflow() {
    const doc = newGraph("新工作流");
    const id = doc.workflow_definition_id;
    entries.value[id] = { document: doc, saved_revision: 0, session_id: null, pending: null };
    workspace.registerGraphWorkflow({ id, title: doc.name, description: "", nodeCount: 0, state: "draft" });
    history.value[id] = { past: [], future: [] };
    workspace.openWorkflow(id);
    persistence?.();
    return id;
  }
  function createSerialAgentExample(modelConfig?: SerialAgentModelConfig) {
    if (catalogLoading.value || catalogError.value) return null;
    try {
      const doc = createSerialAgentDemo(catalog.value, executionPackageLock.value, modelConfig);
      const id = doc.workflow_definition_id;
      entries.value[id] = { document: doc, saved_revision: 0, session_id: null, pending: null };
      history.value[id] = { past: [], future: [] };
      workspace.registerGraphWorkflow({ id, title: doc.name,
        description: "A → B 串行；先选择模型资源与模型名，再保存并打开聊天",
        nodeCount: doc.nodes.length, state: "draft" });
      workspace.openWorkflow(id);
      selectedNodeIds.value = [];
      selectedEdgeId.value = null;
      persistence?.();
      return id;
    } catch (failure) {
      report(failure, "创建串行 Agent 示例");
      return null;
    }
  }
  function typeFor(node: GraphNode) {
    return catalog.value.find(type => type.component_id === node.component_id
      && type.component_version === node.component_version);
  }
  async function loadCatalog() {
    if (catalogLoading.value) return;
    catalogLoading.value = true;
    catalogError.value = null;
    frontendExtensions.value = [];
    try {
      const result = await workflowManagementQueries.catalog();
      if (alive) { catalog.value = result.node_types; packageLock.value = result.package_lock; dataTypes.value = result.data_types;
        executionPackageLock.value = result.execution_package_lock ?? result.package_lock;
        frontendExtensions.value = result.frontend_extensions ?? []; }
    }
    catch (failure) { if (alive) catalogError.value = failure instanceof Error ? failure.message : "节点目录不可用"; }
    finally { catalogLoading.value = false; }
  }
  function report(failure: unknown, action: string) {
    const error = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", String(failure));
    notices.notify(error.kind, error.reason, action);
  }
  function observe(view: GraphSession) {
    const accepted = newerGraphObservation(views.value[view.workflow_session_id], view);
    if (alive) {
      views.value[view.workflow_session_id] = accepted;
      for (const [workflowId, rows] of Object.entries(sessions.value)) {
        if (rows.some(row => row.workflow_session_id === accepted.workflow_session_id && row !== accepted))
          sessions.value[workflowId] = rows.map(row => row.workflow_session_id === accepted.workflow_session_id ? accepted : row);
      }
    }
    return accepted;
  }
  async function dispatch(id: string, command: GraphCommand, reconcile = false): Promise<unknown> {
    const generation = ++mutationGeneration;
    const entry = entries.value[id];
    const eventSessionId = entry.session_id, eventDefinitionId = entry.document.workflow_definition_id,
      eventDefinitionRevision = entry.document.revision;
    return dispatchGraphCommand(entry, command, {
      persist: () => persistence?.() ?? false,
      request: sendGraphCommand,
      readReceipt: readGraphReceipt,
      readDefinition: workflowManagementQueries.definition,
      observedSession: sessionId => views.value[sessionId],
      current: () => alive && entries.value[id] === entry && mutationGeneration === generation
        && (command.action !== "event"
        || entry.session_id === eventSessionId && entry.document.workflow_definition_id === eventDefinitionId
          && entry.document.revision === eventDefinitionRevision),
      accept(receipt, request) {
        if (receipt.kind === "definition") {
          entry.saved_revision = receipt.document.revision;
          entry.saved_document = graphClone(receipt.document);
          entry.document.revision = receipt.document.revision;
        } else {
          const result = receipt.session;
          if (receipt.historicalDocument) {
            entry.document = graphClone(receipt.historicalDocument);
            entry.saved_document = graphClone(receipt.historicalDocument);
            entry.saved_revision = receipt.historicalDocument.revision;
            entry.state_mappings = [];
            history.value[id] = { past: [], future: [] };
            workspace.updateWorkflowProjection(id, entry.document.name, entry.document.nodes.length);
            workspace.markWorkflowSaved(id, entry.document.name);
          }
          entry.session_id = result.workflow_session_id;
          const view = observe(receipt.observation ?? result);
          sessions.value[id] = [...(sessions.value[id] ?? []).filter(row => row.workflow_session_id !== result.workflow_session_id), view];
          if (request.action === "copy") {
            entry.saved_revision = result.definition_revision;
            entry.document.revision = result.definition_revision;
            entry.saved_document = graphClone(entry.document);
            workspace.setCopyPending(id, false);
            workspace.openWorkflow(id);
          }
        }
      },
    }, reconcile);
  }
  const management = createWorkflowManagement({
    entry: id => entries.value[id], observedSession: id => views.value[id], observe, dispatch,
    commandState: value => { applicationBusy.value = value; if (value) mutationGeneration++; },
    canSubmit(id) {
      const workflow = workspace.workflows.find(row => row.id === id);
      return alive && !!workflow && !workflow.copyPending
        && !workspace.workflows.some(row => row.copyPending && row.sourceId === id);
    },
    commitSaved(id) {
      workspace.markWorkflowSaved(id, entries.value[id].document.name);
      history.value[id] = { past: [], future: [] };
      return persistence?.() ?? false;
    },
  });
  const editing = createWorkflowEditing({
    entry: id => entries.value[id],
    isDraft: id => workspace.workflows.find(row => row.id === id)?.state === "draft",
    canEdit: id => alive && !busy.value && !applicationBusy.value && !entries.value[id]?.pending
      && selectingSessionFor.value !== id && !workspace.workflows.find(row => row.id === id)?.copyPending
      && !workspace.workflows.some(row => row.copyPending && row.sourceId === id),
    observedSession: id => views.value[id],
    acceptDraft(id, next) {
      const entry = entries.value[id];
      const past = [...(history.value[id]?.past ?? []), graphClone(entry.document)].slice(-60);
      history.value[id] = { past, future: [] };
      entry.document = next;
      workspace.updateWorkflowProjection(id, next.name, next.nodes.length);
    },
    stageCopy(sourceId, targetId, next, baseline, copying) {
      entries.value[targetId] = { document: next, saved_revision: 0, session_id: null, pending: null };
      history.value[targetId] = { past: [baseline], future: [] };
      workspace.registerGraphWorkflow({ id: targetId, title: next.name, description: "", state: "draft",
        sourceId, nodeCount: next.nodes.length, copyPending: copying });
    },
    open: workspace.openWorkflow, persist: () => persistence?.() ?? false, dispatch,
    copying: value => { busy.value = value ? "copy" : null; },
    report: failure => report(failure, "工作流会话复制"),
  });
  async function saveWorkflow(id = workspace.activeWorkflowId) {
    if (busy.value || entries.value[id]?.pending) return false;
    mutationGeneration++;
    busy.value = "save";
    try {
      await management.commands.save({ workflowId: id });
      return true;
    } catch (failure) { report(failure, "保存工作流"); return false; }
    finally { busy.value = null; }
  }
  function editable(id: string, change: (document: GraphDocument) => boolean) {
    try { return editing.edit({ workflowId: id }, change); }
    catch (failure) { report(failure, "工作流编辑"); return null; }
  }
  function addNode(component: string, position: { x: number; y: number }) {
    const type = catalog.value.find(row => `${row.component_id}@${row.component_version}` === component);
    if (!type) return null;
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
      component_version: type.component_version, title: type.display_name, position: { ...position }, config: graphClone(type.default_config) };
    const id = editable(workspace.activeWorkflowId, doc => {
      const ports = [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode => [...mode.inputs, ...mode.outputs])];
      if (ports.some(port => (port.data_schema_version ?? 1) !== 1) || type.input_storage === "references"
        || type.capabilities?.some(capability => capability.startsWith("objects:")))
        upgradeGraph(doc, executionPackageLock.value);
      doc.nodes.push(node); return true;
    });
    if (id) selectedNodeIds.value = [node.node_binding_id];
    return id ? node.node_binding_id : null;
  }
  function attachFrontendDisplay(request: FrontendWiringRequest) {
    let added: string[] = [];
    const id = editable(workspace.activeWorkflowId, doc => {
      added = wireFrontendDisplay(doc, catalog.value, executionPackageLock.value, request);
      return true;
    });
    if (id) { selectedNodeIds.value = added; selectedEdgeId.value = null; }
    return id;
  }
  function patchFrontendSource(nodeId: string, edgeId: string | null, source: { nodeId: string; portId: string } | null) {
    return editable(workspace.activeWorkflowId, doc => editFrontendSource(doc, catalog.value, nodeId, edgeId, source));
  }
  function patchNode(id: string, patch: Partial<Pick<GraphNode, "title" | "config" | "public_outputs">>) {
    return editable(workspace.activeWorkflowId, doc => {
      const node = doc.nodes.find(row => row.node_binding_id === id);
      if (!node) return false;
      Object.assign(node, graphClone(patch));
      return true;
    });
  }
  function patchNodeConfiguration(request: WorkflowNodeConfigurationRequest): WorkflowNodeConfigurationResult | null {
    const entry = entries.value[request.workflowId];
    if (!alive || locked.value || request.workflowId !== workspace.activeWorkflowId || !entry
      || !graphObject(request.expectedConfig) || !graphObject(request.patch) || !isGraphJsonValue(request.patch)) return null;
    const node = entry.document.nodes.find(row => row.node_binding_id === request.nodeId);
    const definition = node && typeFor(node);
    const extension = frontendExtensions.value.find(row => row.extension_id === request.extensionId
      && row.binding.slot === "node-fields" && row.binding.surface === "workbench"
      && row.component_id === request.componentId && row.component_version === request.componentVersion);
    if (!node || !definition?.executable || !extension
      || !packageLock.value.some(row => row.package_id === extension.package_id && row.version === extension.package_version)
      || node.component_id !== request.componentId || node.component_version !== request.componentVersion) return null;
    const properties = definition.config_schema.properties;
    if (!graphObject(properties) || !Object.keys(request.patch).length
      || Object.keys(request.patch).some(field => !Object.hasOwn(properties, field))) return null;
    const withConfig = (config: Record<string, unknown>) => ({ ...entry.document, nodes: [{ ...node, config }] });
    if (graphSignature(withConfig(node.config)) !== graphSignature(withConfig(request.expectedConfig))) return null;
    const id = editable(request.workflowId, doc => {
      const target = doc.nodes.find(row => row.node_binding_id === request.nodeId);
      if (!target) return false;
      target.config = { ...target.config, ...graphClone(request.patch) };
      return true;
    });
    return id ? { workflowId: id, nodeId: request.nodeId } : null;
  }
  function moveNodes(moves: { id: string; position: { x: number; y: number } }[]) {
    return editable(workspace.activeWorkflowId, doc => {
      for (const move of moves) {
        const node = doc.nodes.find(row => row.node_binding_id === move.id);
        if (node) node.position = { ...move.position };
      }
      return true;
    });
  }
  function connect(edge: Omit<GraphEdge, "edge_id" | "order">) {
    return editable(workspace.activeWorkflowId, doc => {
      if (!doc.nodes.some(n => n.node_binding_id === edge.source_node_id)
        || !doc.nodes.some(n => n.node_binding_id === edge.target_node_id)) return false;
      if (doc.edges.some(e => e.source_node_id === edge.source_node_id && e.target_node_id === edge.target_node_id
        && e.source_port_id === edge.source_port_id && e.target_port_id === edge.target_port_id)) return false;
      const incoming = doc.edges.filter(e => e.target_node_id === edge.target_node_id && e.target_port_id === edge.target_port_id);
      doc.edges.push({ ...edge, edge_id: crypto.randomUUID(), order: incoming.length ? Math.max(...incoming.map(e => e.order)) + 1 : 0 });
      return true;
    });
  }
  function removeSelection() {
    const ids = new Set(selectedNodeIds.value);
    const changed = editable(workspace.activeWorkflowId, doc => {
      doc.nodes = doc.nodes.filter(n => !ids.has(n.node_binding_id));
      doc.edges = doc.edges.filter(e => !ids.has(e.source_node_id) && !ids.has(e.target_node_id) && e.edge_id !== selectedEdgeId.value);
      if (doc.schema_version === 2) {
        doc.execution_roots = (doc.execution_roots ?? []).filter(id => !ids.has(id));
        doc.control_edges = (doc.control_edges ?? []).filter(edge => !ids.has(edge.source_node_id)
          && !ids.has(edge.target_node_id) && edge.edge_id !== selectedEdgeId.value);
        if (doc.event_bindings) doc.event_bindings = doc.event_bindings.map(binding => ({ ...binding,
          target_node_ids: binding.target_node_ids.filter(id => !ids.has(id)) })).filter(binding => binding.target_node_ids.length);
        doc.object_bindings = (doc.object_bindings ?? []).filter(binding =>
          binding.scope !== "private" || !ids.has(binding.owner_node_id!)).map(binding => ({
          ...binding, readers: binding.readers.filter(id => !ids.has(id)),
          writers: binding.writers.filter(id => !ids.has(id)),
        }));
      }
      return true;
    });
    if (changed) { selectedNodeIds.value = []; selectedEdgeId.value = null; }
  }
  function reorderEdge(id: string, offset: number) {
    return editable(workspace.activeWorkflowId, doc => {
      const edge = doc.edges.find(e => e.edge_id === id);
      if (!edge) return false;
      const siblings = doc.edges.filter(e => e.target_node_id === edge.target_node_id
        && e.target_port_id === edge.target_port_id).sort((a, b) => a.order - b.order);
      const at = siblings.indexOf(edge); const destination = at + offset;
      if (destination < 0 || destination >= siblings.length) return false;
      [siblings[at], siblings[destination]] = [siblings[destination]!, siblings[at]!];
      siblings.forEach((sibling, order) => { sibling.order = order; });
      return true;
    });
  }
  function undo(redo = false) {
    if (locked.value) return;
    const id = workspace.activeWorkflowId;
    const h = history.value[id];
    const snapshot = (redo ? h?.future : h?.past)?.pop();
    if (!snapshot) return;
    (redo ? h.past : h.future).push(graphClone(entries.value[id].document));
    entries.value[id].document = snapshot;
    workspace.updateWorkflowProjection(id, snapshot.name, snapshot.nodes.length);
    persistence?.();
  }
  function duplicateSelection() {
    const selected = new Set(selectedNodeIds.value); const clonedIds: string[] = [];
    const changed = editable(workspace.activeWorkflowId, doc => {
      const mapping = new Map<string, string>();
      const nodes = doc.nodes.filter(n => selected.has(n.node_binding_id)).map(n => {
        const copy = graphClone(n); copy.node_binding_id = crypto.randomUUID();
        mapping.set(n.node_binding_id, copy.node_binding_id); clonedIds.push(copy.node_binding_id);
        copy.position.x += 35; copy.position.y += 35; return copy;
      });
      for (const node of nodes) {
        for (const reference of typeFor(node)?.config_node_references ?? []) {
          const value = node.config[reference.config_field];
          if (reference.multiple && Array.isArray(value))
            node.config[reference.config_field] = value.map(id =>
              typeof id === "string" ? mapping.get(id) ?? id : id);
          else if (!reference.multiple && typeof value === "string")
            node.config[reference.config_field] = mapping.get(value) ?? value;
        }
      }
      const edges = doc.edges.filter(e => mapping.has(e.source_node_id) && mapping.has(e.target_node_id))
        .map(e => ({ ...e, edge_id: crypto.randomUUID(), source_node_id: mapping.get(e.source_node_id)!, target_node_id: mapping.get(e.target_node_id)! }));
      if (doc.schema_version === 2) {
        doc.execution_roots = [...(doc.execution_roots ?? []), ...(doc.execution_roots ?? [])
          .filter(id => mapping.has(id)).map(id => mapping.get(id)!)];
        doc.control_edges = [...(doc.control_edges ?? []), ...(doc.control_edges ?? [])
          .filter(edge => mapping.has(edge.source_node_id) && mapping.has(edge.target_node_id))
          .map(edge => ({ ...edge, edge_id: crypto.randomUUID(), source_node_id: mapping.get(edge.source_node_id)!,
            target_node_id: mapping.get(edge.target_node_id)! }))];
        const privateBindings: NonNullable<GraphDocument["object_bindings"]> = [];
        doc.object_bindings = (doc.object_bindings ?? []).map(binding => {
          if (binding.scope === "private") {
            const owner = mapping.get(binding.owner_node_id!);
            if (!owner) return binding;
            const key = `private/${crypto.randomUUID()}`;
            privateBindings.push({ ...graphClone(binding), object_key: key, owner_node_id: owner,
              readers: binding.readers.length ? [owner] : [], writers: binding.writers.length ? [owner] : [] });
            const copy = nodes.find(node => node.node_binding_id === owner)!;
            if (copy.config.object_key === binding.object_key) copy.config.object_key = key;
            if (Array.isArray(copy.config.object_keys)) copy.config.object_keys = copy.config.object_keys
              .map(existing => existing === binding.object_key ? key : existing);
            return binding;
          }
          return { ...binding, readers: [...binding.readers, ...binding.readers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)],
            writers: [...binding.writers, ...binding.writers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)] };
        });
        doc.object_bindings.push(...privateBindings);
      }
      doc.nodes.push(...nodes); doc.edges.push(...edges); return !!nodes.length;
    });
    if (changed) selectedNodeIds.value = clonedIds;
  }
  function replaceNode(id: string, component: string) {
    const type = catalog.value.find(t => `${t.component_id}@${t.component_version}` === component);
    if (!type) return null;
    return editable(workspace.activeWorkflowId, doc => {
      const node = doc.nodes.find(n => n.node_binding_id === id); if (!node) return false;
      node.component_id = type.component_id; node.component_version = type.component_version;
      node.config = graphClone(type.default_config); node.public_outputs = [];
      if (type.input_storage === "references" || type.capabilities?.some(capability => capability.startsWith("objects:"))
        || [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode =>
          [...mode.inputs, ...mode.outputs])].some(port => (port.data_schema_version ?? 1) !== 1))
        upgradeGraph(doc, executionPackageLock.value);
      // Changed types preserve old edges for explicit diagnosis, never guess port mappings.
      return true;
    });
  }
  function setExecutionRoot(nodeId: string, enabled: boolean) {
    return editable(workspace.activeWorkflowId, doc => {
      if (!doc.nodes.some(node => node.node_binding_id === nodeId)) return false;
      upgradeGraph(doc, executionPackageLock.value);
      doc.execution_roots = [...doc.execution_roots!.filter(id => id !== nodeId), ...(enabled ? [nodeId] : [])];
      return true;
    });
  }
  function setControlDependencies(nodeId: string, sources: string[]) {
    return editable(workspace.activeWorkflowId, doc => {
      const ids = new Set(doc.nodes.map(node => node.node_binding_id));
      if (!ids.has(nodeId) || !sources.every(id => ids.has(id) && id !== nodeId)
        || new Set(sources).size !== sources.length) return false;
      upgradeGraph(doc, executionPackageLock.value);
      const existing = doc.control_edges!.filter(edge => edge.target_node_id === nodeId);
      doc.control_edges = [...doc.control_edges!.filter(edge => edge.target_node_id !== nodeId),
        ...sources.map(source => existing.find(edge => edge.source_node_id === source) ?? {
          edge_id: crypto.randomUUID(), source_node_id: source, target_node_id: nodeId,
        })];
      return true;
    });
  }
  function setObjectBindings(value: unknown) {
    if (!document.value || !isGraphObjectBindings(value, document.value.nodes)) {
      notices.notify("rejected", "对象绑定须包含唯一键、确切类型版本和有效的节点权限；私有对象仅允许所有者访问", "会话对象绑定");
      return null;
    }
    return editable(workspace.activeWorkflowId, doc => {
      upgradeGraph(doc, executionPackageLock.value);
      doc.object_bindings = graphClone(value); return true;
    });
  }
  async function refresh(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry) return;
    const generation = (refreshGenerations.get(id) ?? 0) + 1;
    refreshGenerations.set(id, generation);
    const definitionId = entry.document.workflow_definition_id, sessionId = entry.session_id;
    const mutationVersion = mutationGeneration, viewToken = workspace.currentViewToken();
    const current = () => alive && refreshGenerations.get(id) === generation && entries.value[id] === entry
      && entry.document.workflow_definition_id === definitionId && entry.session_id === sessionId
      && mutationGeneration === mutationVersion && workspace.isCurrentView(viewToken);
    try {
      const rows = await management.queries.sessions(definitionId);
      if (!current()) return;
      sessions.value[id] = rows.map(observe);
      if (sessionId) {
        const incoming = await management.queries.session(sessionId, definitionId);
        if (current()) observe(incoming);
      }
    } catch (failure) { if (current()) report(failure, "读取工作流会话"); }
  }
  function schedulePoll(id: string, generation: number) {
    if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    const entry = entries.value[id];
    const view = entry?.session_id ? views.value[entry.session_id] : null;
    if (!["running", "prepared", "pausing"].includes(view?.status ?? "")) return;
    pollTimer = setTimeout(async () => {
      if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
      const sessionId = entries.value[id]?.session_id, mutationVersion = mutationGeneration;
      await refresh(id);
      const settled = sessionId ? views.value[sessionId] : null;
      if (alive && generation === viewGeneration && workspace.activeWorkflowId === id
        && entries.value[id]?.session_id === sessionId && mutationGeneration === mutationVersion
        && settled && !["running", "prepared", "pausing"].includes(settled.status)) {
        // Node termination can become visible before the run's final permissions.
        await refresh(id);
      }
      schedulePoll(id, generation);
    }, 350);
  }
  async function activate(id: string) {
    const generation = ++viewGeneration;
    sessionSelectionGeneration++; selectingSessionFor.value = null;
    selectedNodeIds.value = []; selectedEdgeId.value = null;
    if (pollTimer) clearTimeout(pollTimer);
    if (!entries.value[id]) return;
    if (!catalog.value.length) await loadCatalog();
    await refresh(id);
    schedulePoll(id, generation);
  }
  async function createSession(id = workspace.activeWorkflowId) {
    if (locked.value) return false;
    busy.value = "create";
    try { await management.commands.createSession({ workflowId: id }); return true; }
    catch (failure) { report(failure, "创建工作流会话"); return false; }
    finally { busy.value = null; }
  }
  async function selectSession(id: string) {
    if (mutationLocked.value || !active.value) return;
    const workflowId = workspace.activeWorkflowId;
    const entry = active.value;
    const generation = ++sessionSelectionGeneration;
    const viewVersion = viewGeneration;
    const mutationVersion = mutationGeneration;
    selectingSessionFor.value = workflowId;
    const current = () => alive && generation === sessionSelectionGeneration && viewVersion === viewGeneration
      && mutationVersion === mutationGeneration
      && workspace.activeWorkflowId === workflowId && entries.value[workflowId] === entry;
    try {
      const row = await management.queries.session(id, entry.document.workflow_definition_id);
      if (!current() || mutationLocked.value) return;
      entry.session_id = row.workflow_session_id; observe(row); persistence?.();
      schedulePoll(workflowId, viewVersion);
    } catch (failure) { if (current()) report(failure, "切换会话"); }
    finally { if (generation === sessionSelectionGeneration) selectingSessionFor.value = null; }
  }
  async function submitPrimary() {
    if (locked.value || !active.value) return;
    const id = workspace.activeWorkflowId;
    const action = primaryAction.value;
    busy.value = action;
    try {
      if (action === "start") await management.commands.start({ workflowId: id });
      else await management.commands.control({ workflowId: id }, action);
      schedulePoll(id, viewGeneration);
    } catch (failure) {
      if (action === "pause" && failure instanceof WorkbenchApiError
        && failure.kind === "rejected" && failure.code === "stale_revision") {
        const message = "会话状态已变化，暂停未提交；请刷新运行后重试 [stale_revision]";
        const entry = entries.value[id];
        if (entry?.diagnostics) entry.diagnostics = entry.diagnostics.map(diagnostic =>
          diagnostic.reason_code === "stale_revision" ? { ...diagnostic, message } : diagnostic);
        notices.notify(failure.kind, message, "暂停工作流");
      } else report(failure, "运行工作流");
    }
    finally { busy.value = null; }
  }
  function setEventBindings(value: unknown) {
    return editable(workspace.activeWorkflowId, doc => {
      if (!isGraphEventBindings(value, doc.nodes)) throw new Error("检查事件身份、版本、节点 UUID、受众和本地对象 schema");
      upgradeGraph(doc, executionPackageLock.value); doc.event_bindings = graphClone(value);
      return true;
    });
  }
  async function submitEvent(eventId: string, eventVersion: number, payload: Record<string, unknown>) {
    if (locked.value || !active.value) return false;
    const id = workspace.activeWorkflowId; busy.value = "event";
    try { await management.commands.submitEvent({ workflowId: id }, eventId, eventVersion, payload);
      schedulePoll(id, viewGeneration); return true; }
    catch (failure) { report(failure, "提交局部事件"); return false; }
    finally { busy.value = null; }
  }
  async function reconcile(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry || busy.value) return;
    const workflow = workspace.workflows.find(row => row.id === id);
    if (!entry.pending && workflow?.copyPending) {
      notices.notify("unknown", entry.rejected_copy ? "复制已明确拒绝；核实不会重新提交，请使用重试已拒绝复制"
        : "副本缺少原请求坐标，无法安全核实或重发；副本仍保留", "核实会话复制");
      return;
    }
    if (!entry.pending) return;
    busy.value = "reconcile";
    try { await dispatch(id, graphClone(entry.pending), true); await refresh(id); schedulePoll(id, viewGeneration); }
    catch (failure) { report(failure, "核实原工作流请求"); }
    finally { busy.value = null; }
  }
  function canRetryRejectedCopy(id: string) {
    const entry = entries.value[id], workflow = workspace.workflows.find(row => row.id === id);
    const source = workflow?.sourceId ? entries.value[workflow.sourceId] : null;
    const rejected = entry?.rejected_copy;
    return !!entry && !entry.pending && !!workflow?.copyPending && !!source?.session_id
      && rejected?.action === "copy" && rejected.path === `/api/graph/sessions/${source.session_id}/copy`
      && isGraphDocument(rejected.body.document)
      && graphSignature(rejected.body.document) === graphSignature(entry.document);
  }
  async function retryRejectedCopy(id: string) {
    if (busy.value || applicationBusy.value || !canRetryRejectedCopy(id)) return;
    const entry = entries.value[id], rejected = graphClone(entry.rejected_copy!);
    const sourceId = workspace.workflows.find(row => row.id === id)!.sourceId!;
    const source = entries.value[sourceId], sessionId = source.session_id!;
    busy.value = "copy";
    try {
      const view = await management.queries.session(sessionId, source.document.workflow_definition_id);
      if (!alive || entries.value[id] !== entry || entries.value[sourceId] !== source
        || source.session_id !== sessionId || !canRetryRejectedCopy(id)
        || JSON.stringify(entry.rejected_copy) !== JSON.stringify(rejected))
        throw new WorkbenchApiError("unknown", "已拒绝复制的依据已变化，未提交新请求");
      await dispatch(id, command(`/api/graph/sessions/${sessionId}/copy`, {
        document: graphClone(entry.document), expected_session_revision: view.revision,
        expected_data_revision: view.data_revision, expected_definition_revision: view.definition_revision,
        expected_head_revision: view.head_revision, mappings: entry.state_mappings ?? [],
      }, "copy"));
    } catch (failure) { report(failure, "重试已拒绝会话复制"); }
    finally { busy.value = null; }
  }
  function discardDraft() {
    const workflow = workspace.activeWorkflow;
    if (locked.value || workflow.state !== "draft" || !workflow.sourceId) return;
    const id = workflow.id;
    workspace.openWorkflow(workflow.sourceId);
    workspace.removeWorkflow(id);
    delete entries.value[id]; delete history.value[id]; persistence?.();
  }
  function discardRejectedCopy(id: string) {
    const entry = entries.value[id]; const workflow = workspace.workflows.find(row => row.id === id);
    if (busy.value || entry?.pending || !workflow?.copyPending) return;
    workspace.removeWorkflow(id);
    delete entries.value[id]; delete history.value[id]; persistence?.();
  }
  async function writeData(variables: Record<string, unknown>, shared?: Record<string, unknown>) {
    const view = session.value; if (!view || locked.value) return false;
    busy.value = "write-data";
    try { await dispatch(workspace.activeWorkflowId, command(`/api/graph/sessions/${view.workflow_session_id}/data`, {
      expected_revision: view.revision, expected_data_revision: view.data_revision, variables, ...(shared ? { shared } : {}) }, "data")); return true; }
    catch (failure) { report(failure, "写入会话数据"); return false; }
    finally { busy.value = null; }
  }
  async function closeRun() {
    const view = session.value; if (!view || locked.value) return;
    busy.value = "close";
    try { await management.commands.control({ workflowId: workspace.activeWorkflowId }, "close"); }
    catch (failure) { report(failure, "关闭运行"); }
    finally { busy.value = null; }
  }
  async function submitAgentAction(action: "extend_budget" | "retry_archive" | "retry_acceptance" | "retry_failed_node", requests = 0, attempts = 0) {
    const view = session.value;
    if (!view || locked.value || !view.available_actions?.includes(action)) return false;
    if (action === "extend_budget" && (!Number.isSafeInteger(requests) || !Number.isSafeInteger(attempts)
      || requests < 0 || attempts < 0 || requests + attempts === 0)) {
      notices.notify("rejected", "预算增额须为非负整数，且至少一项大于零 [invalid_request]", "增加预算"); return false;
    }
    const id = workspace.activeWorkflowId; busy.value = action;
    try {
      await management.commands.control({ workflowId: id }, action,
        action === "extend_budget" ? { add_model_requests: requests, add_model_attempts: attempts } : {});
      schedulePoll(id, viewGeneration); return true;
    } catch (failure) { report(failure, action === "extend_budget" ? "增加预算"
      : action === "retry_acceptance" ? "结果接纳重试" : action === "retry_failed_node" ? "重试失败节点" : "归档重试"); return false; }
    finally { busy.value = null; }
  }
  async function loadCandidates(workflowId = workspace.activeWorkflowId) {
    const entry = entries.value[workflowId];
    const view = entry?.session_id ? views.value[entry.session_id] : null;
    if (!view) return null;
    const generation = (candidateGenerations.get(workflowId) ?? 0) + 1;
    candidateGenerations.set(workflowId, generation);
    const mutationVersion = mutationGeneration, viewToken = workspace.currentViewToken();
    const currentRead = () => alive && candidateGenerations.get(workflowId) === generation
      && entries.value[workflowId] === entry && entry.session_id === view.workflow_session_id
      && entry.document.workflow_definition_id === view.workflow_definition_id
      && mutationGeneration === mutationVersion && workspace.isCurrentView(viewToken);
    try {
      const value = await management.queries.candidates(view.workflow_session_id, view.workflow_definition_id);
      const current = views.value[view.workflow_session_id];
      if (!currentRead()
        || current && (value.session_revision < current.revision || value.head_revision < current.head_revision)) return null;
      const previous = candidates.value[view.workflow_session_id];
      if (previous && previous.session_revision > value.session_revision) return previous;
      candidates.value[view.workflow_session_id] = value; return value;
    } catch (failure) { if (currentRead()) report(failure, "读取工作流候选"); return null; }
  }
  async function changeCandidate(candidateId: string, fork = false) {
    const view = session.value;
    const snapshot = view ? candidates.value[view.workflow_session_id] : null;
    const candidate = snapshot?.candidates.find(row => row.candidate_id === candidateId);
    if (!view || locked.value || !view.can_submit || !candidate?.can_select) return false;
    const id = workspace.activeWorkflowId, action = fork ? "candidate-fork" : "candidate-select";
    busy.value = action;
    try {
      await management.commands.restoreCandidate({ workflowId: id }, snapshot!, candidateId, fork);
      await refresh(id); await loadCandidates(id); return true;
    } catch (failure) { report(failure, fork ? "从候选分叉" : "选择工作流候选"); return false; }
    finally { busy.value = null; }
  }
  function setInputs(value: Record<string, unknown>) {
    if (!active.value || locked.value) return;
    active.value.external_inputs = graphClone(value); persistence?.();
  }
  function resetPrivateState(id: string, nodeId: string) {
    const entry = entries.value[id]; if (!entry || entry.pending || busy.value) return;
    if (!entry.document.nodes.some(node => node.node_binding_id === nodeId)) return;
    entry.state_mappings = [...(entry.state_mappings ?? []).filter(mapping => mapping.target_node_id !== nodeId),
      { source_node_id: nodeId, target_node_id: nodeId, action: "reset" }];
    persistence?.();
  }
  return { entries, catalog, frontendExtensions, packageLock, executionPackageLock, dataTypes, catalogLoading, catalogError, sessions, views, candidates, history, busy, active, document,
    session, pending, locked, primaryAction, statusLabel, selectedNodeIds, selectedEdgeId, canUndo, canRedo, setEventBindings, submitEvent,
    isGeneric, createWorkflow, createSerialAgentExample, typeFor, loadCatalog, setPersistenceGuard, storeSnapshot, restoreSnapshot,
    saveWorkflow, addNode, patchNode, patchNodeConfiguration, moveNodes, connect, removeSelection, reorderEdge, undo, duplicateSelection,
    replaceNode, setExecutionRoot, setControlDependencies, setObjectBindings, attachFrontendDisplay, patchFrontendSource,
    activate, refresh, createSession, selectSession, submitPrimary, reconcile, canRetryRejectedCopy, retryRejectedCopy, discardDraft, discardRejectedCopy, writeData, closeRun, setInputs, resetPrivateState, submitAgentAction, loadCandidates, changeCandidate,
    management, editing };
});
