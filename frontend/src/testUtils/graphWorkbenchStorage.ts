import type { SavedWorkbench, WorkbenchConfiguration } from "../adapters/workbenchPersistence";

export function graphWorkbenchRaw(configuration: WorkbenchConfiguration) {
  const value: SavedWorkbench = { ...configuration, schemaVersion: 7, kind: "graph-workbench",
    revision: 1, savedAt: "2026-10-01T00:00:00.000Z" };
  return JSON.stringify(value);
}
export function graphWorkbenchStorage(configuration: WorkbenchConfiguration) {
  let raw: string | null = graphWorkbenchRaw(configuration);
  return { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
}
