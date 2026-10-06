import { describe, expect, it } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import CanvasNodeMenu from "./CanvasNodeMenu.vue";
import type { CanvasMenuGroup } from "../domain/canvasMenu";

async function renderMenu(view: "context" | "add", groups: CanvasMenuGroup[]) {
  const context: { teleports?: Record<string, string> } = {};
  await renderToString(createSSRApp(CanvasNodeMenu, {
    state: { x: 100, y: 100, view }, groups,
  }), context);
  return context.teleports?.body ?? "";
}

describe("canvas menu rendering", () => {
  it("renders the custom menu action and its shortcut metadata", async () => {
    const html = await renderMenu("context", [{ items: [{ id: "prompt-item", label: "提示词条目" }] }]);
    expect(html).toContain('aria-label="画布菜单"');
    expect(html).toContain('aria-keyshortcuts="Shift+A"');
    expect(html).toContain("添加节点");
  });

  it("renders stage groups and disables unique or unavailable node types", async () => {
    const html = await renderMenu("add", [{
      label: "Agent B",
      items: [
        { id: "B:assemble", label: "提示词装配", disabled: true },
        { id: "B:prompt-item", label: "提示词条目" },
      ],
    }]);
    expect(html).toContain("Agent B");
    expect(html).toMatch(/disabled[^>]*>提示词装配/);
    expect(html).toContain("提示词条目");
  });

  it("keeps an unavailable add action visible on the empty workflow", async () => {
    const html = await renderMenu("add", []);
    expect(html).toContain("添加节点");
    expect(html).toContain("disabled");
    expect(html).toContain("空工作流暂不支持节点编辑");
  });
});
