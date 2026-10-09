import { computed, ref, type Ref } from "vue";
import { graphClone, graphObject, graphUuid } from "../domain/workflowGraph";
import { useBoundFormDraft } from "./useBoundFormDraft";

interface ResourceRecord { scope: string; resource_id: string; update_sequence: number }
interface ResourceEdit<T> { record: T; expectedSequence: number }
interface PanelState<T> {
  form: T | null;
  expectedSequence: number;
  drafts: Record<string, ResourceEdit<T>>;
}

export function useResourceFormDraft<T extends ResourceRecord>(
  kind: string, create: () => T, label: (record: T) => string,
) {
  const form = ref(null) as Ref<T | null>, expectedSequence = ref(0);
  const drafts = ref({}) as Ref<Record<string, ResourceEdit<T>>>;
  const key = (record: T) => JSON.stringify([record.scope, record.resource_id]);
  const validRecord = (record: unknown): record is T => graphObject(record)
    && typeof record.scope === "string" && graphUuid(record.resource_id)
    && Number.isSafeInteger(record.update_sequence) && Number(record.update_sequence) >= 0
    && graphObject(record.value);
  const localDraft = useBoundFormDraft<PanelState<T>>({
    id: () => `resource-panel:${kind}`,
    basis: () => ({ editor: kind, format: 1 }),
    capture: () => ({ form: form.value, expectedSequence: expectedSequence.value, drafts: drafts.value }),
    restore: value => {
      if (!graphObject(value) || !graphObject(value.drafts)
        || !Number.isSafeInteger(value.expectedSequence) || value.expectedSequence < 0
        || value.form !== null && !validRecord(value.form)
        || !Object.entries(value.drafts).every(([id, entry]) => graphObject(entry)
          && validRecord(entry.record) && key(entry.record) === id
          && Number.isSafeInteger(entry.expectedSequence) && Number(entry.expectedSequence) >= 0))
        throw new Error("资源表单草稿格式不兼容，原文仍保留");
      form.value = graphClone(value.form);
      expectedSequence.value = value.expectedSequence;
      drafts.value = graphClone(value.drafts);
    },
  });
  function stash() {
    if (form.value) drafts.value[key(form.value)] = {
      record: graphClone(form.value), expectedSequence: expectedSequence.value,
    };
  }
  function edit(record: T | null, fresh = false) {
    if (localDraft.loading.value || localDraft.conflict.value || localDraft.error.value) return;
    stash();
    const next = record ?? create();
    const saved = !fresh && drafts.value[key(next)];
    form.value = graphClone(saved ? saved.record : next);
    expectedSequence.value = saved ? saved.expectedSequence : fresh || !record ? 0 : record.update_sequence;
  }
  function close() {
    if (form.value) delete drafts.value[key(form.value)];
    form.value = null;
    expectedSequence.value = 0;
  }
  function resume(id: string) {
    const saved = drafts.value[id];
    if (!saved || localDraft.loading.value || localDraft.conflict.value || localDraft.error.value) return;
    stash();
    form.value = graphClone(saved.record);
    expectedSequence.value = saved.expectedSequence;
  }
  const choices = computed(() => Object.entries(drafts.value)
    .filter(([id]) => !form.value || id !== key(form.value))
    .map(([id, draft], index) => ({ id, label: label(draft.record) || `未提交草稿 ${index + 1}` })));
  return { form, expectedSequence, localDraft, choices, edit, close, resume };
}
