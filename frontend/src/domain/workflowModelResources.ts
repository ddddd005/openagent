import { graphClone, graphObject, graphUuid } from "./workflowGraph";

export const chatProviderType = "workflow.chat-provider";
export const modelProfileType = "workflow.model-profile";
export const modelFieldsExtensionId = "workflow.models.node-fields-v7";
export type ProviderProtocol = "chat" | "gemini";
export type CredentialReference = `local:${string}`;
export type ThinkingLevel = "minimal" | "low" | "medium" | "high";
export type ModelThinking = "disabled"
  | { mode: "budget"; budget: number; include_summary: true }
  | { mode: "level"; level: ThinkingLevel; include_summary: true };
export interface ProviderIdentity {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
}
export interface CurrentProvider {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
  data_schema_version: 3; update_sequence: number;
  value: { name: string; protocol: ProviderProtocol; base_url: string;
    credential_ref: null | CredentialReference; enabled: boolean };
}
export interface GeminiThinkingCapability {
  mode: "budget" | "level"; canDisable: boolean; budgetMin?: number; budgetMax?: number; levels?: ThinkingLevel[];
}
const geminiThinkingCapabilities: Record<string, GeminiThinkingCapability> = {
  "gemini-2.5-pro": { mode: "budget", canDisable: false, budgetMin: 128, budgetMax: 32768 },
  "gemini-2.5-flash": { mode: "budget", canDisable: true, budgetMin: 0, budgetMax: 24576 },
  "gemini-2.5-flash-lite": { mode: "budget", canDisable: true, budgetMin: 512, budgetMax: 24576 },
  "gemini-3-flash-preview": { mode: "level", canDisable: false, levels: ["minimal", "low", "medium", "high"] },
  "gemini-3.1-pro-preview": { mode: "level", canDisable: false, levels: ["low", "medium", "high"] },
};
export function geminiThinkingCapability(model: string): GeminiThinkingCapability | null {
  return Object.hasOwn(geminiThinkingCapabilities, model) ? graphClone(geminiThinkingCapabilities[model]!) : null;
}
export function isModelThinking(value: unknown): value is ModelThinking {
  if (value === "disabled") return true;
  return graphObject(value) && value.include_summary === true
    && (value.mode === "budget" && keysAre(value, ["mode", "budget", "include_summary"])
      && Number.isSafeInteger(value.budget) && Number(value.budget) >= -1
      || value.mode === "level" && keysAre(value, ["mode", "level", "include_summary"])
        && ["minimal", "low", "medium", "high"].includes(String(value.level)));
}
export function modelParameterDiagnostic(model: string, thinking: ModelThinking, protocol: ProviderProtocol): string | null {
  if (protocol === "chat") return thinking === "disabled" ? null : "Chat 路线不支持此思考配置";
  const capability = geminiThinkingCapability(model);
  if (!capability) return "此 Gemini 型号尚未登记能力";
  if (thinking === "disabled") return capability.canDisable ? null : "此 Gemini 型号不能关闭思考";
  if (thinking.mode !== capability.mode) return "思考模式与所选 Gemini 型号不匹配";
  if (thinking.mode === "level") return capability.levels?.includes(thinking.level) ? null : "此 Gemini 型号不支持所选思考强度";
  return thinking.budget === -1 || thinking.budget === 0 && capability.canDisable
    || thinking.budget >= capability.budgetMin! && thinking.budget <= capability.budgetMax!
    ? null : "思考预算超出所选 Gemini 型号范围";
}
export interface ModelProfileIdentity {
  envelope_version: 1; scope: string; type_id: typeof modelProfileType; resource_id: string;
}
export interface ModelSourceConfiguration { reference: ModelProfileIdentity }
export interface ModelCapacity {
  context_window_tokens: number; input_limit_tokens: number; max_output_tokens: number;
}
export interface ModelRuntime {
  thinking: ModelThinking;
  context_length_tokens: number | null;
  compaction_threshold_bps: number;
  output_limit_tokens: number;
}
export interface ModelDescription {
  id: string;
  capacity: ModelCapacity;
}
export interface CurrentModelProfile extends ModelProfileIdentity {
  data_schema_version: 2; update_sequence: number;
  value: { name: string; provider: ProviderIdentity; model: ModelDescription;
    runtime: ModelRuntime; enabled: boolean };
}
// Read-only request verification for the schema-1 envelope; never used by list or save.
export interface LegacyModelProfileRecord extends ModelProfileIdentity {
  data_schema_version: 1; update_sequence: number;
  value: { name: string; protocol: ProviderProtocol; provider: ProviderIdentity;
    parameters: { model: string; thinking: "disabled"
      | { mode: "budget"; budget: number; include_summary: boolean }
      | { mode: "level"; level: ThinkingLevel; include_summary: boolean };
      stream: false; max_tokens?: number; temperature?: number };
    capacity: { context_window_tokens: number; output_reserve_tokens: number;
      summary_max_tokens: number; max_cold_input_tokens: number };
    enabled: boolean };
}
const keysAre = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).length === keys.length && keys.every(key => key in value);
export function isProviderIdentity(value: unknown): value is ProviderIdentity {
  return graphObject(value) && keysAre(value, ["envelope_version", "scope", "type_id", "resource_id"])
    && value.envelope_version === 1 && typeof value.scope === "string" && !!value.scope.trim()
    && value.scope.length <= 128 && value.type_id === chatProviderType && graphUuid(value.resource_id);
}
export function providerIdentity(value: CurrentProvider): ProviderIdentity {
  return { envelope_version: 1, scope: value.scope, type_id: chatProviderType, resource_id: value.resource_id };
}
export function sameProviderIdentity(left: ProviderIdentity, right: ProviderIdentity) {
  return left.envelope_version === right.envelope_version && left.scope === right.scope
    && left.type_id === right.type_id && left.resource_id === right.resource_id;
}
export function isProviderValue(value: unknown): value is CurrentProvider["value"] {
  if (!graphObject(value) || !keysAre(value, ["name", "protocol", "base_url", "credential_ref", "enabled"])
    || typeof value.name !== "string" || !value.name.trim() || value.name.length > 128 || /[\u0000-\u001f\u007f]/.test(value.name)
    || !["chat", "gemini"].includes(String(value.protocol)) || typeof value.enabled !== "boolean"
    || value.credential_ref !== null
      && !(typeof value.credential_ref === "string" && value.credential_ref.startsWith("local:") && graphUuid(value.credential_ref.slice(6)))
    || typeof value.base_url !== "string" || value.base_url.length > 2048 || /[\s\\?#\u0000-\u001f\u007f]/.test(value.base_url)) return false;
  try {
    const url = new URL(value.base_url);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash
      && (url.protocol === "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname));
  } catch { return false; }
}
export function isCurrentProvider(value: unknown): value is CurrentProvider {
  return graphObject(value) && keysAre(value, ["envelope_version", "scope", "type_id", "resource_id",
    "data_schema_version", "update_sequence", "value"])
    && isProviderIdentity(Object.fromEntries(["envelope_version", "scope", "type_id", "resource_id"].map(key => [key, value[key]])))
    && value.data_schema_version === 3 && Number.isSafeInteger(value.update_sequence)
    && Number(value.update_sequence) > 0 && isProviderValue(value.value);
}
export function isModelSourceConfiguration(value: unknown): value is ModelSourceConfiguration {
  return graphObject(value) && keysAre(value, ["reference"]) && isModelProfileIdentity(value.reference);
}
export function isModelCapacity(value: unknown, allowUnconfigured = false): value is ModelCapacity {
  return graphObject(value) && keysAre(value, ["context_window_tokens", "input_limit_tokens", "max_output_tokens"])
    && Object.values(value).every(item => Number.isSafeInteger(item)
      && Number(item) >= (allowUnconfigured ? 0 : 1));
}
export function isModelRuntime(value: unknown): value is ModelRuntime {
  return graphObject(value) && keysAre(value, ["thinking", "context_length_tokens",
    "compaction_threshold_bps", "output_limit_tokens"])
    && isModelThinking(value.thinking)
    && (value.context_length_tokens === null
      || Number.isSafeInteger(value.context_length_tokens) && Number(value.context_length_tokens) >= 1)
    && Number.isSafeInteger(value.compaction_threshold_bps)
    && Number(value.compaction_threshold_bps) >= 1 && Number(value.compaction_threshold_bps) <= 10_000
    && Number.isSafeInteger(value.output_limit_tokens) && Number(value.output_limit_tokens) >= 1;
}
export function isModelDescription(value: unknown, allowUnconfiguredCapacity = false): value is ModelDescription {
  return graphObject(value) && keysAre(value, ["id", "capacity"])
    && typeof value.id === "string" && value.id.length <= 256
    && !/[\u0000-\u001f\u007f]/.test(value.id)
    && isModelCapacity(value.capacity, allowUnconfiguredCapacity);
}
export function isModelProfileIdentity(value: unknown): value is ModelProfileIdentity {
  return graphObject(value) && keysAre(value, ["envelope_version", "scope", "type_id", "resource_id"])
    && value.type_id === modelProfileType && isProviderIdentity({ ...value, type_id: chatProviderType });
}
export function modelProfileIdentity(value: Pick<ModelProfileIdentity, "scope" | "resource_id">): ModelProfileIdentity {
  return { envelope_version: 1, scope: value.scope, type_id: modelProfileType, resource_id: value.resource_id };
}
export function sameModelProfileIdentity(left: ModelProfileIdentity, right: ModelProfileIdentity) {
  return left.envelope_version === right.envelope_version && left.scope === right.scope
    && left.type_id === right.type_id && left.resource_id === right.resource_id;
}
export function modelProfileDiagnostic(value: CurrentModelProfile["value"]): string | null {
  if (!value.name.trim()) return "请填写配置名称";
  if (!isProviderIdentity(value.provider)) return "请选择已保存的供应商";
  if (!isModelDescription(value.model)) return "请填写模型名称及真实上下文、输入和输出容量";
  if (!value.model.id.trim()) return "请填写模型名称";
  if (!isModelRuntime(value.runtime)) return "请填写有效的思考、上下文、压缩阈值和输出设置";
  const contextLength = value.runtime.context_length_tokens ?? value.model.capacity.context_window_tokens;
  if (contextLength > value.model.capacity.context_window_tokens)
    return "上下文长度超过真实模型窗口";
  if (value.runtime.output_limit_tokens > value.model.capacity.max_output_tokens
    || value.runtime.output_limit_tokens >= value.model.capacity.context_window_tokens)
    return "输出上限超过真实模型容量";
  return null;
}
export function isCurrentModelProfile(value: unknown): value is CurrentModelProfile {
  if (!graphObject(value) || !keysAre(value, ["envelope_version", "scope", "type_id", "resource_id",
    "data_schema_version", "update_sequence", "value"]) || value.data_schema_version !== 2
    || !Number.isSafeInteger(value.update_sequence) || Number(value.update_sequence) < 1
    || !isModelProfileIdentity(Object.fromEntries(["envelope_version", "scope", "type_id", "resource_id"]
      .map(key => [key, value[key]]))) || !graphObject(value.value)
    || !keysAre(value.value, ["name", "provider", "model", "runtime", "enabled"])
    || typeof value.value.name !== "string" || value.value.name.length > 128
    || /[\u0000-\u001f\u007f]/.test(value.value.name)
    || typeof value.value.enabled !== "boolean"
    || !isProviderIdentity(value.value.provider) || !isModelDescription(value.value.model)
    || !isModelRuntime(value.value.runtime)) return false;
  return modelProfileDiagnostic(value.value as unknown as CurrentModelProfile["value"]) === null;
}
function isLegacyModelThinking(value: unknown): value is LegacyModelProfileRecord["value"]["parameters"]["thinking"] {
  if (value === "disabled") return true;
  return graphObject(value) && typeof value.include_summary === "boolean"
    && (value.mode === "budget" && keysAre(value, ["mode", "budget", "include_summary"])
      && Number.isSafeInteger(value.budget) && Number(value.budget) >= -1 && Number(value.budget) <= 32768
      || value.mode === "level" && keysAre(value, ["mode", "level", "include_summary"])
        && ["minimal", "low", "medium", "high"].includes(String(value.level)));
}
function isLegacyModelParameters(value: unknown): value is LegacyModelProfileRecord["value"]["parameters"] {
  return graphObject(value) && Object.keys(value).every(key =>
    ["model", "thinking", "stream", "max_tokens", "temperature"].includes(key))
    && typeof value.model === "string" && !!value.model.trim() && value.model.length <= 256
    && !/[\u0000-\u001f\u007f]/.test(value.model)
    && isLegacyModelThinking(value.thinking) && value.stream === false
    && (value.max_tokens === undefined || Number.isSafeInteger(value.max_tokens)
      && Number(value.max_tokens) >= 1 && Number(value.max_tokens) <= 8192)
    && (value.temperature === undefined || typeof value.temperature === "number"
      && Number.isFinite(value.temperature) && value.temperature >= 0 && value.temperature <= 2);
}
function isLegacyModelCapacity(value: unknown): value is LegacyModelProfileRecord["value"]["capacity"] {
  return graphObject(value) && keysAre(value, ["context_window_tokens", "output_reserve_tokens",
    "summary_max_tokens", "max_cold_input_tokens"])
    && Object.values(value).every(item => Number.isSafeInteger(item) && Number(item) >= 0)
    && Number(value.summary_max_tokens) > 0 && Number(value.summary_max_tokens) <= Number(value.output_reserve_tokens)
    && Number(value.context_window_tokens) > 0 && Number(value.max_cold_input_tokens) > 0
    && Number(value.output_reserve_tokens) < Number(value.context_window_tokens);
}
function legacyModelThinkingDiagnostic(parameters: LegacyModelProfileRecord["value"]["parameters"],
  protocol: ProviderProtocol): string | null {
  if (protocol === "chat") return parameters.thinking === "disabled" ? null : "Chat route does not support thinking";
  const capability = geminiThinkingCapability(parameters.model);
  if (!capability) return "Gemini model has no installed capability";
  const thinking = parameters.thinking;
  if (thinking === "disabled") return capability.canDisable ? null : "Gemini model cannot disable thinking";
  if (thinking.mode !== capability.mode) return "Thinking mode differs from Gemini capability";
  if (thinking.mode === "level") return capability.levels?.includes(thinking.level) ? null : "Unsupported thinking level";
  return thinking.budget === -1 || thinking.budget === 0 && capability.canDisable
    || thinking.budget >= capability.budgetMin! && thinking.budget <= capability.budgetMax!
    ? null : "Thinking budget is outside the Gemini model range";
}
export function isLegacyModelProfileRecord(value: unknown): value is LegacyModelProfileRecord {
  if (!graphObject(value) || !keysAre(value, ["envelope_version", "scope", "type_id", "resource_id",
    "data_schema_version", "update_sequence", "value"]) || value.data_schema_version !== 1
    || !Number.isSafeInteger(value.update_sequence) || Number(value.update_sequence) < 1
    || !isModelProfileIdentity(Object.fromEntries(["envelope_version", "scope", "type_id", "resource_id"]
      .map(key => [key, value[key]]))) || !graphObject(value.value)
    || !keysAre(value.value, ["name", "protocol", "provider", "parameters", "capacity", "enabled"])
    || typeof value.value.name !== "string" || !value.value.name.trim() || value.value.name.length > 128
    || /[\u0000-\u001f\u007f]/.test(value.value.name)
    || !["chat", "gemini"].includes(String(value.value.protocol)) || typeof value.value.enabled !== "boolean"
    || !isProviderIdentity(value.value.provider) || !isLegacyModelParameters(value.value.parameters)
    || !isLegacyModelCapacity(value.value.capacity)) return false;
  return Number.isSafeInteger(value.value.parameters.max_tokens)
    && legacyModelThinkingDiagnostic(value.value.parameters, value.value.protocol as ProviderProtocol) === null
    && value.value.capacity.output_reserve_tokens >= Number(value.value.parameters.max_tokens)
    && value.value.capacity.summary_max_tokens <= 8192;
}
export function newModelProfile(provider: CurrentProvider): CurrentModelProfile {
  return { envelope_version: 1, scope: "workspace", type_id: modelProfileType, resource_id: crypto.randomUUID(),
    data_schema_version: 2, update_sequence: 1,
    value: { name: "", provider: providerIdentity(provider),
      model: { id: "", capacity: { context_window_tokens: 0, input_limit_tokens: 0, max_output_tokens: 0 } },
      runtime: { thinking: "disabled", context_length_tokens: 300_000,
        compaction_threshold_bps: 8500, output_limit_tokens: 32_000 }, enabled: true } };
}
export function newProvider(): CurrentProvider {
  return { envelope_version: 1, scope: "workspace", type_id: chatProviderType, resource_id: crypto.randomUUID(),
    data_schema_version: 3, update_sequence: 1,
    value: { name: "DeepSeek", protocol: "chat", base_url: "https://api.deepseek.com",
      credential_ref: null, enabled: true } };
}
export function cloneProvider(value: CurrentProvider) { return graphClone(value); }
