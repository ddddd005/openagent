import { hasInjectionContext, inject, ref, type InjectionKey } from "vue";
import { graphClone, graphUuid } from "../domain/workflowGraph";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { isSessionMetadata, isSessionMetadataSaveRequest, readSessionMetadata, readSessionMetadataReceipt,
  saveSessionMetadata, validSessionName, validateSessionMetadataResult,
  type SessionMetadata, type SessionMetadataSaveRequest } from "../adapters/sessionMetadataApi";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";

export interface SessionMetadataPorts extends ResourceOutboxPorts<SessionMetadataSaveRequest> {
  read(sessionId: string): Promise<SessionMetadata>;
}
export function createSessionMetadata(portsFor: (sessionId: string) => SessionMetadataPorts) {
  const entries = new Map<string, ReturnType<typeof createEntry>>();
  function createEntry(sessionId: string) {
    const ports = portsFor(sessionId);
    const record = ref<SessionMetadata | null>(null), loading = ref(false), readError = ref("");
    let generation = 0, loaded = false, reading: Promise<void> | null = null;
    function observe(value: SessionMetadata) {
      if (!isSessionMetadata(value) || value.workflow_session_id !== sessionId) return;
      if (!record.value || value.metadata_sequence > record.value.metadata_sequence)
        record.value = graphClone(value);
    }
    async function accept(request: SessionMetadataSaveRequest, reconcile = false) {
      const result = validateSessionMetadataResult(await (reconcile ? ports.readReceipt : ports.save)(request), request);
      observe(result);
      return result;
    }
    const outbox = createResourceOutbox({ ...ports, save: request => accept(request),
      readReceipt: request => accept(request, true) },
    (value: unknown): value is SessionMetadataSaveRequest =>
      isSessionMetadataSaveRequest(value) && value.session_id === sessionId, refresh, "会话名称");
    async function refresh() {
      const current = ++generation;
      loading.value = true;
      try {
        const value = await ports.read(sessionId);
        if (!isSessionMetadata(value) || value.workflow_session_id !== sessionId)
          throw new Error("会话名称读取身份或字段无效");
        if (current === generation) { observe(value); loaded = true; readError.value = ""; }
      } catch (failure) {
        if (current === generation) readError.value = failure instanceof Error ? failure.message : "会话名称读取失败";
      } finally { if (current === generation) loading.value = false; }
    }
    async function load() {
      if (loaded) return;
      reading ??= refresh().finally(() => { reading = null; });
      await reading;
    }
    async function save(name: string) {
      if (outbox.locked.value || loading.value || readError.value || !record.value) return false;
      if (!validSessionName(name)) { outbox.error.value = "名称最多 128 个字符，尚未提交"; return false; }
      if (name.trim() === record.value.name) return true;
      return outbox.send({ session_id: sessionId, name,
        expected_metadata_sequence: record.value.metadata_sequence, idempotency_key: crypto.randomUUID() });
    }
    return { record, loading, readError, ...outbox, observe, refresh, load, save };
  }
  function forSession(sessionId: string) {
    if (!graphUuid(sessionId)) throw new Error("会话名称目标无效");
    let entry = entries.get(sessionId);
    if (!entry) { entry = createEntry(sessionId); entries.set(sessionId, entry); }
    return entry;
  }
  return { forSession };
}
export const sessionMetadataKey: InjectionKey<ReturnType<typeof createSessionMetadata>> = Symbol("workflow.session.metadata");
let installed: ReturnType<typeof createSessionMetadata> | undefined;
export function useSessionMetadata() {
  const provided = hasInjectionContext() ? inject(sessionMetadataKey, null) : null;
  return provided ?? (installed ??= createSessionMetadata(sessionId => ({
    read: readSessionMetadata, save: saveSessionMetadata, readReceipt: readSessionMetadataReceipt,
    ...createResourcePendingStorage(`session-metadata:${sessionId}`, "", "session.metadata.update"),
  })));
}
