import { graphClone, graphObject, graphUuid } from "./workflowGraph";

export const chatProviderType = "workflow.chat-provider";
export const modelFieldsExtensionId = "workflow.models.node-fields";
export const capacityModelFieldsExtensionId = "workflow.models.node-fields-v2";
export interface ProviderIdentity {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
}
export interface CurrentProvider {
  envelope_version: 1; scope: string; type_id: typeof chatProviderType; resource_id: string;
  data_schema_version: 1; update_sequence: number;
  value: { name: string; protocol: "chat"; base_url: string; credential_ref: null | "env:DEEPSEEK_API_KEY"; enabled: boolean };
}
export interface ModelParameters {
  model: string; thinking: "disabled"; stream: false; max_tokens?: number; temperature?: number;
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
    || value.protocol !== "chat" || typeof value.enabled !== "boolean"
    || value.credential_ref !== null && value.credential_ref !== "env:DEEPSEEK_API_KEY"
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
    && value.data_schema_version === 1 && Number.isSafeInteger(value.update_sequence)
    && Number(value.update_sequence) > 0 && isProviderValue(value.value);
}
export function isModelParameters(value: unknown): value is ModelParameters {
  return graphObject(value) && Object.keys(value).every(key => ["model", "thinking", "stream", "max_tokens", "temperature"].includes(key))
    && typeof value.model === "string" && !!value.model.trim()
    && value.thinking === "disabled" && value.stream === false
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
