import { graphClone, graphObject, graphUuid } from "./workflowGraph";

export const chatProviderType = "workflow.chat-provider";
export const modelFieldsExtensionId = "workflow.models.node-fields";
export const capacityModelFieldsExtensionId = "workflow.models.node-fields-v2";
export const geminiModelFieldsExtensionId = "workflow.models.node-fields-v3";
export const capacityGeminiModelFieldsExtensionId = "workflow.models.node-fields-v4";
export type ProviderProtocol = "chat" | "gemini";
export type ThinkingLevel = "minimal" | "low" | "medium" | "high";
export type ModelThinking = "disabled"
  | { mode: "budget"; budget: number; include_summary: boolean }
  | { mode: "level"; level: ThinkingLevel; include_summary: boolean };
export interface ProviderIdentity {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
}
export interface CurrentProvider {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
  data_schema_version: 1 | 2; update_sequence: number;
  value: { name: string; protocol: ProviderProtocol; base_url: string;
    credential_ref: null | "env:DEEPSEEK_API_KEY" | "env:GEMINI_API_KEY"; enabled: boolean };
}
export interface ModelParameters {
  model: string; thinking: ModelThinking; stream: false; max_tokens?: number; temperature?: number;
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
  return graphObject(value) && typeof value.include_summary === "boolean"
    && (value.mode === "budget" && keysAre(value, ["mode", "budget", "include_summary"])
      && Number.isSafeInteger(value.budget) && Number(value.budget) >= -1 && Number(value.budget) <= 32768
      || value.mode === "level" && keysAre(value, ["mode", "level", "include_summary"])
        && ["minimal", "low", "medium", "high"].includes(String(value.level)));
}
export function modelParameterDiagnostic(parameters: ModelParameters, protocol: ProviderProtocol): string | null {
  if (protocol === "chat") return parameters.thinking === "disabled" ? null : "Chat 路线不支持此思考配置";
  const capability = geminiThinkingCapability(parameters.model);
  if (!capability) return "此 Gemini 型号尚未登记能力";
  const thinking = parameters.thinking;
  if (thinking === "disabled") return capability.canDisable ? null : "此 Gemini 型号不能关闭思考";
  if (thinking.mode !== capability.mode) return "思考模式与所选 Gemini 型号不匹配";
  if (thinking.mode === "level") return capability.levels?.includes(thinking.level) ? null : "此 Gemini 型号不支持所选思考强度";
  return thinking.budget === -1 || thinking.budget === 0 && capability.canDisable
    || thinking.budget >= capability.budgetMin! && thinking.budget <= capability.budgetMax!
    ? null : "思考预算超出所选 Gemini 型号范围";
}
export interface ModelSourceConfiguration { reference: ProviderIdentity; parameters: ModelParameters }
export interface ModelCapacity {
  context_window_tokens: number; output_reserve_tokens: number;
  summary_max_tokens: number; max_cold_input_tokens: number;
}
export interface CapacityModelSourceConfiguration extends ModelSourceConfiguration { capacity: ModelCapacity }
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
    || value.credential_ref !== null && value.credential_ref !== (value.protocol === "gemini" ? "env:GEMINI_API_KEY" : "env:DEEPSEEK_API_KEY")
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
    && [1, 2].includes(Number(value.data_schema_version)) && Number.isSafeInteger(value.update_sequence)
    && Number(value.update_sequence) > 0 && isProviderValue(value.value)
    && value.data_schema_version === (value.value.protocol === "gemini" ? 2 : 1);
}
export function isModelParameters(value: unknown): value is ModelParameters {
  return graphObject(value) && Object.keys(value).every(key => ["model", "thinking", "stream", "max_tokens", "temperature"].includes(key))
    && typeof value.model === "string" && !!value.model.trim() && value.model.length <= 256
    && !/[\u0000-\u001f\u007f]/.test(value.model)
    && isModelThinking(value.thinking) && value.stream === false
    && (value.max_tokens === undefined || Number.isSafeInteger(value.max_tokens) && Number(value.max_tokens) >= 1 && Number(value.max_tokens) <= 8192)
    && (value.temperature === undefined || typeof value.temperature === "number" && Number.isFinite(value.temperature)
      && value.temperature >= 0 && value.temperature <= 2);
}
export function isModelSourceConfiguration(value: unknown): value is ModelSourceConfiguration {
  return graphObject(value) && keysAre(value, ["reference", "parameters"])
    && isProviderIdentity(value.reference) && isModelParameters(value.parameters);
}
export function isModelCapacity(value: unknown, allowUnconfigured = false): value is ModelCapacity {
  return graphObject(value) && keysAre(value, ["context_window_tokens", "output_reserve_tokens",
    "summary_max_tokens", "max_cold_input_tokens"])
    && Object.values(value).every(item => Number.isSafeInteger(item) && Number(item) >= 0)
    && Number(value.summary_max_tokens) > 0 && Number(value.summary_max_tokens) <= Number(value.output_reserve_tokens)
    && (allowUnconfigured || Number(value.context_window_tokens) > 0 && Number(value.max_cold_input_tokens) > 0)
    && (value.context_window_tokens === 0 && allowUnconfigured
      || Number(value.output_reserve_tokens) < Number(value.context_window_tokens));
}
export function isCapacityModelSourceConfiguration(value: unknown): value is CapacityModelSourceConfiguration {
  return graphObject(value) && keysAre(value, ["reference", "parameters", "capacity"])
    && isProviderIdentity(value.reference) && isModelParameters(value.parameters)
    && Number.isSafeInteger(value.parameters.max_tokens)
    && isModelCapacity(value.capacity, true)
    && value.capacity.output_reserve_tokens >= Number(value.parameters.max_tokens);
}
export function newProvider(): CurrentProvider {
  return { envelope_version: 1, scope: "workspace", type_id: chatProviderType, resource_id: crypto.randomUUID(),
    data_schema_version: 1, update_sequence: 1,
    value: { name: "DeepSeek", protocol: "chat", base_url: "https://api.deepseek.com",
      credential_ref: "env:DEEPSEEK_API_KEY", enabled: true } };
}
export function cloneProvider(value: CurrentProvider) { return graphClone(value); }
