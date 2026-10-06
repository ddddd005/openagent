import { describe, expect, it } from "vitest";
import { createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { createPreparationNode } from "../domain/preparation";
import IncrementalNodeFields from "./IncrementalNodeFields.vue";

async function render(kind: Parameters<typeof createPreparationNode>[0], hasTextInput = false) {
  const node = createPreparationNode(kind);
  if (node.kind === "variable-register") node.config.name = "倒计时";
  const app = createSSRApp(IncrementalNodeFields, {
    node, hasTextInput, workflowId: "frontend:main-test", stage: "A",
  });
  app.use(createPinia());
  return renderToString(app);
}
describe("incremental node fields", () => {
  it("exposes independent text/prompt regex configuration with backend engine fields", async () => {
    const html = await render("regex");
    expect(html).toContain('value="text"');
    expect(html).toContain('value="prompt"');
    expect(html).toContain("python-re-v1");
    expect(html).toContain("替换文本");
    expect(html).toContain("首处");
    expect(html).toContain("点匹配换行");
    expect(html).not.toContain("优先级");
  });
  it("distinguishes registration defaults from session values and connected text sources", async () => {
    const html = await render("variable-register");
    expect(html).toContain("{{倒计时}}");
    expect(html).toContain("初始值：未赋值");
    expect(html).toContain("配置初始值");
    const connected = await render("variable-register", true);
    expect(connected).toContain("值来源：文本连接");
    expect(connected).not.toContain("初始值：未赋值");
  });
  it("renders set/add/subtract as explicit operations, not inline scripting", async () => {
    const html = await render("variable-assign");
    expect(html).toContain('value="set"');
    expect(html).toContain('value="add"');
    expect(html).toContain('value="subtract"');
    expect(html).toContain("变量类型");
    expect(html).not.toContain("eval");
  });
});
