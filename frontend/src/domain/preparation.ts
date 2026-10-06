export type PreparationStage = "A" | "B";
export type PromptRole = "system" | "user" | "assistant";
export type PromptPlacement = "before" | "middle" | "after";
export interface RolePlacement {
  role: PromptRole;
  placement: PromptPlacement;
  depth: number | null;
  order: number;
  enabled: boolean;
}
export type PreparationNodeKind =
  | "prompt-item"
  | "prompt-group"
  | "prompt-collect"
  | "tool"
  | "tool-collect"
  | "context"
  | "assemble"
  | "root-input"
  | "text"
  | "text-to-prompt"
  | "prompt-to-text"
  | "regex"
  | "variable-register"
  | "variable-assign"
  | "variable-replace"
  | "global-content"
  | "session-data-read"
  | "session-data-write"
  | "json-to-text";
export type ProcessingMode = "text" | "prompt";
export type VariableType = "string" | "integer" | "number" | "boolean";
export type VariableValue = string | number | boolean;
export interface RegexNodeConfig {
  mode: ProcessingMode;
  pattern: string;
  replacement: string;
  flags: ("i" | "m" | "s" | "x" | "a")[];
  replaceMode: "first" | "all";
}
export interface VariableRegisterConfig {
  name: string;
  valueType: VariableType;
  hasInitialValue: boolean;
  initialValue: VariableValue | null;
}
export interface VariableAssignConfig {
  name: string;
  valueType: VariableType;
  operation: "set" | "add" | "subtract";
  value: VariableValue;
}
export interface ToolReference {
  name: string;
  version: string;
}
export interface ToolDefinition extends ToolReference {
  description: string;
  parametersSchema: Record<string, unknown>;
}
export interface PromptMember {
  id: string;
  itemId: string;
  revision: number;
  name: string;
  text: string;
  presentation: RolePlacement;
}
export interface PromptItemConfig {
  itemId: string;
  revision: number;
  text: string;
  presentation: RolePlacement;
}
export interface PromptGroupConfig {
  groupId: string;
  revision: number;
  enabled: boolean;
  members: PromptMember[];
}
export interface ToolNodeConfig {
  toolRef: ToolReference;
  revision: number;
  descriptionItemId: string;
  schemaItemId: string;
  descriptionInstanceId: string;
  schemaInstanceId: string;
  description: RolePlacement;
  schema: RolePlacement;
}
export interface CollectorConfig {
  inputs: string[];
}
export interface ContextReference {
  workflowId: string;
  stage: PreparationStage;
  workflowSessionId: string;
  nodeBindingId: string;
  selectedChainId: string | null;
  basis?: {
    sessionRevision: number;
    refRevision: number;
    headCommitId: string | null;
    stateSnapshotId: string | null;
    parentTurnId: string | null;
  };
}
export interface CanonicalContextMessage {
  readonly schema_version: 1 | 2 | 3;
  readonly message_id: string;
  readonly role: PromptRole | "tool";
  readonly source: Readonly<Record<string, unknown>>;
  readonly blocks: readonly Readonly<Record<string, unknown>>[];
  readonly created_at?: string;
}
export interface ContextLocator {
  reference: ContextReference;
  floorId: string;
  floorKind: "root" | "delta";
  messageId: string;
  blockIndex?: number;
  canonicalMessage?: CanonicalContextMessage;
  turnId?: string;
  runId?: string;
  inputId?: string;
  snapshotId?: string;
  chainId?: string;
  workflowSessionId?: string;
}
export interface MaterialSource {
  kind:
    | "prompt"
    | "tool-description"
    | "tool-schema"
    | "context"
    | "current-input";
  nodeId: string;
  itemId?: string;
  revision?: number;
  groupId?: string;
  groupRevision?: number;
  groupInstanceId?: string;
  toolRef?: ToolReference;
  context?: ContextLocator;
  derivation?: { operation: string; sourceInstanceId: string };
}
export interface ProtectedMessage {
  readonly messageId: string;
  readonly role: PromptRole | "tool";
  readonly blocks: readonly Record<string, unknown>[];
  readonly source: Readonly<Record<string, unknown>>;
  readonly canonical?: CanonicalContextMessage;
}
export type PreparedPayload =
  | { kind: "text"; text: string }
  | { readonly kind: "protected-message"; readonly message: ProtectedMessage }
  | { readonly kind: "structured-final"; readonly value: unknown; readonly displayText: string };
export interface PreparedMaterial {
  id: string;
  name: string;
  role: PromptRole | "tool";
  presentation: RolePlacement | null;
  declarationIndex: number;
  payload: PreparedPayload;
  source: MaterialSource;
}
export interface PreparedCollection {
  schemaVersion: 1;
  kind: "preparation-collection";
  source: "local-preview" | "backend-read" | "derived-preview";
  items: PreparedMaterial[];
}
export interface ContextFloor {
  id: string;
  kind: "root" | "delta";
  items: PreparedMaterial[];
}
export interface ContextNodeConfig {
  sourceBindingId?: string;
  reference: ContextReference | null;
  source: "local-preview" | "backend-read";
  floors: ContextFloor[];
}
interface NodeBase<K extends PreparationNodeKind, C> {
  id: string;
  kind: K;
  title: string;
  position: { x: number; y: number };
  config: C;
  publicOutputs?: string[];
}
export type PreparationNode =
  | NodeBase<"prompt-item", PromptItemConfig>
  | NodeBase<"prompt-group", PromptGroupConfig>
  | NodeBase<"prompt-collect", CollectorConfig>
  | NodeBase<"tool", ToolNodeConfig>
  | NodeBase<"tool-collect", CollectorConfig>
  | NodeBase<"context", ContextNodeConfig>
  | NodeBase<"assemble", { targetStage?: PreparationStage }>
  | NodeBase<"root-input", { text: string }>
  | NodeBase<"text", { text: string }>
  | NodeBase<"text-to-prompt", { presentation: RolePlacement }>
  | NodeBase<"prompt-to-text", { separator: string }>
  | NodeBase<"regex", RegexNodeConfig>
  | NodeBase<"variable-register", VariableRegisterConfig>
  | NodeBase<"variable-assign", VariableAssignConfig>
  | NodeBase<"variable-replace", { mode: ProcessingMode }>
  | NodeBase<"global-content", { resourceId: string | null }>
  | NodeBase<"session-data-read", { definition: SessionDataDefinition }>
  | NodeBase<"session-data-write", { definition: SessionDataDefinition; value: unknown }>
  | NodeBase<"json-to-text", Record<string, never>>;
export interface PreparationEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle: string;
  targetHandle: string;
}
export interface PreparationDraft {
  workflowId: string;
  stage: PreparationStage;
  revision: number;
  configId: string;
  nodes: PreparationNode[];
  edges: PreparationEdge[];
}
export interface PreparationDiagnostic {
  code: string;
  message: string;
  severity: "error" | "warning";
  nodeId?: string;
  field?: string;
  edgeId?: string;
}
export interface PreparationPort {
  id: string;
  direction: "input" | "output";
  type: string;
  label: string;
  multiple?: boolean;
  runtimeOnly?: boolean;
}
export interface PreviewMessage {
  id: string;
  role: PromptRole | "tool";
  text: string;
  source: MaterialSource;
  nodeId: string;
  materialId: string;
  floorId?: string;
  protected: boolean;
}
export interface AssemblyPreview {
  source: "local-preview";
  messages: PreviewMessage[];
  materials: PreparedMaterial[];
  toolDescriptions: PreparedMaterial[];
  toolSchemas: PreparedMaterial[];
  context: PreparedCollection;
  diagnostics: PreparationDiagnostic[];
  usage: { messages: number; totalChars: number };
}
export interface PreparationLimits {
  maxNodes: number;
  maxMaterials: number;
  maxMessages: number;
  maxTotalChars: number;
}
export const PREPARATION_LIMITS: PreparationLimits = {
  maxNodes: 128,
  maxMaterials: 512,
  maxMessages: 1024,
  maxTotalChars: 1_000_000,
};
export const PREPARATION_NODE_TITLES: Record<PreparationNodeKind, string> = {
  "prompt-item": "提示词条目",
  "prompt-group": "提示词条目组",
  "prompt-collect": "提示词汇总",
  tool: "工具",
  "tool-collect": "工具汇总",
  context: "上下文",
  assemble: "提示词装配",
  "root-input": "当前输入",
  text: "文本",
  "text-to-prompt": "文本转提示词",
  "prompt-to-text": "提示词转文本",
  regex: "正则",
  "variable-register": "变量注册",
  "variable-assign": "变量赋值",
  "variable-replace": "变量替换",
  "global-content": "全局内容引用",
  "session-data-read": "会话数据读取",
  "session-data-write": "会话数据写入",
  "json-to-text": "JSON 转文本",
};
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export function newPreparationId(): string {
  return crypto.randomUUID();
}
export function clonePreparation<T>(value: T): T {
  if (Array.isArray(value)) return value.map((entry) => clonePreparation(entry)) as T;
  if (value !== null && typeof value === "object")
    return Object.fromEntries(Object.entries(value).map(([key, entry]) =>
      [key, clonePreparation(entry)])) as T;
  return value;
}
export function defaultRolePlacement(
  overrides: Partial<RolePlacement> = {},
): RolePlacement {
  const value: RolePlacement = {
    role: "system", placement: "before", depth: null, order: 0,
    enabled: true, ...overrides,
  };
  if (value.placement !== "middle") value.depth = null;
  return value;
}
function freezeDeep<T>(value: T): T {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freezeDeep(child);
    Object.freeze(value);
  }
  return value;
}
export function freezeContextNodeConfig(config: ContextNodeConfig): ContextNodeConfig {
  return freezeDeep(clonePreparation(config));
}
const INSPECT_TEXT = freezeDeep<ToolDefinition>({
  name: "inspect_text",
  version: "1",
  description: "Count characters and lines in a text.",
  parametersSchema: {
    type: "object",
    properties: { text: { type: "string", description: "Text to inspect" } },
    required: ["text"],
    additionalProperties: false,
  },
});
export function getToolDefinition(ref: ToolReference): ToolDefinition | null {
  if (ref.name !== INSPECT_TEXT.name || ref.version !== INSPECT_TEXT.version)
    return null;
  return freezeDeep(clonePreparation(INSPECT_TEXT));
}
export function preparationPorts(value: PreparationNodeKind | PreparationNode): PreparationPort[] {
  const kind = typeof value === "string" ? value : value.kind;
  const mode = typeof value !== "string"
    && (value.kind === "regex" || value.kind === "variable-replace") ? value.config.mode : "prompt";
  const port = (
    id: string, direction: "input" | "output", label: string,
    multiple = false,
  ): PreparationPort => ({ id, direction, type: id, label, multiple });
  switch (kind) {
    case "prompt-item":
    case "prompt-group":
    case "global-content":
      return [port("prompt", "output", "提示词")];
    case "prompt-collect":
      return [port("prompt", "input", "提示词", true), port("prompt", "output", "提示词集合")];
    case "tool":
      return [port("tool-descriptions", "output", "工具描述"), port("tool-schemas", "output", "参数 schema")];
    case "tool-collect":
      return [
        port("tool-descriptions", "input", "工具描述", true),
        port("tool-schemas", "input", "参数 schema", true),
        port("tool-descriptions", "output", "描述集合"),
        port("tool-schemas", "output", "schema 集合"),
      ];
    case "context":
      return [port("context", "output", "上下文集合")];
    case "session-data-read":
      return [port("json", "output", "会话数据")];
    case "session-data-write":
      return [port("text", "input", "文本值"), port("json", "output", "会话数据")];
    case "json-to-text":
      return [port("json", "input", "JSON"), port("text", "output", "文本")];
    case "root-input":
      return [port("current-input", "output", "当前输入")];
    case "text":
      return [port("text", "output", "文本")];
    case "text-to-prompt":
      return [port("text", "input", "文本"), port("prompt", "output", "提示词")];
    case "prompt-to-text":
      return [port("prompt", "input", "提示词"), port("text", "output", "文本")];
    case "regex":
    case "variable-replace":
      return [port(mode, "input", mode === "text" ? "文本" : "提示词"),
        port(mode, "output", mode === "text" ? "文本" : "提示词")];
    case "variable-register":
    case "variable-assign":
      return [port("text", "input", "文本值"), port("text", "output", "变量文本")];
    case "assemble":
      return [
        port("prompt", "input", "提示词集合"),
        port("tool-descriptions", "input", "描述集合"),
        port("tool-schemas", "input", "schema 集合"),
        port("context", "input", "上下文集合"),
        port("current-input", "input", "当前输入"),
        port("assembled", "output", "装配结果"),
      ];
  }
}
export function createPreparationNode(
  kind: PreparationNodeKind,
  position = { x: 0, y: 0 },
): PreparationNode {
  const base = {
    id: newPreparationId(), kind, title: PREPARATION_NODE_TITLES[kind],
    position: { ...position },
  };
  switch (kind) {
    case "prompt-item":
      return { ...base, kind, config: {
        itemId: newPreparationId(), revision: 1, text: "",
        presentation: defaultRolePlacement(),
      } };
    case "prompt-group":
      return { ...base, kind, config: {
        groupId: newPreparationId(), revision: 1, enabled: true,
        members: [createPromptMember()],
      } };
    case "tool":
      return { ...base, kind, config: {
        toolRef: { name: "inspect_text", version: "1" }, revision: 1,
        descriptionItemId: newPreparationId(), schemaItemId: newPreparationId(),
        descriptionInstanceId: newPreparationId(), schemaInstanceId: newPreparationId(),
        description: defaultRolePlacement({ order: 20 }),
        schema: defaultRolePlacement({ order: 21 }),
      } };
    case "context":
      return { ...base, kind, config: { reference: null, source: "local-preview", floors: [] } };
    case "global-content":
      return { ...base, kind, config: { resourceId: null } };
    case "session-data-read":
      return { ...base, kind, config: { definition: clonePreparation(CORE_SESSION_NOTE) } };
    case "session-data-write":
      return { ...base, kind, config: { definition: clonePreparation(CORE_SESSION_NOTE), value: "" } };
    case "json-to-text":
      return { ...base, kind, config: {} };
    case "assemble":
      return { ...base, kind, config: {} };
    case "root-input":
      return { ...base, kind, config: { text: "" } };
    case "text":
      return { ...base, kind, config: { text: "" } };
    case "text-to-prompt":
      return { ...base, kind, config: { presentation: defaultRolePlacement() } };
    case "prompt-to-text":
      return { ...base, kind, config: { separator: "\n" } };
    case "regex":
      return { ...base, kind, config: {
        mode: "prompt", pattern: "", replacement: "", flags: [], replaceMode: "all",
      } };
    case "variable-register":
      return { ...base, kind, config: {
        name: "", valueType: "string", hasInitialValue: false, initialValue: null,
      } };
    case "variable-assign":
      return { ...base, kind, config: { name: "", valueType: "string", operation: "set", value: "" } };
    case "variable-replace":
      return { ...base, kind, config: { mode: "prompt" } };
    case "prompt-collect":
    case "tool-collect":
      return { ...base, kind, config: { inputs: [] } };
  }
}
export function createPromptMember(): PromptMember {
  return {
    id: newPreparationId(), itemId: newPreparationId(), revision: 1,
    name: "条目", text: "", presentation: defaultRolePlacement(),
  };
}
function diagnostic(
  code: string, message: string, nodeId?: string, field?: string,
): PreparationDiagnostic {
  return { code, message, severity: "error", nodeId, field };
}
export function validateRolePlacement(
  value: RolePlacement, nodeId?: string, field = "presentation",
): PreparationDiagnostic[] {
  const errors: PreparationDiagnostic[] = [];
  if (!["system", "user", "assistant"].includes(value.role))
    errors.push(diagnostic("invalid_role", "role 仅支持 system、user、assistant", nodeId, `${field}.role`));
  if (!["before", "middle", "after"].includes(value.placement))
    errors.push(diagnostic("invalid_placement", "位置配置无效", nodeId, `${field}.placement`));
  if (value.placement === "middle"
    ? !Number.isSafeInteger(value.depth) || (value.depth as number) < 0
    : value.depth !== null)
    errors.push(diagnostic("invalid_depth", "只有上下文中可设置非负整数深度", nodeId, `${field}.depth`));
  if (!Number.isSafeInteger(value.order))
    errors.push(diagnostic("invalid_order", "顺序必须是整数", nodeId, `${field}.order`));
  if (typeof value.enabled !== "boolean")
    errors.push(diagnostic("invalid_enabled", "启用状态无效", nodeId, `${field}.enabled`));
  return errors;
}
function incoming(draft: PreparationDraft, nodeId: string, handle: string) {
  return draft.edges.filter((edge) => edge.target === nodeId && edge.targetHandle === handle);
}
function nodesOf(draft: PreparationDraft, ids: string[]): PreparationNode[] {
  return ids.map((id) => draft.nodes.find((node) => node.id === id))
    .filter((node): node is PreparationNode => !!node);
}
function collectorNodes(draft: PreparationDraft, collector: PreparationNode) {
  if (collector.kind !== "prompt-collect" && collector.kind !== "tool-collect")
    return [];
  return nodesOf(draft, collector.config.inputs);
}
export const VARIABLE_NAME_PATTERN = /^[\p{L}_][\p{L}\p{N}_]*$/u;
export function isVariableValue(value: unknown, type: VariableType): value is VariableValue {
  if (type === "string") return typeof value === "string";
  if (type === "boolean") return typeof value === "boolean";
  if (type === "integer") return Number.isSafeInteger(value);
  return type === "number" && typeof value === "number" && Number.isFinite(value);
}
export function defaultVariableValue(type: VariableType): VariableValue {
  return type === "boolean" ? false : type === "string" ? "" : 0;
}
export function hasIncrementalPreparation(draft: PreparationDraft): boolean {
  return draft.nodes.some((node) => ["text", "text-to-prompt", "prompt-to-text",
    "regex", "variable-register", "variable-assign", "variable-replace", "global-content",
    "session-data-read", "session-data-write", "json-to-text"].includes(node.kind)
    || node.kind === "context" && !!node.config.sourceBindingId || !!node.publicOutputs?.length)
    || draft.edges.some((edge) => {
      const source = draft.nodes.find((node) => node.id === edge.source);
      const target = draft.nodes.find((node) => node.id === edge.target);
      return target?.kind === "prompt-collect" && source?.kind !== "prompt-item" && source?.kind !== "prompt-group"
        || target?.kind === "assemble" && edge.targetHandle === "prompt" && source?.kind !== "prompt-collect"
        || target?.kind === "assemble" && edge.targetHandle === "context" && source?.kind !== "context";
    });
}
function materialPortType(type: string) {
  return type === "context" ? "prompt" : type === "current-input" ? "text" : type;
}
function contextOrigins(draft: PreparationDraft, nodeId: string, visited = new Set<string>()): Set<string> {
  if (visited.has(nodeId)) return new Set();
  visited.add(nodeId);
  const node = draft.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) return new Set();
  if (node.kind === "context") return new Set([nodeId]);
  if (node.kind === "text-to-prompt" || node.kind === "prompt-to-text") return new Set();
  if (node.kind !== "prompt-collect" && node.kind !== "regex" && node.kind !== "variable-replace")
    return new Set();
  return new Set(draft.edges.filter((edge) => edge.target === nodeId)
    .flatMap((edge) => [...contextOrigins(draft, edge.source, new Set(visited))]));
}
function hasOrdinaryPromptOrigin(draft: PreparationDraft, nodeId: string, visited = new Set<string>()): boolean {
  if (visited.has(nodeId)) return false;
  visited.add(nodeId);
  const node = draft.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) return false;
  if (["prompt-item", "prompt-group", "text-to-prompt", "global-content", "tool", "tool-collect"].includes(node.kind))
    return true;
  if (!["prompt-collect", "regex", "variable-replace"].includes(node.kind)) return false;
  return draft.edges.filter((edge) => edge.target === nodeId)
    .some((edge) => hasOrdinaryPromptOrigin(draft, edge.source, new Set(visited)));
}
function topologicalNodes(draft: PreparationDraft): PreparationNode[] | null {
  const result: PreparationNode[] = [];
  const pending = [...draft.nodes];
  const complete = new Set<string>();
  while (pending.length) {
    const index = pending.findIndex((node) => draft.edges.filter((edge) => edge.target === node.id)
      .every((edge) => complete.has(edge.source)));
    if (index < 0) return null;
    const node = pending.splice(index, 1)[0]!;
    complete.add(node.id);
    result.push(node);
  }
  return result;
}
export function validatePreparation(
  draft: PreparationDraft, limits: PreparationLimits = PREPARATION_LIMITS,
): PreparationDiagnostic[] {
  const errors: PreparationDiagnostic[] = [];
  if (draft.nodes.length > limits.maxNodes)
    return [diagnostic("node_limit", "准备节点数量超限")];
  const ids = draft.nodes.map((node) => node.id);
  if (new Set(ids).size !== ids.length)
    errors.push(diagnostic("duplicate_node", "节点身份重复"));
  const assemblies = draft.nodes.filter((node) => node.kind === "assemble");
  if (assemblies.length !== 1)
    errors.push(diagnostic("assembly_count", "准备图需要且仅支持一个提示词装配节点"));
  let configuredChars = 0;
  for (const node of draft.nodes) {
    if (!node.title.trim()) errors.push(diagnostic("empty_name", "节点名称不能为空", node.id, "title"));
    if (!UUID4.test(node.id))
      errors.push(diagnostic("invalid_identity", "节点实例身份不是 UUID4", node.id));
    if (node.kind === "prompt-item") {
      configuredChars += node.config.text.length;
      errors.push(...validateRolePlacement(node.config.presentation, node.id));
      if (!UUID4.test(node.config.itemId) || !Number.isSafeInteger(node.config.revision) || node.config.revision < 1)
        errors.push(diagnostic("invalid_identity", "条目身份或修订无效", node.id));
    } else if (node.kind === "prompt-group") {
      if (node.config.members.length > limits.maxMaterials) {
        errors.push(diagnostic("material_limit", "条目组成员超限", node.id));
        continue;
      }
      const memberIds = node.config.members.map((member) => member.id);
      if (!UUID4.test(node.config.groupId) || !Number.isSafeInteger(node.config.revision)
        || node.config.revision < 1 || typeof node.config.enabled !== "boolean")
        errors.push(diagnostic("invalid_identity", "条目组身份、修订或启用状态无效", node.id));
      if (new Set(memberIds).size !== memberIds.length)
        errors.push(diagnostic("duplicate_member", "组内条目实例身份重复", node.id, "members"));
      for (const member of node.config.members) {
        configuredChars += member.text.length;
        errors.push(...validateRolePlacement(member.presentation, node.id, `members.${member.id}.presentation`));
        if (!member.name.trim() || !UUID4.test(member.id) || !UUID4.test(member.itemId)
          || !Number.isSafeInteger(member.revision) || member.revision < 1)
          errors.push(diagnostic("invalid_member", "组内条目名称或身份无效", node.id, "members"));
      }
    } else if (node.kind === "tool") {
      if (!getToolDefinition(node.config.toolRef))
        errors.push(diagnostic("unknown_tool", "工具身份或确切版本尚未注册", node.id, "toolRef"));
      errors.push(...validateRolePlacement(node.config.description, node.id, "description"));
      errors.push(...validateRolePlacement(node.config.schema, node.id, "schema"));
    } else if (node.kind === "context") {
      if (node.config.floors.length > limits.maxMessages
        || node.config.floors.reduce((sum, floor) => sum + floor.items.length, 0) > limits.maxMaterials) {
        errors.push(diagnostic("material_limit", "上下文楼层或材料数量超限", node.id));
        continue;
      }
      errors.push(...validateContext(draft, node));
      for (const floor of node.config.floors)
        for (const entry of floor.items) configuredChars += payloadText(entry.payload).length;
    } else if (node.kind === "root-input") {
      configuredChars += node.config.text.length;
    } else if (node.kind === "text") {
      configuredChars += node.config.text.length;
    } else if (node.kind === "text-to-prompt") {
      errors.push(...validateRolePlacement(node.config.presentation, node.id));
    } else if (node.kind === "prompt-to-text") {
      configuredChars += node.config.separator.length;
      if (incoming(draft, node.id, "prompt").some((edge) => contextOrigins(draft, edge.source).size > 0))
        errors.push(diagnostic("protected_context_conversion", "受保护上下文不能展平为文本", node.id, "input"));
    } else if (node.kind === "regex") {
      configuredChars += node.config.pattern.length + node.config.replacement.length;
      if (!["text", "prompt"].includes(node.config.mode))
        errors.push(diagnostic("invalid_processing_mode", "正则模式无效", node.id, "mode"));
      if (node.config.pattern.length + node.config.replacement.length > 16_384)
        errors.push(diagnostic("regex_rule_limit", "正则表达式与替换文本总长度超限", node.id, "pattern"));
      if (!["first", "all"].includes(node.config.replaceMode)
        || new Set(node.config.flags).size !== node.config.flags.length
        || node.config.flags.some((flag) => !["i", "m", "s", "x", "a"].includes(flag)))
        errors.push(diagnostic("invalid_regex_rule", "正则替换范围或 flags 无效", node.id, "flags"));
    } else if (node.kind === "variable-register" || node.kind === "variable-assign") {
      const config = node.config;
      if (!VARIABLE_NAME_PATTERN.test(config.name) || config.name.length > 128)
        errors.push(diagnostic("invalid_variable_name", "变量名须以文字或下划线开头，只包含文字、数字和下划线", node.id, "name"));
      if (["workflow_session_id", "node_binding_id"].includes(config.name))
        errors.push(diagnostic("reserved_variable_name", "预置变量只读，不能注册或赋值", node.id, "name"));
      if (!["string", "integer", "number", "boolean"].includes(config.valueType))
        errors.push(diagnostic("invalid_variable_type", "变量类型无效", node.id, "valueType"));
      const textInput = incoming(draft, node.id, "text").length > 0;
      if (textInput && config.valueType !== "string")
        errors.push(diagnostic("variable_text_type", "文本值来源只适用于字符串变量", node.id, "valueType"));
      if (node.kind === "variable-register") {
        if (draft.nodes.some((candidate) => candidate.kind === "variable-register"
          && candidate.id !== node.id && candidate.config.name === config.name))
          errors.push(diagnostic("duplicate_variable", "变量名称重复注册", node.id, "name"));
        if (!textInput && node.config.hasInitialValue && !isVariableValue(node.config.initialValue, config.valueType))
          errors.push(diagnostic("invalid_variable_value", "初始值与声明类型不一致", node.id, "initialValue"));
      } else {
        if (!["set", "add", "subtract"].includes(node.config.operation)
          || node.config.operation !== "set" && !["integer", "number"].includes(config.valueType))
          errors.push(diagnostic("invalid_variable_operation", "加减操作仅适用于数值变量", node.id, "operation"));
        if (!textInput && !isVariableValue(node.config.value, config.valueType))
          errors.push(diagnostic("invalid_variable_value", "赋值与声明类型不一致", node.id, "value"));
        const declaration = draft.nodes.find((candidate) => candidate.kind === "variable-register"
          && candidate.config.name === config.name);
        if (declaration?.kind === "variable-register" && declaration.config.valueType !== config.valueType)
          errors.push(diagnostic("variable_type_mismatch", "赋值类型与注册类型不一致", node.id, "valueType"));
      }
    } else if (node.kind === "variable-replace") {
      if (!["text", "prompt"].includes(node.config.mode))
        errors.push(diagnostic("invalid_processing_mode", "变量替换模式无效", node.id, "mode"));
    }
    if (node.kind === "prompt-collect" || node.kind === "tool-collect") {
      if (new Set(node.config.inputs).size !== node.config.inputs.length)
        errors.push(diagnostic("duplicate_input", "汇总输入实例重复", node.id, "inputs"));
      const requiredHandles = node.kind === "prompt-collect"
        ? ["prompt"] : ["tool-descriptions", "tool-schemas"];
      for (const sourceId of node.config.inputs) {
        const source = draft.nodes.find((candidate) => candidate.id === sourceId);
        const allowed = node.kind === "prompt-collect"
          ? !!source && preparationPorts(source).some((port) =>
            port.direction === "output" && materialPortType(port.type) === "prompt")
          : source?.kind === "tool";
        if (!allowed)
          errors.push(diagnostic("invalid_collector_input", "汇总仅接受对应类型的材料", node.id, "inputs"));
        for (const handle of requiredHandles)
          if (!draft.edges.some((edge) => edge.source === sourceId && edge.target === node.id
            && (edge.sourceHandle === handle || handle === "prompt" && edge.sourceHandle === "context")
            && edge.targetHandle === handle))
            errors.push(diagnostic("missing_input_edge", "声明输入缺少对应连线", node.id, "inputs"));
      }
      for (const edge of draft.edges.filter((candidate) => candidate.target === node.id))
        if (!node.config.inputs.includes(edge.source))
          errors.push({ ...diagnostic("undeclared_input", "连线没有明确输入顺序声明", node.id, "inputs"), edgeId: edge.id });
      const contexts = node.config.inputs.flatMap((id) => [...contextOrigins(draft, id)]);
      if (contexts.length !== new Set(contexts).size)
        errors.push(diagnostic("duplicate_context", "同一上下文不能通过不同路径重复汇总", node.id, "inputs"));
    }
  }
  if (configuredChars > limits.maxTotalChars)
    errors.push(diagnostic("input_limit", "准备输入字符数量超限"));
  if (draft.nodes.reduce((sum, node) => sum + (node.kind === "prompt-group" ? node.config.members.length
    : node.kind === "prompt-item" || node.kind === "text-to-prompt" ? 1 : node.kind === "tool" ? 2 : 0), 0) > limits.maxMaterials)
    errors.push(diagnostic("material_limit", "声明的提示词材料数量超限"));
  const edgeIds = new Set<string>();
  for (const edge of draft.edges) {
    if (edgeIds.has(edge.id)) errors.push({ ...diagnostic("duplicate_edge", "连接身份重复"), edgeId: edge.id });
    edgeIds.add(edge.id);
    const source = draft.nodes.find((node) => node.id === edge.source);
    const target = draft.nodes.find((node) => node.id === edge.target);
    const out = source && preparationPorts(source).find((port) => port.direction === "output" && port.id === edge.sourceHandle);
    const input = target && preparationPorts(target).find((port) => port.direction === "input" && port.id === edge.targetHandle);
    const baseOnly = target?.kind === "assemble"
      ? (edge.targetHandle === "prompt" ? out && materialPortType(out.type) === "prompt"
        : edge.targetHandle.startsWith("tool-") ? source?.kind === "tool-collect"
        : edge.targetHandle === "context" ? source && contextOrigins(draft, source.id).size > 0
        : source?.kind === "root-input")
      : target?.kind === "prompt-collect"
        ? out && materialPortType(out.type) === "prompt"
        : target?.kind === "tool-collect" ? source?.kind === "tool"
        : !!target && ["regex", "variable-replace", "text-to-prompt", "prompt-to-text",
          "variable-register", "variable-assign", "session-data-write", "json-to-text"].includes(target.kind);
    if (!out || !input || materialPortType(out.type) !== materialPortType(input.type)
      || !baseOnly || edge.source === edge.target)
      errors.push({ ...diagnostic("incompatible_port", "连接端口或材料类型不兼容", edge.target), edgeId: edge.id });
    else if (!input.multiple && incoming(draft, edge.target, edge.targetHandle).length > 1)
      errors.push({ ...diagnostic("multiple_input", "该输入仅允许一条连接", edge.target), edgeId: edge.id });
    if (target?.kind === "assemble" && edge.targetHandle === "context" && source
      && hasOrdinaryPromptOrigin(draft, source.id))
      errors.push({ ...diagnostic("mixed_context_port", "混合提示词与上下文材料应连接提示词入口，上下文入口仅接纯上下文", target.id), edgeId: edge.id });
  }
  const assembly = assemblies[0];
  if (assembly) {
    const reachesAssembly = (id: string, visited = new Set<string>()): boolean => {
      if (id === assembly.id) return true;
      const node = draft.nodes.find((candidate) => candidate.id === id);
      if (node?.kind === "variable-register" || node?.kind === "variable-assign"
        || node?.kind === "session-data-write") return true;
      if (visited.has(id)) return false;
      visited.add(id);
      return draft.edges.some((edge) => edge.source === id && reachesAssembly(edge.target, new Set(visited)));
    };
    for (const node of draft.nodes) {
      if (node.id !== assembly.id && !["variable-register", "variable-assign"].includes(node.kind)
        && !reachesAssembly(node.id))
        errors.push(diagnostic("disconnected_node", "节点尚未接入装配链，不能执行时忽略", node.id));
      const required = node.kind === "regex" || node.kind === "variable-replace" ? node.config.mode
        : node.kind === "text-to-prompt" ? "text" : node.kind === "prompt-to-text" ? "prompt"
        : node.kind === "json-to-text" ? "json" : null;
      if (required && incoming(draft, node.id, required).length === 0)
        errors.push(diagnostic("missing_processing_input", "处理节点缺少对应类型输入", node.id, "input"));
    }
    const descriptions = incoming(draft, assembly.id, "tool-descriptions");
    const schemas = incoming(draft, assembly.id, "tool-schemas");
    if (descriptions.length !== schemas.length || descriptions[0]?.source !== schemas[0]?.source)
      errors.push(diagnostic("tool_pair_mismatch", "工具描述和 schema 必须来自同一工具汇总", assembly.id));
    for (const tool of draft.nodes.filter((node) => node.kind === "tool")) {
      const targets = draft.edges.filter((edge) => edge.source === tool.id).map((edge) => edge.target);
      if (new Set(targets).size !== 1 || targets.length !== 2)
        errors.push(diagnostic("tool_pair_mismatch", "一个工具的两部分必须成对接入同一工具汇总", tool.id));
    }
  }
  if (!topologicalNodes(draft))
    errors.push(diagnostic("preparation_cycle", "准备图不能包含循环，一个节点只执行一次"));
  return errors;
}
function validateContext(
  draft: PreparationDraft, node: Extract<PreparationNode, { kind: "context" }>,
): PreparationDiagnostic[] {
  const { reference, source, floors } = node.config;
  const correctSource = (value: ContextReference) => node.config.sourceBindingId
    ? value.nodeBindingId === node.config.sourceBindingId : value.stage === draft.stage;
  if (!floors.length) return reference && (
    reference.workflowId !== draft.workflowId || !correctSource(reference))
    ? [diagnostic("context_scope_mismatch", "上下文引用不属于当前工作流与 Agent", node.id)] : [];
  if (source !== "backend-read" || !reference)
    return [diagnostic("context_unverified", "非空上下文必须来自明确的后端已归档读取", node.id)];
  if (reference.workflowId !== draft.workflowId || !correctSource(reference))
    return [diagnostic("context_scope_mismatch", "上下文不能跨工作流或 A/B 混用", node.id)];
  const errors: PreparationDiagnostic[] = [];
  if (floors.length % 2 !== 0)
    errors.push(diagnostic("invalid_floors", "历史必须是完整 root/delta 楼层对", node.id));
  const identities = new Set<string>();
  const messageLocations = new Map<string, CanonicalContextMessage | undefined>();
  const origins = new Map<string, CanonicalContextMessage | undefined>();
  for (const [index, floor] of floors.entries()) {
    if (floor.kind !== (index % 2 === 0 ? "root" : "delta") || !floor.items.length
      || floor.kind === "root" && floor.items.some((item) => item.role !== "user"
        || item.source.context?.messageId !== floor.items[0].source.context?.messageId))
      errors.push(diagnostic("invalid_floors", "上下文逻辑楼层不完整或顺序无效", node.id));
    for (const item of floor.items) {
      const locator = item.source.context;
      if (identities.has(item.id))
        errors.push(diagnostic("duplicate_context", "上下文材料不能重复累计", node.id));
      identities.add(item.id);
      if (locator?.messageId) {
        const location = `${locator.messageId}:${locator.blockIndex ?? "message"}`;
        if (messageLocations.has(location))
          errors.push(diagnostic("duplicate_context", "上下文消息或 block 定位重复", node.id));
        if (origins.has(locator.messageId) && (
          locator.blockIndex === undefined
          || messageLocations.has(`${locator.messageId}:message`)
          || JSON.stringify(origins.get(locator.messageId)) !== JSON.stringify(locator.canonicalMessage)))
          errors.push(diagnostic("duplicate_context", "上下文 block 不能与整条消息重复或混用不同原消息", node.id));
        origins.set(locator.messageId, locator.canonicalMessage);
        messageLocations.set(location, locator.canonicalMessage);
        if (locator.canonicalMessage && (
          locator.canonicalMessage.message_id !== locator.messageId
          || locator.canonicalMessage.role !== item.role
          || locator.blockIndex !== undefined && (
            !Number.isSafeInteger(locator.blockIndex) || locator.blockIndex < 0
            || locator.canonicalMessage.blocks[locator.blockIndex]?.kind !== "text"
            || item.payload.kind !== "text"
            || item.payload.text !== locator.canonicalMessage.blocks[locator.blockIndex]?.text)))
          errors.push(diagnostic("context_provenance", "上下文 block 定位与完整原消息不一致", node.id));
      }
      if (item.source.kind !== "context" || !locator || locator.floorId !== floor.id
        || locator.floorKind !== floor.kind
        || JSON.stringify(locator.reference) !== JSON.stringify(reference))
        errors.push(diagnostic("context_provenance", "上下文材料缺少匹配的会话与楼层来源", node.id));
      if (item.payload.kind === "protected-message"
        && (item.payload.message.messageId !== locator?.messageId
          || item.payload.message.role !== item.role))
        errors.push(diagnostic("context_provenance", "受保护消息身份或 role 与来源不一致", node.id));
      if (item.role === "tool" && item.payload.kind !== "protected-message")
        errors.push(diagnostic("tool_protocol", "tool 消息必须保留受保护的原生协议结构", node.id));
      if (item.payload.kind === "protected-message") {
        const allowedSources = floor.kind === "root"
          ? ["human", "upstream_node"]
          : ["model", "tool", "protocol_feedback", "runtime_tool_observation"];
        if (!allowedSources.includes(item.payload.message.source.kind as string))
          errors.push(diagnostic("context_provenance", "受保护消息来源不属于对应逻辑楼层", node.id));
      }
    }
    const pendingCalls: string[] = [];
    for (const item of floor.items) {
      if (item.payload.kind !== "protected-message") {
        if (pendingCalls.length)
          errors.push(diagnostic("tool_protocol", "工具调用和结果之间不能插入文本材料", node.id));
        continue;
      }
      const message = item.payload.message;
      if (message.role === "assistant") {
        if (pendingCalls.length)
          errors.push(diagnostic("tool_protocol", "前一组工具结果尚未完整", node.id));
        for (const block of message.blocks)
          if (block.kind === "tool_call") {
            if (typeof block.tool_call_id !== "string" || !block.tool_call_id)
              errors.push(diagnostic("tool_protocol", "工具调用身份无效", node.id));
            else pendingCalls.push(block.tool_call_id);
          }
      } else if (message.role === "tool") {
        for (const block of message.blocks)
          if (block.kind !== "tool_result" || block.tool_call_id !== pendingCalls.shift())
            errors.push(diagnostic("tool_protocol", "工具结果缺失、重复或顺序不匹配", node.id));
      } else if (pendingCalls.length)
        errors.push(diagnostic("tool_protocol", "工具调用和结果之间不能插入其他消息", node.id));
    }
    if (pendingCalls.length)
      errors.push(diagnostic("tool_protocol", "完整增量楼层不能缺少工具结果", node.id));
  }
  return errors;
}
function sourceCollector(draft: PreparationDraft, handle: string) {
  const assembly = draft.nodes.find((node) => node.kind === "assemble");
  if (!assembly) return undefined;
  const edge = incoming(draft, assembly.id, handle)[0];
  return edge && draft.nodes.find((node) => node.id === edge.source);
}
function material(
  id: string, name: string, text: string, presentation: RolePlacement,
  source: MaterialSource, declarationIndex: number,
): PreparedMaterial {
  return {
    id, name, role: presentation.role, presentation: clonePreparation(presentation),
    declarationIndex, payload: { kind: "text", text }, source,
  };
}
export function flattenPreparedItems(draft: PreparationDraft): PreparedMaterial[] {
  if (draft.nodes.length > PREPARATION_LIMITS.maxNodes)
    throw new RangeError("Node limit exceeded");
  const result: PreparedMaterial[] = [];
  let index = 0;
  const prompts = sourceCollector(draft, "prompt");
  if (prompts?.kind === "prompt-collect") {
    const sources = collectorNodes(draft, prompts);
    if (sources.reduce((sum, node) => sum + (
      node.kind === "prompt-group" ? node.config.members.length : 1), 0) > PREPARATION_LIMITS.maxMaterials)
      throw new RangeError("Material limit exceeded");
    for (const node of sources) {
      if (node.kind === "prompt-item") {
        const config = node.config;
        const entry = material(node.id, node.title, config.text, config.presentation,
          { kind: "prompt", nodeId: node.id, itemId: config.itemId, revision: config.revision }, index++);
        if (config.presentation.enabled) result.push(entry);
      } else if (node.kind === "prompt-group") {
        for (const member of node.config.members) {
          const entry = material(`${node.id}:${member.id}`, member.name, member.text, member.presentation, {
            kind: "prompt", nodeId: node.id, itemId: member.itemId, revision: member.revision,
            groupId: node.config.groupId, groupRevision: node.config.revision, groupInstanceId: node.id,
          }, index++);
          if (node.config.enabled && member.presentation.enabled) result.push(entry);
        }
      }
    }
  }
  const tools = sourceCollector(draft, "tool-descriptions");
  if (tools?.kind === "tool-collect") {
    const sources = collectorNodes(draft, tools);
    if (index + sources.length * 2 > PREPARATION_LIMITS.maxMaterials)
      throw new RangeError("Material limit exceeded");
    for (const node of sources) {
      if (node.kind !== "tool") continue;
      const config = node.config;
      const definition = getToolDefinition(config.toolRef);
      if (!definition) continue;
      for (const part of ["description", "schema"] as const) {
        const entry = material(
          part === "description" ? config.descriptionInstanceId : config.schemaInstanceId,
          `${node.title} / ${part}`,
          part === "description" ? definition.description : JSON.stringify(definition.parametersSchema, null, 2),
          config[part], {
            kind: part === "description" ? "tool-description" : "tool-schema",
            nodeId: node.id, itemId: part === "description" ? config.descriptionItemId : config.schemaItemId,
            revision: config.revision, toolRef: clonePreparation(config.toolRef),
          }, index++,
        );
        if (config[part].enabled) result.push(entry);
      }
    }
  }
  return clonePreparation(result);
}
export function getContextCollection(draft: PreparationDraft): PreparedCollection {
  const node = sourceCollector(draft, "context");
  if (node?.kind === "context"
    && (node.config.floors.length > PREPARATION_LIMITS.maxMessages
      || node.config.floors.reduce((sum, floor) => sum + floor.items.length, 0) > PREPARATION_LIMITS.maxMaterials))
    throw new RangeError("Context material limit exceeded");
  const items = node?.kind === "context"
    ? clonePreparation(node.config.floors.flatMap((floor) => floor.items)) : [];
  for (const item of items)
    if (item.payload.kind !== "text") item.payload = freezeDeep(item.payload);
  for (const item of items)
    if (item.source.context?.canonicalMessage)
      freezeDeep(item.source.context.canonicalMessage);
  return {
    schemaVersion: 1, kind: "preparation-collection",
    source: node?.kind === "context" ? node.config.source : "local-preview",
    items,
  };
}
export function getPromptCollection(draft: PreparationDraft): PreparedCollection {
  return {
    schemaVersion: 1, kind: "preparation-collection", source: "local-preview",
    items: flattenPreparedItems(draft),
  };
}
export function transformPreparedCollection(
  collection: PreparedCollection, operation: string,
  transform: (text: string, material: PreparedMaterial) => string,
  limits: PreparationLimits = PREPARATION_LIMITS,
): PreparedCollection {
  if (!operation.trim()) throw new Error("A named transformation is required");
  if (collection.items.length > limits.maxMaterials) throw new Error("Material limit exceeded");
  if (collection.items.reduce((sum, item) => sum + payloadText(item.payload).length, 0) > limits.maxTotalChars)
    throw new Error("Source text limit exceeded");
  const detached = clonePreparation(collection);
  detached.source = "derived-preview";
  let chars = 0;
  detached.items = detached.items.map((item) => {
    if (item.source.context?.canonicalMessage) freezeDeep(item.source.context.canonicalMessage);
    if (item.payload.kind !== "text") {
      chars += payloadText(item.payload).length;
      if (chars > limits.maxTotalChars) throw new Error("Derived text limit exceeded");
      return { ...item, payload: freezeDeep(item.payload) };
    }
    const text = transform(item.payload.text, clonePreparation(item));
    if (typeof text !== "string") throw new Error("Text transformations must return text");
    chars += text.length;
    if (chars > limits.maxTotalChars) throw new Error("Derived text limit exceeded");
    return {
      ...item, id: `${item.id}:derived:${operation}`, payload: { kind: "text", text },
      source: { ...item.source, derivation: { operation, sourceInstanceId: item.id } },
    };
  });
  return detached;
}
function payloadText(payload: PreparedPayload): string {
  if (payload.kind === "text") return payload.text;
  if (payload.kind === "structured-final") return payload.displayText;
  return JSON.stringify(payload.message.blocks);
}
function previewMessage(item: PreparedMaterial, floorId?: string): PreviewMessage {
  return {
    id: item.id, role: item.role, text: payloadText(item.payload),
    source: clonePreparation(item.source), nodeId: item.source.nodeId,
    materialId: item.id, floorId, protected: item.payload.kind !== "text",
  };
}
export function assemblePreparationPreview(
  draft: PreparationDraft, limits: PreparationLimits = PREPARATION_LIMITS,
): AssemblyPreview {
  const diagnostics = validatePreparation(draft, limits);
  const invalid = diagnostics.some((entry) => entry.severity === "error");
  const materials = invalid ? [] : flattenPreparedItems(draft);
  const context: PreparedCollection = invalid
    ? { schemaVersion: 1, kind: "preparation-collection", source: "local-preview", items: [] }
    : getContextCollection(draft);
  const result: AssemblyPreview = {
    source: "local-preview", messages: [], materials,
    toolDescriptions: materials.filter((item) => item.source.kind === "tool-description"),
    toolSchemas: materials.filter((item) => item.source.kind === "tool-schema"),
    context, diagnostics, usage: { messages: 0, totalChars: 0 },
  };
  if (hasIncrementalPreparation(draft)) {
    diagnostics.push({
      code: "backend_preparation_required", message: "增量材料的确定结果由服务端预览提供",
      severity: "warning",
    });
    result.materials = [];
    result.context = { schemaVersion: 1, kind: "preparation-collection", source: "local-preview", items: [] };
    result.toolDescriptions = [];
    result.toolSchemas = [];
    return result;
  }
  if (materials.length + context.items.length > limits.maxMaterials) {
    diagnostics.push(diagnostic("material_limit", "装配材料数量超限"));
    return result;
  }
  if (invalid) return result;
  const contextNode = sourceCollector(draft, "context");
  const floors: { id: string; items: PreparedMaterial[] }[] = contextNode?.kind === "context"
    ? clonePreparation(contextNode.config.floors) : [];
  const root = sourceCollector(draft, "current-input");
  if (root?.kind === "root-input") {
    const item: PreparedMaterial = {
      id: `${root.id}:root`, name: "当前输入", role: "user", presentation: null,
      declarationIndex: -1, payload: { kind: "text", text: root.config.text },
      source: { kind: "current-input", nodeId: root.id },
    };
    floors.push({ id: `${root.id}:floor`, items: [item] });
    if (!root.config.text.trim()) diagnostics.push({
      code: draft.stage === "B" ? "upstream_input_pending" : "empty_current_input",
      message: draft.stage === "B" ? "运行时当前输入由 A 的业务结果提供" : "当前输入尚未填写",
      severity: "warning", nodeId: root.id, field: "text",
    });
  }
  const placed = materials.map((item) => {
    const placement = item.presentation!;
    return {
      item, boundary: placement.placement === "before" ? 0
        : placement.placement === "after" ? floors.length
        : Math.max(0, floors.length - (placement.depth ?? 0)),
      region: { before: 0, middle: 1, after: 2 }[placement.placement],
    };
  }).sort((a, b) => a.boundary - b.boundary || a.region - b.region
    || a.item.presentation!.order - b.item.presentation!.order
    || a.item.declarationIndex - b.item.declarationIndex
    || (a.item.id < b.item.id ? -1 : a.item.id > b.item.id ? 1 : 0));
  let cursor = 0;
  for (const entry of placed) {
    while (cursor < entry.boundary) {
      const floor = floors[cursor++];
      result.messages.push(...floor.items.map((item) => previewMessage(item, floor.id)));
    }
    result.messages.push(previewMessage(entry.item));
  }
  while (cursor < floors.length) {
    const floor = floors[cursor++];
    result.messages.push(...floor.items.map((item) => previewMessage(item, floor.id)));
  }
  result.usage = {
    messages: result.messages.length,
    totalChars: result.messages.reduce((sum, message) => sum + message.text.length, 0),
  };
  if (result.usage.messages > limits.maxMessages || result.usage.totalChars > limits.maxTotalChars) {
    diagnostics.push(diagnostic("assembly_limit", "装配消息或字符数量超限"));
    result.messages = [];
  }
  return result;
}
export interface BackendPromptItem {
  schema_version: 1;
  kind: "item";
  item_id: string;
  revision: number;
  name: string;
  text: string;
  role: PromptRole;
  enabled: boolean;
  placement: PromptPlacement;
  depth: number | null;
  order: number;
  interpolation: "literal";
  source: { kind: "configuration" };
}
export interface BackendPromptGroup {
  schema_version: 1;
  kind: "group";
  group_id: string;
  revision: number;
  name: string;
  members: { item_instance_id: string; item_id: string; revision: number; overrides: Record<string, never> }[];
}
export interface BackendPreparationNode {
  node_id: string;
  kind: "text" | "prompt-source" | "context-source" | "prompt-collector"
    | "text-to-prompt" | "prompt-to-text" | "regex"
    | "variable-register" | "variable-assign" | "variable-replace"
    | "global-source" | "session-data-read" | "session-data-write" | "json-to-text";
  inputs: Record<string, string>;
  config: Record<string, unknown>;
  public_outputs?: string[];
  output_ports?: string[];
}
export interface BackendPreparationProgram {
  schema_version: 1;
  kind: "prompt_preparation_program";
  nodes: BackendPreparationNode[];
  outputs: { prompt: string | null; context: string | null };
}
interface BackendPromptConfigBase {
  kind: "config";
  config_id: string;
  revision: number;
  name: string;
  inputs: (
    | { name: string; kind: "item"; item_instance_id: string; item_id: string; revision: number; overrides: Record<string, never> }
    | { name: string; kind: "group"; group_instance_id: string; group_id: string; revision: number; enabled: boolean; member_overrides: [] }
  )[];
}
export type BackendPromptConfig = BackendPromptConfigBase & (
  | { schema_version: 1 }
  | { schema_version: 2; preparation: BackendPreparationProgram }
);
export interface CompiledPromptItems {
  items: BackendPromptItem[];
  groups: BackendPromptGroup[];
  config: BackendPromptConfig;
  tools: ToolReference[];
  diagnostics: PreparationDiagnostic[];
}
export function compilePromptItems(draft: PreparationDraft): CompiledPromptItems {
  // Observations are never submitted or validated as editable prompt definitions.
  draft = clonePreparation(draft);
  for (const node of draft.nodes)
    if (node.kind === "context")
      node.config = { sourceBindingId: node.config.sourceBindingId, reference: null, source: "local-preview", floors: [] };
  const diagnostics = validatePreparation(draft);
  const output: CompiledPromptItems = {
    items: [], groups: [], tools: [], diagnostics,
    config: {
      schema_version: 1, kind: "config", config_id: draft.configId,
      revision: draft.revision, name: `${draft.stage} preparation`,
      inputs: [],
    },
  };
  if (!UUID4.test(draft.configId) || !Number.isSafeInteger(draft.revision) || draft.revision < 1)
    diagnostics.push(diagnostic("invalid_identity", "配置身份或修订无效"));
  for (const kind of ["context", "root-input"] as const) {
    const nodes = draft.nodes.filter((node) => node.kind === kind);
    const handle = kind === "context" ? "context" : "current-input";
    if (kind === "root-input" && (nodes.length !== 1 || sourceCollector(draft, handle)?.kind !== kind)
      || kind === "context" && !hasIncrementalPreparation(draft) && sourceCollector(draft, handle)?.kind !== kind)
      diagnostics.push(diagnostic("required_runtime_input", "固定运行需要唯一且已连接的上下文与当前输入节点"));
  }
  const liveTools = draft.nodes.filter((node) => node.kind === "tool");
  if (liveTools.length !== 1 || liveTools[0].config.toolRef.name !== "inspect_text"
    || liveTools[0].config.toolRef.version !== "1")
    diagnostics.push(diagnostic("fixed_tool_set", "固定后端仅支持一份 inspect_text@1 工具定义"));
  if (diagnostics.some((entry) => entry.severity === "error")) return output;
  if (!hasIncrementalPreparation(draft)) {
    const preview = assemblePreparationPreview(draft);
    diagnostics.push(...preview.diagnostics.filter((entry) =>
      !diagnostics.some((existing) => existing.code === entry.code && existing.nodeId === entry.nodeId)));
  }
  if (diagnostics.some((entry) => entry.severity === "error")) return output;
  const appendItem = (
    id: string, revision: number, name: string, text: string,
    presentation: RolePlacement, instanceId?: string,
  ) => {
    output.items.push({
      schema_version: 1, kind: "item", item_id: id, revision, name, text,
      ...clonePreparation(presentation), interpolation: "literal",
      source: { kind: "configuration" },
    });
    if (instanceId) output.config.inputs.push({
      name: `input-${output.config.inputs.length}`, kind: "item",
      item_instance_id: instanceId, item_id: id, revision, overrides: {},
    });
  };
  const prompts = sourceCollector(draft, "prompt");
  const promptNodes = hasIncrementalPreparation(draft)
    ? draft.nodes.filter((node) => node.kind === "prompt-item" || node.kind === "prompt-group")
    : prompts?.kind === "prompt-collect" ? collectorNodes(draft, prompts) : [];
  if (promptNodes.length) {
    for (const node of promptNodes) {
      if (node.kind === "prompt-item")
        appendItem(node.config.itemId, node.config.revision, node.title, node.config.text, node.config.presentation, node.id);
      else if (node.kind === "prompt-group") {
        for (const member of node.config.members)
          appendItem(member.itemId, member.revision, member.name, member.text, member.presentation);
        output.groups.push({
          schema_version: 1, kind: "group", group_id: node.config.groupId,
          revision: node.config.revision, name: node.title,
          members: node.config.members.map((member) => ({
            item_instance_id: member.id, item_id: member.itemId,
            revision: member.revision, overrides: {},
          })),
        });
        output.config.inputs.push({
          name: `input-${output.config.inputs.length}`, kind: "group",
          group_instance_id: node.id, group_id: node.config.groupId,
          revision: node.config.revision, enabled: node.config.enabled, member_overrides: [],
        });
      }
    }
  }
  const tools = sourceCollector(draft, "tool-descriptions");
  if (tools?.kind === "tool-collect") {
    for (const node of collectorNodes(draft, tools)) {
      if (node.kind !== "tool") continue;
      const config = node.config;
      const definition = getToolDefinition(config.toolRef)!;
      output.tools.push(clonePreparation(config.toolRef));
      appendItem(config.descriptionItemId, config.revision, `${node.title} / description`,
        definition.description, config.description, config.descriptionInstanceId);
      appendItem(config.schemaItemId, config.revision, `${node.title} / schema`,
        JSON.stringify(definition.parametersSchema, null, 2), config.schema, config.schemaInstanceId);
    }
  }
  const exactItems = new Map<string, BackendPromptItem>();
  for (const item of output.items) {
    const previous = exactItems.get(item.item_id);
    if (previous && JSON.stringify(previous) !== JSON.stringify(item))
      diagnostics.push(diagnostic("conflicting_definition", "同一条目身份包含不同修订或内容，固定发布不可合并"));
    exactItems.set(item.item_id, item);
  }
  output.items = [...exactItems.values()];
  const exactGroups = new Map<string, BackendPromptGroup>();
  for (const group of output.groups) {
    const previous = exactGroups.get(group.group_id);
    if (previous && JSON.stringify(previous) !== JSON.stringify(group))
      diagnostics.push(diagnostic("conflicting_definition", "同一条目组身份包含不同修订或内容，固定发布不可合并"));
    exactGroups.set(group.group_id, group);
  }
  output.groups = [...exactGroups.values()];
  if (hasIncrementalPreparation(draft)) {
    output.config = { ...output.config, schema_version: 2, preparation: compilePreparationProgram(draft) };
  }
  return output;
}
export function compilePreparationProgram(draft: PreparationDraft): BackendPreparationProgram {
  const nodes: BackendPreparationNode[] = [];
  const ordered = topologicalNodes(draft);
  if (!ordered) throw new Error("Preparation graph contains a cycle");
  const byId = new Map(draft.nodes.map((node) => [node.id, node]));
  const program: BackendPreparationProgram = {
    schema_version: 1, kind: "prompt_preparation_program", nodes,
    outputs: { prompt: null, context: null },
  };
  const emit = (node: PreparationNode, kind: BackendPreparationNode["kind"],
    config: Record<string, unknown>, inputs: Record<string, string> = {}) =>
    nodes.push({ node_id: node.id, kind, inputs, config,
      output_ports: preparationPorts(node).filter(port => port.direction === "output").map(port => port.id),
      ...(node.publicOutputs?.length ? { public_outputs: clonePreparation(node.publicOutputs) } : {}) });
  const inputOf = (node: PreparationNode, handle: string) =>
    incoming(draft, node.id, handle)[0]?.source;
  for (const node of ordered) {
    switch (node.kind) {
      case "prompt-item":
        emit(node, "prompt-source", {
          instances: [{ group_instance_id: null, item_instance_id: node.id }],
        });
        break;
      case "prompt-group":
        emit(node, "prompt-source", {
          instances: node.config.members.map((member) => ({
            group_instance_id: node.id, item_instance_id: member.id,
          })),
        });
        break;
      case "context":
        emit(node, "context-source", node.config.sourceBindingId ? { binding_id: node.config.sourceBindingId } : {});
        break;
      case "global-content":
        emit(node, "global-source", { resource_id: node.config.resourceId });
        break;
      case "session-data-read":
      case "session-data-write":
        emit(node, node.kind, { definition: clonePreparation(node.config.definition),
          ...(node.kind === "session-data-write" ? { value: clonePreparation(node.config.value) } : {}) },
          node.kind === "session-data-write" && inputOf(node, "text") ? { text: inputOf(node, "text")! } : {});
        break;
      case "json-to-text":
        emit(node, "json-to-text", {}, { input: inputOf(node, "json")! });
        break;
      case "text":
        emit(node, "text", { text: node.config.text });
        break;
      case "root-input":
        if (node.publicOutputs?.length || draft.edges.some((edge) => edge.source === node.id && byId.get(edge.target)?.kind !== "assemble"))
          emit(node, "text", { source: "root_input" });
        break;
      case "tool":
        emit(node, "prompt-source", {
          instances: [node.config.descriptionInstanceId, node.config.schemaInstanceId].map((id) => ({
            group_instance_id: null, item_instance_id: id,
          })),
        });
        break;
      case "prompt-collect":
      case "tool-collect":
        emit(node, "prompt-collector", {
          input_order: node.config.inputs.map((_, index) => `input-${index}`),
        },
          Object.fromEntries(node.config.inputs.map((id, index) => [`input-${index}`, id])));
        break;
      case "regex":
        emit(node, "regex", {
          mode: node.config.mode,
          rule: {
            pattern: node.config.pattern, replacement: node.config.replacement,
            flags: node.config.flags.join(""), mode: node.config.replaceMode,
          },
        }, { input: inputOf(node, node.config.mode)! });
        break;
      case "variable-replace":
        emit(node, "variable-replace", { mode: node.config.mode },
          { input: inputOf(node, node.config.mode)! });
        break;
      case "variable-register":
        emit(node, "variable-register", {
          name: node.config.name, type: node.config.valueType,
          ...(node.config.hasInitialValue && !inputOf(node, "text") ? { initial: node.config.initialValue } : {}),
        }, inputOf(node, "text") ? { text: inputOf(node, "text")! } : {});
        break;
      case "variable-assign":
        emit(node, "variable-assign", {
          name: node.config.name, operation: node.config.operation,
          ...(!inputOf(node, "text") ? { value: node.config.value } : {}),
        }, inputOf(node, "text") ? { text: inputOf(node, "text")! } : {});
        break;
      case "text-to-prompt": {
        const { role, placement, depth, order, enabled } = node.config.presentation;
        emit(node, "text-to-prompt", { item_instance_id: node.id, role, placement, depth, order, enabled },
          { input: inputOf(node, "text")! });
        break;
      }
      case "prompt-to-text":
        emit(node, "prompt-to-text", { separator: node.config.separator },
          { input: inputOf(node, "prompt")! });
        break;
      case "assemble": {
        const prompt = inputOf(node, "prompt");
        const tools = inputOf(node, "tool-descriptions");
        if (prompt && tools && prompt !== tools) {
          const collectorId = `${node.id}:materials`;
          nodes.push({
            node_id: collectorId, kind: "prompt-collector", config: { input_order: ["prompt", "tools"] },
            inputs: { prompt, tools },
          });
          program.outputs.prompt = collectorId;
        } else program.outputs.prompt = prompt ?? tools ?? null;
        program.outputs.context = inputOf(node, "context") ?? null;
        break;
      }
    }
  }
  // Independent declarations must be visible before consumers; explicit text dependencies remain ordered.
  const declarations = nodes.filter((node) => node.kind === "variable-register" && Object.keys(node.inputs).length === 0);
  program.nodes = [...declarations, ...nodes.filter((node) => !declarations.includes(node))];
  return program;
}
import { CORE_SESSION_NOTE, type SessionDataDefinition } from "./workbenchResources";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
