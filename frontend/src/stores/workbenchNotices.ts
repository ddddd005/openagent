import { ref } from "vue";
import { defineStore } from "pinia";
import type { FailureKind } from "../adapters/workbenchApi";

export interface WorkbenchNotice {
  sequence: number;
  kind: FailureKind;
  reason: string;
  requestId: string;
}
export const useWorkbenchNoticesStore = defineStore("workbench-notices", () => {
  const error = ref<WorkbenchNotice | null>(null);
  let sequence = 0;
  function notify(kind: FailureKind, reason: string, requestId = "本地") {
    error.value = { sequence: ++sequence, kind, reason, requestId };
  }
  function clearError(value: number) {
    if (error.value?.sequence === value) error.value = null;
  }
  return { error, notify, clearError };
});
