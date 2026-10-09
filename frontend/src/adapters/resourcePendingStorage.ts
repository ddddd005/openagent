import { createLocalDraftChannel } from "./localDraftsApi";
import { graphObject } from "../domain/workflowGraph";

export function createResourcePendingStorage(key: string, legacyKey: string) {
  const channel = createLocalDraftChannel({ scope: "management", kind: "intent", key,
    workflow_definition_id: null, workflow_session_id: null });
  let importedLegacy = false;
  async function readPending() {
    const record = await channel.read(true);
    if (!record || record.deleted) return null;
    if (record.format_version !== 1 || !graphObject(record.value)
      || Object.keys(record.value).length !== 2 || typeof record.value.legacy_imported !== "boolean"
      || !Object.hasOwn(record.value, "request")) throw new Error("本机原资源请求格式无效，原记录未覆盖");
    importedLegacy = record.value.legacy_imported;
    return record.value.request === null ? null : JSON.stringify(record.value.request);
  }
  async function writePending(value: string | null) {
    await channel.write({ request: value === null ? null : JSON.parse(value), legacy_imported: importedLegacy });
  }
  function readLegacyPending() {
    return importedLegacy || typeof localStorage === "undefined" ? null : localStorage.getItem(legacyKey);
  }
  async function importLegacyPending(value: string) {
    await channel.write({ request: JSON.parse(value), legacy_imported: true });
    importedLegacy = true;
  }
  return { readPending, writePending, readLegacyPending, importLegacyPending };
}
