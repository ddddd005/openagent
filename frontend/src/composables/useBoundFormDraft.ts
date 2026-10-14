import { computed, ref } from "vue";
import { useVolatileFormState } from "./useVolatileFormState";

interface FormBinding<T> {
  id(): string;
  basis(): unknown;
  capture(): T;
  restore(value: T): void;
  reset?(): void;
  enabled?: boolean;
}
const retired = "持久表单草稿恢复已退役；输入仅保留在当前内存中";

// Kept as a typed retired surface for uninstalled consumers. Live editors use
// useVolatileFormState directly and expose no restore/discard action.
export function useBoundFormDraft<T>(binding: FormBinding<T>) {
  const state = useVolatileFormState({ id: binding.id, basis: binding.basis, reset: binding.reset });
  const loading = ref(false), conflict = ref(false), error = ref("");
  const message = computed(() => retired);
  async function flush() {
    error.value = retired;
    return false;
  }
  function adoptBasis(_nextBasis: unknown) {
    error.value = retired;
    return false;
  }
  function restoreConflict(): never {
    error.value = retired;
    throw new Error(retired);
  }
  async function discardConflict() {
    error.value = retired;
    return false;
  }
  return { loading, conflict, error, message, context: state.context, flush,
    adoptBasis, restoreConflict, discardConflict };
}
