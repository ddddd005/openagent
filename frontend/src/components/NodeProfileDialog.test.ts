import { describe, expect, it } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import NodeProfileDialog from "./NodeProfileDialog.vue";
import { buildNodeFamilies } from "../domain/nodeCatalog";
import type { GraphNodeType } from "../domain/workflowGraph";

const port = (port_id: string, data_type: string, data_schema_version: number, required = true) =>
  ({ port_id, data_type, data_schema_version, required, multiple: false });
const definition = (id: string, version: string): GraphNodeType => {
  const currentAgent = id === "agents.execute" && version === "10";
  const currentModelSource = id === "models.source" && version === "7";
  return {
    component_id: id, component_version: version, display_name: "Original", category: "Test",
    config_schema: {}, default_config: {},
    inputs: currentAgent ? [port("prompt", "PROMPT", 6), port("model", "MODEL_BINDING", 5),
      port("compaction_policy", "CONTEXT_COMPACTION_POLICY", 2, false)] : [],
    outputs: currentAgent ? [port("result", "TEXT", 2), port("receipts", "AGENT_RECEIPTS", 2)]
      : currentModelSource ? [port("output", "MODEL_BINDING", 5)] : [],
    is_output: false, executable: true,
  };
};
async function render(props: InstanceType<typeof NodeProfileDialog>["$props"]) {
  const context: { teleports?: Record<string, string> } = {};
  await renderToString(createSSRApp(NodeProfileDialog, { ...props }), context);
  return context.teleports?.body ?? "";
}
describe("node profile selection", () => {
  it("starts on the exact preferred Agent declaration and previews its current contract", async () => {
    const families = buildNodeFamilies([definition("agents.execute", "10"), definition("agents.execute", "9"),
      definition("agents.execute", "8"), definition("agents.execute", "4")]);
    const html = await render({ families, familyId: "agents.execute" });
    expect(html).toContain("添加Agent 执行");
    expect(html).toContain("agents.execute@10");
    expect(html).toContain("PROMPT@6");
    expect(html).toContain("MODEL_BINDING@5");
    expect(html).toContain("CONTEXT_COMPACTION_POLICY@2");
    expect(html).toContain("TEXT@2");
    expect(html).toContain("AGENT_RECEIPTS@2");
    expect(families[0]?.profiles.map(profile => profile.key)).toEqual(["agents.execute@10"]);
    expect(html).not.toContain("高级兼容配置");
    expect(html).not.toContain(">可摘要上下文</button>");
    expect(html).not.toContain('aria-label="精确声明"');
  });
  it("requires reset confirmation when switching current protocol", async () => {
    const families = buildNodeFamilies([definition("agents.execute", "10"), definition("agents.execute", "9")]);
    const html = await render({ families, familyId: "agents.execute", currentKey: "agents.execute@9" });
    expect(html).toContain("替换节点 / 切换模型协议");
    expect(html).toContain("agents.execute@10");
    expect(html).not.toContain("agents.execute@9");
    expect(html).toContain("PROMPT@6");
    expect(html).toContain("确认重置配置与公开输出");
    expect(html).toMatch(/type="submit"[^>]*disabled/);
  });
  it("preselects the only installed current declaration without a version picker", async () => {
    const plugin = await render({ families: buildNodeFamilies([definition("plugin.node", "42")]), familyId: "plugin.node" });
    expect(plugin).not.toContain('aria-label="精确声明"');
    expect(plugin).toContain('aria-label="节点契约"');
    expect(plugin).not.toMatch(/type="submit"[^>]*disabled/);

    const model = await render({ families: buildNodeFamilies([definition("models.source", "7")]), familyId: "models.source" });
    expect(model).not.toContain('aria-label="精确声明"');
    expect(model).toContain('aria-label="节点契约"');
    expect(model).toContain("models.source@7");
    expect(model).toContain("MODEL_BINDING@5");
    expect(model).not.toMatch(/type="submit"[^>]*disabled/);
    expect(buildNodeFamilies([definition("models.source", "6")])).toEqual([]);
  });
  it("blocks stale confirmation and keeps unavailable existing families explicit", async () => {
    const families = buildNodeFamilies([definition("models.chat", "5"), definition("models.chat", "3")]);
    expect(families.find(row => row.id === "models.chat")?.profiles.map(profile => profile.key))
      .toEqual(["models.chat@5"]);
    const blocked = await render({ families, familyId: "models.chat", issue: "节点目录已变化" });
    expect(blocked).toContain('role="alert"');
    expect(blocked).toMatch(/type="submit"[^>]*disabled/);
    const missing = await render({ families, familyId: "plugin.missing", currentKey: "plugin.missing@2" });
    expect(missing).toContain("选择节点");
    expect(missing).not.toContain('aria-label="节点契约"');
    expect(missing).toMatch(/type="submit"[^>]*disabled/);
  });
});
