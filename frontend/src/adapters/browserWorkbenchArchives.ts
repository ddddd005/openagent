import type { DraftStorage } from "./browserStorage";
import { createLocalDraftChannel, listLocalDrafts, localDraftPorts,
  type LocalDraftAddress, type LocalDraftPorts, type LocalDraftRecord } from "./localDraftsApi";
import { WORKBENCH_RECOVERY_STORAGE_KEY, WORKBENCH_STORAGE_KEY } from "./workbenchPersistence";
import { graphJsonSignature, graphObject } from "../domain/workflowGraph";

const sources = [
  { key: WORKBENCH_STORAGE_KEY, namespace: "original", label: "原浏览器工作台" },
  { key: WORKBENCH_RECOVERY_STORAGE_KEY, namespace: "isolated", label: "旧隔离工作台" },
] as const;
const prefix = "workbench:browser-archive:";
export interface BrowserWorkbenchArchive {
  schema_version: 1;
  kind: "workflow.browser-workbench-archive";
  source_key: string;
  raw_sha256: string;
  raw: string;
}
export async function browserRecordDigest(raw: string) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}
function address(source: typeof sources[number], digest: string): LocalDraftAddress {
  return { scope: "management", kind: "form", key: `${prefix}${source.namespace}:${digest}`,
    workflow_definition_id: null, workflow_session_id: null };
}
export function browserArchiveValue(record: LocalDraftRecord): BrowserWorkbenchArchive {
  const value = record.value;
  if (record.deleted || record.format_version !== 1 || !graphObject(value)
    || Object.keys(value).length !== 5 || value.schema_version !== 1
    || value.kind !== "workflow.browser-workbench-archive" || typeof value.raw !== "string"
    || typeof value.raw_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(value.raw_sha256))
    throw new Error("旧版存档格式不兼容，原文未覆盖");
  const source = sources.find(row => row.key === value.source_key);
  if (!source || graphJsonSignature(record.address) !== graphJsonSignature(address(source, value.raw_sha256)))
    throw new Error("旧版存档身份不匹配，原文未覆盖");
  return value as unknown as BrowserWorkbenchArchive;
}
export function browserArchiveSource(key: string) {
  return sources.find(row => row.key === key)?.label ?? "旧版存档";
}
export async function listBrowserWorkbenchArchives() {
  const records = (await listLocalDrafts({ kind: "form" }))
    .filter(row => !row.deleted && row.address.key.startsWith(prefix));
  for (const record of records) {
    const value = browserArchiveValue(record);
    if (await browserRecordDigest(value.raw) !== value.raw_sha256)
      throw new Error("旧版存档内容校验失败，原文未覆盖");
  }
  return records.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

export function createBrowserWorkbenchArchiver(ports: LocalDraftPorts = localDraftPorts) {
  const channels = new Map<string, ReturnType<typeof createLocalDraftChannel>>();
  async function archive(storage: DraftStorage) {
    if (!storage.removeItem) throw new Error("旧浏览器存储不支持归档清理，原文仍保留");
    let count = 0;
    for (const source of sources) {
      const raw = storage.getItem(source.key);
      if (raw === null) continue;
      const digest = await browserRecordDigest(raw), target = address(source, digest);
      let channel = channels.get(target.key);
      if (!channel) { channel = createLocalDraftChannel(target, ports); channels.set(target.key, channel); }
      const value: BrowserWorkbenchArchive = { schema_version: 1, kind: "workflow.browser-workbench-archive",
        source_key: source.key, raw_sha256: digest, raw };
      const existing = await channel.read();
      if (existing && !existing.deleted
        && graphJsonSignature(browserArchiveValue(existing)) !== graphJsonSignature(value))
        throw new Error("旧版存档内容与原记录不一致，未覆盖任何一方");
      // Unknown writes are settled by the channel's original receipt, never by replaying a workflow.
      await channel.write(value);
      if (storage.getItem(source.key) !== raw)
        throw new Error("其他页面更新了浏览器旧记录，已归档原快照，未清理新记录");
      storage.removeItem(source.key);
      if (storage.getItem(source.key) !== null)
        throw new Error("浏览器旧记录清理未完成，本机存档已保留");
      count++;
    }
    return count;
  }
  return { archive };
}
