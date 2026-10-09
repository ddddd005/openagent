import { hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { graphClone } from "../../domain/workflowGraph";
import { isCurrentLorebookResource, type CurrentLorebookResource } from "./lorebookResources";
import { isLorebookSaveRequest, listCurrentLorebooks, readCurrentLorebookReceipt, saveCurrentLorebook,
  type LorebookSaveRequest } from "./lorebookResourcesApi";
import { createResourcePendingStorage } from "../../adapters/resourcePendingStorage";
import { createResourceOutbox, type ResourceOutboxPorts } from "../../application/resourceOutbox";

export interface LorebookResourcesPorts extends ResourceOutboxPorts<LorebookSaveRequest> {
  list(): Promise<CurrentLorebookResource[]>;
}
export function createLorebookResources(ports: LorebookResourcesPorts) {
  const records = ref<CurrentLorebookResource[]>([]), loading = ref(false);
  let loadGeneration = 0;
  const outbox = createResourceOutbox(ports, isLorebookSaveRequest, refresh, "Lorebook 资源");
  async function refresh() {
    const generation = ++loadGeneration; loading.value = true;
    try {
      const incoming = await ports.list();
      if (!Array.isArray(incoming) || !incoming.every(isCurrentLorebookResource)) throw new Error("Lorebook 资源契约无效");
      if (generation === loadGeneration) records.value = graphClone(incoming);
    } catch (failure) {
      if (generation === loadGeneration) outbox.error.value = failure instanceof Error ? failure.message : "Lorebook 资源读取失败";
    } finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function save(resource: CurrentLorebookResource, expectedSequence: number) {
    if (!isCurrentLorebookResource(resource) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      outbox.error.value = "Lorebook 字段或当前序号无效"; return false;
    }
    const record = graphClone(resource);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!isLorebookSaveRequest(request)) { outbox.error.value = "Lorebook 字段或当前序号无效"; return false; }
    return outbox.send(request);
  }
  return { records, loading, ...outbox, refresh, save };
}
export const lorebookResourceControllerKey: InjectionKey<ReturnType<typeof createLorebookResources>> =
  Symbol("workflow.tavern.lorebook-resources");
let installed: ReturnType<typeof createLorebookResources> | undefined;
export function useLorebookResources() {
  const provided = hasInjectionContext() ? inject(lorebookResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createLorebookResources({
    list: listCurrentLorebooks, save: saveCurrentLorebook, readReceipt: readCurrentLorebookReceipt,
    ...createResourcePendingStorage("lorebooks:resource-pending:v1", "workflow.tavern.lorebook.pending.v1"),
  });
}
