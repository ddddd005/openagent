export interface GraphNode {
  node_binding_id: string;
  component_id: string;
  component_version: string;
  title: string;
  position: { x: number; y: number };
  config: Record<string, unknown>;
  public_outputs?: string[];
}
export interface GraphEdge {
  edge_id: string;
  source_node_id: string;
  source_port_id: string;
  target_node_id: string;
  target_port_id: string;
  order: number;
}
export interface GraphDocument {
  schema_version: 1 | 2;
  workflow_definition_id: string;
  revision: number;
  name: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  object_bindings?: GraphObjectBinding[];
  package_lock?: { package_id: string; version: string }[];
  execution_roots?: string[];
  control_edges?: GraphControlEdge[];
  event_bindings?: GraphEventBinding[];
}
export interface GraphEventBinding {
  event_id: string;
  schema_version: number;
  display_name: string;
  audience: "management" | "consumer";
  target_node_ids: string[];
  payload_schema: Record<string, unknown> & { type: "object" };
}
export interface GraphControlEdge {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
}
export interface GraphObjectBinding {
  object_key: string;
  type_id: string;
  schema_version: number;
  scope: "shared" | "private";
  readers: string[];
  writers: string[];
  owner_node_id: string | null;
  default_value?: unknown;
}
export interface GraphSessionObject {
  revision_id: string;
  revision: number;
  type_id: string;
  schema_version: number;
  deleted: boolean;
  value: unknown;
  binding: GraphObjectBinding;
}
export interface GraphDataType {
  type_id: string;
  schema_version: number;
  scope: "session" | "content" | "global";
  schema: Record<string, unknown>;
  has_default?: boolean;
  default_value?: unknown;
  [key: string]: unknown;
}
export interface GraphPort {
  port_id: string;
  data_type: string;
  required: boolean;
  multiple: boolean;
  data_schema_version?: number;
}
export interface GraphNodeType {
  component_id: string;
  component_version: string;
  display_name: string;
  category: string;
  config_schema: Record<string, unknown>;
  default_config: Record<string, unknown>;
  inputs: GraphPort[];
  outputs: GraphPort[];
  is_output: boolean;
  executable: boolean;
  input_storage?: "inline" | "references";
  capabilities?: string[];
  object_accesses?: Record<string, unknown>[];
  config_node_references?: {
    config_field: string;
    multiple: boolean;
    target_types: { component_id: string; component_version: string }[];
  }[];
  [key: string]: unknown;
  port_modes?: Record<string, { inputs: GraphPort[]; outputs: GraphPort[] }>;
}
export interface GraphDiagnostic {
  reason_code: string;
  message: string;
  node_id?: string;
  port_id?: string;
  edge_id?: string;
  dependent_outputs?: string[];
  phase?: string;
}
export interface GraphNodeRun {
  node_binding_id: string;
  node_run_id?: string;
  component_id?: string;
  status: string;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  diagnostics?: GraphDiagnostic[];
}
export interface GraphRun {
  chain_run_id: string;
  status: string;
  node_runs: GraphNodeRun[];
  diagnostics?: GraphDiagnostic[];
  outputs?: Record<string, unknown>;
  definition_revision?: number;
}
export interface GraphSession {
  schema_version: 2;
  execution_model: "graph";
  workflow_session_id: string;
  workflow_definition_id: string;
  definition_revision: number;
  revision: number;
  data_revision: number;
  head_revision: number;
  status: string;
  can_submit: boolean;
  nodes: { node_binding_id: string; label: string; status: string; run_id: string | null;
    revision: number | null; outputs: Record<string, unknown>; diagnostic: GraphDiagnostic | null;
    budget?: { max_model_requests: number; max_model_attempts: number; model_requests?: number;
      attempts?: number; accepted_messages?: number } | null }[];
  chains: { chain_run_id: string; status: string; revision: number; targets: string[];
    completed_nodes: string[]; node_run_ids: string[]; diagnostic: GraphDiagnostic | null;
    workflow_session_id?: string; workflow_definition_id?: string; definition_revision?: number }[];
  outputs: { output_id: string; node_binding_id: string; port_id: string;
    chain_run_id: string; payload: unknown }[];
  data: { revision: number; values: Record<string, unknown>; [key: string]: unknown };
  private_states?: Record<string, unknown>;
  objects?: Record<string, GraphSessionObject>;
  available_actions?: string[];
  selected_chain_run_id?: string | null;
  inherited_history?: GraphSession["chains"];
  source?: Record<string, unknown>;
}
export interface GraphCommand {
  path: string;
  body: Record<string, unknown>;
  action: string;
  source_session_id?: string;
  expected_chain_run_id?: string;
  expected_definition_id?: string;
  expected_definition_revision?: number;
}
export interface GraphCandidate {
  candidate_id: string;
  chain_run_id: string;
  source_session_id: string;
  source_workflow_definition_id: string;
  source_definition_revision: number;
  data_revision: number;
  selected: boolean;
  can_select: boolean;
  diagnostic: GraphDiagnostic | null;
}
export interface GraphCandidates {
  schema_version: 1;
  kind: "workflow.graph-candidates";
  workflow_session_id: string;
  workflow_definition_id: string;
  definition_revision: number;
  session_revision: number;
  head_revision: number;
  candidates: GraphCandidate[];
}
export interface GraphRunDetail {
  chain: GraphSession["chains"][number];
  node_runs: { run_id: string; chain_run_id: string; node_binding_id: string; workflow_session_id: string;
    status: string; input_values: Record<string, unknown>; input_refs: Record<string, unknown>;
    output_refs: Record<string, string>; reads: unknown[]; effects: unknown[]; diagnostic: GraphDiagnostic | null;
    schema_version?: number; input_storage?: "inline" | "references";
    control_refs?: { edge_id: string; run_id: string }[] }[];
  outputs: GraphSession["outputs"];
}
export interface GraphEntry {
  document: GraphDocument;
  saved_revision: number;
  session_id: string | null;
  pending: GraphCommand | null;
  rejected_copy?: GraphCommand;
  saved_document?: GraphDocument;
  diagnostics?: GraphDiagnostic[];
  external_inputs?: Record<string, unknown>;
  state_mappings?: { source_node_id: string; target_node_id: string; action: "copy" | "reset" }[];
}
export const graphClone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
export const graphObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
export function isGraphJsonValue(value: unknown, ancestors = new Set<object>()): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object" || ancestors.has(value) || Object.getOwnPropertySymbols(value).length) return false;
  const prototype = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && prototype !== null
    && (Object.getPrototypeOf(prototype) !== null || prototype.constructor?.name !== "Object")) return false;
  ancestors.add(value);
  const descriptors = Object.getOwnPropertyDescriptors(value);
  let valid: boolean;
  if (Array.isArray(value)) {
    valid = prototype.constructor?.name === "Array" && Object.getOwnPropertyNames(value).length === value.length + 1
      && Object.keys(value).length === value.length && Array.from({ length: value.length }, (_, index) => {
      const descriptor = descriptors[String(index)];
      return !!descriptor && descriptor.enumerable && "value" in descriptor && isGraphJsonValue(descriptor.value, ancestors);
    }).every(Boolean);
  } else {
    valid = Object.values(descriptors).every(descriptor => descriptor.enumerable && "value" in descriptor
      && isGraphJsonValue(descriptor.value, ancestors));
  }
  ancestors.delete(value); return valid;
}
export const graphUuid = (value: unknown): value is string => typeof value === "string"
  && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
const positive = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0;
const nonnegative = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;
const boundedName = (value: unknown): value is string => typeof value === "string"
  && value.length > 0 && value.length <= 128 && value.trim() === value;
export function isLocalEventSchema(value: unknown): value is GraphEventBinding["payload_schema"] {
  if (!graphObject(value) || value.type !== "object" || !isGraphJsonValue(value)) return false;
  const pending: unknown[] = [value];
  while (pending.length) {
    const schema = pending.pop();
    if (!graphObject(schema)) continue;
    if (["$ref", "$dynamicRef", "$recursiveRef"].some(key => Object.hasOwn(schema, key)
      && (typeof schema[key] !== "string" || !schema[key].startsWith("#")))) return false;
    for (const key of ["$defs", "definitions", "properties", "patternProperties", "dependentSchemas"])
      if (graphObject(schema[key])) pending.push(...Object.values(schema[key]));
    for (const key of ["additionalProperties", "unevaluatedProperties", "propertyNames", "contains", "items",
      "additionalItems", "unevaluatedItems", "not", "if", "then", "else"]) if (Object.hasOwn(schema, key)) pending.push(schema[key]);
    for (const key of ["allOf", "anyOf", "oneOf", "prefixItems", "items"]) if (Array.isArray(schema[key])) pending.push(...schema[key]);
    if (graphObject(schema.dependencies)) pending.push(...Object.values(schema.dependencies).filter(graphObject));
  }
  return true;
}
export function isGraphEventBindings(value: unknown, nodes?: GraphNode[]): value is GraphEventBinding[] {
  if (!Array.isArray(value) || value.length > 512) return false;
  const identities = new Set<string>(), ids = nodes ? new Set(nodes.map(node => node.node_binding_id)) : null;
  return value.every(binding => {
    if (!graphObject(binding) || Object.keys(binding).length !== 6
      || !["event_id", "schema_version", "display_name", "audience", "target_node_ids", "payload_schema"].every(key => Object.hasOwn(binding, key))
      || !boundedName(binding.event_id) || !positive(binding.schema_version) || typeof binding.display_name !== "string"
      || !binding.display_name.trim() || binding.display_name.length > 128
      || !["management", "consumer"].includes(String(binding.audience))
      || !Array.isArray(binding.target_node_ids) || !binding.target_node_ids.length || binding.target_node_ids.length > 512
      || !binding.target_node_ids.every(id => graphUuid(id) && (!ids || ids.has(id)))
      || new Set(binding.target_node_ids).size !== binding.target_node_ids.length || !isLocalEventSchema(binding.payload_schema)) return false;
    const key = JSON.stringify([binding.event_id, binding.schema_version]);
    if (identities.has(key)) return false;
    identities.add(key); return true;
  });
}
export function remapGraphEventBindings(bindings: GraphEventBinding[], mapping: ReadonlyMap<string, string>) {
  return bindings.map(binding => ({ ...graphClone(binding), target_node_ids: binding.target_node_ids.map(id => mapping.get(id) ?? id) }));
}
export function isGraphPackageLock(value: unknown): value is NonNullable<GraphDocument["package_lock"]> {
  return Array.isArray(value) && value.length <= 256 && value.every(item => graphObject(item)
    && boundedName(item.package_id) && boundedName(item.version))
    && new Set(value.map(item => item.package_id)).size === value.length;
}
export function isGraphObjectBinding(value: unknown, nodeIds?: Set<string>): value is GraphObjectBinding {
  if (!graphObject(value) || !boundedName(value.object_key) || !boundedName(value.type_id)
    || Object.keys(value).some(key => !["object_key", "type_id", "schema_version", "scope",
      "readers", "writers", "owner_node_id", "default_value"].includes(key))
    || !positive(value.schema_version) || !["shared", "private"].includes(String(value.scope))
    || !["readers", "writers"].every(key => Array.isArray(value[key]) && value[key].length <= 512
      && value[key].every(graphUuid) && new Set(value[key]).size === value[key].length)
    || !(value.scope === "shared" ? value.owner_node_id === null : graphUuid(value.owner_node_id))) return false;
  const members = [...value.readers as string[], ...value.writers as string[]];
  if (value.scope === "private" && members.some(id => id !== value.owner_node_id)) return false;
  return !nodeIds || [...members, ...(value.owner_node_id ? [value.owner_node_id as string] : [])]
    .every(id => nodeIds.has(id));
}
export function isGraphObjectBindings(value: unknown, nodes?: GraphNode[]): value is GraphObjectBinding[] {
  const ids = nodes ? new Set(nodes.map(node => node.node_binding_id)) : undefined;
  return Array.isArray(value) && value.length <= 1024 && value.every(binding => isGraphObjectBinding(binding, ids))
    && new Set(value.map(binding => binding.object_key)).size === value.length;
}
function graphBudget(value: unknown) {
  return graphObject(value) && positive(value.max_model_requests) && positive(value.max_model_attempts)
    && ["model_requests", "attempts", "accepted_messages"].every(key => value[key] === undefined || nonnegative(value[key]));
}
export function isGraphDocument(value: unknown): value is GraphDocument {
  if (!graphObject(value) || value.schema_version !== 1 && value.schema_version !== 2 || !graphUuid(value.workflow_definition_id)
    || !positive(value.revision) || typeof value.name !== "string"
    || !Array.isArray(value.nodes) || !Array.isArray(value.edges)
    || value.nodes.length > 1024 || value.edges.length > 8192) return false;
  const ids = new Set<string>();
  for (const node of value.nodes) {
    if (!graphObject(node) || !graphUuid(node.node_binding_id) || ids.has(node.node_binding_id)
      || typeof node.component_id !== "string" || !node.component_id || typeof node.component_version !== "string"
      || !node.component_version || typeof node.title !== "string" || !graphObject(node.position)
      || ![node.position.x, node.position.y].every(n => typeof n === "number" && Number.isFinite(n))
      || !graphObject(node.config) || node.public_outputs !== undefined
      && (!Array.isArray(node.public_outputs) || !node.public_outputs.every(p => typeof p === "string"))) return false;
    ids.add(node.node_binding_id);
  }
  const edges = new Set<string>();
  if (!value.edges.every(edge => {
    if (!graphObject(edge) || !graphUuid(edge.edge_id) || edges.has(edge.edge_id)
      || !graphUuid(edge.source_node_id) || !graphUuid(edge.target_node_id)
      || typeof edge.source_port_id !== "string" || typeof edge.target_port_id !== "string"
      || !nonnegative(edge.order)) return false;
    edges.add(edge.edge_id);
    return true;
  })) return false;
  if (value.schema_version === 1) return !["object_bindings", "package_lock", "execution_roots", "control_edges", "event_bindings"]
    .some(key => value[key] !== undefined);
  if (!isGraphObjectBindings(value.object_bindings, value.nodes as GraphNode[])
    || value.package_lock !== undefined && !isGraphPackageLock(value.package_lock)) return false;
  if (value.event_bindings !== undefined && (!isGraphEventBindings(value.event_bindings, value.nodes as GraphNode[])
    || value.event_bindings.length > 0 && !Object.hasOwn(value, "execution_roots"))) return false;
  const roots = value.execution_roots ?? [];
  if (!Array.isArray(roots) || roots.length > 1024 || !roots.every(id => graphUuid(id) && ids.has(id))
    || new Set(roots).size !== roots.length) return false;
  const controls = value.control_edges ?? [];
  return Array.isArray(controls) && controls.length <= 8192 && controls.every(control => {
    if (!graphObject(control) || !graphUuid(control.edge_id) || edges.has(control.edge_id)
      || Object.keys(control).length !== 3
      || !graphUuid(control.source_node_id) || !ids.has(control.source_node_id)
      || !graphUuid(control.target_node_id) || !ids.has(control.target_node_id)) return false;
    edges.add(control.edge_id); return true;
  });
}
export function isGraphCatalog(value: unknown): value is GraphNodeType[] {
  const ports = (v: unknown) => Array.isArray(v) && v.every(p => graphObject(p)
    && typeof p.port_id === "string" && typeof p.data_type === "string"
    && typeof p.required === "boolean" && typeof p.multiple === "boolean"
    && (p.data_schema_version === undefined || positive(p.data_schema_version)));
  return Array.isArray(value) && value.every(type => graphObject(type)
    && typeof type.component_id === "string" && typeof type.component_version === "string"
    && typeof type.display_name === "string" && typeof type.category === "string"
    && graphObject(type.config_schema) && graphObject(type.default_config)
    && ports(type.inputs) && ports(type.outputs) && typeof type.is_output === "boolean"
    && typeof type.executable === "boolean"
    && (type.input_storage === undefined || ["inline", "references"].includes(String(type.input_storage)))
    && (type.config_node_references === undefined || Array.isArray(type.config_node_references)
      && type.config_node_references.every(reference => graphObject(reference)
        && typeof reference.config_field === "string" && reference.config_field.length > 0
        && typeof reference.multiple === "boolean" && Array.isArray(reference.target_types)
        && reference.target_types.length > 0 && reference.target_types.every(target => graphObject(target)
          && typeof target.component_id === "string" && target.component_id.length > 0
          && typeof target.component_version === "string" && target.component_version.length > 0)))
    && (type.port_modes === undefined || graphObject(type.port_modes)
      && Object.values(type.port_modes).every(mode => graphObject(mode) && ports(mode.inputs) && ports(mode.outputs))))
    && new Set(value.map(type => `${type.component_id}@${type.component_version}`)).size === value.length;
}
export function isGraphSession(value: unknown, definitionId?: string): value is GraphSession {
  if (!graphObject(value) || value.schema_version !== 2 || value.execution_model !== "graph"
    || !graphUuid(value.workflow_session_id) || !graphUuid(value.workflow_definition_id)
    || definitionId !== undefined && definitionId !== value.workflow_definition_id
    || !positive(value.definition_revision) || !positive(value.revision)
    || !nonnegative(value.data_revision) || !nonnegative(value.head_revision)
    || !["idle", "prepared", "running", "pausing", "paused", "budget_exhausted", "archive_failed", "failed", "succeeded", "closed", "recovery_unavailable"].includes(String(value.status))
    || typeof value.can_submit !== "boolean" || !graphObject(value.data) || !graphObject(value.data.values)
    || !Array.isArray(value.nodes) || !Array.isArray(value.chains) || !Array.isArray(value.outputs)
    || value.selected_chain_run_id !== undefined && value.selected_chain_run_id !== null && !graphUuid(value.selected_chain_run_id)
    || value.objects !== undefined && (!graphObject(value.objects) || !Object.entries(value.objects).every(([key, object]) =>
      graphObject(object) && positive(object.revision) && graphUuid(object.revision_id)
      && positive(object.schema_version) && boundedName(object.type_id) && typeof object.deleted === "boolean"
      && Object.prototype.hasOwnProperty.call(object, "value")
      && isGraphObjectBinding(object.binding) && object.binding.object_key === key
      && object.binding.type_id === object.type_id && object.binding.schema_version === object.schema_version))) return false;
  return value.nodes.every(node => graphObject(node) && graphUuid(node.node_binding_id)
    && typeof node.status === "string" && typeof node.label === "string" && graphObject(node.outputs)
    && (node.run_id === null || graphUuid(node.run_id))
    && (node.budget === undefined || node.budget === null || graphBudget(node.budget)))
    && value.chains.every(chain => graphObject(chain) && graphUuid(chain.chain_run_id)
      && typeof chain.status === "string" && nonnegative(chain.revision)
      && Array.isArray(chain.targets) && chain.targets.every(graphUuid)
      && Array.isArray(chain.completed_nodes) && chain.completed_nodes.every(graphUuid))
    && value.outputs.every(output => graphObject(output) && graphUuid(output.output_id)
      && graphUuid(output.node_binding_id) && graphUuid(output.chain_run_id) && typeof output.port_id === "string")
    && new Set(value.nodes.map(node => node.node_binding_id)).size === value.nodes.length;
}
export function nodePorts(type: GraphNodeType | undefined, node: GraphNode, direction: "inputs" | "outputs") {
  return type?.port_modes?.[String(node.config.mode)]?.[direction] ?? type?.[direction] ?? [];
}
function canonicalGraphValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalGraphValue);
  if (graphObject(value)) return Object.fromEntries(Object.keys(value).sort()
    .map(key => [key, canonicalGraphValue(value[key])]));
  return value;
}
export function graphSignature(document: GraphDocument) {
  return JSON.stringify(canonicalGraphValue({ ...document, revision: 0 }));
}
export function newGraph(name: string): GraphDocument {
  return { schema_version: 1, workflow_definition_id: crypto.randomUUID(), revision: 1, name, nodes: [], edges: [] };
}
export function upgradeGraph(document: GraphDocument, packageLock: NonNullable<GraphDocument["package_lock"]> = []) {
  document.schema_version = 2;
  document.object_bindings ??= [];
  document.package_lock ??= graphClone(packageLock);
  const knownPackages = new Set(document.package_lock.map(item => item.package_id));
  document.package_lock.push(...graphClone(packageLock.filter(item => !knownPackages.has(item.package_id))));
  document.execution_roots ??= [];
  document.control_edges ??= [];
  return document;
}
export function resolveGraphRunInputs(detail: GraphRunDetail, run: GraphRunDetail["node_runs"][number]) {
  const artifacts = new Map(detail.outputs.map(output => [output.output_id, output]));
  return Object.fromEntries(Object.entries(run.input_refs).map(([port, references]) => [port,
    (Array.isArray(references) ? references : []).map(reference => {
      const outputId = graphObject(reference) && typeof reference.output_id === "string" ? reference.output_id : "";
      const artifact = artifacts.get(outputId);
      return artifact ? { output_id: outputId, availability: "produced", payload: artifact.payload }
        : { output_id: outputId, availability: "unavailable" };
    }),
  ]));
}
export function graphEnumLabel(key: string, definition: Record<string, unknown>, option: unknown) {
  const choices = Array.isArray(definition.enum) ? definition.enum : [];
  const at = choices.indexOf(option);
  const labels = definition["x-enum-labels"] ?? definition.enumNames;
  const label = Array.isArray(labels) ? labels[at] : graphObject(labels) ? labels[String(option)] : undefined;
  if (typeof label === "string") return label;
  if (["scope", "schema_scope"].includes(key) && choices.includes("body") && choices.includes("all"))
    return option === "body" ? "仅文本" : option === "all" ? "全部" : String(option);
  return String(option);
}
