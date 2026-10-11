import { hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { graphClone } from "../domain/workflowGraph";
import { isPromptIdentity, samePromptIdentity, type PromptIdentity } from "../domain/workflowPromptResources";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { isContentMetadata, isContentMetadataSaveRequest, readContentMetadata, readContentMetadataReceipt,
  saveContentMetadata, validContentDisplayName, validateContentMetadataReceipt,
  type ContentMetadata, type ContentMetadataSaveRequest } from "../adapters/contentMetadataApi";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

export interface ContentMetadataPorts extends ResourceOutboxPorts<ContentMetadataSaveRequest> {
  read(reference: PromptIdentity): Promise<ContentMetadata>;
}
export function createContentMetadata(portsFor: (reference: PromptIdentity) => ContentMetadataPorts) {
  const entries = new Map<string, ReturnType<typeof createEntry>>();
  function createEntry(reference: PromptIdentity) {
    const target = graphClone(reference), ports = portsFor(target);
    const record = ref<ContentMetadata | null>(null), loading = ref(false), readError = ref("");
    let generation = 0, loaded = false, reading: Promise<void> | null = null;
    const validRequest = (value: unknown): value is ContentMetadataSaveRequest =>
      isContentMetadataSaveRequest(value) && samePromptIdentity(value.reference, target);
    const accept = async (request: ContentMetadataSaveRequest, receipt = false) => {
      const result = await (receipt ? ports.readReceipt : ports.save)(request);
      const accepted = validateContentMetadataReceipt(result, request);
      generation++;
      record.value = graphClone(accepted);
      loaded = true; loading.value = false; readError.value = "";
      return result;
    };
    const outbox = createResourceOutbox({ ...ports, save: request => accept(request),
      readReceipt: request => accept(request, true) }, validRequest, async () => {}, "提示词名称");
    async function refresh() {
      const current = ++generation;
      loading.value = true;
      try {
        const value = await ports.read(graphClone(target));
        if (!isContentMetadata(value) || !samePromptIdentity(value.reference, target))
          throw new Error("内容名称读取身份或字段无效");
        if (current === generation) { record.value = graphClone(value); loaded = true; readError.value = ""; }
      } catch (failure) {
        if (current === generation) readError.value = failure instanceof Error ? failure.message : "内容名称读取失败";
      } finally { if (current === generation) loading.value = false; }
    }
    async function load() {
      if (loaded) return;
      reading ??= refresh().finally(() => { reading = null; });
      await reading;
    }
    async function save(displayName: string) {
      if (outbox.locked.value || loading.value || readError.value || !record.value) return false;
      if (!validContentDisplayName(displayName)) { outbox.error.value = "名称最多 128 个字符，尚未提交"; return false; }
      const display_name = displayName.trim();
      if (display_name === record.value.display_name) return true;
      return outbox.send({ reference: graphClone(target), display_name,
        expected_metadata_sequence: record.value.metadata_sequence, idempotency_key: crypto.randomUUID() });
    }
    return { record, loading, readError, ...outbox, refresh, load, save };
  }
  function forReference(reference: PromptIdentity) {
    if (!isPromptIdentity(reference)) throw new Error("内容名称引用无效");
    const key = JSON.stringify([reference.scope, reference.type_id, reference.resource_id]);
    let entry = entries.get(key);
    if (!entry) { entry = createEntry(reference); entries.set(key, entry); }
    return entry;
  }
  return { forReference };
}
export const contentMetadataKey: InjectionKey<ReturnType<typeof createContentMetadata>> = Symbol("workflow.content.metadata");
let installed: ReturnType<typeof createContentMetadata> | undefined;
export function useContentMetadata() {
  const provided = hasInjectionContext() ? inject(contentMetadataKey, null) : null;
  if (provided) return provided;
  return installed ??= createContentMetadata(reference => {
    const key = `content-metadata:${JSON.stringify([reference.scope, reference.type_id, reference.resource_id])}`;
    const { readPending, writePending, archivePending } = createResourcePendingStorage(key, "", "content.metadata.update");
    return { read: readContentMetadata, save: saveContentMetadata, readReceipt: readContentMetadataReceipt,
      readPending, writePending, archivePending };
  });
}
