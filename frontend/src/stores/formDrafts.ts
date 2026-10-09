import { computed, onScopeDispose, ref } from "vue";
import { defineStore } from "pinia";
import { createLocalDraftChannel, type LocalDraftAddress, type LocalDraftRecord } from "../adapters/localDraftsApi";
import { graphClone, graphObject, graphUuid, isGraphJsonValue } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";

type DraftStatus = "loading" | "saved" | "pending" | "blocked";
interface FormDraftValue { basis: unknown; raw: unknown }
interface FormState {
  channel: ReturnType<typeof createLocalDraftChannel>;
  basis: unknown;
  raw: unknown;
  generation: number;
  savedGeneration: number;
  loaded: boolean;
  conflict: boolean;
  loadGeneration: number;
  clearGeneration: number | null;
  operation: Promise<unknown>;
}
export interface FormDraftLoad {
  value: unknown | null;
  conflict: boolean;
  record: LocalDraftRecord | null;
}
function address(id: string, basis: unknown): LocalDraftAddress {
  const data = graphObject(basis) ? basis : {};
  const workflow = data.workflowId ?? data.workflow_definition_id;
  const session = data.sessionId ?? data.workflow_session_id;
  return { scope: "management", kind: "form", key: id,
    workflow_definition_id: graphUuid(workflow) ? workflow : null,
    workflow_session_id: graphUuid(workflow) && graphUuid(session) ? session : null };
}
export const useFormDraftsStore = defineStore("form-drafts", () => {
  const statuses = ref<Record<string, DraftStatus>>({});
  const errors = ref<Record<string, string>>({});
  const states = new Map<string, FormState>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const hasUnsaved = computed(() => Object.entries(statuses.value).some(([key, value]) => value === "pending"
    || states.get(key)?.generation !== states.get(key)?.savedGeneration
    || (states.get(key)?.clearGeneration ?? null) !== null
    || value === "blocked" && !states.get(key)?.loaded));
  function stateFor(id: string, basis: unknown) {
    let state = states.get(id);
    if (!state) {
      state = { channel: createLocalDraftChannel(address(id, basis)), basis: graphClone(basis),
        raw: null, generation: 0, savedGeneration: 0, loaded: false, conflict: false,
        loadGeneration: 0, clearGeneration: null, operation: Promise.resolve() };
      states.set(id, state);
    }
    return state;
  }
  async function load(id: string, basis: unknown): Promise<FormDraftLoad> {
    const state = stateFor(id, basis);
    const token = ++state.loadGeneration, initialGeneration = state.generation;
    statuses.value[id] = "loading";
    try {
      const record = await state.channel.read();
      if (token !== state.loadGeneration || state.loaded && state.generation !== initialGeneration) {
        if (token === state.loadGeneration)
          statuses.value[id] = state.conflict ? "blocked"
            : state.generation === state.savedGeneration && state.clearGeneration === null ? "saved" : "pending";
        return { value: state.raw === null ? null : graphClone(state.raw), conflict: state.conflict, record };
      }
      if (record && !record.deleted && record.format_version !== 1)
        throw new Error("原始表单草稿版本不兼容，原文未覆盖");
      const value = record && !record.deleted ? record.value : null;
      if (value !== null && (!graphObject(value) || Object.keys(value).length !== 2
        || !Object.hasOwn(value, "basis") || !Object.hasOwn(value, "raw"))) throw new Error("原始表单草稿格式无效，原文未覆盖");
      const draft = value as FormDraftValue | null;
      const memory = state.loaded && (state.generation !== state.savedGeneration || state.clearGeneration !== null);
      state.conflict = JSON.stringify(memory ? state.basis : draft?.basis ?? basis) !== JSON.stringify(basis);
      if (!state.loaded || state.generation === state.savedGeneration) {
        state.basis = graphClone(draft?.basis ?? basis);
        state.raw = draft ? graphClone(draft.raw) : null;
      }
      state.loaded = true;
      statuses.value[id] = state.conflict ? "blocked"
        : state.generation !== state.savedGeneration || state.clearGeneration !== null ? "pending" : "saved";
      errors.value[id] = state.conflict ? "编辑依据已变化，保留旧草稿，未自动应用" : "";
      return { value: state.raw === null ? null : graphClone(state.raw), conflict: state.conflict, record };
    } catch (failure) {
      if (token === state.loadGeneration) {
        statuses.value[id] = "blocked";
        errors.value[id] = failure instanceof Error ? failure.message : "原始表单草稿读取失败";
      }
      throw failure;
    }
  }
  function stage(id: string, basis: unknown, value: unknown) {
    if (!isGraphJsonValue(value) || !isGraphJsonValue(basis)) return false;
    const state = stateFor(id, basis);
    if (!state.loaded || state.conflict || JSON.stringify(state.basis) !== JSON.stringify(basis)) return false;
    state.raw = graphClone(value);
    state.generation++;
    statuses.value[id] = "pending";
    errors.value[id] = "";
    const timer = timers.get(id);
    if (timer) clearTimeout(timer);
    timers.set(id, setTimeout(() => { timers.delete(id); void flush(id); }, 200));
    return true;
  }
  function rebase(id: string, basis: unknown) {
    const state = states.get(id);
    if (!state?.loaded || !isGraphJsonValue(basis)) return false;
    state.basis = graphClone(basis);
    state.conflict = false;
    state.generation++;
    statuses.value[id] = "pending";
    errors.value[id] = "";
    return true;
  }
  function serialize<T>(state: FormState, operation: () => Promise<T>) {
    const pending = state.operation.then(operation);
    state.operation = pending.catch(() => {});
    return pending;
  }
  async function clear(key: string, state: FormState) {
    if (state.clearGeneration === null) return true;
    const generation = state.clearGeneration;
    try {
      await state.channel.write(null);
      state.clearGeneration = null;
      state.savedGeneration = generation;
      if (state.generation === generation) state.raw = null;
      statuses.value[key] = state.conflict ? "blocked" : state.generation === generation ? "saved" : "pending";
      if (!state.conflict) errors.value[key] = "";
      return true;
    } catch (failure) {
      statuses.value[key] = "blocked";
      errors.value[key] = failure instanceof Error ? failure.message : "原始表单草稿清理失败";
      return false;
    }
  }
  async function flush(id?: string): Promise<boolean> {
    let success = true;
    for (const [key, state] of states) {
      if (id !== undefined && id !== key) continue;
      const timer = timers.get(key);
      if (timer) clearTimeout(timer);
      timers.delete(key);
      const stored = await serialize(state, async () => {
        if (!state.loaded || state.conflict)
          return state.loaded && state.generation === state.savedGeneration && state.clearGeneration === null;
        if (!await clear(key, state)) return false;
        while (!state.conflict && state.clearGeneration === null && state.generation !== state.savedGeneration) {
          const generation = state.generation;
          try {
            await state.channel.write({ basis: graphClone(state.basis), raw: graphClone(state.raw) });
            state.savedGeneration = generation;
            statuses.value[key] = state.conflict ? "blocked"
              : state.generation === generation && state.clearGeneration === null ? "saved" : "pending";
            if (!state.conflict) errors.value[key] = "";
          } catch (failure) {
            statuses.value[key] = "blocked";
            errors.value[key] = failure instanceof Error ? failure.message : "原始表单草稿尚未写入本机";
            if (failure instanceof WorkbenchApiError && failure.code === "stale_draft_sequence") {
              state.conflict = true;
              try {
                const copyId = `${key.slice(0, 460)}:conflict:${crypto.randomUUID()}`;
                await createLocalDraftChannel(address(copyId, state.basis)).write({
                  basis: graphClone(state.basis), raw: graphClone(state.raw),
                });
                errors.value[key] = "其他页面更新了原草稿，当前编辑已独立保存；未覆盖任何一方";
              } catch { errors.value[key] = "草稿版本冲突，当前编辑尚在内存，未覆盖原记录"; }
            }
            return false;
          }
        }
        return state.generation === state.savedGeneration && state.clearGeneration === null;
      });
      if (!stored) success = false;
    }
    return success;
  }
  async function remove(id: string, basis: unknown) {
    const state = stateFor(id, basis);
    if (!state.loaded || state.conflict || JSON.stringify(state.basis) !== JSON.stringify(basis)) return false;
    state.clearGeneration ??= state.generation;
    statuses.value[id] = "pending";
    return serialize(state, async () => {
      if (state.conflict || JSON.stringify(state.basis) !== JSON.stringify(basis)) return false;
      if (!await clear(id, state)) return false;
      if (state.generation !== state.savedGeneration) void flush(id);
      return true;
    });
  }
  onScopeDispose(() => { for (const timer of timers.values()) clearTimeout(timer); });
  return { statuses, status: statuses, errors, hasUnsaved, load, stage, rebase, flush, flushAll: () => flush(), remove };
});
