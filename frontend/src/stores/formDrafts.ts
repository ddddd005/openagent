import { computed, ref } from "vue";
import { defineStore } from "pinia";
import type { LocalDraftRecord } from "../adapters/localDraftsApi";

type DraftStatus = "loading" | "saved" | "pending" | "blocked";
export interface FormDraftLoad {
  value: unknown | null;
  conflict: boolean;
  record: LocalDraftRecord | null;
}
const retired = "持久表单草稿已退役；当前输入仅保留在内存中";

export const useFormDraftsStore = defineStore("form-drafts", () => {
  const statuses = ref<Record<string, DraftStatus>>({});
  const errors = ref<Record<string, string>>({});
  const hasUnsaved = computed(() => false);

  async function load(id: string, _basis: unknown): Promise<FormDraftLoad> {
    statuses.value[id] = "saved";
    errors.value[id] = "";
    return { value: null, conflict: false, record: null };
  }
  function stage(id: string, _basis: unknown, _value: unknown) {
    statuses.value[id] = "blocked";
    errors.value[id] = retired;
    return false;
  }
  function rebase(_id: string, _basis: unknown) { return false; }
  async function flush(id?: string): Promise<boolean> {
    if (id) {
      statuses.value[id] = "blocked";
      errors.value[id] = retired;
    }
    return false;
  }
  async function remove(id: string, _basis: unknown): Promise<boolean> {
    statuses.value[id] = "blocked";
    errors.value[id] = retired;
    return false;
  }
  return { statuses, status: statuses, errors, hasUnsaved, load, stage, rebase,
    flush, flushAll: () => flush(), remove };
});
