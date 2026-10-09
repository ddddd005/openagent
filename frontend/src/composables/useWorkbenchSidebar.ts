import { ref } from "vue";
import type { DraftStorage } from "../adapters/browserStorage";

export const sidebarPreferenceKey = "workflow-workbench:sidebar-collapsed:v1";

function browserStorage(): DraftStorage | undefined {
  try { return globalThis.localStorage; }
  catch { return undefined; }
}

export function useWorkbenchSidebar(storage: DraftStorage | undefined = browserStorage()) {
  let savedCollapsed = false;
  try { savedCollapsed = storage?.getItem(sidebarPreferenceKey) === "true"; }
  catch { /* Layout preferences must not block the workbench. */ }
  const sidebarCollapsed = ref(savedCollapsed);

  function setCollapsed(value: boolean) {
    if (sidebarCollapsed.value === value) return;
    sidebarCollapsed.value = value;
    try { storage?.setItem(sidebarPreferenceKey, String(value)); }
    catch { /* The current layout still works when browser storage is unavailable. */ }
  }

  return {
    sidebarCollapsed,
    toggleSidebar: () => setCollapsed(!sidebarCollapsed.value),
    expandSidebar: () => setCollapsed(false),
  };
}
