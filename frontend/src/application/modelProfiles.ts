import { computed, hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { isLegacyModelProfileSaveRequest, isModelProfileSaveRequest, listModelProfiles,
  readLegacyModelProfileReceipt, readModelProfileReceipt, saveModelProfile,
  type LegacyModelProfileSaveRequest, type ModelProfileSaveRequest } from "../adapters/workflowModelProfilesApi";
import { graphClone } from "../domain/workflowGraph";
import { isCurrentModelProfile, type CurrentModelProfile } from "../domain/workflowModelResources";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

interface ModelProfilePorts extends ResourceOutboxPorts<ModelProfileSaveRequest> {
  list(): Promise<CurrentModelProfile[]>;
  readLegacyReceipt?(request: LegacyModelProfileSaveRequest): Promise<unknown>;
}
export function createModelProfiles(ports: ModelProfilePorts) {
  const records = ref<CurrentModelProfile[]>([]), loading = ref(false), readError = ref("");
  let generation = 0;
  let legacyEvidenceRaw: string | null = null;
  function currentPending(raw: string | null) {
    if (raw === null) { legacyEvidenceRaw = null; return null; }
    try {
      if (isLegacyModelProfileSaveRequest(JSON.parse(raw))) {
        legacyEvidenceRaw = raw;
        return null;
      }
    } catch { /* Leave malformed evidence for the outbox's fail-closed reader. */ }
    legacyEvidenceRaw = null;
    return raw;
  }
  const outboxPorts: ModelProfilePorts = {
    ...ports,
    readPending: () => {
      const raw = ports.readPending();
      return raw instanceof Promise ? raw.then(currentPending) : currentPending(raw);
    },
    readLegacyPending: () => legacyEvidenceRaw ?? ports.readLegacyPending?.() ?? null,
  };
  const outbox = createResourceOutbox(outboxPorts, isModelProfileSaveRequest, refresh, "模型配置");
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
  async function reconcileLegacyPending() {
    if (!await outbox.initialize() || outbox.busy.value || outbox.pending.value || outbox.legacyRaw.value === null)
      return false;
    let request: unknown;
    try { request = JSON.parse(outbox.legacyRaw.value); }
    catch { error.value = "旧模型配置原请求无法读取，原文保留"; return false; }
    if (!isLegacyModelProfileSaveRequest(request)) {
      error.value = "旧模型配置原请求格式无效，原文保留";
      return false;
    }
    outbox.busy.value = true;
    try {
      await (ports.readLegacyReceipt ?? readLegacyModelProfileReceipt)(graphClone(request));
      error.value = "旧模型配置原请求回执已核实；原请求保留，未恢复配置或重新提交";
      return true;
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : "旧模型配置原请求回执无法核实；原文保留";
      return false;
    } finally { outbox.busy.value = false; }
  }
  return { records, loading, ...outbox, error, refresh, save, reconcileLegacyPending };
}
export const modelProfilesKey: InjectionKey<ReturnType<typeof createModelProfiles>> = Symbol("workflow.models.profiles");
let installed: ReturnType<typeof createModelProfiles> | undefined;
export function useModelProfiles() {
  const provided = hasInjectionContext() ? inject(modelProfilesKey, null) : null;
  return provided ?? (installed ??= createModelProfiles({
    list: listModelProfiles, save: saveModelProfile, readReceipt: readModelProfileReceipt,
    readLegacyReceipt: readLegacyModelProfileReceipt,
    ...createResourcePendingStorage("models:profiles:pending:v1", "workflow.models.profiles.pending.v1"),
  }));
}
