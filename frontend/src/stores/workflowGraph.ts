import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { WorkbenchApiError, type FailureKind } from "../adapters/workbenchApi";
import { createGraphCommand as command, type GraphReceipt } from "../application/workflowCommands";
import { createWorkflowManagementRequests } from "../application/workflowManagementRequests";
import { createWorkflowManagement, workflowManagementQueries } from "../application/workflowManagement";
import { createWorkflowConfigurationCopy, type WorkflowConfigurationCopyApplication } from "../application/workflowConfigurationCopy";
import { createWorkflowEditing, type WorkflowEditOutcome } from "../application/workflowEditing";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { newerGraphObservation } from "../domain/graphObservation";
import { graphConnectionIssue } from "../domain/graphConnections";
import { defaultGraphConfigurationPolicy, graphClone, graphNodeAllowsAdjustable, graphObject, graphUuid, isGraphDocument, newGraph,
  isGraphObjectBindings, isGraphEventBindings, isGraphJsonValue, graphSignature, upgradeGraph, graphDanglingEdgeIssues, nodePorts,
  type GraphCommand, type GraphDocument, type GraphEdge, type GraphEntry, type GraphNode,
  type GraphNodeType, type GraphSession, type GraphDataType, type GraphPort, type GraphDiagnostic,
  type GraphManagementResult } from "../domain/workflowGraph";
import type { WorkflowPreview } from "../domain/workflowPreview";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { isGraphConfigurationDocument, type GraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateDocument, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { isWorkflowConfigurationCopyRequest, isWorkflowConfigurationCopyResult,
  workflowConfigurationCopyTargetName,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import type { WorkflowNodeConfigurationRequest, WorkflowNodeConfigurationResult } from "../plugins/workflowFrontendSdk";
import { syncVariableRegistrationEvent, variableRegistrationTarget } from "../domain/workflowVariableRegistration";
import { sessionSummaryFromView, type WorkflowSessionSummary } from "../domain/workflowDirectory";
import { sessionPreloadsIssue } from "../domain/sessionPreloads";
import type { GraphObjectBinding } from "../domain/workflowGraph";
import { evidenceCoordinate, isRequestEvidenceRecord, readPendingWorkflowEvidence, requestEvidencePorts,
  type PendingRequestEvidence, type RequestEvidenceCursor } from "../adapters/requestEvidenceApi";

type WorkflowConfigurationApplication = ReturnType<typeof createWorkflowConfigurationApplication>;
type ConfigurationHistoryAction = "edit" | "undo" | "redo" | "none";
/** One edit entry can end in a formal outcome or a reported failure; never a bare truthy value. */
export interface WorkflowEditFailure { outcome: "failed"; error: Error }
export type WorkflowEditResult = WorkflowEditOutcome | WorkflowEditFailure;
type AcceptedWorkflowEdit = Extract<WorkflowEditOutcome, { outcome: "accepted" }>;
export type { WorkflowEditOutcome };
function isAcceptedEdit(result: WorkflowEditResult | null): result is AcceptedWorkflowEdit {
  return result?.outcome === "accepted";
}
/** A definite refusal that kept the failed proposal, its baseline and the reason (Q03-1). */
export interface WorkflowRejectedEdit {
  workflowId: string;
  reason: string;
  kind: FailureKind;
  code?: string;
  at: number;
  /** Exact failed original request: proposed document, baseline counters and idempotency key. */
  request: GraphConfigurationUpdateRequest;
}
/**
 * Q11-2: the formal current was changed by someone else (typically an external Agent) while
 * this view held a local edit basis. The local proposal stays available as a rejected edit
 * or as this proposal copy; nothing is resubmitted or overwritten automatically.
 */
export interface WorkflowExternalConflict {
  workflowId: string;
  at: number;
  /** stale_edit_sequence / stale_configuration_basis / stale_revision / external_current */
  code: string;
  reason: string;
  targetLabel: string;
  localEditSequence: number | null;
  localRevision: number | null;
  /** Known once the formal current has been read; null until then. */
  externalEditSequence: number | null;
  externalRevision: number | null;
  /** The local unsubmitted proposal, preserved for explicit merge or abandonment. */
  proposal: GraphDocument | null;
  /** True after an explicit read adopted the external formal current; the proposal is still pending. */
  resolved: boolean;
}
/** Persistent structured save/edit failure (Q06-1); target identities stay readable (Q06-1). */export interface WorkflowEditFailureDetail {
  workflowId: string;
  action: "edit" | "undo" | "redo";
  at: number;
  reason: string;
  code?: string;
  status?: number;
  diagnostics: GraphDiagnostic[];
  targetIds: string[];
  targetLabel: string;
}
/** Persistent original run/control error, including prepare failures (O01, R01). */
export interface WorkflowRunFailureDetail {
  workflowId: string;
  operation: "start" | "control" | "agent" | "close";
  at: number;
  reason: string;
  code?: string;
  status?: number;
  phase?: string;
  diagnostics: GraphDiagnostic[];
  sessionId?: string | null;
  targetLabel: string;
}
export interface WorkflowPreviewState {
  workflowId: string;
  at: number;
  targetLabel: string;
  preview: WorkflowPreview | null;
  /** True when the edited document changed after the preview basis was captured. */
  stale: boolean;
  busy: boolean;
  error: string;
  failure: { reason: string; code?: string; status?: number; diagnostics: GraphDiagnostic[] } | null;
}

function configurationUpdateDocument(document: GraphDocument, editSequence: number | null): GraphConfigurationUpdateDocument {
  const next = graphClone(document) as GraphConfigurationUpdateDocument;
  next.schema_version = 3;
  next.object_bindings ??= [];
  if (editSequence === null) delete next.edit_sequence;
  else next.edit_sequence = editSequence;
  for (const node of next.nodes)
    node.configuration_policy ??= { schema_version: 1, prompt_mode: "fixed" };
  return next;
}
// Compare the edit body without the counters that a save is expected to move.
function sameConfigurationBody(left: GraphDocument, right: GraphDocument) {
  const body = (document: GraphDocument) =>
    graphSignature({ ...document, revision: 0, edit_sequence: 0 });
  return body(left) === body(right);
}

function syncProvidedSessionItems(document: GraphDocument, nodeId: string,
  provided: NonNullable<GraphNodeType["provided_session_item_types"]>, preserveExact: boolean,
  accesses: NonNullable<GraphNodeType["object_accesses"]> = []) {
  const bindings = document.object_bindings ?? [];
  const prior = bindings.filter(binding => binding.registrar_node_id === nodeId);
  document.object_bindings = bindings.filter(binding => binding.registrar_node_id !== nodeId);
  const retained = new Set<string>();
  for (const item of provided) {
    const existing = preserveExact ? prior.find(binding => binding.type_id === item.type_id
      && binding.schema_version === item.schema_version && !retained.has(binding.object_key)) : undefined;
    if (existing) {
      document.object_bindings.push(existing);
      retained.add(existing.object_key);
      continue;
    }
    let objectKey: string;
    do { objectKey = `item/${crypto.randomUUID()}`; }
    while (document.object_bindings.some(binding => binding.object_key === objectKey));
    document.object_bindings.push({ object_key: objectKey, type_id: item.type_id,
      schema_version: item.schema_version, scope: "shared", owner_node_id: null,
      readers: [], writers: [], registrar_node_id: nodeId });
  }
  const node = document.nodes.find(row => row.node_binding_id === nodeId);
  if (!node) return;
  for (const access of accesses) {
    const own = document.object_bindings.find(binding => binding.registrar_node_id === nodeId
      && binding.type_id === access.type_id && binding.schema_version === access.schema_version);
    if (!own) continue;
    if (access.access !== "write" && !own.readers.includes(nodeId)) own.readers.push(nodeId);
    if (access.access !== "read" && !own.writers.includes(nodeId)) own.writers.push(nodeId);
    node.config[access.config_field] = access.multiple ? [own.object_key] : own.object_key;
  }
}

/** One concrete reason a delete/replace is blocked before any submission (Q05-2/Q05-3). */
export type WorkflowEditBlockerKind = "node-reference" | "object-access" | "provided-binding"
  | "edge-port" | "edge-type" | "edge-input";
export interface WorkflowEditBlocker {
  kind: WorkflowEditBlockerKind;
  message: string;
  nodeId?: string;
  nodeTitle?: string;
  configField?: string;
  path?: string;
  targetNodeId?: string;
  targetNodeTitle?: string;
  targetComponent?: string;
  edgeId?: string;
  portId?: string;
  objectKey?: string;
  typeId?: string;
  schemaVersion?: number;
}
/** Persistent, workflow-scoped explanation for an edit that was blocked locally. */
export interface WorkflowEditBlock {
  workflowId: string;
  action: "delete-selection" | "replace-node";
  title: string;
  summary: string;
  targetIds: string[];
  targetLabel: string;
  items: WorkflowEditBlocker[];
  at: number;
}
type NodeTypeLookup = (node: Pick<GraphNode, "component_id" | "component_version">) => GraphNodeType | undefined;
function documentNodeTitle(document: GraphDocument, id: string): string {
  return document.nodes.find(node => node.node_binding_id === id)?.title ?? "";
}
function declaredConfigValues(node: GraphNode, field: string, multiple: boolean):
  { value: string; index: number }[] {
  const raw = node.config[field];
  const values = multiple && Array.isArray(raw) ? raw : [raw];
  return values.flatMap((value, index) => typeof value === "string" && value
    ? [{ value, index }] : []);
}
function configFieldPath(document: GraphDocument, node: GraphNode, field: string, multiple: boolean,
  index: number): string {
  return `/nodes/${document.nodes.indexOf(node)}/config/${field}${multiple ? `/${index}` : ""}`;
}
/**
 * Q05-2: compute whether deleting these nodes leaves a surviving declared reference or
 * invalid object key. Data/control edges, event targets, object bindings and preloads
 * are still cleaned in the same batch; declared config references never are.
 */
function removalBlockers(document: GraphDocument, typeOf: NodeTypeLookup,
  removedIds: ReadonlySet<string>): WorkflowEditBlocker[] {
  const blockers: WorkflowEditBlocker[] = [];
  const removedBindings = (document.object_bindings ?? []).filter(binding =>
    binding.registrar_node_id && removedIds.has(binding.registrar_node_id)
    || binding.scope === "private" && binding.owner_node_id !== null && removedIds.has(binding.owner_node_id));
  const removedKeys = new Map(removedBindings.map(binding => [binding.object_key, binding]));
  for (const node of document.nodes) {
    if (removedIds.has(node.node_binding_id)) continue;
    const type = typeOf(node);
    for (const reference of type?.config_node_references ?? []) {
      for (const target of declaredConfigValues(node, reference.config_field, reference.multiple)) {
        if (!removedIds.has(target.value)) continue;
        blockers.push({ kind: "node-reference",
          message: `存活节点“${node.title}”仍按声明引用将被删除的节点“${documentNodeTitle(document, target.value)}”`,
          nodeId: node.node_binding_id, nodeTitle: node.title, configField: reference.config_field,
          path: configFieldPath(document, node, reference.config_field, reference.multiple, target.index),
          targetNodeId: target.value, targetNodeTitle: documentNodeTitle(document, target.value) });
      }
    }
    for (const access of type?.object_accesses ?? []) {
      for (const used of declaredConfigValues(node, access.config_field, access.multiple)) {
        const binding = removedKeys.get(used.value);
        if (!binding || binding.type_id !== access.type_id || binding.schema_version !== access.schema_version)
          continue;
        blockers.push({ kind: "object-access",
          message: `存活节点“${node.title}”仍使用将被删除的会话对象 ${used.value}`,
          nodeId: node.node_binding_id, nodeTitle: node.title, configField: access.config_field,
          path: configFieldPath(document, node, access.config_field, access.multiple, used.index),
          objectKey: used.value, typeId: binding.type_id, schemaVersion: binding.schema_version,
          ...(binding.registrar_node_id ? { targetNodeId: binding.registrar_node_id,
            targetNodeTitle: documentNodeTitle(document, binding.registrar_node_id) } : {}) });
      }
    }
  }
  return blockers;
}
function portsCompatible(output: GraphPort | undefined, input: GraphPort | undefined): boolean {
  if (!output || !input) return false;
  const version = output.data_schema_version ?? 1;
  return output.data_type === input.data_type && version === (input.data_schema_version ?? 1)
    || (input.accepted_types ?? []).some(type =>
      type.data_type === output.data_type && type.data_schema_version === version);
}
/**
 * Q05-3: keep only edges that verifiably fit the new component. Compatibility is matched
 * by exact port id and declared types; no mapping is guessed and nothing is deleted.
 */
function replacementBlockers(document: GraphDocument, typeOf: NodeTypeLookup, node: GraphNode,
  nextType: GraphNodeType): WorkflowEditBlocker[] {
  const blockers: WorkflowEditBlocker[] = [];
  const candidate = { ...node, config: graphClone(nextType.default_config) };
  const nextInputs = nodePorts(nextType, candidate, "inputs");
  const nextOutputs = nodePorts(nextType, candidate, "outputs");
  document.edges.forEach((edge, index) => {
    if (edge.source_node_id === node.node_binding_id) {
      const output = nextOutputs.find(port => port.port_id === edge.source_port_id);
      const target = document.nodes.find(row => row.node_binding_id === edge.target_node_id);
      const targetType = target ? typeOf(target) : undefined;
      const input = target && targetType
        ? nodePorts(targetType, target, "inputs").find(port => port.port_id === edge.target_port_id) : undefined;
      if (!output) blockers.push({ kind: "edge-port", edgeId: edge.edge_id, portId: edge.source_port_id,
        path: `/edges/${index}/source_port_id`, targetNodeId: edge.target_node_id,
        targetNodeTitle: target?.title ?? "",
        message: `连线 ${edge.edge_id} 的源端口 ${edge.source_port_id} 在新组件中不存在` });
      else if (!target || !targetType || !input) blockers.push({ kind: "edge-port", edgeId: edge.edge_id,
        portId: edge.target_port_id, path: `/edges/${index}/target_port_id`,
        message: `无法确认连线 ${edge.edge_id} 的目标端口 ${edge.target_port_id}，不能确认替换后兼容` });
      else if (!portsCompatible(output, input)) blockers.push({ kind: "edge-type", edgeId: edge.edge_id,
        portId: edge.source_port_id, path: `/edges/${index}/source_port_id`, targetNodeId: target.node_binding_id,
        targetNodeTitle: target.title,
        message: `连线 ${edge.edge_id} 端口不兼容：新输出 ${output.data_type}@${output.data_schema_version ?? 1} → ${input.data_type}@${input.data_schema_version ?? 1}` });
    } else if (edge.target_node_id === node.node_binding_id) {
      const input = nextInputs.find(port => port.port_id === edge.target_port_id);
      const source = document.nodes.find(row => row.node_binding_id === edge.source_node_id);
      const sourceType = source ? typeOf(source) : undefined;
      const output = source && sourceType
        ? nodePorts(sourceType, source, "outputs").find(port => port.port_id === edge.source_port_id) : undefined;
      if (!input) blockers.push({ kind: "edge-port", edgeId: edge.edge_id, portId: edge.target_port_id,
        path: `/edges/${index}/target_port_id`, targetNodeId: edge.source_node_id,
        targetNodeTitle: source?.title ?? "",
        message: `连线 ${edge.edge_id} 的目标端口 ${edge.target_port_id} 在新组件中不存在` });
      else if (!source || !sourceType || !output) blockers.push({ kind: "edge-port", edgeId: edge.edge_id,
        portId: edge.source_port_id, path: `/edges/${index}/source_port_id`,
        message: `无法确认连线 ${edge.edge_id} 的源端口 ${edge.source_port_id}，不能确认替换后兼容` });
      else {
        if (!portsCompatible(output, input)) blockers.push({ kind: "edge-type", edgeId: edge.edge_id,
          portId: edge.target_port_id, path: `/edges/${index}/target_port_id`, targetNodeId: source.node_binding_id,
          targetNodeTitle: source.title,
          message: `连线 ${edge.edge_id} 端口不兼容：${output.data_type}@${output.data_schema_version ?? 1} → 新输入 ${input.data_type}@${input.data_schema_version ?? 1}` });
        const siblings = document.edges.filter(row => row.target_node_id === node.node_binding_id
          && row.target_port_id === edge.target_port_id);
        if (!input.multiple && siblings.length > 1) blockers.push({ kind: "edge-input", edgeId: edge.edge_id,
          portId: edge.target_port_id, path: `/edges/${index}/target_port_id`,
          message: `输入端口 ${edge.target_port_id} 已有 ${siblings.length} 条连线，新组件此输入只接受一条` });
      }
    }
  });
  // Surviving declared references only accept the new component/version when listed.
  for (const other of document.nodes) {
    if (other.node_binding_id === node.node_binding_id) continue;
    const otherType = typeOf(other);
    for (const reference of otherType?.config_node_references ?? []) {
      for (const target of declaredConfigValues(other, reference.config_field, reference.multiple)) {
        if (target.value !== node.node_binding_id || !reference.target_types.length) continue;
        if (reference.target_types.some(row => row.component_id === nextType.component_id
          && row.component_version === nextType.component_version)) continue;
        blockers.push({ kind: "node-reference",
          message: `存活节点“${other.title}”的声明引用只接受 ${reference.target_types
            .map(row => `${row.component_id}@${row.component_version}`).join("/")}，新组件 ${nextType.component_id}@${nextType.component_version} 不适用`,
          nodeId: other.node_binding_id, nodeTitle: other.title, configField: reference.config_field,
          path: configFieldPath(document, other, reference.config_field, reference.multiple, target.index),
          targetNodeId: node.node_binding_id, targetNodeTitle: node.title,
          targetComponent: `${nextType.component_id}@${nextType.component_version}` });
      }
    }
  }
  // Old provided item registrations that would disappear may not be silently migrated.
  const nextProvided = new Set((nextType.provided_session_item_types ?? [])
    .map(item => `${item.type_id}@${item.schema_version}`));
  for (const binding of document.object_bindings ?? []) {
    if (binding.registrar_node_id !== node.node_binding_id) continue;
    if (nextProvided.has(`${binding.type_id}@${binding.schema_version}`)) continue;
    const preload = (document.session_preloads ?? []).some(entry => entry.object_key === binding.object_key);
    const externalReaders = binding.readers.filter(id => id !== node.node_binding_id);
    const externalWriters = binding.writers.filter(id => id !== node.node_binding_id);
    const users: WorkflowEditBlocker[] = [];
    for (const other of document.nodes) {
      if (other.node_binding_id === node.node_binding_id) continue;
      const otherType = typeOf(other);
      for (const access of otherType?.object_accesses ?? []) {
        if (access.type_id !== binding.type_id || access.schema_version !== binding.schema_version) continue;
        for (const used of declaredConfigValues(other, access.config_field, access.multiple)) {
          if (used.value !== binding.object_key) continue;
          users.push({ kind: "object-access",
            message: `存活节点“${other.title}”仍使用对象 ${binding.object_key}`,
            nodeId: other.node_binding_id, nodeTitle: other.title, configField: access.config_field,
            path: configFieldPath(document, other, access.config_field, access.multiple, used.index),
            objectKey: binding.object_key, typeId: binding.type_id, schemaVersion: binding.schema_version });
        }
      }
    }
    if (!preload && !externalReaders.length && !externalWriters.length && !users.length) continue;
    blockers.push({ kind: "provided-binding",
      message: `旧组件的会话对象登记 ${binding.object_key} 不适用于新组件，且仍被${preload ? "会话预加载" : "其他节点使用"}，请先明确处理`,
      nodeId: node.node_binding_id, nodeTitle: node.title, objectKey: binding.object_key,
      typeId: binding.type_id, schemaVersion: binding.schema_version,
      path: `/object_bindings/${(document.object_bindings ?? []).indexOf(binding)}`,
      targetNodeId: externalReaders[0] ?? externalWriters[0] ?? undefined,
      targetNodeTitle: documentNodeTitle(document, externalReaders[0] ?? externalWriters[0] ?? "") });
  }
  return blockers;
}

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
  const definitionsLoading = ref(false);
  /** Formal session directory rows (summary projection); never full session views. */
  const sessionRows = ref<Record<string, WorkflowSessionSummary[]>>({});
  const views = ref<Record<string, GraphSession>>({});
  const history = ref<Record<string, { past: GraphDocument[]; future: GraphDocument[] }>>({});
  const busy = ref<string | null>(null);
  const applicationBusy = ref(false);
  const managementTargets = new Map<string, { entry: GraphEntry; sessionId: string | null;
    selection: number; view: ReturnType<typeof workspace.currentViewToken> }>();
  const managementRequests = createWorkflowManagementRequests({
    current: id => alive && !!entries.value[id],
    accept: acceptManagementReceipt,
    completed: (id, command) => finishManagedDeletion(id, command),
  });
  const configurationSubmitting = ref<Record<string, boolean>>({});
  // Q11-2: reads (formal-current reload, preview, definition read) never take the edit lock.
  // Only a real configuration submission sets configurationSubmitting; reads use this guard.
  const configurationReads = ref<Record<string, boolean>>({});
  // A formal current changed by another writer stays visible per workflow until handled.
  const externalConflicts = ref<Record<string, WorkflowExternalConflict | null>>({});
  // Failed proposals stay visible per workflow until the user handles them (Q03-1);
  // they are memory-only and are never resubmitted automatically.
  const rejectedEdits = ref<Record<string, WorkflowRejectedEdit>>({});
  // Q05-2/Q05-3: locally blocked delete/replace keep their impact list visible per workflow
  // until handled; they are memory-only and nothing was submitted.
  const editBlocks = ref<Record<string, WorkflowEditBlock | null>>({});
  const editFailures = ref<Record<string, WorkflowEditFailureDetail | null>>({});
  const runFailures = ref<Record<string, WorkflowRunFailureDetail | null>>({});
  const previewStates = ref<Record<string, WorkflowPreviewState | null>>({});
  // Q09: a fixed definition revision is immutable, so a successful read may be reused for
  // the same (workflow_definition_id, definition_revision) across chain revisions and
  // sessions. Failures are never cached, and the cache stays bounded per store instance.
  const frozenDefinitions = new Map<string, GraphDocument>();
  const FROZEN_DEFINITION_CACHE_LIMIT = 8;
  const configurationApplicationVersion = ref(0);
  const discoveredRequests = ref<PendingRequestEvidence[]>([]);
  const pendingDiscoveryBusy = ref(false), pendingDiscoveryError = ref("");
  const pendingDiscoveryCursor = ref<RequestEvidenceCursor | null>(null);
  const recoveryErrors = ref<Record<string, string>>({});
  const recoveryBusy = ref<Record<string, boolean>>({});
  let configurationApplication: WorkflowConfigurationApplication | null = null;
  let copyApplication: WorkflowConfigurationCopyApplication | null = null;
  type CopyChannel = ReturnType<WorkflowConfigurationCopyApplication["forSource"]>;
  const copyChannels = new Map<string, CopyChannel>();
  const copySubmitting = new Set<string>();
  const copyInitializedSources = new Set<string>();
  const copyOpenTokens = new Map<string, { targetId: string;
    token: ReturnType<typeof workspace.currentViewToken> }>();
  const copyVersion = ref(0);
  const bumpCopyVersion = () => { copyVersion.value++; };
  const selectingSessionFor = ref<string | null>(null);
  const selectedNodeIds = ref<string[]>([]);
  const selectedEdgeId = ref<string | null>(null);
  const active = computed(() => entries.value[workspace.activeWorkflowId]);
  const document = computed(() => active.value?.document ?? null);
  // Q04-3/Q04-4: nodes (multi) and one edge are mutually exclusive. These actions are
  // the single writers that maintain the invariant; S2-03 moves components onto them
  // while the exported refs stay readable for compatibility.
  function clearSelection(): void {
    if (!selectedNodeIds.value.length && selectedEdgeId.value === null) return;
    selectedNodeIds.value = []; selectedEdgeId.value = null;
  }
  function selectNodes(ids: readonly string[]): void {
    const existing = new Set(document.value?.nodes.map(node => node.node_binding_id) ?? []);
    selectedNodeIds.value = [...new Set(ids)].filter(id => existing.has(id));
    selectedEdgeId.value = null;
  }
  function selectEdge(edgeId: string | null): void {
    const exists = !!edgeId && !!document.value && (
      document.value.edges.some(edge => edge.edge_id === edgeId)
      || (document.value.control_edges ?? []).some(edge => edge.edge_id === edgeId));
    selectedNodeIds.value = [];
    selectedEdgeId.value = exists ? edgeId : null;
  }
  function pruneSelection(workflowId = workspace.activeWorkflowId): void {
    // A late result for another workflow must never clear the new view's selection.
    if (workflowId !== workspace.activeWorkflowId) return;
    const current = document.value;
    if (!current) { clearSelection(); return; }
    const nodes = new Set(current.nodes.map(node => node.node_binding_id));
    const keptNodes = selectedNodeIds.value.filter(id => nodes.has(id));
    if (keptNodes.length) {
      if (keptNodes.length !== selectedNodeIds.value.length) selectedNodeIds.value = keptNodes;
      if (selectedEdgeId.value !== null) selectedEdgeId.value = null;
      return;
    }
    if (selectedNodeIds.value.length) selectedNodeIds.value = [];
    if (selectedEdgeId.value !== null && !current.edges.some(edge => edge.edge_id === selectedEdgeId.value)
      && !(current.control_edges ?? []).some(edge => edge.edge_id === selectedEdgeId.value))
      selectedEdgeId.value = null;
  }
  const activeRejectedEdit = computed(() => rejectedEdits.value[workspace.activeWorkflowId] ?? null);
  /** Q11-2: external formal-version conflict visible for the active workflow until handled. */
  const activeExternalConflict = computed(() => externalConflicts.value[workspace.activeWorkflowId] ?? null);
  const activeEditBlock = computed(() => editBlocks.value[workspace.activeWorkflowId] ?? null);
  const activeEditFailure = computed(() => editFailures.value[workspace.activeWorkflowId] ?? null);
  const activeRunFailure = computed(() => runFailures.value[workspace.activeWorkflowId] ?? null);
  const activePreview = computed(() => previewStates.value[workspace.activeWorkflowId] ?? null);
  function dismissEditFailure(workflowId = workspace.activeWorkflowId): void {
    if (editFailures.value[workflowId]) delete editFailures.value[workflowId];
  }
  function dismissRunFailure(workflowId = workspace.activeWorkflowId): void {
    if (runFailures.value[workflowId]) delete runFailures.value[workflowId];
  }
  function dismissPreview(workflowId = workspace.activeWorkflowId): void {
    if (previewStates.value[workflowId]) delete previewStates.value[workflowId];
  }
  function recordEditBlock(block: WorkflowEditBlock): void {
    editBlocks.value[block.workflowId] = block;
  }
  function clearEditBlock(workflowId: string): void {
    if (editBlocks.value[workflowId]) delete editBlocks.value[workflowId];
  }
  function dismissEditBlock(workflowId = workspace.activeWorkflowId): void {
    clearEditBlock(workflowId);
  }
  /** The one pending derivation ask belongs to this workflow only (Q03-4). */
  const activeDerivationAsk = computed(() => {
    configurationApplicationVersion.value;
    const id = workspace.activeWorkflowId;
    return id && configurationApplication ? configurationApplication.forWorkflow(id).confirmation.value : null;
  });
  /** Persistent per-workflow configuration request state for the feedback area. */
  const activeConfigurationRequest = computed(() => {
    configurationApplicationVersion.value;
    const id = workspace.activeWorkflowId;
    if (!id || !configurationApplication) return null;
    const request = configurationApplication.forWorkflow(id);
    return { workflowId: id, busy: request.busy.value, pending: request.pending.value !== null,
      progress: request.progress.value, error: request.error.value,
      acceptanceFailed: request.acceptanceFailed.value,
      confirmation: request.confirmation.value !== null };
  });
  /** A formal fixed revision with no current configuration: read-only, never editable. */
  const fixedOnly = computed(() => active.value?.fixed_only === true);
  const session = computed(() => active.value?.session_id ? views.value[active.value.session_id] ?? null : null);
  const activeManagement = computed(() => active.value
    ? managementRequests.forWorkflow(active.value.document.workflow_definition_id) : null);
  const pending = computed(() => activeManagement.value?.pendingCommand.value ?? null);
  const activeConfigurationLocked = computed(() => {
    configurationApplicationVersion.value;
    const id = workspace.activeWorkflowId;
    return !!id && (!!configurationSubmitting.value[id]
      || !!configurationApplication?.forWorkflow(id).locked.value);
  });
  const mutationLocked = computed(() => !!busy.value || applicationBusy.value || activeConfigurationLocked.value
    || !!activeManagement.value?.locked.value
    || !!pending.value || !!workspace.activeWorkflow.copyPending
    || fixedOnly.value
    || workspace.workflows.some(row => row.copyPending && row.sourceId === workspace.activeWorkflowId));
  const controlLocked = computed(() => mutationLocked.value || selectingSessionFor.value === workspace.activeWorkflowId
    || !!workspace.activeWorkflow.archived || !!workspace.activeWorkflow.deleted
    || !!session.value?.archived);
  const locked = computed(() => controlLocked.value || !!session.value?.readonly);
  /** Configuration copy reads the formal saved current only.
   *
   * Workflow/session archive and read-only state belong to editing and execution,
   * not to copy: an archived source keeps its formal current (G-A allows it), and
   * the selected session has no part in the copy request.  Draft, fixed-only,
   * remotely deleted, pending and already-pending-copy sources stay blocked.
   */
  const copyLocked = computed(() => {
    copyVersion.value; // channel state refs and lazily restored channels invalidate the gate
    const id = workspace.activeWorkflowId, workflow = workspace.activeWorkflow;
    const channel = copyChannels.get(id.toLowerCase());
    return !id || !entries.value[id] || fixedOnly.value || workflow.state !== "saved"
      || !!workflow.deleted || !!workflow.copyPending
      || !!workspace.workflows.some(row => row.copyPending && row.sourceId === id)
      || !!channel?.busy.value || !!channel?.pending.value || !!channel?.locked.value
      || !!busy.value || applicationBusy.value;
  });
  const primaryAction = computed(() => ["running", "prepared", "pausing"].includes(session.value?.status ?? "") ? "pause"
    : session.value?.available_actions?.includes("resume") || session.value?.status === "paused" ? "resume" : "start");
  const statusLabel = computed(() => busy.value ? "提交中" : primaryAction.value === "pause" ? "暂停"
    : primaryAction.value === "resume" ? "继续" : "启动");
  const editorStable = computed(() => !active.value?.session_id || !!session.value?.can_submit
    && !["prepared", "running", "pausing", "paused"].includes(session.value.status));
  const editorLocked = computed(() => locked.value || !editorStable.value);
  const canUndo = computed(() => !!history.value[workspace.activeWorkflowId]?.past.length
    && !locked.value && editorStable.value);
  const canRedo = computed(() => !!history.value[workspace.activeWorkflowId]?.future.length
    && !locked.value && editorStable.value);
  let alive = true;
  let viewGeneration = 0;
  let sessionSelectionGeneration = 0;
  let mutationGeneration = 0;
  const refreshGenerations = new Map<string, number>();
  const definitionEntryLoads = new Map<string, Promise<boolean>>();
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  onScopeDispose(() => { alive = false; frontendExtensions.value = []; frozenDefinitions.clear(); viewGeneration++; sessionSelectionGeneration++; if (pollTimer) clearTimeout(pollTimer); });
  const isGeneric = (id: string) => !!entries.value[id];
  function selectConfigurationBasis() {
    const id = workspace.activeWorkflowId, entry = entries.value[id];
    if (!configurationApplication || !graphUuid(id)) return;
    configurationApplication.select(id, entry?.session_id ?? null);
    void configurationApplication.forWorkflow(id).initialize()
      .catch(failure => report(failure, "读取工作流原请求"));
  }
  watch(() => workspace.activeWorkflowId, () => clearSelection());
  watch(() => [workspace.activeWorkflowId, entries.value[workspace.activeWorkflowId]?.session_id] as const,
    selectConfigurationBasis);
  function installWorkflowConfigurationApplication(application: WorkflowConfigurationApplication,
    options: { seed?: boolean } = {}) {
    configurationApplication = application;
    configurationApplicationVersion.value++;
    // Production installs before the directory read, so the current read below
    // establishes accepted. Tests that construct entries by hand must ask for
    // this explicit seed instead of relying on an implicit reverse observe.
    if (options.seed)
      for (const entry of Object.values(entries.value))
        if (isGraphConfigurationDocument(entry.document) && Number.isSafeInteger(entry.document.edit_sequence))
          application.observe(entry.document);
    selectConfigurationBasis();
  }
  function storeSnapshot(): { schema_version: 1; entries: Record<string, GraphEntry> } {
    return graphClone({ schema_version: 1, entries: entries.value });
  }
  async function registerWorkflowDraft(value: GraphDocument, description = ""): Promise<string> {
    const doc = graphClone(value), id = doc.workflow_definition_id;
    entries.value[id] = { document: doc, saved_revision: 0, session_id: null };
    history.value[id] = { past: [], future: [] };
    workspace.registerGraphWorkflow({ id, title: doc.name, description, nodeCount: doc.nodes.length, state: "draft" });
    workspace.openWorkflow(id);
    clearSelection();
    if (configurationApplication) {
      try { await submitConfigurationDocument(id, doc, "none"); }
      catch (failure) { report(failure, "创建工作流配置"); }
    }
    return id;
  }
  function createWorkflow(): Promise<string> {
    return registerWorkflowDraft(newGraph("新工作流", executionPackageLock.value));
  }
  async function saveWorkflow(_id = workspace.activeWorkflowId) {
    notices.notify("unavailable", "旧整图发布入口已退役；正式保存暂不可用，未提交新请求", "保存工作流");
    return false;
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
  function projectAcceptedConfiguration(workflowId: string, archived: boolean) {
    const accepted = configurationApplication?.accepted(workflowId);
    if (!accepted) return false;
    if (!entries.value[workflowId]) {
      entries.value[workflowId] = { document: accepted, saved_document: graphClone(accepted),
        saved_revision: accepted.revision, session_id: null };
      history.value[workflowId] = { past: [], future: [] };
      workspace.registerGraphWorkflow({ id: workflowId, title: accepted.name, description: "",
        nodeCount: accepted.nodes.length, state: "saved", archived });
    }
    projectAcceptedState(workflowId, workflowId, accepted, workspace.currentViewToken(), "none", false);
    workspace.setWorkflowManagement(workflowId, archived);
    return true;
  }
  /** Refresh only the formal directory projection from the summary query.
   *
   * This never constructs an editor entry and never accepts a summary row as a
   * configuration document. Opening a workflow goes through
   * `ensureDefinitionEntry` and the single configuration acceptance path.
   */
  async function loadDefinitions() {
    if (definitionsLoading.value) return false;
    definitionsLoading.value = true;
    const generation = mutationGeneration;
    try {
      const rows = await management.queries.definitionSummaries(true);
      if (!alive || generation !== mutationGeneration) return false;
      // A formal row that is listed but withheld from this projection is only older
      // than an already accepted current; it must not be treated as a deletion.
      const listed = new Set(rows.map(row => row.workflow_definition_id));
      // An opened workflow keeps its accepted basis. A directory row older than the
      // accepted current must not roll the visible projection back.
      workspace.applyDefinitionSummaries(rows.filter(row => {
        const accepted = configurationApplication?.accepted(row.workflow_definition_id);
        if (!accepted) return true;
        return row.edit_sequence !== null && row.edit_sequence >= accepted.edit_sequence;
      }), { retainMissing: workflowId => listed.has(workflowId) });
      selectConfigurationBasis();
      return true;
    } catch (failure) { report(failure, "读取工作流目录"); return false; }
    finally { definitionsLoading.value = false; }
  }
  /** Read the formal current configuration only when a workflow is opened.
   *
   * The existing configuration acceptance arbitrates duplicates/stale/conflict;
   * a formal fixed revision without a current configuration stays a read-only
   * fixed display and is never accepted as an editable current configuration.
   */
  async function ensureDefinitionEntry(id: string): Promise<boolean> {
    if (!alive || !graphUuid(id)) return false;
    if (entries.value[id]) return true;
    const loading = definitionEntryLoads.get(id);
    if (loading) return loading;
    const load = (async () => {
      try {
        const document = await management.queries.definition(id);
        if (!alive) return false;
        if (entries.value[id]) return true;
        if (!isGraphDocument(document) || document.workflow_definition_id !== id)
          throw new WorkbenchApiError("unavailable", "工作流定义身份或版本不匹配");
        const workflow = workspace.workflows.find(row => row.id === id);
        const archived = workflow?.archived === true;
        if (isGraphConfigurationDocument(document)) {
          if (configurationApplication) {
            const prior = configurationApplication.accepted(id);
            const acceptance = configurationApplication.acceptDocument(document);
            if (acceptance.status === "conflict" || acceptance.status === "invalid")
              throw new WorkbenchApiError("unavailable", acceptance.message ?? "工作流配置无法接纳");
            // Q11-2: a formal current newer than the already accepted one was changed by
            // another writer; keep it visible where the workflow is opened.
            observeExternalCurrent(id, prior, configurationApplication.accepted(id), false);
            if (!projectAcceptedConfiguration(id, archived))
              throw new WorkbenchApiError("unknown", "配置结果尚未被正式配置应用接纳");
          } else {
            entries.value[id] = { document: graphClone(document), saved_document: graphClone(document),
              saved_revision: document.revision, latest_published_revision: document.revision,
              session_id: null };
            history.value[id] = { past: [], future: [] };
          }
          if (!workspace.workflows.some(row => row.id === id))
            workspace.registerGraphWorkflow({ id, title: document.name, description: "",
              nodeCount: document.nodes.length, state: "saved", archived });
          workspace.markWorkflowSaved(id, document.name);
          workspace.setWorkflowManagement(id, archived);
          workspace.setWorkflowCurrent(id, document.revision, document.edit_sequence);
          return true;
        }
        // Formal fixed record without current configuration: keep the honest
        // fixed read-only display; do not invent edit_sequence or acceptance.
        const fixed = graphClone(document);
        entries.value[id] = { document: fixed, saved_document: graphClone(fixed),
          saved_revision: fixed.revision, latest_published_revision: fixed.revision,
          session_id: null, fixed_only: true };
        history.value[id] = { past: [], future: [] };
        if (!workspace.workflows.some(row => row.id === id))
          workspace.registerGraphWorkflow({ id, title: fixed.name, description: "",
            nodeCount: fixed.nodes.length, state: "saved", archived });
        workspace.updateWorkflowProjection(id, fixed.name, fixed.nodes.length);
        workspace.markWorkflowSaved(id, fixed.name);
        workspace.setWorkflowManagement(id, archived);
        const fixedRow = workspace.workflows.find(row => row.id === id);
        if (fixedRow) {
          fixedRow.fixedDefinitionRevision = fixed.revision;
          fixedRow.editSequence = null;
          fixedRow.hasCurrentConfiguration = false;
        }
        notices.notify("unavailable",
          `工作流 ${fixed.name || id} 只有固定修订、没有正式 current 配置，仅可按只读方式查看`,
          "打开工作流");
        return true;
      } catch (failure) {
        if (alive) report(failure, "读取工作流配置");
        return false;
      } finally { definitionEntryLoads.delete(id); }
    })();
    definitionEntryLoads.set(id, load);
    return load;
  }
  function report(failure: unknown, action: string) {
    const error = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", String(failure));
    notices.notify(error.kind, error.reason, action);
  }
  function projectAcceptedState(sourceId: string, targetId: string, accepted: GraphConfigurationDocument,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction,
    derived: boolean) {
    const source = entries.value[sourceId];
    if (!source) throw new WorkbenchApiError("unknown", "配置结果的原工作流已不存在");
    if (!derived) {
      const unchanged = source.document.edit_sequence === accepted.edit_sequence
        && graphSignature(source.document) === graphSignature(accepted);
      if (!unchanged) {
        const record = history.value[sourceId] ?? { past: [], future: [] };
        if (historyAction === "edit" && source.saved_revision > 0) {
          record.past = [...record.past, graphClone(source.document)].slice(-60);
          record.future = [];
        } else if (historyAction === "undo") {
          const previous = record.past.at(-1);
          if (previous) {
            record.past = record.past.slice(0, -1);
            record.future = [...record.future, graphClone(source.document)].slice(-60);
          }
        } else if (historyAction === "redo") {
          const next = record.future.at(-1);
          if (next) {
            record.future = record.future.slice(0, -1);
            record.past = [...record.past, graphClone(source.document)].slice(-60);
          }
        }
        history.value[sourceId] = record;
        source.document = accepted;
      }
      source.saved_document = graphClone(accepted);
      source.saved_revision = accepted.revision;
      source.latest_published_revision = Math.max(source.latest_published_revision ?? 0, accepted.revision);
      workspace.updateWorkflowProjection(sourceId, accepted.name, accepted.nodes.length);
      workspace.markWorkflowSaved(sourceId, accepted.name);
      workspace.setWorkflowCurrent(sourceId, accepted.revision, accepted.edit_sequence);
      // Accepted formal documents are new reality: keep only still-existing identities.
      if (workspace.activeWorkflowId === sourceId) pruneSelection(sourceId);
      return { workflowId: targetId, document: accepted };
    }
    const existing = entries.value[targetId];
    entries.value[targetId] = { ...(existing ?? {}), document: accepted, saved_document: graphClone(accepted),
      saved_revision: accepted.revision, latest_published_revision: accepted.revision,
      session_id: null };
    history.value[targetId] = history.value[targetId] ?? { past: [], future: [] };
    if (!workspace.workflows.some(row => row.id === targetId))
      workspace.registerGraphWorkflow({ id: targetId, title: accepted.name, description: "",
        nodeCount: accepted.nodes.length, state: "saved", sessionCount: 0 });
    workspace.updateWorkflowProjection(targetId, accepted.name, accepted.nodes.length);
    workspace.markWorkflowSaved(targetId, accepted.name);
    workspace.setWorkflowCurrent(targetId, accepted.revision, accepted.edit_sequence);
    if (workspace.activeWorkflowId === sourceId && workspace.isCurrentView(viewToken))
      workspace.openWorkflow(targetId);
    if (workspace.activeWorkflowId === targetId) pruneSelection(targetId);
    return { workflowId: targetId, document: accepted };
  }
  function projectConfigurationResult(sourceId: string, result: GraphConfigurationResult,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction) {
    if (result.outcome === "confirmation_required") return null;
    const targetId = result.workflow_definition_id, derived = targetId !== sourceId;
    if (derived && result.outcome !== "derived")
      throw new WorkbenchApiError("unknown", "配置回执的工作流归属无效");
    const accepted = configurationApplication?.accepted(targetId);
    if (!accepted) throw new WorkbenchApiError("unknown", "配置结果尚未被正式配置应用接纳");
    return projectAcceptedState(sourceId, targetId, accepted, viewToken, historyAction, derived);
  }
  // A save may have been accepted before evidence closing failed. Project that
  // accepted state without reading the unaccepted lastResult backdoor.
  function projectChangedAcceptance(workflowId: string,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction) {
    const accepted = configurationApplication?.accepted(workflowId), source = entries.value[workflowId];
    if (!accepted || !source) return false;
    if (source.document.edit_sequence === accepted.edit_sequence
      && graphSignature(source.document) === graphSignature(accepted)) return false;
    projectAcceptedState(workflowId, workflowId, accepted, viewToken, historyAction, false);
    return true;
  }
  function clearRejectedEdit(workflowId: string): void {
    if (!rejectedEdits.value[workflowId]) return;
    delete rejectedEdits.value[workflowId];
    configurationApplicationVersion.value++;
  }
  /** Keep the exact failed proposal when the service gave a definite refusal (Q03-1). */
  function recordRejectedEdit(workflowId: string, failure: unknown,
    request: GraphConfigurationUpdateRequest | null): void {
    if (!request || !(failure instanceof WorkbenchApiError) || failure.kind !== "rejected") return;
    rejectedEdits.value[workflowId] = { workflowId, reason: failure.reason, kind: failure.kind,
      ...(failure.code ? { code: failure.code } : {}), at: Date.now(), request: graphClone(request) };
    configurationApplicationVersion.value++;
  }
  function dismissRejectedEdit(workflowId = workspace.activeWorkflowId): void {
    clearRejectedEdit(workflowId);
    clearExternalConflict(workflowId);
  }
  // Q11-2: these service rejections mean the formal current changed under this edit basis
  // (CAS/stale), most often because another client or an external Agent submitted first.
  const EXTERNAL_CHANGE_CODES = new Set([
    "stale_edit_sequence", "stale_configuration_basis", "stale_revision",
  ]);
  function conflictCounters(document: GraphDocument | null | undefined) {
    const sequence = document && Number.isSafeInteger(document.edit_sequence) ? Number(document.edit_sequence) : null;
    return { editSequence: sequence, revision: document ? document.revision : null };
  }
  function recordExternalConflict(workflowId: string, change: {
    code: string; reason: string; local: GraphDocument | null; external?: GraphDocument | null;
    proposal?: GraphDocument | null; resolved?: boolean;
  }): void {
    const entry = entries.value[workflowId];
    const local = conflictCounters(change.local), external = conflictCounters(change.external);
    externalConflicts.value[workflowId] = {
      workflowId, at: Date.now(), code: change.code, reason: change.reason,
      targetLabel: entry?.document.name || workspace.workflows.find(row => row.id === workflowId)?.title || workflowId,
      localEditSequence: local.editSequence, localRevision: local.revision,
      externalEditSequence: external.editSequence, externalRevision: external.revision,
      proposal: change.proposal ? graphClone(change.proposal) : null,
      resolved: change.resolved === true,
    };
    configurationApplicationVersion.value++;
  }
  function clearExternalConflict(workflowId: string): void {
    if (!externalConflicts.value[workflowId]) return;
    delete externalConflicts.value[workflowId];
    configurationApplicationVersion.value++;
  }
  function dismissExternalConflict(workflowId = workspace.activeWorkflowId): void {
    clearExternalConflict(workflowId);
  }
  /** A read returned a newer formal current than the locally accepted one (Q11-2). */
  function observeExternalCurrent(workflowId: string, prior: GraphConfigurationDocument | null,
    next: GraphConfigurationDocument | null, resolved: boolean): boolean {
    if (!prior || !next || next.edit_sequence <= prior.edit_sequence) return false;
    recordExternalConflict(workflowId, {
      code: "external_current",
      reason: "正式版本已被外部修改（可能由外部 Agent 修改）：读取到比本地已接纳更新的正式配置",
      local: prior, external: next, proposal: rejectedEdits.value[workflowId]?.request.document ?? null, resolved,
    });
    return true;
  }
  async function submitConfigurationDocument(workflowId: string, proposed: GraphDocument,
    historyAction: ConfigurationHistoryAction): Promise<GraphConfigurationResult | null> {
    const application = configurationApplication, source = entries.value[workflowId];
    if (!application || !source)
      throw new WorkbenchApiError("unavailable", "正式配置应用不可用，未提交工作流修改");
    if (configurationSubmitting.value[workflowId])
      throw new WorkbenchApiError("rejected", "工作流配置正在提交", undefined, "graph_configuration_pending");
    const entry = application.forWorkflow(workflowId);
    if (source.document.workflow_definition_id !== workflowId
      || proposed.workflow_definition_id !== workflowId)
      throw new WorkbenchApiError("rejected", "工作流配置编辑依据无效");
    // Capture the body and counters from the same edit baseline. Waiting for
    // initialization must re-check this baseline, never fetch a newer version
    // and silently attach the old body to it.
    const baseline = graphClone(source.document);
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    let request: GraphConfigurationUpdateRequest | null = null;
    try {
      await entry.initialize();
      const workflow = workspace.workflows.find(row => row.id === workflowId);
      const management = managementRequests.forWorkflow(workflowId);
      if (busy.value || applicationBusy.value || management.pending.value || management.busy.value
        || workflow?.archived || workflow?.deleted)
        throw new WorkbenchApiError("rejected", "工作流管理操作正在进行，未提交配置修改",
          undefined, "graph_management_pending");
      const current = application.accepted(workflowId);
      if (!current && source.saved_revision > 0)
        throw new WorkbenchApiError("unavailable", "正式配置读取暂不可用；未提交工作流修改");
      if (current && graphSignature(current) !== graphSignature(baseline))
        throw new WorkbenchApiError("rejected", "配置编辑依据已变化；请重新读取工作流",
          undefined, "stale_configuration_basis");
      const editSequence = current?.edit_sequence ?? 0;
      const document = configurationUpdateDocument(proposed, current ? editSequence : null);
      document.revision = current?.revision ?? 1;
      request = { workflow_definition_id: workflowId,
        expected_edit_sequence: editSequence, document, idempotency_key: crypto.randomUUID() };
      let result: GraphConfigurationResult;
      try {
        result = await entry.submit(request);
      } catch (failure) {
        recordRejectedEdit(workflowId, failure, request);
        // The application may have accepted the write before evidence closing
        // failed; reflect only that accepted state, not an unaccepted result.
        const accepted = application.accepted(workflowId);
        const fromThisRequest = !!request && !!accepted && sameConfigurationBody(accepted, request.document);
        projectChangedAcceptance(workflowId, viewToken, fromThisRequest ? historyAction : "none");
        throw failure;
      }
      configurationApplicationVersion.value++;
      clearRejectedEdit(workflowId);
      clearExternalConflict(workflowId);
      // The backend answered a fixed-revision question without writing anything. The
      // ask stays scoped to this workflow and is answered from its own view (Q03-4).
      if (result.outcome === "confirmation_required") return result;
      projectConfigurationResult(workflowId, result, viewToken, historyAction);
      return result;
    } catch (failure) {
      // A changed basis or management gate rejects this edit; only sync the
      // accepted projection, without advancing history for the rejected body.
      projectChangedAcceptance(workflowId, viewToken, "none");
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code && EXTERNAL_CHANGE_CODES.has(failure.code)) {
        // Q11-2: CAS/stale refusals mean the formal current moved under this basis.
        const accepted = configurationApplication?.accepted(workflowId) ?? null;
        const local = request
          ? { ...baseline, edit_sequence: request.document.edit_sequence, revision: request.document.revision } as GraphDocument
          : baseline;
        const localSequence = Number(local.edit_sequence ?? 0);
        recordExternalConflict(workflowId, {
          code: failure.code, reason: failure.reason, local,
          external: accepted && accepted.edit_sequence > localSequence ? accepted : null,
          proposal: graphClone(proposed), resolved: false,
        });
      }
      throw failure;
    } finally {
      delete configurationSubmitting.value[workflowId];
    }
  }
  async function patchNodeConfiguration(request: WorkflowNodeConfigurationRequest):
    Promise<WorkflowNodeConfigurationResult | null> {
    const application = configurationApplication;
    if (!application)
      throw new WorkbenchApiError("unavailable", "正式配置应用不可用，节点配置尚未提交");
    const source = entries.value[request.workflowId], document = application.accepted(request.workflowId);
    if (!alive || request.workflowId !== workspace.activeWorkflowId || !source
      || !isGraphConfigurationDocument(document) || !graphObject(request.patch) || !isGraphJsonValue(request.patch))
      return null;
    if (configurationSubmitting.value[request.workflowId])
      throw new WorkbenchApiError("rejected", "工作流配置正在提交，请等待当前请求结束",
        undefined, "graph_configuration_pending");
    if (locked.value)
      throw new WorkbenchApiError("rejected",
        "当前不可编辑工作流配置（存在待核实请求、管理操作或会话未稳定）", undefined, "graph_configuration_pending");
    if (graphSignature(source.document) !== graphSignature(document)) return null;
    const node = document.nodes.find(row => row.node_binding_id === request.nodeId);
    if (!node || !node.configuration_policy)
      return null;
    const definition = typeFor(node), properties = definition?.config_schema.properties;
    if (!definition?.executable || !graphObject(properties) || !Object.keys(request.patch).length
      || Object.keys(request.patch).some(field => !Object.hasOwn(properties, field))) return null;
    if (graphSignature({ ...document, nodes: [{ ...node, config: node.config }] })
      !== graphSignature({ ...document, nodes: [{ ...node, config: request.expectedConfig }] })) return null;
    configurationSubmitting.value[request.workflowId] = true;
    const viewToken = workspace.currentViewToken(), entry = application.forWorkflow(request.workflowId);
    try {
      await entry.initialize();
      const workflow = workspace.workflows.find(row => row.id === request.workflowId);
      const management = managementRequests.forWorkflow(request.workflowId);
      if (busy.value || applicationBusy.value || management.pending.value || management.busy.value
        || workflow?.archived || workflow?.deleted)
        throw new WorkbenchApiError("rejected", "工作流管理操作正在进行，未提交节点配置",
          undefined, "graph_management_pending");
      const current = application.accepted(request.workflowId);
      if (!isGraphConfigurationDocument(current) || graphSignature(current) !== graphSignature(document))
        throw new WorkbenchApiError("rejected", "配置编辑依据已变化；请重新读取工作流",
          undefined, "stale_configuration_basis");
      const submission = {
        workflowId: request.workflowId, nodeId: request.nodeId, componentId: request.componentId,
        componentVersion: request.componentVersion, extensionId: request.extensionId,
        expectedEditSequence: document.edit_sequence,
        expectedConfig: graphClone(request.expectedConfig),
        nextConfig: { ...graphClone(node.config), ...graphClone(request.patch) },
        nextPolicy: graphClone(node.configuration_policy),
      };
      const result = await application.sdk.submitNodeConfiguration(submission);
      configurationApplicationVersion.value++;
      // A retained derivation ask is answered from the workflow's own view (Q03-4);
      // this narrow-patch entry cannot confirm it and reports that nothing was applied.
      if (result.outcome === "confirmation_required") return null;
      const accepted = projectConfigurationResult(request.workflowId, result, viewToken, "edit");
      if (!accepted) return null;
      clearExternalConflict(request.workflowId);
      return { workflowId: accepted.workflowId, nodeId: request.nodeId, outcome: result.outcome };
    } catch (failure) {
      projectChangedAcceptance(request.workflowId, viewToken, "edit");
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code && EXTERNAL_CHANGE_CODES.has(failure.code)) {
        // Q11-2: keep the narrow local proposal with the external-change record.
        const proposal = graphClone(document);
        const target = proposal.nodes.find(row => row.node_binding_id === request.nodeId);
        if (target && graphObject(request.patch))
          target.config = { ...graphClone(target.config), ...graphClone(request.patch) };
        const accepted = application.accepted(request.workflowId) ?? null;
        recordExternalConflict(request.workflowId, { code: failure.code, reason: failure.reason,
          local: document, external: accepted && accepted.edit_sequence > document.edit_sequence ? accepted : null,
          proposal, resolved: false });
      }
      report(failure, "保存节点配置");
      return null;
    } finally {
      delete configurationSubmitting.value[request.workflowId];
    }
  }
  async function patchNodeConfigurationPolicy(nodeId: string, mode: "fixed" | "adjustable"):
    Promise<WorkflowEditResult> {
    const id = workspace.activeWorkflowId;
    return editable(id, document => {
      const node = document.nodes.find(row => row.node_binding_id === nodeId);
      if (!node || !graphNodeAllowsAdjustable(node, typeFor(node))
        || document.schema_version !== 3 || !node.configuration_policy
        || !["fixed", "adjustable"].includes(mode)) return false;
      node.configuration_policy = { ...node.configuration_policy, prompt_mode: mode };
      return true;
    });
  }
  function configurationPendingRequests() {
    configurationApplicationVersion.value;
    return [...new Set([...Object.keys(entries.value), ...discoveredRequests.value
      .filter(row => row.owner_kind === "workflow-configuration").map(row => row.owner_id)])].flatMap(workflowId => {
      if (!configurationApplication) return [];
      const request = configurationApplication.forWorkflow(workflowId);
      return request.pending.value && !request.busy.value
        ? [{ workflowId, error: request.error.value, busy: request.busy.value, progress: request.progress.value,
          requestKey: request.pending.value.idempotency_key }] : [];
    });
  }
  async function reconcileConfiguration(workflowId: string) {
    if (!configurationApplication) return false;
    const request = configurationApplication.forWorkflow(workflowId);
    if (request.busy.value || !request.pending.value) return false;
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    try {
      const result = await request.reconcile();
      configurationApplicationVersion.value++;
      if (result.outcome === "confirmation_required") return true;
      projectConfigurationResult(workflowId, result, viewToken, "edit");
      return true;
    } catch (failure) {
      projectChangedAcceptance(workflowId, viewToken, "edit");
      report(failure, "核实工作流配置原请求");
      return false;
    } finally { delete configurationSubmitting.value[workflowId]; }
  }
  /** User-initiated terminal arbitration of one unresolved original request (Q03-2). */
  async function abandonConfiguration(workflowId = workspace.activeWorkflowId): Promise<boolean> {
    const application = configurationApplication, entry = entries.value[workflowId];
    if (!application || !entry || configurationSubmitting.value[workflowId]) return false;
    const request = application.forWorkflow(workflowId);
    if (request.busy.value) return false;
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    try {
      if (!request.pending.value) await request.initialize(true);
      if (!request.pending.value) return false;
      const terminal = await request.abandon();
      configurationApplicationVersion.value++;
      if (terminal.outcome === "abandoned") {
        notices.publish({ id: `abandon:${workflowId}`, severity: "warning", title: "原请求已终止",
          reason: "服务端已仲裁该请求未执行，原请求不会再执行；证据已归档。", requestId: "" });
        return true;
      }
      if (terminal.accepted) projectConfigurationResult(workflowId, terminal.result, viewToken, "none");
      else projectChangedAcceptance(workflowId, viewToken, "none");
      notices.publish({ id: `abandon:${workflowId}`, severity: "success", title: "原请求已由服务端确认提交",
        reason: "已接纳正式结果并结束等待。", requestId: "" });
      return true;
    } catch (failure) {
      // No terminal answer: the request stays pending and blocked, never reported cancelled.
      if (request.pending.value) {
        notices.publish({ id: `abandon-unresolved:${workflowId}`, severity: "major", title: "结果未决",
          reason: `服务端暂无法仲裁该请求，保持等待核实：${
            failure instanceof Error ? failure.message : "请求仲裁不可用"}`, requestId: "" });
      } else {
        report(failure, "结束工作流原请求等待");
      }
      return false;
    } finally { delete configurationSubmitting.value[workflowId]; }
  }
  /**
   * Re-read the formal current after a local acceptance failure; never resubmits (Q03-3).
   * Q11-2: this is a read, so it never takes the edit lock; a separate guard only keeps a
   * duplicate read from racing itself. The local rejected proposal stays available.
   */
  async function reloadAcceptedConfiguration(workflowId = workspace.activeWorkflowId): Promise<boolean> {
    const application = configurationApplication, entry = entries.value[workflowId];
    if (!application || !entry || configurationSubmitting.value[workflowId]
      || configurationReads.value[workflowId]) return false;
    configurationReads.value[workflowId] = true;
    const prior = application.accepted(workflowId);
    try {
      const document = await management.queries.definition(workflowId);
      if (!isGraphConfigurationDocument(document) || document.workflow_definition_id !== workflowId)
        throw new WorkbenchApiError("unavailable", "正式配置读取不可用，当前视图仍待修复");
      const acceptance = application.acceptDocument(document);
      if (acceptance.status === "conflict" || acceptance.status === "invalid" || !acceptance.document)
        throw new WorkbenchApiError("unknown", acceptance.message ?? "正式配置无法被接纳");
      // Q11-2: confirm/refresh the external-change record with the counters just read. The
      // read adopts the newer formal current (the explicit recovery action), but the local
      // proposal stays visible until the user merges or abandons it.
      const observed = application.accepted(workflowId) ?? document;
      const existing = externalConflicts.value[workflowId];
      if (existing) {
        const counters = conflictCounters(observed);
        externalConflicts.value[workflowId] = { ...existing,
          externalEditSequence: counters.editSequence, externalRevision: counters.revision, resolved: true };
      } else observeExternalCurrent(workflowId, prior, observed, true);
      projectChangedAcceptance(workflowId, workspace.currentViewToken(), "none");
      application.forWorkflow(workflowId).markAcceptanceRestored();
      configurationApplicationVersion.value++;
      notices.publish({ id: `reload:${workflowId}`, severity: "success", title: "正式配置已重新读取",
        reason: "已按服务端正式结果修复当前视图；未重新提交保存。", requestId: "" });
      return true;
    } catch (failure) {
      report(failure, "重新读取正式配置");
      return false;
    } finally { delete configurationReads.value[workflowId]; }
  }
  /** Confirm one retained derivation ask, only from its own workflow view (Q03-4). */
  async function confirmDerivation(workflowId = workspace.activeWorkflowId): Promise<boolean> {
    const application = configurationApplication;
    if (!application || workflowId !== workspace.activeWorkflowId || configurationSubmitting.value[workflowId])
      return false;
    const request = application.forWorkflow(workflowId), ask = request.confirmation.value;
    if (!ask) return false;
    const current = application.accepted(workflowId);
    if (!current || current.edit_sequence !== ask.request.expected_edit_sequence
      || current.revision !== ask.request.document.revision) {
      request.reject();
      configurationApplicationVersion.value++;
      report(new WorkbenchApiError("rejected",
        "原提案的编辑基线已变化，不能按旧基线确认派生；请重新编辑当前工作流",
        undefined, "stale_configuration_basis"), "确认派生修改");
      return false;
    }
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    try {
      const result = await request.confirm();
      configurationApplicationVersion.value++;
      if (result.outcome === "confirmation_required") return false;
      projectConfigurationResult(workflowId, result, viewToken, "edit");
      return true;
    } catch (failure) {
      report(failure, "确认派生修改");
      return false;
    } finally { delete configurationSubmitting.value[workflowId]; }
  }
  function declineDerivation(workflowId = workspace.activeWorkflowId): boolean {
    const application = configurationApplication;
    if (!application || workflowId !== workspace.activeWorkflowId) return false;
    const request = application.forWorkflow(workflowId);
    if (!request.confirmation.value) return false;
    try {
      request.reject();
      configurationApplicationVersion.value++;
      return true;
    } catch (failure) { report(failure, "放弃派生修改"); return false; }
  }
  function observe(view: GraphSession) {
    const accepted = newerGraphObservation(views.value[view.workflow_session_id], view);
    if (alive) views.value[view.workflow_session_id] = accepted;
    return accepted;
  }
  /** Project a freshly accepted full session view into its directory row only. */
  function projectAcceptedSessionRow(workflowId: string, view: GraphSession) {
    const row = sessionSummaryFromView(view);
    const rows = sessionRows.value[workflowId] ?? [];
    const index = rows.findIndex(item => item.workflow_session_id === row.workflow_session_id);
    sessionRows.value[workflowId] = index >= 0
      ? rows.map(item => item.workflow_session_id === row.workflow_session_id ? row : item)
      : [...rows, row];
  }
  function markSessionRowArchived(workflowId: string, sessionId: string) {
    const rows = sessionRows.value[workflowId] ?? [];
    if (!rows.some(row => row.workflow_session_id === sessionId)) return;
    sessionRows.value[workflowId] = rows.map(row => row.workflow_session_id === sessionId
      ? { ...row, archived: true } : row);
  }
  async function acceptManagementReceipt(id: string, receipt: GraphReceipt, request: GraphCommand) {
    const entry = entries.value[id];
    if (!alive || !entry) return;
    if (receipt.kind === "session") {
      const accepted = observe(receipt.session);
      projectAcceptedSessionRow(id, accepted);
      const target = managementTargets.get(String(request.body.idempotency_key));
      if (request.action === "create" && target?.entry === entry
        && target.sessionId === entry.session_id && target.selection === sessionSelectionGeneration
        && workspace.isCurrentView(target.view)) {
        entry.session_id = accepted.workflow_session_id;
        delete entry.session_document;
      }
      return;
    }
    if (receipt.kind !== "management")
      throw new WorkbenchApiError("unknown", "管理回执不是当前入口的结果");
    // Accepting a definition management receipt invalidates directory reads
    // that were issued while the command was in flight.
    mutationGeneration++;
    if (receipt.result.deleted) {
      // The business is deleted, but the original request is not closed yet.
      // Keep the entry and its verification owner until the outbox resolves;
      // a failed resolution must remain visible instead of disappearing here.
      if (request.action === "definition-delete") workspace.setWorkflowManagement(id, true, true);
    } else if (request.action === "definition-archive") {
      workspace.setWorkflowManagement(id, true);
    } else {
      const sessionId = request.source_session_id, view = sessionId ? views.value[sessionId] : null;
      if (sessionId) markSessionRowArchived(id, sessionId);
      if (view) observe({ ...view, archived: true, readonly: true, can_submit: false, available_actions: [] });
    }
  }
  function managementPendingRequests() {
    return [...new Set([...Object.keys(entries.value), ...discoveredRequests.value
      .filter(row => row.owner_kind === "workflow-management").map(row => row.owner_id)])].flatMap(workflowId => {
      const request = managementRequests.forWorkflow(workflowId);
      return !request.busy.value && (request.pending.value || request.error.value) ? [{ workflowId, error: request.error.value,
        busy: request.busy.value, pending: !!request.pending.value, progress: request.progress.value,
        requestKey: String(request.pending.value?.parameters.idempotency_key ?? ""),
        requestOperation: request.pending.value?.operation ?? "" }] : [];
    });
  }
  const guardedManagementActions = new Set([
    "definition-archive", "definition-delete", "session-archive", "session-delete",
  ]);
  // The public management commands and the outer manageWorkflow/manageSession
  // both reach this shared dispatch path. Check the target's own configuration
  // and management state here, before any new original-request evidence.
  async function guardManagementWrite(id: string, command: GraphCommand) {
    if (!guardedManagementActions.has(command.action)) return;
    const entry = entries.value[id], application = configurationApplication;
    if (!entry) throw new WorkbenchApiError("unavailable", "工作流不存在");
    if (command.action.startsWith("session-")) {
      const sessionId = command.source_session_id;
      const view = sessionId ? views.value[sessionId] : null;
      if (!sessionId || !view || view.workflow_definition_id !== entry.document.workflow_definition_id)
        throw new WorkbenchApiError("rejected", "目标会话不属于该工作流，未提交管理请求",
          undefined, "graph_management_target_invalid");
    }
    const channel = application?.forWorkflow(id);
    if (channel) {
      const initialized = await channel.initialize();
      if (!initialized)
        throw new WorkbenchApiError("unavailable", "工作流原请求证据未能初始化，未提交管理请求");
      if (!alive || entries.value[id] !== entry || configurationApplication !== application)
        throw new WorkbenchApiError("unavailable", "工作流目标已变化，未提交管理请求");
      if (configurationSubmitting.value[id] || channel.locked.value)
        throw new WorkbenchApiError("rejected", "工作流配置正在提交或待核实，未提交管理请求",
          undefined, "graph_configuration_pending");
    }
    const requests = managementRequests.forWorkflow(id);
    const ready = await requests.initialize();
    if (!ready)
      throw new WorkbenchApiError("unavailable", "工作流管理原请求证据未能初始化，未提交管理请求");
    if (!alive || entries.value[id] !== entry || configurationApplication !== application)
      throw new WorkbenchApiError("unavailable", "工作流目标已变化，未提交管理请求");
    if (channel && (configurationSubmitting.value[id] || channel.locked.value))
      throw new WorkbenchApiError("rejected", "工作流配置正在提交或待核实，未提交管理请求",
        undefined, "graph_configuration_pending");
    if (requests.pending.value || requests.busy.value)
      throw new WorkbenchApiError("unknown", "目标工作流仍有管理请求未结束，未提交新请求");
    const workflow = workspace.workflows.find(row => row.id === id);
    if (workflow?.deleted)
      throw new WorkbenchApiError("rejected", "目标工作流已删除，未提交管理请求");
  }
  async function dispatch(id: string, command: GraphCommand): Promise<unknown> {
    await guardManagementWrite(id, command);
    mutationGeneration++;
    const entry = entries.value[id];
    const key = String(command.body.idempotency_key);
    managementTargets.set(key, { entry, sessionId: entry.session_id,
      selection: sessionSelectionGeneration, view: workspace.currentViewToken() });
    try { return await managementRequests.forWorkflow(id).dispatch(command); }
    finally { managementTargets.delete(key); }
  }
  const management = createWorkflowManagement({
    entry: id => entries.value[id], observedSession: id => views.value[id], observe, dispatch,
    commandState: value => { applicationBusy.value = value; mutationGeneration++; },
    canSubmit(id) {
      const workflow = workspace.workflows.find(row => row.id === id);
      return alive && !!workflow && !workflow.copyPending
        && !workspace.workflows.some(row => row.copyPending && row.sourceId === id);
    },
  });
  // One reason for every refused edit entry: busy, pending request, archived/copying/fixed-only.
  function editRefusal(id: string): string | null {
    if (!alive) return "工作区已关闭";
    const entry = entries.value[id];
    if (!entry) return "工作流不存在或已关闭";
    if (busy.value) return "工作流操作正在提交，请等待当前操作结束";
    if (applicationBusy.value) return "工作流命令正在提交，请等待当前操作结束";
    if (entry.fixed_only) return "该工作流只有固定修订、没有正式配置，仅可只读查看";
    if (managementRequests.forWorkflow(id).locked.value) return "工作流管理原请求尚未结束，不能开始新的修改";
    if (configurationSubmitting.value[id]) return "工作流配置正在提交，请等待当前请求结束";
    if (configurationApplication?.forWorkflow(id).locked.value) return "工作流原请求尚未结束，不能开始新的修改";
    if (selectingSessionFor.value === id) return "正在切换工作流会话，不能修改";
    const workflow = workspace.workflows.find(row => row.id === id);
    if (workflow?.archived) return "工作流已归档，不能修改";
    if (workflow?.deleted) return "工作流已删除，原请求待核实";
    if (workflow?.copyPending) return "工作流副本尚未完成，不能修改";
    if (workspace.workflows.some(row => row.copyPending && row.sourceId === id))
      return "该工作流存在未完成的复制请求，不能修改";
    return null;
  }
  const editing = createWorkflowEditing({
    entry: id => entries.value[id],
    refusal: editRefusal,
    observedSession: id => views.value[id],
    async acceptDraft(id, next) {
      if (configurationApplication) {
        const result = await submitConfigurationDocument(id, next, "edit");
        return result ? { result } : null;
      }
      const entry = entries.value[id];
      const past = [...(history.value[id]?.past ?? []), graphClone(entry.document)].slice(-60);
      history.value[id] = { past, future: [] };
      entry.document = next;
      workspace.updateWorkflowProjection(id, next.name, next.nodes.length);
      workspace.markWorkflowDraft(id);
      return { result: null };
    },
  });
  async function finishManagedDeletion(id: string, request: Pick<GraphCommand, "action" | "source_session_id">) {
    const entry = entries.value[id];
    if (!entry) return;
    if (request.action === "definition-delete") {
      mutationGeneration++;
      const ownedSessions = Object.values(views.value)
        .filter(view => view.workflow_definition_id === entry.document.workflow_definition_id);
      for (const view of ownedSessions) {
        delete views.value[view.workflow_session_id];
      }
      if (workspace.activeWorkflowId === id) {
        const fallback = workspace.workflows.find(row => row.id !== id && !row.copyPending && !row.deleted);
        if (fallback) workspace.openWorkflow(fallback.id);
        else { workspace.activeWorkflowId = ""; workspace.selectedWorkflowId = ""; workspace.viewVersion++; }
      }
      workspace.removeWorkflow(id);
      delete entries.value[id]; delete history.value[id]; delete sessionRows.value[id];
      clearEditBlock(id); delete editFailures.value[id]; delete runFailures.value[id]; delete previewStates.value[id];
      configurationApplication?.forget(id);
    } else if (request.action === "session-delete") {
      const sessionId = request.source_session_id!;
      sessionRows.value[id] = (sessionRows.value[id] ?? [])
        .filter(row => row.workflow_session_id !== sessionId);
      delete views.value[sessionId];
      if (entry.session_id === sessionId) {
        entry.session_id = null; delete entry.session_document;
      }
    } else return;
  }
  async function manageWorkflow(id: string, action: "archive" | "delete") {
    if (busy.value || applicationBusy.value || !entries.value[id]) return false;
    const configuration = configurationApplication?.forWorkflow(id);
    if (configuration) {
      await configuration.initialize();
      if (configuration.locked.value || configurationSubmitting.value[id]) return false;
    }
    const requests = managementRequests.forWorkflow(id);
    if (requests.pending.value || requests.busy.value) return false;
    busy.value = `definition-${action}`;
    try {
      await management.commands.manageDefinition({ workflowId: id }, action);
      if (action === "archive") await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档工作流" : "删除工作流"); return false; }
    finally { busy.value = null; }
  }
  async function manageSession(sessionId: string, action: "archive" | "delete", id = workspace.activeWorkflowId) {
    if (busy.value || applicationBusy.value || !entries.value[id]) return false;
    const configuration = configurationApplication?.forWorkflow(id);
    if (configuration) {
      await configuration.initialize();
      if (configuration.locked.value || configurationSubmitting.value[id]) return false;
    }
    const requests = managementRequests.forWorkflow(id);
    if (requests.pending.value || requests.busy.value) return false;
    busy.value = `session-${action}`;
    try {
      await management.commands.manageSession({ workflowId: id }, sessionId, action);
      if (action === "archive") await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档会话" : "删除会话"); return false; }
    finally { busy.value = null; }
  }
  function copyChannel(sourceWorkflowDefinitionId: string): CopyChannel {
    const sourceId = sourceWorkflowDefinitionId.toLowerCase();
    copyApplication ??= createWorkflowConfigurationCopy({
      current: id => alive && entries.value[id] !== undefined,
      accept: acceptCopiedConfiguration,
    });
    let channel = copyChannels.get(sourceId);
    if (!channel) {
      channel = copyApplication.forSource(sourceId);
      copyChannels.set(sourceId, channel);
      bumpCopyVersion();
    }
    return channel;
  }
  async function restoreCopyChannel(sourceWorkflowDefinitionId: string) {
    const sourceId = sourceWorkflowDefinitionId.toLowerCase();
    if (copyInitializedSources.has(sourceId)) return;
    copyInitializedSources.add(sourceId);
    try {
      const channel = copyChannel(sourceId);
      if (!await channel.initialize() && channel.error.value)
        notices.notify("unavailable", channel.error.value, "读取配置复制原请求");
    } catch (failure) { report(failure, "读取配置复制原请求"); }
    finally { bumpCopyVersion(); }
  }
  /** Project a first-time copy result into the formal target directory/entry. */
  async function acceptCopiedConfiguration(request: WorkflowConfigurationCopyRequest,
    result: WorkflowConfigurationCopyResult, reconcile: boolean) {
    if (!isWorkflowConfigurationCopyResult(result, request))
      throw new WorkbenchApiError("unknown", "配置复制结果与请求不匹配，保留原请求");
    // A reconcile receipt proves the original copy happened; the target's current
    // availability is confirmed separately and never by registering the historical document.
    if (reconcile) return;
    const targetId = result.workflow_definition_id;
    let accepted: GraphConfigurationDocument | null = null;
    if (configurationApplication) {
      const acceptance = configurationApplication.acceptDocument(result.document);
      if (acceptance.status === "conflict" || acceptance.status === "invalid")
        throw new WorkbenchApiError("unknown", acceptance.message ?? "配置复制结果无法被正式配置应用接纳");
      accepted = configurationApplication.accepted(targetId);
    }
    accepted ??= isGraphConfigurationDocument(result.document) ? result.document : null;
    if (!accepted || accepted.workflow_definition_id !== targetId)
      throw new WorkbenchApiError("unknown", "配置复制结果缺少正式 current 配置");
    const source = entries.value[request.source_workflow_definition_id];
    if (!source) throw new WorkbenchApiError("unknown", "配置复制来源工作流已关闭");
    const token = copyOpenTokens.get(request.source_workflow_definition_id.toLowerCase())?.token
      ?? workspace.currentViewToken();
    projectAcceptedState(request.source_workflow_definition_id, targetId, accepted, token, "none", true);
    bumpCopyVersion();
  }
  /** After a matched receipt, confirm the target exists now without restoring history. */
  async function confirmCopiedTarget(result: WorkflowConfigurationCopyResult) {
    const targetId = result.workflow_definition_id;
    const sourceId = result.source_workflow_definition_id.toLowerCase();
    if (entries.value[targetId]
      || workspace.workflows.some(row => row.id === targetId && row.state === "saved" && !row.deleted))
      return;
    try {
      const document = await management.queries.definition(targetId);
      if (!isGraphConfigurationDocument(document) || document.workflow_definition_id !== targetId)
        throw new WorkbenchApiError("unavailable", "目标工作流当前配置不可用");
      let accepted: GraphConfigurationDocument | null = null;
      if (configurationApplication) {
        const acceptance = configurationApplication.acceptDocument(document);
        if (acceptance.status === "conflict" || acceptance.status === "invalid")
          throw new WorkbenchApiError("unavailable", acceptance.message ?? "目标工作流当前配置无法接纳");
        accepted = configurationApplication.accepted(targetId);
      }
      accepted ??= document;
      if (!entries.value[targetId]) {
        entries.value[targetId] = { document: graphClone(accepted), saved_document: graphClone(accepted),
          saved_revision: accepted.revision, latest_published_revision: accepted.revision,
          session_id: null };
        history.value[targetId] = { past: [], future: [] };
      }
      if (!workspace.workflows.some(row => row.id === targetId))
        workspace.registerGraphWorkflow({ id: targetId, title: accepted.name, description: "",
          nodeCount: accepted.nodes.length, state: "saved", sessionCount: 0 });
      workspace.updateWorkflowProjection(targetId, accepted.name, accepted.nodes.length);
      workspace.markWorkflowSaved(targetId, accepted.name);
      workspace.setWorkflowCurrent(targetId, accepted.revision, accepted.edit_sequence);
      copyChannel(sourceId).note("");
    } catch {
      copyChannel(sourceId).note("原复制已确认，目标现不可用");
      notices.notify("unavailable", `原复制已确认，目标 ${targetId} 现不可用`, "核实原配置复制请求");
    }
  }
  /** Formal configuration copy from the saved current; selection and editor state do not matter. */
  async function copyWorkflow(id = workspace.activeWorkflowId): Promise<string | null> {
    const sourceId = id.toLowerCase();
    if (copySubmitting.has(sourceId)) {
      notices.notify("unknown", "该来源的配置复制正在提交，请等待当前请求结束", "复制工作流");
      return null;
    }
    const existingChannel = copyChannels.get(sourceId);
    if (existingChannel && (existingChannel.busy.value || existingChannel.pending.value
      || existingChannel.locked.value)) {
      notices.notify(existingChannel.pending.value ? "unknown" : "rejected",
        existingChannel.pending.value ? "该来源已有待核实的配置复制请求，不能提交新复制"
          : "该来源的配置复制正在提交，未提交新复制", "复制工作流");
      return null;
    }
    copySubmitting.add(sourceId);
    try {
      const entry = entries.value[id];
      if (!entry) {
        notices.notify("rejected", "工作流尚未打开，无法读取正式来源配置", "复制工作流");
        return null;
      }
      const source = configurationApplication?.accepted(id)
        ?? (isGraphConfigurationDocument(entry.saved_document) ? entry.saved_document : null)
        ?? (isGraphConfigurationDocument(entry.document) ? entry.document : null);
      if (!source || source.workflow_definition_id !== id) {
        notices.notify("rejected", "来源工作流缺少正式 current 配置，不能复制", "复制工作流");
        return null;
      }
      const targetId = crypto.randomUUID();
      const request: WorkflowConfigurationCopyRequest = {
        source_workflow_definition_id: id,
        expected_source_definition_revision: source.revision,
        expected_source_edit_sequence: source.edit_sequence,
        target_workflow_definition_id: targetId,
        target_name: workflowConfigurationCopyTargetName(source.name),
        idempotency_key: crypto.randomUUID(),
      };
      if (!isWorkflowConfigurationCopyRequest(request)) {
        notices.notify("rejected", "复制请求来源版本或目标身份无效，未提交", "复制工作流");
        return null;
      }
      const channel = copyChannel(id);
      if (!await channel.initialize()) {
        notices.notify("unavailable", channel.error.value || "配置复制原请求无法读取，未提交", "复制工作流");
        return null;
      }
      if (channel.pending.value) {
        notices.notify("unknown", "该来源已有待核实的配置复制请求，不能提交新复制", "复制工作流");
        return null;
      }
      copyOpenTokens.set(id.toLowerCase(), { targetId, token: workspace.currentViewToken() });
      bumpCopyVersion();
      const success = await channel.submit(request);
      bumpCopyVersion();
      if (!success) {
        if (channel.error.value)
          notices.notify(channel.pending.value ? "unknown" : "rejected", channel.error.value, "复制工作流");
        // The target identity is fixed only when the original request was captured.
        return channel.pending.value ? targetId : null;
      }
      return targetId;
    } catch (failure) {
      report(failure, "复制工作流");
      return null;
    } finally {
      copySubmitting.delete(sourceId);
      copyOpenTokens.delete(sourceId);
      bumpCopyVersion();
    }
  }
  /** Read-only verification of a pending copy request; never resends. */
  async function reconcileCopy(workflowId: string): Promise<boolean> {
    const sourceId = workflowId.toLowerCase();
    try {
      copyInitializedSources.add(sourceId);
      const channel = copyChannel(sourceId);
      if (!await channel.initialize()) return false;
      if (!channel.pending.value) { bumpCopyVersion(); return false; }
      const done = await channel.reconcile();
      bumpCopyVersion();
      if (!done) {
        if (channel.error.value)
          notices.notify("unknown", channel.error.value, "核实原配置复制请求");
        return false;
      }
      const result = channel.lastResult.value;
      if (!result) return false;
      await confirmCopiedTarget(result);
      return true;
    } catch (failure) {
      report(failure, "核实原配置复制请求");
      return false;
    } finally { bumpCopyVersion(); }
  }
  /** Unresolved copy rows; normal submissions stay quiet while in flight. */
  function copyPendingRequests() {
    copyVersion.value;
    const activeId = workspace.activeWorkflowId;
    if (graphUuid(activeId) && entries.value[activeId] && !copyInitializedSources.has(activeId.toLowerCase()))
      void restoreCopyChannel(activeId);
    return [...copyChannels.entries()].flatMap(([sourceId, channel]) => {
      if (channel.busy.value || !channel.pending.value && !channel.error.value) return [];
      return [{ workflowId: sourceId,
        title: workspace.workflows.find(row => row.id === sourceId)?.title ?? sourceId,
        error: channel.error.value, busy: channel.busy.value, pending: !!channel.pending.value,
        progress: channel.progress.value, requestKey: channel.pending.value?.idempotency_key ?? "" }];
    });
  }
  function supportedDiscoveredRequest(row: PendingRequestEvidence) {
    return graphUuid(row.owner_id) && (row.owner_kind === "workflow-configuration" && row.operation === "definition.update"
      || row.owner_kind === "workflow-configuration-copy" && row.operation === "definition.copy"
      || row.owner_kind === "workflow-management" && new Set(["definition.archive", "definition.delete",
        "session.create", "session.archive", "session.delete", "session.data.update", "run.start", "run.control",
        "event.submit"]).has(row.operation));
  }
  function discoveredRequestBusy(row: PendingRequestEvidence) {
    if (recoveryBusy.value[evidenceCoordinate(row)]) return true;
    if (!supportedDiscoveredRequest(row)) return false;
    if (row.owner_kind === "workflow-configuration")
      return !!configurationApplication?.forWorkflow(row.owner_id).busy.value
        || !!configurationSubmitting.value[row.owner_id];
    if (row.owner_kind === "workflow-configuration-copy")
      return copyChannels.get(row.owner_id)?.busy.value === true || copySubmitting.has(row.owner_id);
    return managementRequests.forWorkflow(row.owner_id).busy.value;
  }
  function discoveredPendingRequests() {
    return discoveredRequests.value.filter(row => {
      if (discoveredRequestBusy(row)) return false;
      if (!supportedDiscoveredRequest(row)) return true;
      if (row.owner_kind === "workflow-configuration")
        return configurationApplication?.forWorkflow(row.owner_id).settledRequestKey.value !== row.idempotency_key;
      if (row.owner_kind === "workflow-configuration-copy")
        return copyChannels.get(row.owner_id)?.completed.value?.idempotency_key !== row.idempotency_key;
      return managementRequests.forWorkflow(row.owner_id).completed.value?.parameters.idempotency_key !== row.idempotency_key;
    });
  }
  async function discoverPendingRequests(more = false) {
    if (pendingDiscoveryBusy.value || more && !pendingDiscoveryCursor.value) return false;
    pendingDiscoveryBusy.value = true; pendingDiscoveryError.value = "";
    try {
      const page = await readPendingWorkflowEvidence({ limit: 50,
        ...(more && pendingDiscoveryCursor.value ? { cursor: pendingDiscoveryCursor.value } : {}) });
      const rows = more ? [...discoveredRequests.value, ...page.items] : page.items;
      discoveredRequests.value = [...new Map(rows.map(row => [evidenceCoordinate(row), row])).values()];
      pendingDiscoveryCursor.value = page.next_cursor;
      return true;
    } catch (cause) {
      pendingDiscoveryError.value = cause instanceof Error ? cause.message : "工作流原请求发现失败";
      return false;
    } finally { pendingDiscoveryBusy.value = false; }
  }
  /** Discovery is a coordinate hint. Re-read the current exact owner before touching its channel. */
  async function reconcileDiscoveredRequest(row: PendingRequestEvidence) {
    const identity = evidenceCoordinate(row);
    if (!supportedDiscoveredRequest(row) || discoveredRequestBusy(row)) return false;
    recoveryBusy.value[identity] = true; recoveryErrors.value[identity] = "";
    try {
      const records = await requestEvidencePorts.read(row.owner_kind, row.owner_id);
      const pending = records.filter(record => record.resolution === "pending");
      if (pending.length !== 1 || !isRequestEvidenceRecord(pending[0])
        || evidenceCoordinate(pending[0]) !== identity || pending[0].request_digest !== row.request_digest)
        throw new WorkbenchApiError("unknown", "摘要与当前原请求坐标不同；未核实或结案，请刷新原请求列表");
      let done = false;
      if (row.owner_kind === "workflow-configuration") {
        if (!configurationApplication) throw new WorkbenchApiError("unavailable", "工作流配置应用尚未就绪");
        const channel = configurationApplication.forWorkflow(row.owner_id);
        await channel.initialize(true);
        if (!channel.pending.value || !channel.matchesEvidence(row))
          throw new WorkbenchApiError("unknown", "当前配置原请求已变化，未核实或结案");
        await channel.reconcile({ acceptResult: false });
        done = true;
      } else if (row.owner_kind === "workflow-configuration-copy") {
        const channel = copyChannel(row.owner_id);
        await channel.reloadPending();
        if (!channel.pending.value || !channel.matchesEvidence(row))
          throw new WorkbenchApiError("unknown", "当前配置复制原请求已变化，未核实或结案");
        done = await channel.reconcile();
      } else {
        const channel = managementRequests.forWorkflow(row.owner_id);
        await channel.reloadPending();
        if (!channel.pending.value || !channel.matchesEvidence(row))
          throw new WorkbenchApiError("unknown", "当前管理原请求已变化，未核实或结案");
        done = await channel.reconcile({ acceptResult: false });
      }
      if (!done) throw new WorkbenchApiError("unknown", row.owner_kind === "workflow-configuration-copy"
        ? copyChannel(row.owner_id).error.value : managementRequests.forWorkflow(row.owner_id).error.value);
      discoveredRequests.value = discoveredRequests.value.filter(value => evidenceCoordinate(value) !== identity);
      notices.publish({ id: `recovery:${identity}`, severity: "success", title: "原请求已核实",
        reason: "原业务回执已匹配，证据已结案；当前目录可独立刷新。", requestId: row.idempotency_key });
      return true;
    } catch (cause) {
      recoveryErrors.value[identity] = cause instanceof Error ? cause.message : "原请求仍待核实";
      return false;
    } finally { delete recoveryBusy.value[identity]; bumpCopyVersion(); }
  }
  /** Optional injection used by tests or a workflow-owned assembly; the store otherwise
   * lazily creates exactly one default application instance. */
  function installWorkflowConfigurationCopyApplication(application: WorkflowConfigurationCopyApplication) {
    copyApplication = application;
    copyChannels.clear();
    copyInitializedSources.clear();
    bumpCopyVersion();
  }
  async function renameWorkflow(name: string): Promise<WorkflowEditResult> {
    const title = name.trim();
    if (!title || title.length > 128 || /[\u0000-\u001f\u007f]/.test(title))
      throw new WorkbenchApiError("rejected", "工作流名称无效，最多 128 个字符且不能包含控制字符",
        undefined, "invalid_request");
    return editable(workspace.activeWorkflowId, doc => { doc.name = title; return true; });
  }
  /**
   * Q09: read one frozen definition revision, reusing a successful read by its formal
   * (workflow_definition_id, definition_revision) identity. Failures are not cached so a
   * later trigger retries; the bounded cache is cleared with the store lifetime.
   */
  async function readFrozenDefinition(identity: string, revision: number): Promise<GraphDocument> {
    const key = `${identity}@r${revision}`;
    const cached = frozenDefinitions.get(key);
    if (cached) {
      // Refresh LRU order so the bound evicts genuinely older identities.
      frozenDefinitions.delete(key); frozenDefinitions.set(key, cached);
      return graphClone(cached);
    }
    const document = await management.queries.definition(identity, revision);
    if (!alive) return document;
    if (frozenDefinitions.size >= FROZEN_DEFINITION_CACHE_LIMIT) {
      const oldest = frozenDefinitions.keys().next().value;
      if (oldest !== undefined) frozenDefinitions.delete(oldest);
    }
    frozenDefinitions.set(key, graphClone(document));
    return document;
  }
  async function refreshPublishedRevision(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry?.saved_revision || locked.value) return false;
    const definitionId = entry.document.workflow_definition_id;
    try {
      // Q09: read the summary row's fixed revision instead of the whole current document.
      // A "latest" question always issues a summary read; a possibly stale local summary
      // is never used as the answer.
      const rows = await management.queries.definitionSummaries(true);
      if (!alive || entries.value[id] !== entry || entry.document.workflow_definition_id !== definitionId) return false;
      const row = rows.find(candidate => candidate.workflow_definition_id === definitionId);
      if (!row)
        throw new WorkbenchApiError("unavailable", "最新已发布修订摘要缺失，未更新本地固定版本");
      entry.latest_published_revision = Math.max(entry.latest_published_revision ?? entry.saved_revision,
        row.fixed_definition_revision);
      return true;
    } catch (failure) { report(failure, "读取最新已发布修订"); return false; }
  }
  async function editable(id: string, change: (document: GraphDocument) => boolean): Promise<WorkflowEditResult> {
    try {
      const result = await editing.edit({ workflowId: id }, doc => {
        if (!change(doc)) return false;
        // Q05-1: save-time structure check for dangling ordinary data edges. The same rule
        // is enforced by the backend compiler; here it yields a locatable save error.
        const dangling = graphDanglingEdgeIssues(doc);
        if (dangling.length) {
          const error = new WorkbenchApiError("rejected",
            `保存被阻止：存在 ${dangling.length} 条端点缺失的数据边（${dangling.slice(0, 3)
              .map(issue => `${issue.message}，路径 ${issue.path}`).join("；")}${dangling.length > 3 ? "；…" : ""}）`,
            undefined, "graph_dangling_edge");
          error.nodeId = dangling[0]!.node_id;
          error.diagnostics = dangling.slice(0, 20).map(issue => ({
            reason_code: "graph_dangling_edge", message: issue.message,
            edge_id: issue.edge_id, node_id: issue.node_id,
          }));
          throw error;
        }
        return true;
      });
      // Any accepted edit is a new reality; a previous local block no longer describes it.
      if (result.outcome === "accepted") {
        clearEditBlock(id);
        if (editFailures.value[id]) delete editFailures.value[id];
        // A saved preview describes the document it read, never a later accepted edit.
        const preview = previewStates.value[id];
        if (preview?.preview) previewStates.value[id] = { ...preview, stale: true };
      }
      return result;
    } catch (failure) {
      report(failure, "工作流编辑");
      if (failure instanceof WorkbenchApiError && (failure.diagnostics?.length ?? 0) > 0) {
        const entry = entries.value[id], failureLabel = failure.diagnostics![0]!.phase === "save"
          ? "保存校验" : "编辑提交";
        editFailures.value[id] = { workflowId: id, action: "edit", at: Date.now(),
          reason: failure.reason, ...(failure.code ? { code: failure.code } : {}),
          ...(failure.status !== undefined ? { status: failure.status } : {}),
          diagnostics: graphClone(failure.diagnostics!),
          targetIds: entry ? [entry.document.workflow_definition_id] : [],
          targetLabel: `${entry?.document.name ?? id}（${failureLabel}）` };
      }
      return { outcome: "failed", error: failure instanceof Error ? failure : new Error(String(failure)) };
    }
  }
  function recordRunFailure(id: string, operation: WorkflowRunFailureDetail["operation"], failure: unknown): void {
    const error = failure instanceof Error ? failure : new Error(String(failure));
    const diagnostics = failure instanceof WorkbenchApiError ? failure.diagnostics ?? [] : [];
    const workflow = workspace.workflows.find(row => row.id === id);
    runFailures.value[id] = { workflowId: id, operation, at: Date.now(), reason: error.message,
      ...(failure instanceof WorkbenchApiError && failure.code ? { code: failure.code } : {}),
      ...(failure instanceof WorkbenchApiError && failure.status !== undefined ? { status: failure.status } : {}),
      ...(diagnostics[0]?.phase ? { phase: diagnostics[0].phase } : {}),
      diagnostics: graphClone(diagnostics), sessionId: entries.value[id]?.session_id ?? null,
      targetLabel: workflow?.title || entries.value[id]?.document.name || id };
  }
  /** Read-only formal save preview (Q06-2); it never writes or takes the save right. */
  async function previewConfiguration(workflowId = workspace.activeWorkflowId): Promise<boolean> {
    const entry = entries.value[workflowId], current = entry?.document;
    if (!entry || !current) {
      previewStates.value[workflowId] = { workflowId, at: Date.now(), targetLabel: workflowId,
        preview: null, stale: false, busy: false, error: "工作流不存在或已关闭，无法预演", failure: null };
      return false;
    }
    if (previewStates.value[workflowId]?.busy) return false;
    const accepted = configurationApplication?.accepted(workflowId);
    const baseline = accepted?.edit_sequence ?? (current.schema_version === 3 ? current.edit_sequence : undefined);
    if (!configurationApplication || !Number.isSafeInteger(baseline) || Number(baseline) < 0) {
      previewStates.value[workflowId] = { workflowId, at: Date.now(),
        targetLabel: current.name || workflowId, preview: null, stale: false, busy: false,
        error: "缺少已接纳的正式配置基线，当前不能预演保存", failure: null };
      return false;
    }
    const documentSignature = graphSignature(current), targetLabel = `${current.name || workflowId} · 编辑序 ${baseline}`;
    previewStates.value[workflowId] = { workflowId, at: Date.now(), targetLabel, preview: null,
      stale: false, busy: true, error: "", failure: null };
    try {
      const preview = await management.queries.preview({ workflow_definition_id: current.workflow_definition_id,
        document: graphClone(current), expected_edit_sequence: Number(baseline) });
      const latest = entries.value[workflowId]?.document;
      previewStates.value[workflowId] = { workflowId, at: Date.now(), targetLabel, preview,
        stale: !latest || graphSignature(latest) !== documentSignature, busy: false, error: "", failure: null };
      return true;
    } catch (failure) {
      const error = failure instanceof Error ? failure : new Error(String(failure));
      const diagnostics = failure instanceof WorkbenchApiError ? graphClone(failure.diagnostics ?? []) : [];
      previewStates.value[workflowId] = { workflowId, at: Date.now(), targetLabel, preview: null,
        stale: false, busy: false, error: error.message,
        failure: { reason: error.message,
          ...(failure instanceof WorkbenchApiError && failure.code ? { code: failure.code } : {}),
          ...(failure instanceof WorkbenchApiError && failure.status !== undefined ? { status: failure.status } : {}),
          diagnostics } };
      report(failure, "保存预演");
      return false;
    }
  }
  function prepareGraphEdit(document: GraphDocument) {
    if (document.schema_version !== 3) return upgradeGraph(document, executionPackageLock.value);
    document.object_bindings ??= [];
    document.package_lock ??= graphClone(executionPackageLock.value);
    const knownPackages = new Set(document.package_lock.map(item => item.package_id));
    document.package_lock.push(...graphClone(
      executionPackageLock.value.filter(item => !knownPackages.has(item.package_id))));
    document.control_edges ??= [];
    return document;
  }
  async function addNode(component: string, position: { x: number; y: number }): Promise<string | null> {
    const type = catalog.value.find(row => `${row.component_id}@${row.component_version}` === component);
    if (!type) return null;
    const workflowId = workspace.activeWorkflowId;
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
      component_version: type.component_version, title: type.display_name, position: { ...position },
      config: graphClone(type.default_config), configuration_policy: defaultGraphConfigurationPolicy(type) };
    const providedTypes = type.provided_session_item_types ?? [];
    for (const access of type.object_accesses ?? []) {
      node.config[access.config_field] = access.multiple ? [] : "";
    }
    const result = await editable(workflowId, doc => {
      const ports = [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode => [...mode.inputs, ...mode.outputs])];
      if (ports.some(port => (port.data_schema_version ?? 1) !== 1 || (port.accepted_types?.length ?? 0) > 0)
        || type.input_storage === "references"
        || type.capabilities?.some(capability => capability.startsWith("objects:")) || providedTypes.length)
        prepareGraphEdit(doc);
      doc.nodes.push(node);
      syncProvidedSessionItems(doc, node.node_binding_id, providedTypes, false, type.object_accesses);
      syncVariableRegistrationEvent(doc, node);
      return true;
    });
    if (!isAcceptedEdit(result)) return null;
    // A late acceptance must not replace the selection of a different active workflow.
    if (workspace.activeWorkflowId === workflowId) selectNodes([node.node_binding_id]);
    return node.node_binding_id;
  }
  async function patchNode(id: string, patch: Partial<Pick<GraphNode, "title" | "config" | "public_outputs">>):
    Promise<WorkflowEditResult> {
    return editable(workspace.activeWorkflowId, doc => {
      const node = doc.nodes.find(row => row.node_binding_id === id);
      if (!node) return false;
      Object.assign(node, graphClone(patch));
      return true;
    });
  }
  async function moveNodes(moves: { id: string; position: { x: number; y: number } }[]): Promise<WorkflowEditResult> {
    return editable(workspace.activeWorkflowId, doc => {
      for (const move of moves) {
        const node = doc.nodes.find(row => row.node_binding_id === move.id);
        if (node) node.position = { ...move.position };
      }
      return true;
    });
  }
  async function connect(edge: Omit<GraphEdge, "edge_id" | "order">): Promise<WorkflowEditResult> {
    return editable(workspace.activeWorkflowId, doc => {
      const issue = graphConnectionIssue(doc, catalog.value, edge);
      if (issue) { notices.notify("rejected", issue, "添加连线"); return false; }
      const incoming = doc.edges.filter(e => e.target_node_id === edge.target_node_id && e.target_port_id === edge.target_port_id);
      doc.edges.push({ ...edge, edge_id: crypto.randomUUID(), order: incoming.length ? Math.max(...incoming.map(e => e.order)) + 1 : 0 });
      return true;
    });
  }
  async function removeSelection(): Promise<WorkflowEditResult> {
    const workflowId = workspace.activeWorkflowId;
    const current = entries.value[workflowId]?.document;
    if (!current) return { outcome: "no-change", workflowId };
    // Delete and the trash action share this one target computation: node multiselect
    // wins (nodes plus their related existing edges); otherwise exactly one selected
    // edge. A stale unrelated edge identity never joins a node deletion.
    const nodeIds = new Set(selectedNodeIds.value.filter(id =>
      current.nodes.some(node => node.node_binding_id === id)));
    const edgeId = !nodeIds.size && selectedEdgeId.value
      ? [...current.edges, ...(current.control_edges ?? [])]
        .find(edge => edge.edge_id === selectedEdgeId.value)?.edge_id ?? null
      : null;
    if (!nodeIds.size && !edgeId) {
      // Nothing valid to delete: drop invalid identities and submit nothing.
      pruneSelection(workflowId);
      return { outcome: "no-change", workflowId };
    }
    if (nodeIds.size) {
      // Q05-2: declared references from surviving nodes block the whole batch; edges,
      // event targets, bindings and preloads are still cleaned in the same batch.
      const blockers = removalBlockers(current,
        node => catalog.value.find(row => row.component_id === node.component_id
          && row.component_version === node.component_version), nodeIds);
      if (blockers.length) {
        const removedLabel = [...nodeIds].map(id => {
          const node = current.nodes.find(row => row.node_binding_id === id);
          return node ? `${node.title}（${id}）` : id;
        }).join("、");
        recordEditBlock({ workflowId, action: "delete-selection", at: Date.now(),
          title: "删除被阻止：仍有存活引用", targetIds: [...nodeIds], targetLabel: removedLabel,
          summary: `删除 ${nodeIds.size} 个节点会留下 ${blockers.length} 处失效的声明引用或对象键；未提交任何修改。请先修正引用，或在同一批中一并选择引用者。`,
          items: blockers });
        return { outcome: "failed", error: new Error(
          `删除被阻止：${blockers[0]!.message}${blockers.length > 1 ? ` 等 ${blockers.length} 处` : ""}`) };
      }
    }
    if (nodeIds.size) selectNodes([...nodeIds]);
    else selectEdge(edgeId);
    const result = await editable(workflowId, doc => {
      if (!nodeIds.size) {
        const edges = doc.edges.length, controls = doc.control_edges?.length ?? 0;
        doc.edges = doc.edges.filter(edge => edge.edge_id !== edgeId);
        doc.control_edges = (doc.control_edges ?? []).filter(edge => edge.edge_id !== edgeId);
        return doc.edges.length !== edges || (doc.control_edges?.length ?? 0) !== controls;
      }
      doc.nodes = doc.nodes.filter(n => !nodeIds.has(n.node_binding_id));
      doc.edges = doc.edges.filter(e => !nodeIds.has(e.source_node_id) && !nodeIds.has(e.target_node_id));
      if (doc.schema_version >= 2) {
        doc.control_edges = (doc.control_edges ?? []).filter(edge => !nodeIds.has(edge.source_node_id)
          && !nodeIds.has(edge.target_node_id));
        if (doc.event_bindings) doc.event_bindings = doc.event_bindings.map(binding => ({ ...binding,
          target_node_ids: binding.target_node_ids.filter(id => !nodeIds.has(id)) })).filter(binding => binding.target_node_ids.length);
        doc.object_bindings = (doc.object_bindings ?? []).filter(binding =>
          (binding.scope !== "private" || !nodeIds.has(binding.owner_node_id!))
          && !(binding.registrar_node_id && nodeIds.has(binding.registrar_node_id))).map(binding => ({
          ...binding, readers: binding.readers.filter(id => !nodeIds.has(id)),
          writers: binding.writers.filter(id => !nodeIds.has(id)),
        }));
        if (doc.session_preloads) {
          const keys = new Set(doc.object_bindings.map(binding => binding.object_key));
          doc.session_preloads = doc.session_preloads.filter(entry => keys.has(entry.object_key));
        }
      }
      return true;
    });
    // Removal from the accepted document prunes the deleted identities; a definite
    // failure keeps the still-valid selection untouched.
    if (isAcceptedEdit(result) || result.outcome === "no-change") pruneSelection(workflowId);
    return result;
  }
  async function reorderEdge(id: string, offset: number): Promise<WorkflowEditResult> {
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
  async function undo(redo = false): Promise<WorkflowEditResult | null> {
    if (redo ? !canRedo.value : !canUndo.value) return null;
    const id = workspace.activeWorkflowId;
    const h = history.value[id];
    const snapshot = (redo ? h?.future : h?.past)?.pop();
    if (!snapshot) return null;
    if (configurationApplication) {
      // Restore the history entry first: a rejected undo must not consume it.
      if (redo) h?.future.push(snapshot);
      else h?.past.push(snapshot);
      try {
        const result = await submitConfigurationDocument(id, snapshot, redo ? "redo" : "undo");
        if (!result) return { outcome: "declined", workflowId: id };
        if (result.outcome === "confirmation_required") return { outcome: "confirmation-required", workflowId: id };
        // Only identities still present after the restored document survive; the
        // historical selection is not restored.
        pruneSelection(id);
        return { outcome: "accepted", workflowId: result.workflow_definition_id, result };
      } catch (failure) {
        report(failure, redo ? "重做工作流修改" : "撤销工作流修改");
        return { outcome: "failed", error: failure instanceof Error ? failure : new Error(String(failure)) };
      }
    }
    (redo ? h.past : h.future).push(graphClone(entries.value[id].document));
    entries.value[id].document = snapshot;
    workspace.updateWorkflowProjection(id, snapshot.name, snapshot.nodes.length);
    if (entries.value[id].saved_document
      && graphSignature(snapshot) === graphSignature(entries.value[id].saved_document!)) workspace.markWorkflowSaved(id);
    else workspace.markWorkflowDraft(id);
    pruneSelection(id);
    return { outcome: "accepted", workflowId: id, result: null };
  }
  async function duplicateSelection(): Promise<WorkflowEditResult> {
    const workflowId = workspace.activeWorkflowId;
    const selected = new Set(selectedNodeIds.value); const clonedIds: string[] = [];
    const result = await editable(workflowId, doc => {
      const mapping = new Map<string, string>();
      const sourceForCopy = new Map<string, GraphNode>();
      const nodes = doc.nodes.filter(n => selected.has(n.node_binding_id)).map(n => {
        const copy = graphClone(n); copy.node_binding_id = crypto.randomUUID();
        mapping.set(n.node_binding_id, copy.node_binding_id); clonedIds.push(copy.node_binding_id);
        sourceForCopy.set(copy.node_binding_id, n);
        copy.position.x += 35; copy.position.y += 35; return copy;
      });
      const copiedProviderBindings: NonNullable<GraphDocument["object_bindings"]> = [];
      const copiedItemKeys = new Map<string, { object_key: string; type_id: string; schema_version: number;
        readers: string[]; writers: string[] }>();
      for (const [copyId, source] of sourceForCopy) {
        for (const provided of typeFor(source)?.provided_session_item_types ?? []) {
          const sourceBinding = (doc.object_bindings ?? []).find(binding =>
            binding.registrar_node_id === source.node_binding_id && binding.type_id === provided.type_id
            && binding.schema_version === provided.schema_version);
          let objectKey: string;
          do { objectKey = `item/${crypto.randomUUID()}`; }
          while ((doc.object_bindings ?? []).some(binding => binding.object_key === objectKey)
            || copiedProviderBindings.some(binding => binding.object_key === objectKey));
          const owner = sourceBinding?.owner_node_id;
          const copyOwner = owner ? mapping.get(owner) ?? owner : null;
          const readers = sourceBinding?.readers.filter(id => mapping.has(id)).map(id => mapping.get(id)!) ?? [];
          const writers = sourceBinding?.writers.filter(id => mapping.has(id)).map(id => mapping.get(id)!) ?? [];
          copiedProviderBindings.push({
            ...(sourceBinding ? graphClone(sourceBinding) : {
              type_id: provided.type_id, schema_version: provided.schema_version, scope: "shared" as const,
              owner_node_id: null, readers: [], writers: [],
            }),
            object_key: objectKey, registrar_node_id: copyId,
            owner_node_id: copyOwner, readers, writers,
          });
          if (sourceBinding) copiedItemKeys.set(sourceBinding.object_key, {
            object_key: objectKey, type_id: provided.type_id, schema_version: provided.schema_version,
            readers: sourceBinding.readers, writers: sourceBinding.writers,
          });
        }
      }
      for (const node of nodes) {
        const source = sourceForCopy.get(node.node_binding_id)!;
        for (const access of typeFor(source)?.object_accesses ?? []) {
          const rewrite = (key: unknown) => {
            if (typeof key !== "string") return key;
            const item = copiedItemKeys.get(key);
            return item?.type_id === access.type_id && item.schema_version === access.schema_version
              && (access.access !== "write" && item.readers.includes(source.node_binding_id)
                || access.access !== "read" && item.writers.includes(source.node_binding_id))
              ? item.object_key : key;
          };
          const value = source.config[access.config_field];
          node.config[access.config_field] = access.multiple && Array.isArray(value)
            ? value.map(rewrite) : rewrite(value);
        }
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
      if (doc.schema_version >= 2) {
        doc.control_edges = [...(doc.control_edges ?? []), ...(doc.control_edges ?? [])
          .filter(edge => mapping.has(edge.source_node_id) && mapping.has(edge.target_node_id))
          .map(edge => ({ ...edge, edge_id: crypto.randomUUID(), source_node_id: mapping.get(edge.source_node_id)!,
            target_node_id: mapping.get(edge.target_node_id)! }))];
        const privateBindings: NonNullable<GraphDocument["object_bindings"]> = [];
        doc.object_bindings = (doc.object_bindings ?? []).map(binding => {
          if (binding.registrar_node_id && mapping.has(binding.registrar_node_id)) return binding;
          if (binding.scope === "private") {
            const owner = mapping.get(binding.owner_node_id!);
            if (!owner) return binding;
            const key = `private/${crypto.randomUUID()}`;
            privateBindings.push({ ...graphClone(binding), object_key: key, owner_node_id: owner,
              readers: binding.readers.length ? [owner] : [], writers: binding.writers.length ? [owner] : [] });
            const preload = doc.session_preloads?.find(entry => entry.object_key === binding.object_key);
            if (preload) doc.session_preloads!.push({ object_key: key, value: graphClone(preload.value) });
            const copy = nodes.find(node => node.node_binding_id === owner)!;
            if (copy.config.object_key === binding.object_key) copy.config.object_key = key;
            if (Array.isArray(copy.config.object_keys)) copy.config.object_keys = copy.config.object_keys
              .map(existing => existing === binding.object_key ? key : existing);
            for (const access of typeFor(copy)?.object_accesses ?? []) {
              const value = copy.config[access.config_field];
              if (access.multiple && Array.isArray(value))
                copy.config[access.config_field] = value.map(existing => existing === binding.object_key ? key : existing);
              else if (value === binding.object_key) copy.config[access.config_field] = key;
            }
            return binding;
          }
          return { ...binding, readers: [...binding.readers, ...binding.readers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)],
            writers: [...binding.writers, ...binding.writers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)] };
        });
        doc.object_bindings.push(...privateBindings, ...copiedProviderBindings);
      }
      doc.nodes.push(...nodes); doc.edges.push(...edges);
      for (const node of nodes) syncVariableRegistrationEvent(doc, node);
      return !!nodes.length;
    });
    if (isAcceptedEdit(result) && workspace.activeWorkflowId === workflowId) selectNodes(clonedIds);
    return result;
  }
  async function replaceNode(id: string, component: string): Promise<WorkflowEditResult | null> {
    const type = catalog.value.find(t => `${t.component_id}@${t.component_version}` === component);
    if (!type) return null;
    const workflowId = workspace.activeWorkflowId;
    const current = entries.value[workflowId]?.document;
    const target = current?.nodes.find(node => node.node_binding_id === id);
    if (current && target) {
      // Q05-3: old edges/registrations must verifiably fit the new component before the
      // shared node id and anything else is committed. Nothing is remapped or deleted.
      const blockers = replacementBlockers(current,
        node => catalog.value.find(row => row.component_id === node.component_id
          && row.component_version === node.component_version), target, type);
      if (blockers.length) {
        recordEditBlock({ workflowId, action: "replace-node", at: Date.now(),
          title: "替换被阻止：旧边或登记与新组件不兼容", targetIds: [id],
          targetLabel: `${target.title}（${target.component_id}@${target.component_version}）→ ${type.component_id}@${type.component_version}`,
          summary: `替换 ${target.title} 有 ${blockers.length} 处无法确认兼容的旧边、声明引用或对象登记；未提交任何修改。请先断开或处理后再替换。`,
          items: blockers });
        return { outcome: "failed", error: new Error(
          `替换被阻止：${blockers[0]!.message}${blockers.length > 1 ? ` 等 ${blockers.length} 处` : ""}`) };
      }
    }
    const result = await editable(workflowId, doc => {
      const node = doc.nodes.find(n => n.node_binding_id === id); if (!node) return false;
      const hadRegistration = (doc.object_bindings ?? []).some(binding => binding.registrar_node_id === id);
      node.component_id = type.component_id; node.component_version = type.component_version;
      node.config = graphClone(type.default_config); node.public_outputs = [];
      node.configuration_policy ??= defaultGraphConfigurationPolicy(type);
      if (node.configuration_policy.prompt_mode === "adjustable" && !graphNodeAllowsAdjustable(node, type))
        node.configuration_policy = { schema_version: 1, prompt_mode: "fixed" };
      if (type.input_storage === "references" || type.capabilities?.some(capability => capability.startsWith("objects:"))
        || [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode =>
          [...mode.inputs, ...mode.outputs])].some(port => (port.data_schema_version ?? 1) !== 1
            || (port.accepted_types?.length ?? 0) > 0)
        || (type.provided_session_item_types?.length ?? 0) > 0 || hadRegistration)
        prepareGraphEdit(doc);
      for (const access of type.object_accesses ?? []) {
        node.config[access.config_field] = access.multiple ? [] : "";
      }
      syncProvidedSessionItems(doc, id, type.provided_session_item_types ?? [], true, type.object_accesses);
      syncVariableRegistrationEvent(doc, node);
      // Changed types preserve old edges for explicit diagnosis, never guess port mappings.
      return true;
    });
    // The replaced node keeps its identity and becomes the explicit selection.
    if (isAcceptedEdit(result) && workspace.activeWorkflowId === workflowId) selectNodes([id]);
    return result;
  }
  async function setControlDependencies(nodeId: string, sources: string[]): Promise<WorkflowEditResult> {
    return editable(workspace.activeWorkflowId, doc => {
      const ids = new Set(doc.nodes.map(node => node.node_binding_id));
      if (!ids.has(nodeId) || !sources.every(id => ids.has(id) && id !== nodeId)
        || new Set(sources).size !== sources.length) return false;
      prepareGraphEdit(doc);
      const existing = doc.control_edges!.filter(edge => edge.target_node_id === nodeId);
      doc.control_edges = [...doc.control_edges!.filter(edge => edge.target_node_id !== nodeId),
        ...sources.map(source => existing.find(edge => edge.source_node_id === source) ?? {
          edge_id: crypto.randomUUID(), source_node_id: source, target_node_id: nodeId,
        })];
      return true;
    });
  }
  async function setObjectBindings(value: unknown): Promise<WorkflowEditResult | null> {
    if (!document.value || !isGraphObjectBindings(value, document.value.nodes)) {
      notices.notify("rejected", "对象绑定须包含唯一键、确切类型版本和有效的节点权限；私有对象仅允许所有者访问", "会话对象绑定");
      return null;
    }
    const issue = document.value.session_preloads
      ? sessionPreloadsIssue(value, document.value.session_preloads, dataTypes.value) : null;
    if (issue) { notices.notify("rejected", issue, "会话对象绑定"); return null; }
    return editable(workspace.activeWorkflowId, doc => {
      prepareGraphEdit(doc);
      doc.object_bindings = graphClone(value); return true;
    });
  }
  async function setSessionPreload(binding: GraphObjectBinding, value: unknown, previousKey?: string):
    Promise<WorkflowEditResult | null> {
    const current = document.value;
    if (!current || editorLocked.value) return null;
    if (!isGraphJsonValue(value)) {
      notices.notify("rejected", "初值必须是有效 JSON。", "会话预加载数据"); return null;
    }
    if (previousKey !== undefined && binding.object_key !== previousKey) {
      notices.notify("rejected", "已创建预加载项的对象键不可修改。", "会话预加载数据"); return null;
    }
    const previous = previousKey ? current.session_preloads?.find(entry => entry.object_key === previousKey) : null;
    if (previousKey && !previous || (current.object_bindings ?? []).some(item =>
      item.object_key === binding.object_key && item.object_key !== previousKey)) {
      notices.notify("rejected", "对象键已存在或原预加载项已变化。", "会话预加载数据"); return null;
    }
    const bindings = [...(current.object_bindings ?? []).filter(item => item.object_key !== previousKey), graphClone(binding)];
    const preloads = [...(current.session_preloads ?? []).filter(item => item.object_key !== previousKey),
      { object_key: binding.object_key, value: graphClone(value) }];
    const issue = !isGraphObjectBindings(bindings, current.nodes) ? "请检查对象键与节点权限。"
      : sessionPreloadsIssue(bindings, preloads, dataTypes.value);
    if (issue) { notices.notify("rejected", issue, "会话预加载数据"); return null; }
    return editable(workspace.activeWorkflowId, doc => {
      prepareGraphEdit(doc);
      doc.schema_version = 3;
      for (const node of doc.nodes) node.configuration_policy ??= { schema_version: 1, prompt_mode: "fixed" };
      doc.object_bindings = bindings; doc.session_preloads = preloads;
      return true;
    });
  }
  async function removeSessionPreload(key: string): Promise<WorkflowEditResult | null> {
    if (!document.value?.session_preloads?.some(entry => entry.object_key === key)) return null;
    return editable(workspace.activeWorkflowId, doc => {
      prepareGraphEdit(doc);
      doc.session_preloads = doc.session_preloads!.filter(entry => entry.object_key !== key);
      doc.object_bindings = doc.object_bindings!.filter(binding => binding.object_key !== key);
      return true;
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
      // Directory refresh reads only session summaries and never builds full views.
      const rows = await management.queries.sessionSummaries(definitionId, true);
      if (!current()) return;
      sessionRows.value[id] = rows;
      if (sessionId) {
        const incoming = await management.queries.session(sessionId,
          views.value[sessionId]?.workflow_definition_id ?? entry.session_document?.workflow_definition_id ?? definitionId);
        if (current()) observe(incoming);
      }
      // Formal refresh uses the same existence rule as acceptance for the active graph.
      if (current()) pruneSelection(id);
    } catch (failure) { if (current()) report(failure, "读取工作流会话"); }
  }
  function schedulePoll(id: string, generation: number) {
    if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    const entry = entries.value[id];
    const view = entry?.session_id ? views.value[entry.session_id] : null;
    if (view?.archived || !["running", "prepared", "pausing"].includes(view?.status ?? "")) return;
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
    clearSelection();
    if (pollTimer) clearTimeout(pollTimer);
    if (!alive) return;
    const ready = await ensureDefinitionEntry(id);
    if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
    if (!ready || !entries.value[id]) return;
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
      entry.session_id = row.workflow_session_id;
      if (entry.session_document?.workflow_definition_id !== row.workflow_definition_id
        || entry.session_document?.revision !== row.definition_revision) delete entry.session_document;
      observe(row);
      schedulePoll(workflowId, viewVersion);
    } catch (failure) { if (current()) report(failure, "切换会话"); }
    finally { if (generation === sessionSelectionGeneration) selectingSessionFor.value = null; }
  }
  async function submitPrimary() {
    if (!active.value) return;
    const id = workspace.activeWorkflowId;
    const action = primaryAction.value;
    if (action === "start") {
      if (locked.value || !!session.value && (!session.value.can_submit || session.value.readonly)) return;
    } else if (controlLocked.value || !session.value?.available_actions?.includes(action)) return;
    busy.value = action;
    try {
      if (action === "start") await management.commands.start({ workflowId: id });
      else await management.commands.control({ workflowId: id }, action);
      if (runFailures.value[id]) delete runFailures.value[id];
      schedulePoll(id, viewGeneration);
    } catch (failure) {
      recordRunFailure(id, action === "start" ? "start" : "control", failure);
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
  async function setEventBindings(value: unknown): Promise<WorkflowEditResult> {
    return editable(workspace.activeWorkflowId, doc => {
      if (!isGraphEventBindings(value, doc.nodes)) throw new Error("检查事件身份、版本、节点 UUID、受众和本地对象 schema");
      prepareGraphEdit(doc); doc.event_bindings = graphClone(value);
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
  function registrationTarget(nodeId: string) {
    return variableRegistrationTarget(active.value, session.value, nodeId);
  }
  async function submitVariableRegistration(nodeId: string) {
    const target = registrationTarget(nodeId);
    if (!target?.canSubmit || !target.event || locked.value) return false;
    return submitEvent(target.event.event_id, target.event.schema_version, {});
  }
  async function reconcile(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry || busy.value) return;
    const managed = managementRequests.forWorkflow(id);
    await managed.initialize();
    if (!managed.pending.value) return;
    busy.value = "reconcile";
    try {
      if (!await managed.reconcile())
        throw new WorkbenchApiError("unknown", managed.error.value || "原管理请求仍待核实");
    } catch (failure) { report(failure, "核实原工作流管理请求"); }
    finally { busy.value = null; }
  }
  function discardDraft() {
    const workflow = workspace.activeWorkflow;
    if (locked.value || !editorStable.value || workflow.state !== "draft") return;
    const id = workflow.id;
    const entry = entries.value[id];
    if (!entry) return;
    // Restoring a saved draft returns to the accepted current, not to an
    // arbitrary Store value that happens to sit in saved_document.
    const accepted = configurationApplication?.accepted(id) ?? null;
    if (accepted || entry.saved_document) {
      const restored = graphClone(accepted ?? entry.saved_document!);
      entry.document = restored;
      entry.saved_document = graphClone(restored);
      entry.saved_revision = restored.revision;
      history.value[id] = { past: [], future: [] };
      workspace.updateWorkflowProjection(id, restored.name, restored.nodes.length);
      workspace.markWorkflowSaved(id, restored.name);
      return;
    }
    const fallback = workspace.workflows.find(row => row.id !== id && !row.copyPending)?.id;
    if (fallback) workspace.openWorkflow(fallback);
    else {
      workspace.activeWorkflowId = ""; workspace.selectedWorkflowId = ""; workspace.viewVersion++;
    }
    workspace.removeWorkflow(id);
    delete entries.value[id]; delete history.value[id];
  }
  async function closeRun() {
    const view = session.value;
    if (!view || controlLocked.value || !view.available_actions?.includes("close")) return;
    const id = workspace.activeWorkflowId;
    busy.value = "close";
    try {
      await management.commands.control({ workflowId: id }, "close");
      if (runFailures.value[id]) delete runFailures.value[id];
    }
    catch (failure) { recordRunFailure(id, "close", failure); report(failure, "关闭运行"); }
    finally { busy.value = null; }
  }
  const retryAgentActions = ["retry_archive", "retry_acceptance", "retry_failed_node"] as const;
  async function submitAgentAction(action: typeof retryAgentActions[number]) {
    if (!(retryAgentActions as readonly string[]).includes(action)) return false;
    const view = session.value;
    if (!view || controlLocked.value || !view.available_actions?.includes(action)) return false;
    const id = workspace.activeWorkflowId; busy.value = action;
    try {
      await management.commands.control({ workflowId: id }, action);
      if (runFailures.value[id]) delete runFailures.value[id];
      schedulePoll(id, viewGeneration); return true;
    } catch (failure) { recordRunFailure(id, "agent", failure);
      report(failure, action === "retry_acceptance" ? "结果接纳重试"
      : action === "retry_failed_node" ? "重试失败节点" : "归档重试"); return false; }
    finally { busy.value = null; }
  }
  function setInputs(value: Record<string, unknown>) {
    if (!active.value || editorLocked.value) return;
    active.value.external_inputs = graphClone(value);
  }
  function resetPrivateState(id: string, nodeId: string) {
    const entry = entries.value[id]; if (!entry || busy.value) return;
    // The visible control stays available for the current document target. The former
    // in-memory reset mapping fed the retired session-copy retry path, so no mapping
    // is queued any more and no business request is sent.
    if (!entry.document.nodes.some(node => node.node_binding_id === nodeId)) return;
  }
  return { entries, catalog, frontendExtensions, packageLock, executionPackageLock, dataTypes, catalogLoading, catalogError, definitionsLoading, sessionRows, views, history, busy, active, document,
    fixedOnly, session, pending, locked, copyLocked, controlLocked, editorLocked, primaryAction, statusLabel,
    selectedNodeIds, selectedEdgeId, selectNodes, selectEdge, clearSelection, pruneSelection,
    canUndo, canRedo, setEventBindings, submitEvent, registrationTarget, submitVariableRegistration,
    activeRejectedEdit, activeExternalConflict, activeDerivationAsk, activeConfigurationRequest, dismissRejectedEdit,
    dismissExternalConflict,
    activeEditBlock, dismissEditBlock, activeEditFailure, dismissEditFailure,
    activeRunFailure, dismissRunFailure, activePreview, dismissPreview, previewConfiguration,
    abandonConfiguration, reloadAcceptedConfiguration, confirmDerivation, declineDerivation,
    isGeneric, createWorkflow, typeFor, loadCatalog, storeSnapshot,
    saveWorkflow, loadDefinitions, ensureDefinitionEntry, manageWorkflow, manageSession, copyWorkflow, renameWorkflow, refreshPublishedRevision, readFrozenDefinition, addNode, patchNode,
    patchNodeConfiguration, patchNodeConfigurationPolicy, moveNodes, connect, removeSelection, reorderEdge, undo, duplicateSelection,
    replaceNode, setControlDependencies, setObjectBindings, setSessionPreload, removeSessionPreload,
    activate, refresh, createSession, selectSession, submitPrimary, reconcile, discardDraft, closeRun, setInputs, resetPrivateState, submitAgentAction,
    management, editing, installWorkflowConfigurationApplication, configurationPendingRequests, reconcileConfiguration,
    managementPendingRequests, copyPendingRequests, reconcileCopy, installWorkflowConfigurationCopyApplication,
    discoveredRequests, pendingDiscoveryBusy, pendingDiscoveryError, pendingDiscoveryCursor, recoveryErrors,
    discoverPendingRequests, reconcileDiscoveredRequest, discoveredRequestBusy, supportedDiscoveredRequest,
    discoveredPendingRequests };
});
