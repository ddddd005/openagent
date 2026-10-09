import { computed, ref } from "vue";
import { graphClone } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";

export interface ResourceOutboxPorts<T> {
  save(request: T): Promise<unknown>;
  readReceipt(request: T): Promise<unknown>;
  readPending(): string | null | Promise<string | null>;
  writePending(value: string | null): void | Promise<void>;
  readLegacyPending?(): string | null;
  importLegacyPending?(value: string): Promise<void>;
}
export function createResourceOutbox<T>(ports: ResourceOutboxPorts<T>, valid: (value: unknown) => value is T,
  refresh: () => Promise<void>, label: string) {
  const pending = ref<T | null>(null);
  const error = ref("");
  const busy = ref(false), initialized = ref(false), blocked = ref(false);
  const legacyRaw = ref<string | null>(null);
  const locked = computed(() => busy.value || !initialized.value || blocked.value || !!pending.value || legacyRaw.value !== null);
  let generation = 0;
  let initializing: Promise<void> | null = null;
  function acceptSaved(raw: string | null) {
    if (raw !== null) {
      const request: unknown = JSON.parse(raw);
      if (!valid(request)) throw new Error("Invalid resource intent");
      pending.value = graphClone(request) as typeof pending.value;
    }
    legacyRaw.value = pending.value ? null : ports.readLegacyPending?.() ?? null;
    if (legacyRaw.value !== null) error.value = "发现浏览器中的旧资源请求，请明确导入后核实；未自动提交";
    initialized.value = true;
  }
  function failedInitialization() {
    blocked.value = true;
    initialized.value = true;
    error.value = `原${label}请求无法读取，资源修改已禁用`;
  }
  try {
    const raw = ports.readPending();
    if (raw instanceof Promise) initializing = raw.then(acceptSaved).catch(failedInitialization);
    else acceptSaved(raw);
  } catch { failedInitialization(); }
  async function initialize() {
    if (initializing) await initializing;
    return initialized.value && !blocked.value;
  }
  async function submit(request: T, reconcile = false): Promise<boolean> {
    const original = graphClone(request), identity = JSON.stringify(original), version = ++generation;
    const locallyCurrent = () => version === generation && pending.value !== null && JSON.stringify(pending.value) === identity;
    const current = async () => {
      if (!locallyCurrent()) return false;
      try {
        const durable = await ports.readPending();
        return locallyCurrent() && (durable === null || JSON.stringify(JSON.parse(durable)) === identity);
      } catch { return false; }
    };
    busy.value = true; error.value = "";
    try {
      await (reconcile ? ports.readReceipt : ports.save)(graphClone(original));
      if (!await current()) return false;
      await ports.writePending(null);
      if (!locallyCurrent()) return false;
      pending.value = null;
      await refresh();
      return true;
    } catch (failure) {
      if (!await current()) return false;
      error.value = failure instanceof Error ? failure.message : `${label}保存失败`;
      if (!reconcile && failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code !== "idempotency_conflict") {
        try {
          await ports.writePending(null);
          if (locallyCurrent()) pending.value = null;
        } catch { error.value = "原请求状态无法保存，先核实原请求"; }
      }
      return false;
    } finally { if (version === generation) busy.value = false; }
  }
  async function send(request: T) {
    if (!initialized.value && !await initialize()) return false;
    if (blocked.value || locked.value || !valid(request)) return false;
    const original = graphClone(request);
    busy.value = true;
    try { await ports.writePending(JSON.stringify(original)); }
    catch { error.value = `无法保存原${label}请求，未提交`; busy.value = false; return false; }
    pending.value = original as typeof pending.value;
    return submit(original);
  }
  async function reconcile() {
    if (!initialized.value && !await initialize()) return false;
    if (blocked.value || !pending.value || busy.value) return false;
    return submit(graphClone(pending.value) as T, true);
  }
  async function importLegacyPending() {
    if (!await initialize() || busy.value || pending.value || legacyRaw.value === null || !ports.importLegacyPending) return false;
    let request: unknown;
    try { request = JSON.parse(legacyRaw.value); }
    catch { error.value = "旧浏览器原请求无法读取，原文未覆盖"; return false; }
    if (!valid(request)) { error.value = "旧浏览器原请求格式无效，原文未覆盖"; return false; }
    busy.value = true;
    try {
      await ports.importLegacyPending(legacyRaw.value);
      pending.value = graphClone(request) as typeof pending.value;
      legacyRaw.value = null;
      error.value = "已导入原请求，请核实回执；未重新提交";
      return true;
    } catch (failure) { error.value = failure instanceof Error ? failure.message : "原请求导入失败"; return false; }
    finally { busy.value = false; }
  }
  return { pending, error, busy, locked, initialize, send, reconcile, legacyRaw, importLegacyPending };
}
