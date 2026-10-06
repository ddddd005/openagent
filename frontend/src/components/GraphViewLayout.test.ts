import { readFileSync } from "node:fs";
import { parse as parseSfc } from "@vue/compiler-sfc";
import { parse as parseCss } from "postcss";
import { describe, expect, it } from "vitest";

function declarations(file: string, selector: string) {
  const { descriptor, errors } = parseSfc(readFileSync(new URL(file, import.meta.url), "utf8"));
  expect(errors).toHaveLength(0);
  const values: Record<string, string> = {};
  for (const block of descriptor.styles.filter(style => style.scoped)) {
    parseCss(block.content).walkRules(selector, rule => {
      rule.walkDecls(declaration => { values[declaration.prop] = declaration.value; });
    });
  }
  return values;
}

describe("ordinary graph view layout declarations", () => {
  it.each([
    ["GraphWorkbench.vue", ".graph-workbench"],
    ["GraphHistory.vue", ".graph-history"],
  ])("keeps %s filling its horizontal flex parent", (file, selector) => {
    expect(declarations(file, selector)).toMatchObject({
      display: "flex", flex: "1", "min-width": "0", "min-height": "0",
    });
  });

  it("keeps the canvas flexible beside a fixed-width inspector", () => {
    expect(declarations("GraphWorkbench.vue", ".graph-body")).toMatchObject({
      display: "flex", flex: "1", "min-height": "0",
    });
    expect(declarations("GraphWorkbench.vue", ".graph-canvas")).toMatchObject({
      flex: "1", "min-width": "0", position: "relative",
    });
    expect(declarations("GraphWorkbench.vue", ".graph-inspector")).toMatchObject({
      flex: "0 0 280px", width: "280px", "min-width": "0", overflow: "hidden",
    });
  });
});
