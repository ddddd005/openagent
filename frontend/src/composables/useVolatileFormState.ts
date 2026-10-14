import { computed, ref, watch } from "vue";
import { graphJsonSignature } from "../domain/workflowGraph";

interface VolatileBinding {
  id(): string;
  basis(): unknown;
  reset?(): void;
}

export function useVolatileFormState(binding: VolatileBinding) {
  const context = computed(() => graphJsonSignature([binding.id(), binding.basis()]));
  const generation = ref(0);
  let observed = "";
  watch(context, value => {
    if (value === observed) return;
    observed = value;
    generation.value++;
    binding.reset?.();
  }, { immediate: true, flush: "sync" });
  return {
    context,
    generation: computed(() => generation.value),
    isCurrent: (token: number) => token === generation.value,
  };
}
