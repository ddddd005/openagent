import { workbenchRequest, WorkbenchApiError } from "./workbenchApi";
import { graphApplicationQuery } from "./workflowApplicationApi";
import { graphJsonSignature, graphObject, graphUuid } from "../domain/workflowGraph";
import { isProviderIdentity, sameProviderIdentity, type ProviderIdentity, type ProviderProtocol } from "../domain/workflowModelResources";

export interface CredentialRequest { credential_ref: `local:${string}`; expected_generation: number; idempotency_key: string }
export interface CredentialStatus {
  schema_version: 1; kind: "workflow.credential-status"; credential_ref: string;
  generation: number; state: "saved" | "missing";
}
export interface ProviderCredentialRequest extends CredentialRequest {
  identity: ProviderIdentity; expected_sequence: number;
  provider_seed: null | { name: string; protocol: ProviderProtocol; base_url: string; enabled: boolean };
}
export interface ProviderCredentialResult {
  schema_version: 1; kind: "workflow.provider-credential-save"; outcome: "saved" | "partial" | "unresolved";
  reason_code: string; credential: CredentialStatus | null;
  provider: { reference: ProviderIdentity; update_sequence: number; deleted: false } | null;
}
export interface DiscoveredModel {
  model: string; adapted: boolean; input_token_limit: number | null;
  output_token_limit: number | null; context_window_tokens: number | null;
}
function status(value: unknown, reference: string): CredentialStatus {
  if (!graphObject(value) || Object.keys(value).length !== 5 || value.schema_version !== 1
    || value.kind !== "workflow.credential-status" || value.credential_ref !== reference
    || !Number.isSafeInteger(value.generation) || Number(value.generation) < 0
    || !["saved", "missing"].includes(String(value.state)))
    throw new WorkbenchApiError("unknown", "凭据状态无法安全核实");
  return value as unknown as CredentialStatus;
}
export function isCredentialRequest(value: unknown): value is CredentialRequest {
  return graphObject(value) && Object.keys(value).length === 3 && typeof value.credential_ref === "string"
    && value.credential_ref.startsWith("local:") && graphUuid(value.credential_ref.slice(6))
    && Number.isSafeInteger(value.expected_generation) && Number(value.expected_generation) >= 0
    && Number(value.expected_generation) < Number.MAX_SAFE_INTEGER && graphUuid(value.idempotency_key);
}
export function isProviderCredentialRequest(value: unknown): value is ProviderCredentialRequest {
  if (!graphObject(value) || Object.keys(value).length !== 6 || !isProviderIdentity(value.identity)
    || !Number.isSafeInteger(value.expected_sequence) || Number(value.expected_sequence) < 0
    || !isCredentialRequest(Object.fromEntries(["credential_ref", "expected_generation", "idempotency_key"].map(key => [key, value[key]]))))
    return false;
  if (value.provider_seed === null) return Number(value.expected_sequence) > 0;
  return graphObject(value.provider_seed) && Object.keys(value.provider_seed).length === 4
    && typeof value.provider_seed.name === "string" && !!value.provider_seed.name.trim()
    && ["chat", "gemini"].includes(String(value.provider_seed.protocol))
    && typeof value.provider_seed.base_url === "string" && typeof value.provider_seed.enabled === "boolean";
}
function bindingResult(value: unknown, request: ProviderCredentialRequest,
  bind = false): ProviderCredentialResult {
  if (!graphObject(value) || Object.keys(value).length !== 6 || value.schema_version !== 1
    || value.kind !== "workflow.provider-credential-save" || !["saved", "partial", "unresolved"].includes(String(value.outcome))
    || typeof value.reason_code !== "string" || !value.reason_code)
    throw new WorkbenchApiError("unknown", "Key 与供应商绑定结果无法核实，保留原请求");
  if (value.credential !== null) {
    const credential = status(value.credential, request.credential_ref);
    if (value.outcome === "saved" && (credential.generation !== request.expected_generation + (bind ? 0 : 1) || credential.state !== "saved"))
      throw new WorkbenchApiError("unknown", "Key 回执代次不匹配，保留原请求");
  }
  if (value.provider !== null && (!graphObject(value.provider) || Object.keys(value.provider).length !== 3
    || !isProviderIdentity(value.provider.reference) || !sameProviderIdentity(value.provider.reference, request.identity)
    || value.provider.update_sequence !== request.expected_sequence + 1 || value.provider.deleted !== false))
    throw new WorkbenchApiError("unknown", "供应商绑定回执不匹配，保留原请求");
  if (value.outcome === "saved" && (!value.credential || !value.provider))
    throw new WorkbenchApiError("unknown", "Key 或供应商绑定缺少回执，保留原请求");
  return value as unknown as ProviderCredentialResult;
}
function checkEvidenceDigest(digest: string) {
  if (!/^[0-9a-f]{64}$/.test(digest))
    throw new WorkbenchApiError("unknown", "缺少准确原 Key 请求证据，Key 未发送");
}
async function boundCommand(request: ProviderCredentialRequest, secret: string, requestDigest: string) {
  checkEvidenceDigest(requestDigest);
  const operation = "provider.credential.save";
  const value = await workbenchRequest<unknown>("/api/graph/commands", {
    body: { operation, parameters: { ...request, secret } },
  });
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  if (!graphObject(value) || Object.keys(value).length !== 4 || value.schema_version !== 1
    || value.kind !== "workflow.application-command" || !receipt || receipt.operation !== operation
    || Object.keys(receipt).length !== 9 || receipt.schema_version !== 1
    || receipt.kind !== "workflow.application-receipt"
    || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 0
    || receipt.operation_scope !== "management" || receipt.idempotency_key !== request.idempotency_key
    || receipt.authority !== "service_receipt" || receipt.request_sha256 !== requestDigest)
    throw new WorkbenchApiError("unknown", "Key 生效回执不匹配，只能核实原请求");
  const accepted = bindingResult(receipt.accepted, request);
  const result = bindingResult(value.result, request);
  if (graphJsonSignature(accepted) !== graphJsonSignature(result))
    throw new WorkbenchApiError("unknown", "Key 生效回执与返回结果不一致");
  return result;
}
export const providerConfigurationPorts = {
  async protection() {
    const value = await graphApplicationQuery("credential.protection");
    if (!graphObject(value) || Object.keys(value).length !== 4 || value.schema_version !== 1
      || value.kind !== "workflow.credential-protection" || typeof value.available !== "boolean" || value.mode !== "current-user")
      throw new WorkbenchApiError("unavailable", "系统凭据保护不可用");
    return value.available;
  },
  async status(reference: string) { return status(await graphApplicationQuery("credential.status", { credential_ref: reference }), reference); },
  async delete(request: CredentialRequest, requestDigest: string) {
    checkEvidenceDigest(requestDigest);
    const operation = "credential.delete";
    const value = await workbenchRequest<unknown>("/api/graph/commands",
      { body: { operation, parameters: { ...request } } });
    const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
    if (!graphObject(value) || Object.keys(value).length !== 4
      || value.schema_version !== 1 || value.kind !== "workflow.application-command"
      || !receipt || Object.keys(receipt).length !== 9 || receipt.schema_version !== 1
      || receipt.kind !== "workflow.application-receipt"
      || receipt.operation !== operation || receipt.operation_scope !== "management"
      || receipt.idempotency_key !== request.idempotency_key || receipt.authority !== "service_receipt"
      || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 0
      || receipt.request_sha256 !== requestDigest)
      throw new WorkbenchApiError("unknown", "凭据删除回执不匹配，只能核实原请求");
    const result = status(value.result, request.credential_ref);
    const accepted = status(receipt.accepted, request.credential_ref);
    if (result.generation !== request.expected_generation + 1 || result.state !== "missing"
      || accepted.generation !== result.generation || accepted.state !== result.state)
      throw new WorkbenchApiError("unknown", "凭据接纳依据不匹配");
    return result;
  },
  async receipt(request: CredentialRequest) {
    const value = await graphApplicationQuery("credential.receipt", {
      operation: "credential.delete", request,
    });
    if (value === null) throw new WorkbenchApiError("unknown", "原凭据请求尚无回执，未重发 Key");
    const result = status(value, request.credential_ref);
    if (result.generation !== request.expected_generation + 1 || result.state !== "missing")
      throw new WorkbenchApiError("unknown", "原凭据回执不匹配");
    return result;
  },
  async saveBound(request: ProviderCredentialRequest, secret: string, requestDigest: string) {
    if (!isProviderCredentialRequest(request) || request.provider_seed === null)
      throw new WorkbenchApiError("rejected", "供应商保存需要完整配置");
    return boundCommand(request, secret, requestDigest);
  },
  async boundReceipt(request: ProviderCredentialRequest, bind = false) {
    const value = await graphApplicationQuery(bind ? "provider.credential.bind-receipt" : "provider.credential.receipt", { request });
    if (value === null) throw new WorkbenchApiError("unknown", "原 Key 生效请求尚无回执，未重发");
    return bindingResult(value, request, bind);
  },
};
export async function listProviderModels(identity: ProviderIdentity, sequence: number, protocol: ProviderProtocol): Promise<DiscoveredModel[]> {
  const value = await graphApplicationQuery("provider.models.list", { identity, expected_sequence: sequence });
  const limit = (v: unknown) => v === null || Number.isSafeInteger(v) && Number(v) > 0;
  if (!graphObject(value) || Object.keys(value).length !== 6 || value.schema_version !== 1
    || value.kind !== "workflow.provider-models" || !isProviderIdentity(value.reference)
    || !sameProviderIdentity(value.reference, identity) || value.update_sequence !== sequence || value.protocol !== protocol
    || !Array.isArray(value.models) || value.models.length > 1000 || !value.models.every(row => graphObject(row)
      && Object.keys(row).length === 5 && typeof row.model === "string" && !!row.model.trim() && row.model.length <= 256
      && typeof row.adapted === "boolean" && limit(row.input_token_limit) && limit(row.output_token_limit) && limit(row.context_window_tokens)))
    throw new WorkbenchApiError("unavailable", "上游模型列表契约无效");
  return value.models as DiscoveredModel[];
}
