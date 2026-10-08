import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { describe, expect, it } from "vitest";
import { modelPresentationJson, thinkingSummaries } from "./modelPresentation";
import ThinkingSummary from "../components/ThinkingSummary.vue";

const fixture = {
  content: "Answer", thinking_summary: "Visible <summary>",
  provider_metadata: { content: { parts: [{ text: "Not display authority", thoughtSignature: "opaque-secret" }] } },
  details: [{ thinking_summary: "Visible <summary>", wire_request: {
    contents: [{ parts: [{ text: "Answer", thoughtSignature: "other-secret" }] }] } }],
};

describe("thinking presentation boundary", () => {
  it("separates summaries and removes opaque protocol data without changing the original", () => {
    const before = JSON.stringify(fixture), text = modelPresentationJson(fixture);
    expect(text).toContain("Answer"); expect(text).not.toContain("secret");
    expect(text).not.toContain("provider_metadata"); expect(text).not.toContain("thinking_summary");
    expect(thinkingSummaries(fixture)).toEqual(["Visible <summary>"]);
    expect(JSON.stringify(fixture)).toBe(before);
  });
  it("renders escaped visible summaries in a closed independent disclosure", async () => {
    const html = await renderToString(createSSRApp(ThinkingSummary, { value: fixture }));
    expect(html).toContain('aria-label="思考摘要"'); expect(html).toContain("Visible &lt;summary&gt;");
    expect(html).not.toContain("secret"); expect(html).not.toContain(" open");
    expect(await renderToString(createSSRApp(ThinkingSummary, { value: { thinking_summary: null } }))).not.toContain("<details");
  });
  it("gives standalone consumers the same display projection and supports model result v2", () => {
    const context: Record<string, any> = {};
    runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), context);
    expect(context.GraphChat.thinkingSummaries(fixture)).toEqual(thinkingSummaries(fixture));
    expect(JSON.stringify(context.GraphChat.modelPresentation(fixture))).toBe(JSON.stringify(JSON.parse(modelPresentationJson(fixture))));
    const result = { schema_version: 2, kind: "workflow.model-result", ...fixture };
    expect(context.GraphChat.supportedContent("MODEL_RESULT", 2)).toBe(true);
    expect(context.GraphChat.displayText(result)).toBe("Answer");
  });
  it("reads only public summary channels, including empty intermediate pages, with exact owner association", async () => {
    const context: Record<string, any> = {};
    for (const file of ["static/graph-chat-core.js", "tavern/frontend/adapter.js"]) runInNewContext(
      readFileSync(new URL(`../../../backend/src/phase1_agent/${file}`, import.meta.url), "utf8"), context);
    const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
    const view = {}, owner = id(1), binding = { kind: "information_binding", owner: { node_run_id: owner },
      declaration: { channel_id: "thinking-summaries", format_id: "workflow.thinking-summaries" }, read_public: true };
    const calls: unknown[] = [], client = { consumer: view,
      listRegistrations: async () => ({ items: [binding], next_cursor: null }),
      readInformation: async (_: unknown, options: any) => {
        calls.push(options);
        return options.cursor === null ? { items: [], status: "ok", next_cursor: "p2" }
          : { items: [{ message_id: id(2), request_id: id(3), thinking_summary: "Visible" }], status: "ok", next_cursor: null };
      } };
    const rows: any[] = [{ role: "assistant", producer: { node_run_id: owner }, text: "Answer" },
      { role: "user", producer: { node_run_id: owner }, text: "Input" }];
    const cache = new Map();
    await context.TavernAdapter.attachThinking(client, rows, cache, view);
    expect(rows[0].thinking_summary).toBe("Visible"); expect(rows[1].thinking_summary).toBeUndefined();
    expect(calls).toHaveLength(2); expect(JSON.stringify(rows)).not.toContain("provider_metadata");
    await context.TavernAdapter.attachThinking(client, rows, cache, view);
    expect(calls).toHaveLength(2);
  });
});
