import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { WorkbenchApiError } from "./workbenchApi";
import { graphObject, graphUuid } from "../domain/workflowGraph";
import { chatProviderType, isCurrentProvider, isProviderIdentity, providerIdentity, sameProviderIdentity,
  type CurrentProvider, type ProviderIdentity } from "../domain/workflowModelResources";

export interface ProviderSaveRequest { record: CurrentProvider; expected_sequence: number; idempotency_key: string }
export async function listCurrentProviders(): Promise<CurrentProvider[]> {
  const value = await graphApplicationQuery("resource.list", { type_id: chatProviderType });
  if (!Array.isArray(value) || !value.every(isCurrentProvider))
    throw new WorkbenchApiError("unavailable", "供应商当前资源契约无效");
  return value;
}
export async function readCurrentProvider(identity: ProviderIdentity): Promise<CurrentProvider | null> {
  if (!isProviderIdentity(identity)) throw new WorkbenchApiError("rejected", "供应商引用无效");
  const value = await graphApplicationQuery("resource.read", { identity });
  if (value === null) return null;
  if (!isCurrentProvider(value) || !sameProviderIdentity(providerIdentity(value), identity))
    throw new WorkbenchApiError("unavailable", "供应商当前资源身份不匹配");
  return value;
}
function validateProviderSaveRequest(request: ProviderSaveRequest) {
  if (!isCurrentProvider(request.record) || !graphUuid(request.idempotency_key)
    || !Number.isSafeInteger(request.expected_sequence) || request.expected_sequence < 0
    || request.record.update_sequence !== request.expected_sequence + 1)
    throw new WorkbenchApiError("rejected", "供应商字段或当前序号无效");
}
function validateProviderSaveReceipt(value: unknown, request: ProviderSaveRequest) {
  if (!graphObject(value) || !isProviderIdentity(value.reference)
    || !sameProviderIdentity(value.reference, providerIdentity(request.record))
    || value.update_sequence !== request.record.update_sequence || value.deleted !== false)
    throw new WorkbenchApiError("unknown", "供应商回执与原请求不匹配，保留原请求");
  return value;
}
export async function saveCurrentProvider(request: ProviderSaveRequest) {
  validateProviderSaveRequest(request);
  return validateProviderSaveReceipt(await sendGraphCommand("/api/graph/resources/save", { ...request }), request);
}
export async function readCurrentProviderReceipt(request: ProviderSaveRequest) {
  validateProviderSaveRequest(request);
  return validateProviderSaveReceipt(await readGraphReceipt("/api/graph/resources/save", { ...request }), request);
}
