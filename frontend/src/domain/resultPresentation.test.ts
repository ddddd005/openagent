import { describe, expect, it } from "vitest";
import { resultTexts } from "./resultPresentation";
import { modelPresentationJson, thinkingSummaries } from "./modelPresentation";
describe("user result presentation", () => {
  it("separates model content, thinking and opaque protocol metadata without changing facts", () => {
    const value = { schema_version: 2, kind: "workflow.model-result", content: "Answer",
      thinking_summary: "Summary", provider_metadata: { parts: [{ thoughtSignature: "private-signature" }] } };
    const original = JSON.stringify(value);
    expect(resultTexts(value)).toEqual([{ label: "回答", text: "Answer" }]);
    expect(thinkingSummaries(value)).toEqual(["Summary"]);
    expect(modelPresentationJson(value)).not.toContain("private-signature");
    expect(JSON.stringify(value)).toBe(original);
  });
  it("uses kind and schema, never arbitrary text-looking fields", () => {
    expect(resultTexts({ kind: "unknown", schema_version: 2, text: "opaque" })).toEqual([]);
    expect(resultTexts({ kind: "workflow.text", schema_version: 99, text: "future" })).toEqual([]);
    expect(resultTexts({ kind: "workflow.model-result", schema_version: "2", content: "invalid version" })).toEqual([]);
    expect(resultTexts({ kind: "workflow.model-result", schema_version: 99, content: "future" })).toEqual([]);
    expect(resultTexts([{ kind: "workflow.text", schema_version: 2, text: "input" }])).toEqual([{ label: "正文", text: "input" }]);
  });
  it("presents content from model result schema 3 without exposing other facts", () => {
    expect(resultTexts({ kind: "workflow.model-result", schema_version: 3,
      content: "Schema 3 answer", fact_refs: [{ fact_id: crypto.randomUUID() }] }))
      .toEqual([{ label: "回答", text: "Schema 3 answer" }]);
  });
});
