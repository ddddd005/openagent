import { describe, expect, it } from "vitest";
import { isLorebookConfig, isLorebookEntry, lorebookBindingErrors, lorebookLimits,
  lorebookVariableBindings, moveLorebookEntry, newLorebookEntry } from "./lorebook";
import { graphClone, newGraph } from "./workflowGraph";

describe("lorebook editable contract", () => {
  it("uses safe per-entry defaults and distinct stable UUIDs", () => {
    const first = newLorebookEntry(), second = newLorebookEntry();
    expect(first).toMatchObject({ mode: "keyword", recursive: false, scan_depth: 20,
      case_sensitive: false, probability_enabled: false, probability: 100,
      keyword_rule: "primary_or_secondary", primary_keywords: [], secondary_keywords: [],
      presentation: { role: "system", placement: "before", depth: null, order: 0, enabled: true } });
    expect(first.id).not.toEqual(second.id);
    expect(isLorebookEntry(first)).toBe(true);
    expect(isLorebookConfig({ entry: first, object_keys: [] }, "lorebook.item")).toBe(true);
    expect(isLorebookConfig({ entries: [], object_keys: [] }, "lorebook.group")).toBe(true);
    expect(Object.hasOwn(first, "lifecycle")).toBe(false);
  });
  it("accepts Chinese, empty keywords, negative order and valid numeric boundaries", () => {
    const entry = newLorebookEntry(-3);
    entry.primary_keywords = ["山谷", "", "Moon", "{{地点}}"];
    entry.secondary_keywords = ["\n"];
    entry.presentation = { ...entry.presentation, placement: "middle", depth: 0 };
    entry.scan_depth = lorebookLimits.scanDepth; entry.probability = 0;
    expect(isLorebookEntry(entry)).toBe(true);
    entry.probability = 42.5;
    expect(isLorebookEntry(entry)).toBe(true);
  });
  it.each([
    ["id", "not-uuid"], ["name", "a".repeat(129)], ["mode", "legacy"],
    ["primary_keywords", ["a".repeat(4097)]], ["secondary_keywords", Array(129).fill("a")],
    ["keyword_rule", "primary_only"], ["scan_depth", -1], ["scan_depth", 1.5],
    ["scan_depth", 4097], ["probability", -1], ["probability", 101], ["probability", Number.NaN],
    ["case_sensitive", "false"], ["recursive", 1], ["text", "\ud800"], ["metadata", { nested: "\ud800" }],
  ])("rejects invalid %s without repairing it", (key, value) => {
    const entry = { ...newLorebookEntry(), [key]: value };
    expect(isLorebookEntry(entry)).toBe(false);
    expect(entry[key as keyof typeof entry]).toEqual(value);
  });
  it("requires exact contracts and rejects duplicate identities, object keys and excessive sizes", () => {
    const entry = newLorebookEntry();
    expect(isLorebookEntry({ ...entry, lifecycle: "context_once" })).toBe(false);
    expect(isLorebookConfig({ entries: [entry, graphClone(entry)], object_keys: [] }, "lorebook.group")).toBe(false);
    expect(isLorebookConfig({ entries: [], object_keys: ["shared/a", "shared/a"] }, "lorebook.group")).toBe(false);
    expect(isLorebookConfig({ entry, object_keys: [" shared/a"] }, "lorebook.item")).toBe(false);
    expect(isLorebookConfig({ entries: [], object_keys: [], extra: true }, "lorebook.group")).toBe(false);
    expect(isLorebookConfig({ entries: [], object_keys: [] }, "lorebook.item")).toBe(false);
    expect(isLorebookConfig({ entries: Array.from({ length: 1025 }, () => newLorebookEntry()), object_keys: [] }, "lorebook.group")).toBe(false);
    const members = Array.from({ length: 5 }, () => ({ ...newLorebookEntry(), text: "a".repeat(900_000) }));
    expect(isLorebookConfig({ entries: members, object_keys: [] }, "lorebook.group")).toBe(false);
  });
  it("preserves identity, content and other presentation fields when ordering duplicate default ranks", () => {
    const entries = [newLorebookEntry(), newLorebookEntry(), newLorebookEntry()];
    entries.forEach((entry, at) => { entry.name = String(at); entry.text = `Body ${at}`; });
    entries[1]!.presentation.role = "assistant";
    entries[1]!.presentation.placement = "middle";
    entries[1]!.presentation.depth = 3;
    const original = graphClone(entries);
    expect(moveLorebookEntry(entries, 1, -1)).toBe(true);
    expect(entries.map(entry => entry.id)).toEqual([original[1]!.id, original[0]!.id, original[2]!.id]);
    expect(entries.map(entry => entry.presentation.order)).toEqual([0, 1, 2]);
    expect(entries[0]).toEqual({ ...original[1], presentation: { ...original[1]!.presentation, order: 0 } });
    expect(entries[1]).toEqual({ ...original[0], presentation: { ...original[0]!.presentation, order: 1 } });
    expect(moveLorebookEntry(entries, 0, -1)).toBe(false);
  });
  it("swaps distinct order ranks without changing unrelated ranks", () => {
    const entries = [newLorebookEntry(-5), newLorebookEntry(7), newLorebookEntry(11)];
    const ids = entries.map(entry => entry.id);
    expect(moveLorebookEntry(entries, 0, 1)).toBe(true);
    expect(entries.map(entry => entry.id)).toEqual([ids[1], ids[0], ids[2]]);
    expect(entries.map(entry => entry.presentation.order)).toEqual([-5, 7, 11]);
  });
  it("offers only explicitly permitted current variable bindings, without mutating permissions", () => {
    const nodeId = crypto.randomUUID(), other = crypto.randomUUID(), document = newGraph("bindings");
    const base = { object_key: "shared/a", type_id: "workflow.variable", schema_version: 1, scope: "shared" as const,
      readers: [nodeId], writers: [other], owner_node_id: null };
    document.object_bindings = [base,
      { ...base, object_key: "shared/no-reader", readers: [other] },
      { ...base, object_key: "shared/wrong-version", schema_version: 2 },
      { ...base, object_key: "shared/context", type_id: "workflow.agent-context" }];
    const original = graphClone(document);
    expect(lorebookVariableBindings(document, nodeId).map(binding => binding.object_key)).toEqual(["shared/a"]);
    expect(lorebookBindingErrors(document, nodeId, ["shared/a"])).toEqual([]);
    expect(lorebookBindingErrors(document, nodeId, ["shared/no-reader"])).toEqual(["变量对象未授权或类型不匹配：shared/no-reader"]);
    expect(document).toEqual(original);
  });
});
