import { computed, ref } from "vue";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import { WorkbenchApiError } from "../adapters/workbenchApi";

export interface ResourceOutboxPorts<T> {
  save(request: T): Promise<unknown>;
  readReceipt(request: T): Promise<unknown>;
  readPending(): string | null | Promise<string | null>;
  writePending(value: string | null): void | Promise<void>;
  readLegacyPending?(): string | null;
  archivePending?(value: string): Promise<void>;
}
export type RequestProgress = "ready" | "restored" | "evidence-writing" | "evidence-unknown"
  | "request-in-flight" | "result-unknown" | "business-confirmed" | "acceptance-failed"
  | "closure-failed" | "completed" | "rejected" | "blocked";
export function createResourceOutbox<T>(ports: ResourceOutboxPorts<T>, valid: (value: unknown) => value is T,
  refresh: (request: T | null) => Promise<void>, label: string) {
  const pending = ref<T | null>(null);
  const completed = ref<T | null>(null);
  const error = ref("");
  const busy = ref(false), initialized = ref(false), blocked = ref(false);
  const progress = ref<RequestProgress>("ready");
  const legacyRaw = ref<string | null>(null);
  const locked = computed(() => busy.value || !initialized.value || blocked.value || !!pending.value || legacyRaw.value !== null);
  let generation = 0;
  let completionHandler: ((request: T) => Promise<boolean>) | null = null;
  function setCompletionHandler(handler: (request: T) => Promise<boolean>) {
    completionHandler = handler;
    return () => { if (completionHandler === handler) completionHandler = null; };
  }
  let initializing: Promise<void> | null = null;
  function acceptSaved(raw: string | null) {
    if (raw !== null) {
      const request: unknown = JSON.parse(raw);
      if (!valid(request)) throw new Error("Invalid resource intent");
      pending.value = graphClone(request) as typeof pending.value;
      progress.value = "restored";
    }
    legacyRaw.value = pending.value ? null : ports.readLegacyPending?.() ?? null;
    if (legacyRaw.value !== null) error.value = "旧浏览器原请求仅供核实回执，原文保留；不会恢复旧资源或重新提交";
    initialized.value = true;
  }
  function failedInitialization() {
    blocked.value = true;
    progress.value = "blocked";
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
  async function reloadPending() {
    if (busy.value) return false;
    try {
      const raw = await ports.readPending();
      if (busy.value) return false;
      pending.value = null;
      blocked.value = false;
      acceptSaved(raw);
      if (raw === null) progress.value = "ready";
      error.value = "";
      return true;
    } catch {
      failedInitialization();
      return false;
    }
  }
  async function submit(request: T, reconcile = false): Promise<boolean> {
    const original = graphClone(request), identity = graphJsonSignature(original), version = ++generation;
    const acknowledge = completionHandler;
    const locallyCurrent = () => version === generation && pending.value !== null && graphJsonSignature(pending.value) === identity;
    const current = async () => {
      if (!locallyCurrent()) return false;
      try {
        const durable = await ports.readPending();
        return locallyCurrent() && (durable === null || graphJsonSignature(JSON.parse(durable)) === identity);
      } catch { return false; }
    };
    busy.value = true; error.value = "";
    progress.value = "request-in-flight";
    try {
      await (reconcile ? ports.readReceipt : ports.save)(graphClone(original));
      progress.value = "business-confirmed";
      if (!await current()) return false;
      if (acknowledge && !await acknowledge(graphClone(original))) {
        progress.value = "acceptance-failed";
        throw new Error("已收到提交回执，但当前表单尚未确认；请核实原请求");
      }
      if (!await current()) return false;
      try { await ports.writePending(null); }
      catch (cause) {
        progress.value = "closure-failed";
        throw new Error(`${label}业务结果已确认，原请求证据结案失败：${
          cause instanceof Error ? cause.message : String(cause)}`, { cause });
      }
      if (!locallyCurrent()) return false;
      pending.value = null;
      completed.value = graphClone(original) as typeof completed.value;
      progress.value = "completed";
      // Refresh runs only after the durable original request has been closed;
      // completion handlers must not delete verification entries before this.
      await refresh(graphClone(original) as T);
      return true;
    } catch (failure) {
      if (!await current()) return false;
      error.value = failure instanceof Error ? failure.message : `${label}保存失败`;
      if (progress.value === "request-in-flight") progress.value = "result-unknown";
      if (!reconcile && failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code !== "idempotency_conflict") {
        try {
          await ports.writePending(null);
          if (locallyCurrent()) { pending.value = null; progress.value = "rejected"; }
        } catch { progress.value = "closure-failed"; error.value = "业务请求已明确拒绝，原请求证据结案失败"; }
      }
      return false;
    } finally { if (version === generation) busy.value = false; }
  }
  async function send(request: T) {
    if (!initialized.value && !await initialize()) return false;
    if (blocked.value || locked.value || !valid(request)) return false;
    const original = graphClone(request);
    busy.value = true;
    progress.value = "evidence-writing";
    try { await ports.writePending(JSON.stringify(original)); }
    catch (failure) {
      error.value = `无法保存原${label}请求，未提交`;
      progress.value = "evidence-unknown";
      if (failure instanceof WorkbenchApiError && failure.kind === "unknown") {
        try {
          const raw = await ports.readPending();
          if (raw !== null) {
            const durable: unknown = JSON.parse(raw);
            pending.value = valid(durable) ? graphClone(durable) as typeof pending.value
              : original as typeof pending.value;
          }
        } catch {
          pending.value = original as typeof pending.value;
          error.value = `原${label}请求证据仍待核实，业务未提交`;
        }
      }
      busy.value = false; return false;
    }
    pending.value = original as typeof pending.value;
    return submit(original);
  }
  async function reconcile() {
    if (!initialized.value && !await initialize()) return false;
    if (blocked.value || !pending.value || busy.value) return false;
    return submit(graphClone(pending.value) as T, true);
  }
  async function abandon(confirmed: boolean) {
    if (!confirmed || !await initialize() || !pending.value || busy.value || !ports.archivePending) return false;
    const original = graphClone(pending.value) as T, identity = graphJsonSignature(original);
    const version = ++generation;
    const current = () => version === generation && pending.value !== null
      && graphJsonSignature(pending.value) === identity;
    busy.value = true;
    try {
      const raw = await ports.readPending();
      if (!current() || raw === null || graphJsonSignature(JSON.parse(raw)) !== identity)
        throw new Error("原请求已被其他页面修改，未放弃");
      await ports.archivePending(raw);
      const durable = await ports.readPending();
      if (!current() || durable === null || graphJsonSignature(JSON.parse(durable)) !== identity)
        throw new Error("归档后原请求已变化，未清理");
      await ports.writePending(null);
      if (!current()) return false;
      pending.value = null;
      error.value = "原请求已归档并解除本机锁定；不表示撤销已发生的保存，未重新提交";
      await refresh(null);
      return true;
    } catch (failure) {
      if (current()) error.value = failure instanceof Error ? failure.message : "原请求归档失败，原记录仍保留";
      return false;
    } finally { if (version === generation) busy.value = false; }
  }
  async function reconcileLegacyPending() {
    if (!await initialize() || busy.value || pending.value || legacyRaw.value === null) return false;
    let request: unknown;
    try { request = JSON.parse(legacyRaw.value); }
    catch { error.value = "旧浏览器原请求无法读取，原文未覆盖"; return false; }
    if (!valid(request)) { error.value = "旧浏览器原请求格式无效，原文未覆盖"; return false; }
    busy.value = true;
    try {
      await ports.readReceipt(graphClone(request) as T);
      error.value = "旧原请求回执已核实，原文仍保留；未恢复旧资源、表单或重新提交";
      return true;
    } catch (failure) { error.value = failure instanceof Error ? failure.message : "旧原请求回执无法核实，原文仍保留"; return false; }
    finally { busy.value = false; }
  }
  return { pending, completed, error, busy, locked, progress, initialize, reloadPending, send, reconcile, abandon,
    setCompletionHandler, legacyRaw, reconcileLegacyPending };
}
