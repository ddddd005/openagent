import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { workbenchRequest, WorkbenchApiError } from "../adapters/workbenchApi";
import { clonePreparation } from "../domain/preparation";
import { isDataDefinition, isGlobalContent, type GlobalContent, type SessionDataDefinition } from "../domain/workbenchResources";

const KEY = "workflow-workbench:content-drafts:v1";
interface ContentMutation { record: GlobalContent; expected_revision: number; idempotency_key: string }
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, child]) => `${JSON.stringify(key)}:${stable(child)}`).join(",")}}`;
  return JSON.stringify(value);
}
export const useGlobalContentStore = defineStore("global-content", () => {
  const records = ref<GlobalContent[]>([]);
  const drafts = ref<Record<string, GlobalContent>>({});
  const selectedId = ref<string | null>(null);
  const definitions = ref<SessionDataDefinition[]>([]);
  const pending = ref<ContentMutation | null>(null);
  const busy = ref(false);
  const error = ref<string | null>(null);
  const version = ref(0);
  let initialized = false;
  let expectedRaw: string | null = null;
  let blocked = false;
  let alive = true;
  let mutationGeneration = 0;
  let loadGeneration = 0;
  const controller = new AbortController();
  onScopeDispose(() => { alive = false; mutationGeneration++; loadGeneration++; controller.abort(); });
  const selected = computed(() => selectedId.value ? drafts.value[selectedId.value] ?? null : null);
  const dirty = computed(() => !!selected.value && JSON.stringify(selected.value)
    !== JSON.stringify(records.value.find(r => r.resource_id === selectedId.value)));
  function persist() {
    if (!alive || !initialized || blocked) return false;
    const generation = mutationGeneration;
    const fingerprint = stable(pending.value);
    const current = () => alive && generation === mutationGeneration && stable(pending.value) === fingerprint;
    try {
      const storage = window.localStorage;
      if (storage.getItem(KEY) !== expectedRaw) throw new Error("其他页面更新了内容草稿");
      if (!current()) return false;
      const raw = JSON.stringify({ schemaVersion: 1, drafts: drafts.value, selectedId: selectedId.value, pending: pending.value });
      if (raw.length > 2_000_000) throw new Error("内容草稿超出容量");
      storage.setItem(KEY, raw);
      if (!current()) return false;
      expectedRaw = raw;
      return true;
    } catch (failure) {
      if (current()) {
        blocked = true;
        error.value = failure instanceof Error ? failure.message : "内容草稿保存失败";
      }
      return false;
    }
  }
  async function initialize() {
    if (!initialized) {
      mutationGeneration++;
      try {
        expectedRaw = window.localStorage.getItem(KEY);
        if (expectedRaw !== null) {
          const value = JSON.parse(expectedRaw);
          if (expectedRaw.length > 2_000_000 || value.schemaVersion !== 1 || typeof value.drafts !== "object" || !value.drafts
            || Array.isArray(value.drafts) || !Object.entries(value.drafts).every(([id, record]) =>
              isGlobalContent(record) && record.resource_id === id)
            || !(value.selectedId === null || typeof value.selectedId === "string"
              && Object.hasOwn(value.drafts, value.selectedId))
            || value.pending !== null && (!isGlobalContent(value.pending.record)
              || !Number.isSafeInteger(value.pending.expected_revision) || value.pending.expected_revision < 0
              || value.pending.record.revision !== value.pending.expected_revision + 1
              || typeof value.pending.idempotency_key !== "string"
              || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.pending.idempotency_key)))
            throw new Error("内容恢复记录不兼容，原记录未覆盖");
          drafts.value = value.drafts;
          selectedId.value = value.selectedId;
          pending.value = value.pending;
        }
      } catch (failure) { blocked = true; error.value = failure instanceof Error ? failure.message : "内容恢复失败"; }
      initialized = true;
    }
    await load();
  }
  async function load() {
    const generation = ++loadGeneration;
    busy.value = true;
    try {
      const rows = await workbenchRequest<unknown>("/api/global-content", { signal: controller.signal });
      if (!alive || generation !== loadGeneration) return;
      const data = await workbenchRequest<unknown>("/api/session-data/definitions", { signal: controller.signal });
      if (!alive || generation !== loadGeneration) return;
      if (!Array.isArray(rows) || !rows.every(isGlobalContent) || !Array.isArray(data) || !data.every(isDataDefinition))
        throw new WorkbenchApiError("unavailable", "内容或会话定义的读取契约无效");
      for (const saved of rows) {
        const previous = records.value.find(record => record.resource_id === saved.resource_id);
        if (previous && stable(drafts.value[saved.resource_id]) === stable(previous)
          && pending.value?.record.resource_id !== saved.resource_id)
          drafts.value[saved.resource_id] = clonePreparation(saved);
      }
      records.value = rows;
      definitions.value = data;
      version.value++;
      if (!blocked) error.value = null;
      if (!selectedId.value && rows[0]) select(rows[0].resource_id);
    } catch (failure) { if (alive && generation === loadGeneration) error.value = failure instanceof Error ? failure.message : "内容读取失败"; }
    finally { if (alive && generation === loadGeneration) busy.value = false; }
  }
  function select(id: string) {
    const record = records.value.find(r => r.resource_id === id);
    if (!drafts.value[id] && record) drafts.value[id] = clonePreparation(record);
    if (drafts.value[id]) selectedId.value = id;
  }
  function create(kind: GlobalContent["kind"]) {
    if (pending.value || blocked) return;
    const id = crypto.randomUUID();
    drafts.value[id] = {
      schema_version: 1, kind, resource_id: id, revision: 1,
      name: kind === "role_card" ? "新角色卡" : "新全局提示词", enabled: true,
      members: [{ id: crypto.randomUUID(), name: "正文", text: "", role: "system",
        placement: "before", depth: null, order: 0, enabled: true }],
    };
    selectedId.value = id;
  }
  function discard() {
    if (!selectedId.value || pending.value) return;
    const record = records.value.find(r => r.resource_id === selectedId.value);
    if (record) drafts.value[selectedId.value] = clonePreparation(record);
    else {
      delete drafts.value[selectedId.value];
      selectedId.value = null;
      if (records.value[0]) select(records.value[0].resource_id);
    }
  }
  async function save(replay = false) {
    if (replay) {
      if (pending.value && !busy.value) error.value = "旧内容请求缺少可核实的原应用身份，保留原请求且不重新提交";
      return;
    }
    if (!alive || busy.value || blocked || pending.value) return;
    const record = selected.value;
    if (!record) return;
    const head = records.value.find(r => r.resource_id === record.resource_id);
    if (head && head.revision !== record.revision) {
      error.value = "全局内容已在外部更新，当前草稿基线过期；请核对后重新编辑";
      return;
    }
    const expected = head ? record.revision : 0;
    const command: ContentMutation = { record: { ...clonePreparation(record), revision: expected + 1 },
      expected_revision: expected, idempotency_key: crypto.randomUUID() };
    const generation = mutationGeneration;
    const fingerprint = stable(command);
    const draftFingerprint = stable(drafts.value[command.record.resource_id]);
    const current = () => alive && generation === mutationGeneration && stable(pending.value) === fingerprint;
    const settled = () => alive && generation === mutationGeneration && pending.value === null;
    if (!isGlobalContent(command.record)
      || new TextEncoder().encode(JSON.stringify(command)).byteLength > 64 * 1024) {
      error.value = "全局内容字段无效或超过容量，尚未提交";
      return;
    }
    pending.value = clonePreparation(command);
    if (!current()) return;
    const persisted = persist();
    if (!current()) return;
    if (!persisted) {
      pending.value = null;
      return;
    }
    function clearPending() {
      if (!current()) return false;
      pending.value = null;
      if (!settled()) return false;
      const cleared = persist();
      if (!settled()) return false;
      if (!cleared) {
        pending.value = clonePreparation(command);
        throw new WorkbenchApiError("unknown", "原内容请求的本机结算保存失败，保留请求等待核实");
      }
      return true;
    }
    busy.value = true;
    error.value = null;
    try {
      const saved = await workbenchRequest<unknown>("/api/global-content", { body: command, signal: controller.signal });
      if (!current()) return;
      if (!isGlobalContent(saved) || stable(saved) !== stable(command.record))
        throw new WorkbenchApiError("unknown", "内容提交结果待核实");
      records.value = [...records.value.filter(r => r.resource_id !== saved.resource_id), saved];
      if (!current()) return;
      if (stable(drafts.value[saved.resource_id]) === draftFingerprint)
        drafts.value[saved.resource_id] = clonePreparation(saved);
      if (!current() || !clearPending()) return;
      version.value++;
    } catch (failure) {
      if (!current()) return;
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected" && failure.code === "idempotency_conflict")
        failure = new WorkbenchApiError("unknown", "原内容请求身份冲突，无法证明未发生，保留原请求等待核实", failure.status, failure.code);
      else if (failure instanceof WorkbenchApiError && failure.kind === "rejected") {
        try { if (!clearPending()) return; }
        catch (persistenceFailure) { failure = persistenceFailure; }
      }
      if (!current() && !settled()) return;
      error.value = failure instanceof Error ? failure.message : "内容提交结果待核实";
    } finally { if (current() || settled()) busy.value = false; }
  }
  async function remove() {
    const record = records.value.find(r => r.resource_id === selectedId.value);
    if (!record || busy.value || pending.value || blocked) return;
    busy.value = true;
    try {
      await workbenchRequest(`/api/global-content/${record.resource_id}/delete`, { body: { expected_revision: record.revision } });
      records.value = records.value.filter(r => r.resource_id !== record.resource_id);
      delete drafts.value[record.resource_id];
      selectedId.value = null;
      version.value++;
    } catch (failure) { error.value = failure instanceof Error ? failure.message : "内容删除失败"; }
    finally { busy.value = false; }
  }
  watch(() => JSON.stringify([drafts.value, selectedId.value, pending.value]), persist, { flush: "sync" });
  return { records, drafts, selectedId, selected, definitions, dirty, pending, busy, error, version,
    initialize, load, select, create, discard, save, remove };
});
