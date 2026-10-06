import { computed, hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { listCurrentPromptResources, saveCurrentPromptResource, type PromptSaveRequest } from "../adapters/promptResourcesApi";
import { clonePromptResource, isCurrentPromptResource, type CurrentPromptResource } from "../domain/workflowPromptResources";
import { graphObject, graphUuid } from "../domain/workflowGraph";

const pendingKey = "workflow.prompts.resource.pending.v1";
export interface PromptResourcesPorts {
  list(): Promise<CurrentPromptResource[]>;
  save(request: PromptSaveRequest): Promise<unknown>;
  readPending(): string | null;
  writePending(value: string | null): void;
}
function validRequest(value: unknown): value is PromptSaveRequest {
  return graphObject(value) && Object.keys(value).length === 3 && isCurrentPromptResource(value.record)
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase()
    && Number.isSafeInteger(value.expected_sequence) && Number(value.expected_sequence) >= 0
    && value.record.update_sequence === Number(value.expected_sequence) + 1;
}
export function createPromptResources(ports: PromptResourcesPorts) {
  const records = ref<CurrentPromptResource[]>([]), loading = ref(false), busy = ref(false);
  const pending = ref<PromptSaveRequest | null>(null), error = ref("");
  const locked = computed(() => busy.value || !!pending.value);
  let loadGeneration = 0;
  try {
    const saved = ports.readPending();
    if (saved !== null) {
      const value: unknown = JSON.parse(saved);
      if (!validRequest(value)) throw new Error("Invalid pending prompt resource request");
      pending.value = value;
    }
  } catch { error.value = "\u539f\u63d0\u793a\u8bcd\u8bf7\u6c42\u65e0\u6cd5\u8bfb\u53d6\uff0c\u8d44\u6e90\u4fee\u6539\u5df2\u7981\u7528"; busy.value = true; }
  async function refresh() {
    const generation = ++loadGeneration; loading.value = true;
    try { const incoming = await ports.list(); if (generation === loadGeneration) records.value = incoming; }
    catch (failure) {
      if (generation === loadGeneration)
        error.value = failure instanceof Error ? failure.message : "\u63d0\u793a\u8bcd\u8d44\u6e90\u8bfb\u53d6\u5931\u8d25";
    } finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function submit(request: PromptSaveRequest, reconciling = false) {
    busy.value = true; error.value = "";
    try {
      await ports.save(clonePromptResourceRequest(request));
      ports.writePending(null); pending.value = null;
      await refresh();
      return true;
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : "\u63d0\u793a\u8bcd\u8d44\u6e90\u4fdd\u5b58\u5931\u8d25";
      // Rejection during reconciliation cannot settle an earlier unknown mutation.
      if (!reconciling && failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code !== "idempotency_conflict") {
        try { ports.writePending(null); pending.value = null; }
        catch { error.value = "\u539f\u8bf7\u6c42\u72b6\u6001\u65e0\u6cd5\u4fdd\u5b58\uff0c\u5148\u6838\u5b9e\u539f\u8bf7\u6c42"; }
      }
      return false;
    } finally { busy.value = false; }
  }
  async function save(resource: CurrentPromptResource, expectedSequence: number) {
    if (locked.value) return false;
    if (!isCurrentPromptResource(resource) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      error.value = "\u63d0\u793a\u8bcd\u5b57\u6bb5\u6216\u5f53\u524d\u5e8f\u53f7\u65e0\u6548"; return false;
    }
    const record = clonePromptResource(resource);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!validRequest(request)) {
      error.value = "\u63d0\u793a\u8bcd\u5b57\u6bb5\u6216\u5f53\u524d\u5e8f\u53f7\u65e0\u6548"; return false;
    }
    try { ports.writePending(JSON.stringify(request)); }
    catch { error.value = "\u65e0\u6cd5\u4fdd\u5b58\u539f\u63d0\u793a\u8bcd\u8bf7\u6c42\uff0c\u672a\u63d0\u4ea4"; return false; }
    pending.value = request;
    return submit(request);
  }
  async function reconcile() {
    if (!pending.value || busy.value) return false;
    return submit(pending.value, true);
  }
  return { records, loading, busy, pending, error, locked, refresh, save, reconcile };
}
function clonePromptResourceRequest(request: PromptSaveRequest): PromptSaveRequest {
  return { record: clonePromptResource(request.record), expected_sequence: request.expected_sequence,
    idempotency_key: request.idempotency_key };
}
let installed: ReturnType<typeof createPromptResources> | undefined;
export const promptResourceControllerKey: InjectionKey<ReturnType<typeof createPromptResources>> =
  Symbol("workflow.prompts.current-resources");
export function usePromptResources() {
  const provided = hasInjectionContext() ? inject(promptResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createPromptResources({
    list: listCurrentPromptResources, save: saveCurrentPromptResource,
    readPending: () => typeof localStorage === "undefined" ? null : localStorage.getItem(pendingKey),
    writePending: value => {
      if (typeof localStorage === "undefined") throw new Error("Prompt resource request storage unavailable");
      if (value === null) localStorage.removeItem(pendingKey); else localStorage.setItem(pendingKey, value);
    },
  });
}
