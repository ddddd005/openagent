import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { isWorkflowIdentity } from "./workflowIdentity";
import type { WorkspaceEdge } from "./workspace";

export interface ChatProvider {
  schema_version: 1;
  kind: "chat_provider";
  provider_id: string;
  revision: number;
  name: string;
  protocol: "chat";
  base_url: string;
  credential_ref: "env:DEEPSEEK_API_KEY" | null;
  enabled: boolean;
}
export interface ModelProviderNode {
  id: string;
  position: { x: number; y: number };
  provider_ref: { provider_id: string; revision: number } | null;
  parameters: { model: string; max_tokens?: number; temperature?: number };
}
export type ModelConfigurationField = "name" | "protocol" | "base_url" | "credential_ref" | "enabled"
  | "model" | "max_tokens" | "temperature";
export interface ModelFieldDiagnostic {
  field: ModelConfigurationField;
  code: string;
  message: string;
}
export const CHAT_CAPABILITIES = Object.freeze({
  protocol: "chat",
  maxTokens: 8192,
  temperature: { minimum: 0, maximum: 2 },
  streaming: false,
  thinking: "disabled",
  nativeTools: true,
} as const);
export const MODEL_DIAGNOSTIC_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  model_dependency_missing: "A/B 模型依赖缺失",
  provider_missing: "确切供应商配置不存在",
  provider_unavailable: "供应商已停用",
  credential_reference_missing: "后端凭据引用缺失或已撤销",
  credential_unavailable: "后端凭据不可用",
  credential_changed: "原执行凭据已变化",
  model_parameters_unsupported: "参数超出当前 Chat 适配器支持范围",
  model_factory_unsupported: "模型适配器不支持确切供应商配置",
  offline_model_unsupported: "离线服务不支持此模型配置",
});
export interface ModelDependency {
  id: string;
  source: string;
  target_binding_id: string;
}
export interface ModelConfiguration {
  schema_version: 1;
  kind: "workflow_model_configuration";
  config_id: string;
  revision: number;
  workflow_id: string;
  nodes: ModelProviderNode[];
  edges: ModelDependency[];
}
export interface ExactModelSelection {
  schema_version: 1;
  kind: "workflow_model_selection";
  config_id: string;
  revision: number;
}
export interface ModelDraft {
  workflowId: string;
  configId: string;
  backendRevision: number;
  nodes: ModelProviderNode[];
  edges: ModelDependency[];
}
export interface ConfigurationMutation {
  path: "/api/model-configurations/provider" | "/api/model-configurations/model";
  body: { record: ChatProvider | ModelConfiguration; expected_revision: number; idempotency_key: string };
}
export interface ModelConfigurationSnapshot {
  schemaVersion: 1;
  drafts: ModelDraft[];
  pending: ConfigurationMutation | null;
}
export const MAX_MODEL_NODES = 16;
export const uuid = (value: unknown): value is string => typeof value === "string"
  && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exact = (value: unknown, fields: string[], optional: string[] = []): value is Record<string, unknown> =>
  object(value) && fields.every((field) => Object.hasOwn(value, field))
  && Object.keys(value).every((field) => [...fields, ...optional].includes(field));
const integer = (value: unknown, minimum = 1) => Number.isSafeInteger(value) && (value as number) >= minimum;
const finite = (value: unknown) => typeof value === "number" && Number.isFinite(value);
const controlCharacters = /[\u0000-\u001f\u007f]/;
function chatAddress(value: unknown): boolean {
  if (typeof value !== "string" || !value || value.length > 2048
    || /[\s\u0000-\u001f\u007f\\?#]/u.test(value) || !/^https?:\/\/[^/]/i.test(value)
    || /^https?:\/\/[^/]*@/i.test(value)) return false;
  try {
    const address = new URL(value);
    return !!address.hostname && !address.username && !address.password && !address.search && !address.hash
      && address.port !== "0"
      && (address.protocol === "https:" || /^http:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/i.test(value));
  } catch { return false; }
}
export function providerFieldDiagnostics(value: Pick<ChatProvider, "name" | "protocol" | "base_url" | "credential_ref" | "enabled">): ModelFieldDiagnostic[] {
  const result: ModelFieldDiagnostic[] = [];
  if (typeof value.name !== "string" || !value.name.trim() || value.name.length > 128 || controlCharacters.test(value.name))
    result.push({ field: "name", code: "provider_name_invalid", message: "名称须为 1–128 个字符，且不含控制字符" });
  if (value.protocol !== "chat")
    result.push({ field: "protocol", code: "provider_protocol_unsupported", message: "当前仅支持 Chat Completions" });
  if (!chatAddress(value.base_url))
    result.push({ field: "base_url", code: "provider_address_invalid", message: "地址须使用 HTTPS；本机允许 HTTP，不含凭据、查询、片段或空白" });
  if (![null, "env:DEEPSEEK_API_KEY"].includes(value.credential_ref))
    result.push({ field: "credential_ref", code: "credential_reference_invalid", message: "仅支持现有后端凭据引用" });
  if (typeof value.enabled !== "boolean")
    result.push({ field: "enabled", code: "provider_enabled_invalid", message: "启用状态须为布尔值" });
  return result;
}
export function modelParameterDiagnostics(parameters: ModelProviderNode["parameters"]): ModelFieldDiagnostic[] {
  const result: ModelFieldDiagnostic[] = [];
  if (typeof parameters.model !== "string" || !parameters.model.trim() || parameters.model.length > 256
    || controlCharacters.test(parameters.model))
    result.push({ field: "model", code: "model_name_invalid", message: "模型名须为 1–256 个字符，且不含控制字符" });
  if (parameters.max_tokens !== undefined
    && (!integer(parameters.max_tokens) || parameters.max_tokens > CHAT_CAPABILITIES.maxTokens))
    result.push({ field: "max_tokens", code: "model_max_tokens_invalid", message: "max_tokens 须为 1–8192 的整数" });
  if (parameters.temperature !== undefined
    && (!finite(parameters.temperature) || parameters.temperature < CHAT_CAPABILITIES.temperature.minimum
      || parameters.temperature > CHAT_CAPABILITIES.temperature.maximum))
    result.push({ field: "temperature", code: "model_temperature_invalid", message: "temperature 须为 0–2 的有限数值" });
  return result;
}
export function isExactModelSelection(value: unknown): value is ExactModelSelection {
  return exact(value, ["schema_version", "kind", "config_id", "revision"])
    && value.schema_version === 1 && value.kind === "workflow_model_selection"
    && uuid(value.config_id) && integer(value.revision);
}
export const cloneModel = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
export function isChatProvider(value: unknown): value is ChatProvider {
  if (!exact(value, ["schema_version", "kind", "provider_id", "revision", "name",
    "protocol", "base_url", "credential_ref", "enabled"])
    || value.schema_version !== 1 || value.kind !== "chat_provider"
    || !uuid(value.provider_id) || !integer(value.revision)
    || typeof value.name !== "string" || !value.name.trim() || value.name.length > 128 || controlCharacters.test(value.name)
    || value.protocol !== "chat" || !chatAddress(value.base_url)
    || ![null, "env:DEEPSEEK_API_KEY"].includes(value.credential_ref as null | string)
    || typeof value.enabled !== "boolean") return false;
  return true;
}
function validNodes(nodes: unknown, executable: boolean): nodes is ModelProviderNode[] {
  return Array.isArray(nodes) && nodes.length <= MAX_MODEL_NODES
    && new Set(nodes.map((node) => object(node) && node.id)).size === nodes.length
    && nodes.every((node) => exact(node, ["id", "position", "provider_ref", "parameters"])
      && uuid(node.id) && exact(node.position, ["x", "y"]) && finite(node.position.x) && finite(node.position.y)
      && (node.provider_ref === null || exact(node.provider_ref, ["provider_id", "revision"])
        && uuid(node.provider_ref.provider_id) && integer(node.provider_ref.revision))
      && exact(node.parameters, ["model"], ["max_tokens", "temperature"])
      && typeof node.parameters.model === "string" && node.parameters.model.length <= 256
      && (!executable || !!node.parameters.model.trim() && !controlCharacters.test(node.parameters.model))
      && (node.parameters.max_tokens === undefined || finite(node.parameters.max_tokens)
        && (!executable || integer(node.parameters.max_tokens)))
      && (node.parameters.temperature === undefined || finite(node.parameters.temperature)
        && (!executable || (node.parameters.temperature as number) >= 0 && (node.parameters.temperature as number) <= 2)));
}
function validEdges(edges: unknown, nodes: ModelProviderNode[]): edges is ModelDependency[] {
  const ids = new Set(nodes.map((node) => node.id));
  return Array.isArray(edges) && edges.length <= 2
    && new Set(edges.map((edge) => object(edge) && edge.id)).size === edges.length
    && new Set(edges.map((edge) => object(edge) && edge.target_binding_id)).size === edges.length
    && edges.every((edge) => exact(edge, ["id", "source", "target_binding_id"])
      && uuid(edge.id) && ids.has(edge.source as string)
      && Object.values(AGENT_BINDINGS).includes(edge.target_binding_id as typeof AGENT_BINDINGS.A));
}
export function isModelConfiguration(value: unknown): value is ModelConfiguration {
  return exact(value, ["schema_version", "kind", "config_id", "revision", "workflow_id", "nodes", "edges"])
    && value.schema_version === 1 && value.kind === "workflow_model_configuration"
    && uuid(value.config_id) && integer(value.revision) && isWorkflowIdentity(value.workflow_id)
    && validNodes(value.nodes, true) && validEdges(value.edges, value.nodes);
}
export function isConfigurationMutation(value: unknown): value is ConfigurationMutation {
  if (!exact(value, ["path", "body"]) || !exact(value.body, ["record", "expected_revision", "idempotency_key"])
    || !integer(value.body.expected_revision, 0) || !uuid(value.body.idempotency_key)
    || new TextEncoder().encode(JSON.stringify(value.body)).byteLength > 64 * 1024) return false;
  const record = value.body.record;
  return (value.path === "/api/model-configurations/provider" ? isChatProvider(record)
    : value.path === "/api/model-configurations/model" && isModelConfiguration(record))
    && (record as ChatProvider | ModelConfiguration).revision === Number(value.body.expected_revision) + 1;
}
export function isModelConfigurationSnapshot(value: unknown): value is ModelConfigurationSnapshot {
  return exact(value, ["schemaVersion", "drafts", "pending"]) && value.schemaVersion === 1
    && Array.isArray(value.drafts) && value.drafts.length <= 128
    && value.drafts.every((draft) => exact(draft, ["workflowId", "configId", "backendRevision", "nodes", "edges"])
      && isWorkflowIdentity(draft.workflowId) && uuid(draft.configId) && integer(draft.backendRevision, 0)
      && validNodes(draft.nodes, false) && validEdges(draft.edges, draft.nodes))
    && (value.pending === null || isConfigurationMutation(value.pending));
}
export function modelPublication(draft: ModelDraft): ModelConfiguration {
  return cloneModel({
    schema_version: 1, kind: "workflow_model_configuration", config_id: draft.configId,
    revision: draft.backendRevision + 1, workflow_id: draft.workflowId, nodes: draft.nodes, edges: draft.edges,
  });
}
export function dependencyTarget(stage: "A" | "B") {
  return `${MAIN_WORKFLOW_ID}:agent-${stage.toLowerCase()}`;
}
export interface CanvasModelConnection {
  id?: string;
  source: string | null;
  target: string | null;
  sourceHandle?: string | null;
  targetHandle?: string | null;
}
export function isCanvasModelConnection(
  draft: ModelDraft | null, primaryEdges: WorkspaceEdge[], connection: CanvasModelConnection,
) {
  // Vue Flow also validates restored edges, not just new pointer connections.
  const primary = primaryEdges.find((edge) => edge.id === connection.id);
  if (primary) return primary.source === connection.source && primary.target === connection.target
    && primary.sourceHandle === connection.sourceHandle && primary.targetHandle === connection.targetHandle;
  const stage = (["A", "B"] as const).find((stage) => dependencyTarget(stage) === connection.target);
  if (!stage || !draft?.nodes.some((node) => node.id === connection.source)
    || connection.sourceHandle !== "model-out" || connection.targetHandle !== "model-in") return false;
  const existing = draft.edges.find((edge) => edge.target_binding_id === AGENT_BINDINGS[stage]);
  return !existing || existing.id === connection.id && existing.source === connection.source;
}
export function modelSignature(value: ChatProvider | ModelConfiguration): string {
  return JSON.stringify(value, (_key, child: unknown) => {
    if (child && typeof child === "object" && !Array.isArray(child))
      return Object.fromEntries(Object.entries(child).sort(([a], [b]) => a.localeCompare(b)));
    return child;
  });
}
