import { describe, expect, it } from "vitest";
import { createFixtureDraft } from "../fixtures/catalog";
import {
  copyDraft,
  diagnose,
  groupNodes,
  isLocalDraft,
  ungroupNode,
} from "./graph";

describe("local draft graph", () => {
  it("validates fixtures without treating them as published definitions", () => {
    const d = createFixtureDraft();
    expect(isLocalDraft(d)).toBe(true);
    expect(d.nodes.every((n) => n.id.startsWith("local:"))).toBe(true);
    expect(diagnose(d)).toEqual([]);
  });
  it("keeps mismatched edges and reports exact port types", () => {
    const d = createFixtureDraft();
    d.nodes.find((n) => n.id === "local:regex")!.outputs[0].type = "Text";
    expect(diagnose(d)).toMatchObject([
      {
        nodeId: "local:assemble",
        portId: "collection",
        actual: "Text @ fixture-1",
        expected: "PromptCollection @ fixture-1",
      },
    ]);
    expect(d.edges).toHaveLength(5);
  });
  it("groups and ungroups without changing internal identities or connections", () => {
    const d = createFixtureDraft();
    const grouped = groupNodes(
      d,
      ["local:macro", "local:regex"],
      "local:test-group",
    );
    expect(
      grouped.nodes
        .filter((n) => n.scope === "local:test-group")
        .map((n) => n.id),
    ).toEqual(["local:macro", "local:regex"]);
    expect(grouped.groups["local:test-group"]).toHaveLength(2);
    expect(diagnose(grouped)).toEqual([]);
    expect(isLocalDraft(grouped)).toBe(true);
    expect(ungroupNode(grouped, "local:test-group")).toEqual(d);
  });
  it("renaming an exposed port does not change mapping identity", () => {
    const d = groupNodes(
      createFixtureDraft(),
      ["local:macro", "local:regex"],
      "local:test-group",
    );
    const mappings = copyDraft(d).groups["local:test-group"];
    d.nodes.find((n) => n.id === "local:test-group")!.inputs[0].label =
      "重命名接口";
    expect(d.groups["local:test-group"]).toEqual(mappings);
    expect(diagnose(d)).toEqual([]);
  });
  it("rejects singleton and nested grouping in this slice", () => {
    const d = createFixtureDraft();
    expect(() => groupNodes(d, ["local:macro"])).toThrow();
    const grouped = groupNodes(
      d,
      ["local:macro", "local:regex"],
      "local:test-group",
    );
    expect(() =>
      groupNodes(grouped, ["local:test-group", "local:collect"]),
    ).toThrow();
  });
  it("does not mutate the source when grouping", () => {
    const d = createFixtureDraft(),
      original = copyDraft(d);
    groupNodes(d, ["local:macro", "local:regex"]);
    expect(d).toEqual(original);
  });
  it("rejects corrupt layouts and invalid interface references", () => {
    const d = createFixtureDraft();
    d.layout["local:macro"].x = NaN;
    expect(isLocalDraft(d)).toBe(false);
    const grouped = groupNodes(
      createFixtureDraft(),
      ["local:macro", "local:regex"],
      "local:test-group",
    );
    grouped.groups["local:test-group"][0].nodeId = "local:missing";
    expect(isLocalDraft(grouped)).toBe(false);
  });
});
