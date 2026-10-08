import { computed, hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { WorkbenchApiError } from "../../adapters/workbenchApi";
import { graphClone } from "../../domain/workflowGraph";
import { isCurrentLorebookResource, type CurrentLorebookResource } from "./lorebookResources";
import { isLorebookSaveRequest, listCurrentLorebooks, readCurrentLorebookReceipt, saveCurrentLorebook,
  type LorebookSaveRequest } from "./lorebookResourcesApi";

const pendingKey = "workflow.tavern.lorebook.pending.v1";
export interface LorebookResourcesPorts {
  list(): Promise<CurrentLorebookResource[]>;
  save(request: LorebookSaveRequest): Promise<unknown>;
  readReceipt(request: LorebookSaveRequest): Promise<unknown>;
  readPending(): string | null;
  writePending(value: string | null): void;
}
export function createLorebookResources(ports: LorebookResourcesPorts) {
  const records = ref<CurrentLorebookResource[]>([]), loading = ref(false), busy = ref(false);
  const pending = ref<LorebookSaveRequest | null>(null), error = ref("");
  const locked = computed(() => busy.value || !!pending.value);
  let loadGeneration = 0, commandGeneration = 0;
  try {
    const saved = ports.readPending();
    if (saved !== null) {
      const value: unknown = JSON.parse(saved);
      if (!isLorebookSaveRequest(value)) throw new Error("Invalid pending lorebook request");
      pending.value = value;
    }
  } catch { error.value = "原 Lorebook 请求无法读取，资源修改已禁用"; busy.value = true; }
  async function refresh() {
    const generation = ++loadGeneration;
    loading.value = true;
    try {
      const incoming = await ports.list();
      if (!Array.isArray(incoming) || !incoming.every(isCurrentLorebookResource)) throw new Error("Lorebook 资源契约无效");
      if (generation === loadGeneration) records.value = graphClone(incoming);
    } catch (failure) {
      if (generation === loadGeneration) error.value = failure instanceof Error ? failure.message : "Lorebook 资源读取失败";
    } finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function submit(request: LorebookSaveRequest, reconciling = false) {
    const original = graphClone(request), identity = JSON.stringify(original), generation = ++commandGeneration;
    const current = () => {
      if (generation !== commandGeneration || pending.value === null || JSON.stringify(pending.value) !== identity) return false;
      try {
        const durable = ports.readPending();
        return durable !== null && JSON.stringify(JSON.parse(durable)) === identity;
      } catch { return false; }
    };
    busy.value = true; error.value = "";
    try {
      await (reconciling ? ports.readReceipt : ports.save)(graphClone(original));
      if (!current()) { error.value = "原请求状态已变化，保留待核实请求"; return false; }
      ports.writePending(null); pending.value = null;
      await refresh();
      return true;
    } catch (failure) {
      if (!current()) { error.value = "原请求状态已变化，保留待核实请求"; return false; }
      error.value = failure instanceof Error ? failure.message : "Lorebook 资源保存失败";
      // A read-only receipt rejection cannot settle an earlier unknown mutation.
      if (!reconciling && failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code !== "idempotency_conflict") {
        try { ports.writePending(null); pending.value = null; }
        catch { error.value = "原请求状态无法保存，先核实原请求"; }
      }
      return false;
    } finally { if (generation === commandGeneration) busy.value = false; }
  }
  async function save(resource: CurrentLorebookResource, expectedSequence: number) {
    if (locked.value) return false;
    if (!isCurrentLorebookResource(resource) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      error.value = "Lorebook 字段或当前序号无效"; return false;
    }
    const record = graphClone(resource);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!isLorebookSaveRequest(request)) { error.value = "Lorebook 字段或当前序号无效"; return false; }
    try { ports.writePending(JSON.stringify(request)); }
    catch { error.value = "无法保存原 Lorebook 请求，未提交"; return false; }
    pending.value = request;
    return submit(request);
  }
  async function reconcile() {
    if (!pending.value || busy.value) return false;
    return submit(pending.value, true);
  }
  return { records, loading, busy, pending, error, locked, refresh, save, reconcile };
}
export const lorebookResourceControllerKey: InjectionKey<ReturnType<typeof createLorebookResources>> =
  Symbol("workflow.tavern.lorebook-resources");
let installed: ReturnType<typeof createLorebookResources> | undefined;
export function useLorebookResources() {
  const provided = hasInjectionContext() ? inject(lorebookResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createLorebookResources({
    list: listCurrentLorebooks, save: saveCurrentLorebook, readReceipt: readCurrentLorebookReceipt,
    readPending: () => typeof localStorage === "undefined" ? null : localStorage.getItem(pendingKey),
    writePending: value => {
      if (typeof localStorage === "undefined") throw new Error("Lorebook request storage unavailable");
      if (value === null) localStorage.removeItem(pendingKey); else localStorage.setItem(pendingKey, value);
    },
  });
}
