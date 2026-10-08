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

  it("renders categories without exposing the family leaves or version list at the first level", async () => {
    const html = await renderMenu("add", [{
      id: "agents", label: "Agent",
      items: [
        { id: "agents.execute", label: "Agent 执行", disabled: true },
        { id: "agents.delta", label: "Agent 增量" },
      ],
    }]);
    expect(html).toContain('data-group-id="agents"');
    expect(html).toContain('aria-haspopup="menu" aria-expanded="false"');
    expect(html).toContain(">2</small>");
    expect(html).not.toContain("Agent 执行");
    expect(html).not.toContain("Agent 增量");
  });

  it("keeps an unavailable add action visible on the empty workflow", async () => {
    const html = await renderMenu("add", []);
    expect(html).toContain("添加节点");
    expect(html).toContain("disabled");
    expect(html).toContain("节点目录不可用");
  });
});
