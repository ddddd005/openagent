import { defineStore } from "pinia";
import { ref } from "vue";
import { localDraftPorts, type LocalDraftAddress } from "../adapters/localDraftsApi";
import { graphJsonSignature, graphObject } from "../domain/workflowGraph";

export interface ManagementLabelTarget { kind: "session" | "prompt" | "workflow"; id: string; scope?: string }
interface Label { label: string; archived: boolean }
const keyFor = (target: ManagementLabelTarget) => `management-label:${JSON.stringify([target.kind, target.scope ?? "", target.id])}`;
const basisFor = (target: ManagementLabelTarget) => ({ managementTarget: target });
function addressFor(target: ManagementLabelTarget): LocalDraftAddress {
  return { scope: "management", kind: "form", key: keyFor(target),
    workflow_definition_id: null, workflow_session_id: null };
}
function storedLabel(value: unknown, target: ManagementLabelTarget): Label {
  const expectedBasis = basisFor(target);
  if (!graphObject(value) || Object.keys(value).length !== 2 || !graphObject(value.basis)
    || graphJsonSignature(value.basis) !== graphJsonSignature(expectedBasis) || !graphObject(value.raw)
    || Object.keys(value.raw).length !== 2 || typeof value.raw.label !== "string"
    || value.raw.label.length > 128 || typeof value.raw.archived !== "boolean")
    throw new Error("旧本机显示名称身份或格式无法核对；原记录保留");
  return { label: value.raw.label, archived: value.raw.archived };
}

export const useManagementLabelsStore = defineStore("management-labels", () => {
  const values = ref<Record<string, Label>>({}), errors = ref<Record<string, string>>({});
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
        const record = await localDraftPorts.read(addressFor(target));
        if (record && !record.deleted) {
          if (record.format_version !== 1) throw new Error("旧本机显示名称版本无法核对；原记录保留");
          values.value[key] = storedLabel(record.value, target);
        } else values.value[key] = { label: "", archived: false };
        errors.value[key] = "";
      } catch (failure) {
        errors.value[key] = failure instanceof Error ? failure.message : "旧本机显示名称读取失败";
      }
    })();
    loading.set(key, operation);
    try { await operation; } finally { loading.delete(key); }
  }
  async function update(target: ManagementLabelTarget, _patch: Partial<Label>) {
    await load(target);
    errors.value[keyFor(target)] = "已有本机显示名称保留只读；新编辑草稿写入已退役";
    return false;
  }
  function error(target: ManagementLabelTarget) { return errors.value[keyFor(target)] ?? ""; }
  return { values, errors, label, archived, load, update, error };
});
