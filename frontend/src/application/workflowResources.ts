import { hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { listCurrentProviders, readCurrentProviderReceipt, saveCurrentProvider,
  type ProviderSaveRequest } from "../adapters/workflowResourcesApi";
import { cloneProvider, isCurrentProvider, type CurrentProvider } from "../domain/workflowModelResources";
import { graphUuid } from "../domain/workflowGraph";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

export interface ProviderResourcesPorts extends ResourceOutboxPorts<ProviderSaveRequest> {
  list(): Promise<CurrentProvider[]>;
}
function validRequest(value: unknown): value is ProviderSaveRequest {
  const request = value as ProviderSaveRequest | null;
  return !!request && Object.keys(request).length === 3 && isCurrentProvider(request.record)
    && graphUuid(request.idempotency_key) && Number.isSafeInteger(request.expected_sequence)
    && request.expected_sequence >= 0 && request.record.update_sequence === request.expected_sequence + 1;
}
export function createProviderResources(ports: ProviderResourcesPorts) {
  const records = ref<CurrentProvider[]>([]), loading = ref(false);
  let loadGeneration = 0;
  const outbox = createResourceOutbox(ports, validRequest, refresh, "供应商资源");
  async function refresh() {
    const generation = ++loadGeneration; loading.value = true;
    try { const incoming = await ports.list(); if (generation === loadGeneration) records.value = incoming; }
    catch (failure) { if (generation === loadGeneration) outbox.error.value = failure instanceof Error ? failure.message : "资源读取失败"; }
    finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function save(provider: CurrentProvider, expectedSequence: number) {
    if (!isCurrentProvider(provider) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      outbox.error.value = "供应商字段或当前序号无效"; return false;
    }
    const record = cloneProvider(provider);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!validRequest(request)) { outbox.error.value = "供应商字段或当前序号无效"; return false; }
    return outbox.send(request);
  }
  return { records, loading, ...outbox, refresh, save };
}
let installed: ReturnType<typeof createProviderResources> | undefined;
export const modelResourceControllerKey: InjectionKey<ReturnType<typeof createProviderResources>> = Symbol("workflow.models.current-resources");
export function useProviderResources() {
  const provided = hasInjectionContext() ? inject(modelResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createProviderResources({
    list: listCurrentProviders, save: saveCurrentProvider, readReceipt: readCurrentProviderReceipt,
    ...createResourcePendingStorage("models:resource-pending:v1", "workflow.models.resource.pending.v1"),
  });
}
