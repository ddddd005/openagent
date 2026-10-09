import { defineStore } from "pinia";
import { ref } from "vue";
import { graphObject } from "../domain/workflowGraph";
import { useFormDraftsStore } from "./formDrafts";

export interface ManagementLabelTarget { kind: "session" | "prompt" | "workflow"; id: string; scope?: string }
interface Label { label: string; archived: boolean }
const keyFor = (target: ManagementLabelTarget) => `management-label:${JSON.stringify([target.kind, target.scope ?? "", target.id])}`;
const basisFor = (target: ManagementLabelTarget) => ({ managementTarget: target });
export const useManagementLabelsStore = defineStore("management-labels", () => {
  const forms = useFormDraftsStore(), values = ref<Record<string, Label>>({}), errors = ref<Record<string, string>>({});
  const loading = new Map<string, Promise<void>>();
  const updates = new Map<string, Promise<unknown>>();
  function label(target: ManagementLabelTarget, fallback: string) { return values.value[keyFor(target)]?.label || fallback; }
  function archived(target: ManagementLabelTarget) { return values.value[keyFor(target)]?.archived ?? false; }
  async function load(target: ManagementLabelTarget) {
    const key = keyFor(target);
    if (Object.hasOwn(values.value, key)) return;
    const previous = loading.get(key);
    if (previous) return previous;
    const operation = (async () => {
      try {
        const result = await forms.load(key, basisFor(target));
        if (result.conflict) throw new Error("本机名称记录依据不兼容，原文保留");
        const value = result.value ?? { label: "", archived: false };
        if (!graphObject(value) || Object.keys(value).length !== 2 || typeof value.label !== "string"
          || value.label.length > 128 || typeof value.archived !== "boolean") throw new Error("本机名称记录格式不兼容，原文保留");
        values.value[key] = value as unknown as Label; errors.value[key] = "";
      } catch (failure) { errors.value[key] = failure instanceof Error ? failure.message : "本机名称读取失败"; }
    })();
    loading.set(key, operation);
    try { await operation; } finally { loading.delete(key); }
  }
  async function write(target: ManagementLabelTarget, patch: Partial<Label>) {
    await load(target);
    const key = keyFor(target), current = values.value[key];
    if (!current || errors.value[key]) return false;
    const next = { ...current, ...patch };
    if (typeof next.label !== "string" || next.label.length > 128 || typeof next.archived !== "boolean") return false;
    next.label = next.label.trim();
    if (!forms.stage(key, basisFor(target), next) || !await forms.flush(key)) {
      errors.value[key] = forms.errors[key] || "本机名称尚未保存"; return false;
    }
    values.value[key] = next;
    return true;
  }
  function update(target: ManagementLabelTarget, patch: Partial<Label>) {
    const fixed = { ...target }, fixedPatch = { ...patch }, key = keyFor(fixed);
    const operation = (updates.get(key) ?? Promise.resolve()).then(() => write(fixed, fixedPatch));
    updates.set(key, operation.catch(() => {}));
    return operation;
  }
  function error(target: ManagementLabelTarget) { return errors.value[keyFor(target)] ?? ""; }
  return { values, errors, label, archived, load, update, error };
});
