import { describe, expect, it } from "vitest";
import { buildNodeFamilies, nodeDisplayTitle, nodeMenuGroups, nodeProfileLabel } from "./nodeCatalog";
import { graphClone, type GraphNodeType } from "./workflowGraph";

const type = (component_id: string, component_version = "1"): GraphNodeType => ({
  component_id, component_version, display_name: `Backend ${component_id} ${component_version}`, category: "Backend",
  config_schema: {}, default_config: {}, inputs: [], outputs: [], is_output: false, executable: true,
});
const versions = (id: string, values: string[]) => values.map(version => type(id, version));

describe("current node directory", () => {
  it("offers only the exact current model and Agent declarations without mutating the backend catalog", () => {
    const catalog = [...versions("models.source", ["7", "6", "5", "4", "2", "1", "3"]),
      ...versions("models.chat", ["5", "4", "3"]),
      ...versions("agents.execute", ["10", "9", "8", "4"]),
      ...versions("agents.compaction-policy", ["2", "1"]),
      ...versions("tools.output", ["2", "1"]),
      ...versions("context.output", ["5", "4"]),
      ...versions("context.assembly", ["5", "4"]),
      ...versions("context.merge", ["6", "5", "4"]), type("plugin.text", "9")];
    const before = graphClone(catalog), families = buildNodeFamilies(catalog);
    expect(catalog).toEqual(before);
    for (const [id, version] of [["models.source", "7"], ["models.chat", "5"],
      ["agents.execute", "10"], ["agents.compaction-policy", "2"], ["tools.output", "2"],
      ["context.output", "5"], ["context.assembly", "5"], ["context.merge", "6"]]) {
      const family = families.find(row => row.id === id)!;
      expect(family.preferredKey).toBe(`${id}@${version}`);
      expect(family.profiles.map(row => row.key)).toEqual([`${id}@${version}`]);
    }
    expect(nodeMenuGroups(families, true).every(row => row.items.every(item => item.disabled))).toBe(true);
  });
  it("does not expose protocol-version choices for unified model and Agent declarations", () => {
    for (const [id, version] of [["models.source", "7"], ["models.chat", "5"],
      ["agents.execute", "10"]] as const) {
      const family = buildNodeFamilies(versions(id, [version, "1"]))[0]!;
      expect(family.axes).toEqual([]);
      expect(family.profiles.map(row => row.key)).toEqual([`${id}@${version}`]);
    }
  });
  it("removes retired families and old prompt/context declarations", () => {
    const families = buildNodeFamilies([...versions("context.merge", ["1", "2", "6"]),
      ...versions("prompts.item", ["1", "2", "3"]), type("context.read"), type("agents.delta"),
      type("context.plan")]);
    expect(families.map(row => row.id)).toEqual(["prompts.item", "context.merge"]);
    expect(families.map(row => row.profiles.map(profile => profile.key)))
      .toEqual([["prompts.item@3"], ["context.merge@6"]]);
  });
  it("uses the latest installed external declaration and excludes unknown model versions", () => {
    const families = buildNodeFamilies([...versions("plugin.node", ["7", "42"]), type("models.source", "99")]);
    expect(families.find(row => row.id === "plugin.node")!.profiles.map(row => row.key)).toEqual(["plugin.node@42"]);
    expect(families.find(row => row.id === "models.source")).toBeUndefined();
  });
  it("unifies default names and preserves user titles", () => {
    const definition = type("models.source", "7");
    const node = { node_binding_id: "id", component_id: definition.component_id, component_version: "7",
      title: definition.display_name, config: {}, position: { x: 0, y: 0 } };
    expect(nodeDisplayTitle(node, definition)).toBe("模型来源");
    expect(nodeDisplayTitle({ ...node, title: "我的模型" }, definition)).toBe("我的模型");
    expect(nodeDisplayTitle(node)).toBe(node.title);
    expect(nodeProfileLabel(definition)).toBe("标准");
  });
});
