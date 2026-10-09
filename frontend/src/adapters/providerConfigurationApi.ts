import { workbenchRequest, WorkbenchApiError } from "./workbenchApi";
import { graphApplicationQuery } from "./workflowApplicationApi";
import { graphObject, graphUuid } from "../domain/workflowGraph";
import { isProviderIdentity, sameProviderIdentity, type ProviderIdentity, type ProviderProtocol } from "../domain/workflowModelResources";

export interface CredentialRequest { credential_ref: `local:${string}`; expected_generation: number; idempotency_key: string }
export interface CredentialStatus {
  schema_version: 1; kind: "workflow.credential-status"; credential_ref: string;
  generation: number; state: "saved" | "missing";
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
export const providerConfigurationPorts = {
  async protection() {
    const value = await graphApplicationQuery("credential.protection");
    if (!graphObject(value) || Object.keys(value).length !== 4 || value.schema_version !== 1
      || value.kind !== "workflow.credential-protection" || typeof value.available !== "boolean" || value.mode !== "current-user")
      throw new WorkbenchApiError("unavailable", "系统凭据保护不可用");
    return value.available;
  },
  async status(reference: string) { return status(await graphApplicationQuery("credential.status", { credential_ref: reference }), reference); },
  async save(request: CredentialRequest, secret: string, remove = false) {
    const operation = remove ? "credential.delete" : "credential.save";
    const value = await workbenchRequest<unknown>("/api/graph/commands",
      { body: { operation, parameters: { ...request, ...(!remove ? { secret } : {}) } } });
    const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
    if (!graphObject(value) || Object.keys(value).length !== 4
      || value.schema_version !== 1 || value.kind !== "workflow.application-command"
      || !receipt || Object.keys(receipt).length !== 9 || receipt.schema_version !== 1
      || receipt.kind !== "workflow.application-receipt"
      || receipt.operation !== operation || receipt.operation_scope !== "management"
      || receipt.idempotency_key !== request.idempotency_key || receipt.authority !== "service_receipt"
      || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 0
      || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256))
      throw new WorkbenchApiError("unknown", "凭据保存回执不匹配，只能核实原请求");
    const result = status(value.result, request.credential_ref);
    const accepted = status(receipt.accepted, request.credential_ref);
    if (result.generation !== request.expected_generation + 1 || result.state !== (remove ? "missing" : "saved")
      || accepted.generation !== result.generation || accepted.state !== result.state)
      throw new WorkbenchApiError("unknown", "凭据接纳依据不匹配");
    return result;
  },
  async receipt(request: CredentialRequest, remove = false) {
    const value = await graphApplicationQuery("credential.receipt", {
      operation: remove ? "credential.delete" : "credential.save", request,
    });
    if (value === null) throw new WorkbenchApiError("unknown", "原凭据请求尚无回执，未重发 Key");
    const result = status(value, request.credential_ref);
    if (result.generation !== request.expected_generation + 1 || result.state !== (remove ? "missing" : "saved"))
      throw new WorkbenchApiError("unknown", "原凭据回执不匹配");
    return result;
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
