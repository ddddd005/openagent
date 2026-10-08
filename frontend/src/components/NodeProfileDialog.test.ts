import { describe, expect, it } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import NodeProfileDialog from "./NodeProfileDialog.vue";
import { buildNodeFamilies } from "../domain/nodeCatalog";
import type { GraphNodeType } from "../domain/workflowGraph";

const definition = (id: string, version: string, schema = 1): GraphNodeType => ({
  component_id: id, component_version: version, display_name: "Original", category: "Test",
  config_schema: {}, default_config: {}, inputs: [{ port_id: "prompt", data_type: "PROMPT", data_schema_version: schema,
    required: true, multiple: false }], outputs: [{ port_id: "result", data_type: "TEXT", required: false, multiple: false }],
  is_output: false, executable: true,
});
async function render(props: InstanceType<typeof NodeProfileDialog>["$props"]) {
  const context: { teleports?: Record<string, string> } = {};
  await renderToString(createSSRApp(NodeProfileDialog, { ...props }), context);
  return context.teleports?.body ?? "";
}
describe("node profile selection", () => {
  it("starts on the explicit preferred native DeepSeek declaration and previews the real contract", async () => {
    const families = buildNodeFamilies([definition("agents.execute", "8", 6), definition("agents.execute", "3", 5),
      definition("agents.execute", "4", 6)]);
    const html = await render({ families, familyId: "agents.execute" });
    expect(html).toContain("添加Agent 执行");
    expect(html).toContain("agents.execute@4");
    expect(html).toContain("PROMPT@6");
    expect(html).not.toContain("高级兼容配置");
    expect(html).not.toContain(">可摘要上下文</button>");
    expect(html).not.toContain('aria-label="精确声明"');
  });
  it("requires reset confirmation when switching current protocol", async () => {
    const families = buildNodeFamilies([definition("agents.execute", "8", 6), definition("agents.execute", "4", 6)]);
    const html = await render({ families, familyId: "agents.execute", currentKey: "agents.execute@4" });
    expect(html).toContain("切换节点配置");
    expect(html).toContain("agents.execute@4");
    expect(html).toContain("PROMPT@6");
    expect(html).toContain("确认重置配置与公开输出");
    expect(html).toMatch(/type="submit"[^>]*disabled/);
  });
  it("preselects the only installed current declaration without a version picker", async () => {
    for (const [id, version] of [["plugin.node", "42"], ["models.source", "4"]]) {
      const html = await render({ families: buildNodeFamilies([definition(id!, version!)]), familyId: id! });
      expect(html).not.toContain('aria-label="精确声明"');
      expect(html).toContain('aria-label="节点契约"');
      expect(html).not.toMatch(/type="submit"[^>]*disabled/);
    }
  });
  it("blocks stale confirmation and keeps unavailable existing families explicit", async () => {
    const families = buildNodeFamilies([definition("models.chat", "3")]);
    const blocked = await render({ families, familyId: "models.chat", issue: "节点目录已变化" });
    expect(blocked).toContain('role="alert"');
    expect(blocked).toMatch(/type="submit"[^>]*disabled/);
    const missing = await render({ families, familyId: "plugin.missing", currentKey: "plugin.missing@2" });
    expect(missing).toContain("选择节点");
    expect(missing).not.toContain('aria-label="节点契约"');
    expect(missing).toMatch(/type="submit"[^>]*disabled/);
  });
});
