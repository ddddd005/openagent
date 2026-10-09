import { hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { listCurrentPromptResources, readCurrentPromptReceipt, saveCurrentPromptResource,
  type PromptSaveRequest } from "../adapters/promptResourcesApi";
import { clonePromptResource, isCurrentPromptResource, type CurrentPromptResource } from "../domain/workflowPromptResources";
import { graphObject, graphUuid } from "../domain/workflowGraph";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

export interface PromptResourcesPorts extends ResourceOutboxPorts<PromptSaveRequest> {
  list(): Promise<CurrentPromptResource[]>;
}
function validRequest(value: unknown): value is PromptSaveRequest {
  return graphObject(value) && Object.keys(value).length === 3 && isCurrentPromptResource(value.record)
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase()
    && Number.isSafeInteger(value.expected_sequence) && Number(value.expected_sequence) >= 0
    && value.record.update_sequence === Number(value.expected_sequence) + 1;
}
export function createPromptResources(ports: PromptResourcesPorts) {
  const records = ref<CurrentPromptResource[]>([]), loading = ref(false);
  let loadGeneration = 0;
  const outbox = createResourceOutbox(ports, validRequest, refresh, "提示词资源");
  async function refresh() {
    const generation = ++loadGeneration; loading.value = true;
    try { const incoming = await ports.list(); if (generation === loadGeneration) records.value = incoming; }
    catch (failure) {
      if (generation === loadGeneration) outbox.error.value = failure instanceof Error ? failure.message : "提示词资源读取失败";
    } finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function save(resource: CurrentPromptResource, expectedSequence: number) {
    if (!isCurrentPromptResource(resource) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      outbox.error.value = "提示词字段或当前序号无效"; return false;
    }
    const record = clonePromptResource(resource);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!validRequest(request)) { outbox.error.value = "提示词字段或当前序号无效"; return false; }
    return outbox.send(request);
  }
  return { records, loading, ...outbox, refresh, save };
}
let installed: ReturnType<typeof createPromptResources> | undefined;
export const promptResourceControllerKey: InjectionKey<ReturnType<typeof createPromptResources>> =
  Symbol("workflow.prompts.current-resources");
export function usePromptResources() {
  const provided = hasInjectionContext() ? inject(promptResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createPromptResources({
    list: listCurrentPromptResources, save: saveCurrentPromptResource, readReceipt: readCurrentPromptReceipt,
    ...createResourcePendingStorage("prompts:resource-pending:v1", "workflow.prompts.resource.pending.v1"),
  });
}
