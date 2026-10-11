import { computed, hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { isModelProfileSaveRequest, listModelProfiles, saveModelProfile, readModelProfileReceipt,
  type ModelProfileSaveRequest } from "../adapters/workflowModelProfilesApi";
import { graphClone } from "../domain/workflowGraph";
import { isCurrentModelProfile, type CurrentModelProfile } from "../domain/workflowModelResources";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

interface ModelProfilePorts extends ResourceOutboxPorts<ModelProfileSaveRequest> {
  list(): Promise<CurrentModelProfile[]>;
}
export function createModelProfiles(ports: ModelProfilePorts) {
  const records = ref<CurrentModelProfile[]>([]), loading = ref(false), readError = ref("");
  let generation = 0;
  const outbox = createResourceOutbox(ports, isModelProfileSaveRequest, refresh, "模型配置");
  const error = computed({
    get: () => outbox.error.value || readError.value,
    set: (value: string) => { outbox.error.value = value; if (!value) readError.value = ""; },
  });
  async function refresh() {
    const token = ++generation;
    loading.value = true;
    try {
      const rows = await ports.list();
      if (token === generation) { records.value = rows; readError.value = ""; }
    }
    catch (failure) {
      if (token === generation) readError.value = failure instanceof Error ? failure.message : "模型配置读取失败";
    } finally { if (token === generation) loading.value = false; }
  }
  async function save(profile: CurrentModelProfile, expected: number) {
    const record = graphClone(profile);
    record.update_sequence = expected + 1;
    if (!isCurrentModelProfile(record)) { outbox.error.value = "模型配置尚未填写完整"; return false; }
    return outbox.send({ record, expected_sequence: expected, idempotency_key: crypto.randomUUID() });
  }
  return { records, loading, ...outbox, error, refresh, save };
}
export const modelProfilesKey: InjectionKey<ReturnType<typeof createModelProfiles>> = Symbol("workflow.models.profiles");
let installed: ReturnType<typeof createModelProfiles> | undefined;
export function useModelProfiles() {
  const provided = hasInjectionContext() ? inject(modelProfilesKey, null) : null;
  return provided ?? (installed ??= createModelProfiles({
    list: listModelProfiles, save: saveModelProfile, readReceipt: readModelProfileReceipt,
    ...createResourcePendingStorage("models:profiles:pending:v1", "workflow.models.profiles.pending.v1"),
  }));
}
