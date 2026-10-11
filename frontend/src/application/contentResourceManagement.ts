import { ref } from "vue";
import { graphClone } from "../domain/workflowGraph";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

interface SequencedContentResource { update_sequence: number }
export interface ContentResourceSaveRequest<T> {
  record: T; expected_sequence: number; idempotency_key: string;
}
export interface ContentResourceManagementPorts<T> extends ResourceOutboxPorts<ContentResourceSaveRequest<T>> {
  list(): Promise<T[]>;
}
interface ContentResourceManagementContract<T> {
  isResource(value: unknown): value is T;
  isRequest(value: unknown): value is ContentResourceSaveRequest<T>;
  label: string;
  invalidSaveMessage: string;
}

export function createContentResourceManagement<T extends SequencedContentResource>(
  ports: ContentResourceManagementPorts<T>, contract: ContentResourceManagementContract<T>,
) {
  const records = ref<T[]>([]), loading = ref(false);
  let loadGeneration = 0;
  const outbox = createResourceOutbox(ports, contract.isRequest, refresh, contract.label);
  async function refresh() {
    const generation = ++loadGeneration;
    loading.value = true;
    try {
      const incoming = await ports.list();
      if (!Array.isArray(incoming) || !incoming.every(contract.isResource))
        throw new Error(`${contract.label}契约无效`);
      if (generation === loadGeneration) records.value = graphClone(incoming) as typeof records.value;
    } catch (failure) {
      if (generation === loadGeneration)
        outbox.error.value = failure instanceof Error ? failure.message : `${contract.label}读取失败`;
    } finally { if (generation === loadGeneration) loading.value = false; }
  }
  async function save(resource: T, expectedSequence: number) {
    if (!contract.isResource(resource) || !Number.isSafeInteger(expectedSequence) || expectedSequence < 0) {
      outbox.error.value = contract.invalidSaveMessage;
      return false;
    }
    const record = graphClone(resource);
    record.update_sequence = expectedSequence + 1;
    const request = { record, expected_sequence: expectedSequence, idempotency_key: crypto.randomUUID() };
    if (!contract.isRequest(request)) {
      outbox.error.value = contract.invalidSaveMessage;
      return false;
    }
    return outbox.send(request);
  }
  return { records, loading, ...outbox, refresh, save };
}
