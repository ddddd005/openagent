import { afterEach, describe, expect, it, vi } from "vitest";
import { useWorkbenchSidebar, sidebarPreferenceKey } from "./useWorkbenchSidebar";

afterEach(() => vi.unstubAllGlobals());

function storage(saved?: string) {
  const values = new Map<string, string>(saved === undefined ? [] : [[sidebarPreferenceKey, saved]]);
  return {
    values,
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { values.set(key, value); }),
  };
}

describe("workbench sidebar layout preference", () => {
  it("starts expanded without writing or reading workflow records", () => {
    const cache = storage(), sidebar = useWorkbenchSidebar(cache);
    expect(sidebar.sidebarCollapsed.value).toBe(false);
    expect(cache.getItem).toHaveBeenCalledExactlyOnceWith(sidebarPreferenceKey);
    expect(cache.setItem).not.toHaveBeenCalled();
  });

  it("toggles and restores the preference independently of workflow persistence", () => {
    const cache = storage(), sidebar = useWorkbenchSidebar(cache);
    cache.values.set("workflow-workbench:fixed-base:v1", "original workflow");
    sidebar.toggleSidebar();
    expect(sidebar.sidebarCollapsed.value).toBe(true);
    expect(useWorkbenchSidebar(cache).sidebarCollapsed.value).toBe(true);
    sidebar.toggleSidebar();
    expect(sidebar.sidebarCollapsed.value).toBe(false);
    expect(useWorkbenchSidebar(cache).sidebarCollapsed.value).toBe(false);
    expect(cache.values.get("workflow-workbench:fixed-base:v1")).toBe("original workflow");
  });

  it("expands a collapsed panel when navigating, including the already active section", () => {
    const cache = storage("true"), sidebar = useWorkbenchSidebar(cache);
    sidebar.expandSidebar();
    expect(sidebar.sidebarCollapsed.value).toBe(false);
    expect(cache.setItem).toHaveBeenCalledExactlyOnceWith(sidebarPreferenceKey, "false");
    sidebar.expandSidebar();
    expect(cache.setItem).toHaveBeenCalledTimes(1);
  });

  it.each(["false", "", "invalid", "1", '{"collapsed":true}'])(
    "ignores an invalid or expanded preference: %s", saved => {
      const cache = storage(saved);
      expect(useWorkbenchSidebar(cache).sidebarCollapsed.value).toBe(false);
      expect(cache.setItem).not.toHaveBeenCalled();
    },
  );

  it("continues toggling when storage reads and writes fail", () => {
    const cache = {
      getItem: vi.fn(() => { throw new Error("storage read blocked"); }),
      setItem: vi.fn(() => { throw new Error("storage write blocked"); }),
    };
    const sidebar = useWorkbenchSidebar(cache);
    expect(sidebar.sidebarCollapsed.value).toBe(false);
    expect(() => sidebar.toggleSidebar()).not.toThrow();
    expect(sidebar.sidebarCollapsed.value).toBe(true);
    expect(() => sidebar.expandSidebar()).not.toThrow();
    expect(sidebar.sidebarCollapsed.value).toBe(false);
  });

  it("supports absent storage and a blocked browser storage accessor", () => {
    vi.stubGlobal("localStorage", undefined);
    const sidebar = useWorkbenchSidebar();
    sidebar.toggleSidebar();
    expect(sidebar.sidebarCollapsed.value).toBe(true);
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new Error("browser storage blocked"); },
    });
    expect(() => useWorkbenchSidebar().toggleSidebar()).not.toThrow();
  });
});
