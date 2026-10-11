import { ref, type Ref } from "vue";
import { graphClone, graphJsonSignature, graphObject, graphUuid } from "../domain/workflowGraph";

interface ResourceRecord { scope: string; resource_id: string; update_sequence: number }

export function useResourceFormEditor<T extends ResourceRecord>(create: () => T) {
  const form = ref(null) as Ref<T | null>, expectedSequence = ref(0);
  const key = (record: T) => JSON.stringify([record.scope, record.resource_id]);
  function edit(record: T | null, fresh = false) {
    const next = record ?? create();
    form.value = graphClone(next);
    expectedSequence.value = fresh || !record ? 0 : record.update_sequence;
  }
  function close() {
    form.value = null;
    expectedSequence.value = 0;
  }
  function acceptSubmitted(record: T, expected: number, project: (value: T) => unknown = value => value) {
    if (!form.value || key(form.value) !== key(record) || expectedSequence.value !== expected
      || graphJsonSignature(project({ ...form.value, update_sequence: record.update_sequence }))
        !== graphJsonSignature(project(record))) return false;
    close();
    return true;
  }
  function acceptCredential(record: T, expected: number, patch: Partial<T>) {
    if (!form.value || key(form.value) !== key(record) || expectedSequence.value !== expected) return false;
    Object.assign(form.value, graphClone(patch));
    form.value.update_sequence = record.update_sequence;
    expectedSequence.value = record.update_sequence;
    return true;
  }
  async function reloadSaved(record: T, confirmed: boolean) {
    if (!confirmed || !form.value || key(form.value) !== key(record) || !graphObject(record)
      || typeof record.scope !== "string" || !graphUuid(record.resource_id)
      || !Number.isSafeInteger(record.update_sequence) || record.update_sequence < 0) return false;
    edit(record);
    return true;
  }
  return { form, expectedSequence, edit, close, acceptSubmitted, acceptCredential, reloadSaved };
}
