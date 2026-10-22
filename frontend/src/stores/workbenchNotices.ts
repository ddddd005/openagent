import { computed, onScopeDispose, ref } from "vue";
import { defineStore } from "pinia";
import type { FailureKind } from "../adapters/workbenchApi";

export interface WorkbenchNotice {
  id: string;
  sequence: number;
  severity: "info" | "success" | "warning" | "major";
  title: string;
  kind?: FailureKind;
  reason: string;
  requestId: string;
  action?: { label: string; run: () => void };
}
export const useWorkbenchNoticesStore = defineStore("workbench-notices", () => {
  const notices = ref<WorkbenchNotice[]>([]);
  const error = computed(() => [...notices.value].reverse().find(notice => notice.kind) ?? null);
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  let sequence = 0;
  function dismiss(id: string, expectedSequence?: number) {
    const notice = notices.value.find(row => row.id === id);
    if (!notice || expectedSequence !== undefined && notice.sequence !== expectedSequence) return;
    const timer = timers.get(id);
    if (timer) clearTimeout(timer);
    timers.delete(id);
    notices.value = notices.value.filter(row => row.id !== id);
  }
  function publish(input: Omit<WorkbenchNotice, "sequence" | "id"> & { id?: string }) {
    let id = input.id ?? `notice:${sequence + 1}`;
    const prior = notices.value.find(row => row.id === id);
    if (prior && prior.severity === input.severity && prior.reason === input.reason
      && prior.title === input.title && prior.requestId === input.requestId) return prior.sequence;
    if (prior?.severity === "major" && input.severity !== "major") id = `${id}:notice:${sequence + 1}`;
    dismiss(id);
    const notice = { ...input, id, sequence: ++sequence };
    notices.value.push(notice);
    if (notice.severity !== "major")
      timers.set(id, setTimeout(() => dismiss(id, notice.sequence), 5000));
    return notice.sequence;
  }
  function notify(kind: FailureKind, reason: string, requestId = "本地") {
    const labels = { unavailable: "动作不可用", rejected: "请求被拒绝", unknown: "提交结果未知" };
    publish({ severity: "warning", title: labels[kind], kind, reason, requestId });
  }
  function clearError(value: number) {
    const notice = notices.value.find(row => row.sequence === value);
    if (notice && notice.severity !== "major") dismiss(notice.id, value);
  }
  onScopeDispose(() => { for (const timer of timers.values()) clearTimeout(timer); timers.clear(); });
  return { notices, error, publish, dismiss, notify, clearError };
});
