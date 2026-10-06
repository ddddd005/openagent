import { computed, hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { listCurrentProviders, saveCurrentProvider, type ProviderSaveRequest } from "../adapters/workflowResourcesApi";
import { cloneProvider, isCurrentProvider, type CurrentProvider } from "../domain/workflowModelResources";
import { graphUuid } from "../domain/workflowGraph";

const pendingKey = "workflow.models.resource.pending.v1";
export interface ProviderResourcesPorts {
  list(): Promise<CurrentProvider[]>;
  save(request: ProviderSaveRequest): Promise<unknown>;
  readPending(): string | null;
  writePending(value: string | null): void;
}
function validRequest(value: unknown): value is ProviderSaveRequest {
  const request = value as ProviderSaveRequest | null;
  return !!request && Object.keys(request).length === 3 && isCurrentProvider(request.record)
    && graphUuid(request.idempotency_key) && Number.isSafeInteger(request.expected_sequence)
    && request.expected_sequence >= 0 && request.record.update_sequence === request.expected_sequence + 1;
}
export function createProviderResources(ports: ProviderResourcesPorts) {
  const records = ref<CurrentProvider[]>([]), loading = ref(false), busy = ref(false);
  const pending = ref<ProviderSaveRequest | null>(null), error = ref("");
  const locked = computed(() => busy.value || !!pending.value);
  let loadGeneration = 0;
  try {
    const saved = ports.readPending();
    if (saved) {
      const value: unknown = JSON.parse(saved);
      if (!validRequest(value)) throw new Error("invalid pending");
      pending.value = value;
    }
  } catch { error.value = "原资源请求无法读取，资源修改已禁用"; busy.value = true; }
  async function refresh() {
    const generation = ++loadGeneration; loading.value = true;
    try { const incoming = await ports.list(); if (generation === loadGeneration) records.value = incoming; }
    catch (failure) { if (generation === loadGeneration) error.value = failure instanceof Error ? failure.message : "资源读取失败"; }
    finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function submit(request: ProviderSaveRequest, reconciling = false) {
    busy.value = true; error.value = "";
    try {
      await ports.save(JSON.parse(JSON.stringify(request)));
      ports.writePending(null); pending.value = null;
      await refresh();
      return true;
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : "资源保存失败";
      // A later transport/policy rejection cannot settle an earlier unknown mutation.
      if (!reconciling && failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code !== "idempotency_conflict") {
        try { ports.writePending(null); pending.value = null; } catch { error.value = "原请求状态无法保存，先核实原请求"; }
      }
      return false;
    } finally { busy.value = false; }
  }
  async function save(provider: CurrentProvider, expectedSequence: number) {
    if (locked.value) return false;
    const record = cloneProvider(provider);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!validRequest(request)) { error.value = "供应商字段或当前序号无效"; return false; }
    try { ports.writePending(JSON.stringify(request)); }
    catch { error.value = "无法保存原资源请求，未提交"; return false; }
    pending.value = request;
    return submit(request);
  }
  async function reconcile() {
    if (!pending.value || busy.value) return false;
    return submit(pending.value, true);
  }
  return { records, loading, busy, pending, error, locked, refresh, save, reconcile };
}
let installed: ReturnType<typeof createProviderResources> | undefined;
export const modelResourceControllerKey: InjectionKey<ReturnType<typeof createProviderResources>> = Symbol("workflow.models.current-resources");
export function useProviderResources() {
  const provided = hasInjectionContext() ? inject(modelResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createProviderResources({
    list: listCurrentProviders, save: saveCurrentProvider,
    readPending: () => typeof localStorage === "undefined" ? null : localStorage.getItem(pendingKey),
    writePending: value => {
      if (typeof localStorage === "undefined") throw new Error("Resource request storage unavailable");
      if (value === null) localStorage.removeItem(pendingKey); else localStorage.setItem(pendingKey, value);
    },
  });
}
