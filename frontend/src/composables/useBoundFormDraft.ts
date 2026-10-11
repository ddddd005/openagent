import { computed, onBeforeUnmount, ref, watch } from "vue";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import { useFormDraftsStore } from "../stores/formDrafts";

interface FormBinding<T> {
  id(): string;
  basis(): unknown;
  capture(): T;
  restore(value: T): void;
  reset?(): void;
  enabled?: boolean;
}

export function useBoundFormDraft<T>(binding: FormBinding<T>) {
  const enabled = binding.enabled ?? typeof window !== "undefined";
  const store = enabled ? useFormDraftsStore() : null;
  const loading = ref(false), conflict = ref(false), error = ref("");
  let generation = 0, restoring = false, disposed = false, currentId = "", currentBasis: unknown;
  let conflictedValue: T | null = null, lastValue = "";

  const context = computed(() => graphJsonSignature([binding.id(), binding.basis()]));
  watch(context, async () => {
    const token = ++generation;
    currentId = binding.id();
    currentBasis = graphClone(binding.basis());
    conflict.value = false;
    conflictedValue = null;
    error.value = "";
    restoring = true;
    try { binding.reset?.(); }
    finally { restoring = false; }
    if (!store || !currentId) { loading.value = false; return; }
    loading.value = true;
    try {
      const result = await store.load(currentId, currentBasis);
      if (token !== generation) return;
      if (result.conflict) {
        conflict.value = true;
        conflictedValue = result.value as T;
      } else if (result.value !== null) {
        restoring = true;
        try { binding.restore(graphClone(result.value) as T); }
        finally { restoring = false; }
      }
      lastValue = JSON.stringify(binding.capture());
    } catch (failure) {
      if (token === generation) {
        conflict.value = true;
        error.value = failure instanceof Error ? failure.message : "本机表单草稿读取失败";
      }
    } finally { if (token === generation) loading.value = false; }
  }, { immediate: true, flush: "sync" });

  watch(binding.capture, value => {
    if (!store || !currentId || restoring || loading.value || conflict.value) return;
    const signature = JSON.stringify(value);
    if (signature === lastValue) return;
    lastValue = signature;
    if (!store.stage(currentId, currentBasis, graphClone(value)))
      error.value = "表单草稿尚未安全载入，当前输入未写入本机";
  }, { deep: true, flush: "sync" });

  async function flush() {
    if (disposed) return false;
    if (!store || !currentId) return true;
    if (loading.value || conflict.value || error.value) return false;
    const token = generation, id = currentId;
    return await store.flush(id) && token === generation;
  }
  function adoptBasis(nextBasis: unknown) {
    if (!store || !currentId) return true;
    const acceptedContext = graphJsonSignature(currentBasis) === graphJsonSignature(nextBasis);
    if ((loading.value || conflict.value) && !acceptedContext || error.value
      || !store.rebase(currentId, nextBasis)) return false;
    return store.stage(currentId, nextBasis, graphClone(binding.capture()));
  }
  function restoreConflict() {
    if (!store || conflictedValue === null) return;
    restoring = true;
    try {
      binding.restore(graphClone(conflictedValue));
      if (!store.rebase(currentId, currentBasis)) throw new Error("草稿依据无法更新，原文仍保留");
      conflict.value = false;
      conflictedValue = null;
      lastValue = JSON.stringify(binding.capture());
      store.stage(currentId, currentBasis, graphClone(binding.capture()));
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : "旧草稿格式无法载入，原文仍保留";
    } finally { restoring = false; }
  }
  async function discardConflict() {
    const token = generation;
    if (!store || !store.rebase(currentId, currentBasis)) return;
    if (!await store.remove(currentId, currentBasis)) return;
    if (token !== generation) return;
    conflict.value = false;
    conflictedValue = null;
    error.value = "";
    lastValue = JSON.stringify(binding.capture());
  }
  const message = computed(() => error.value || (conflict.value
    ? "配置依据已变化，旧表单草稿保留，尚未应用"
    : loading.value ? "读取本机表单草稿中" : store?.errors[currentId] ?? ""));
  onBeforeUnmount(() => { disposed = true; generation++; });
  return { loading, conflict, error, message, flush, adoptBasis, context,
    restoreConflict, discardConflict };
}
