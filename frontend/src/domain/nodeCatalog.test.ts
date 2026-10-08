import { describe, expect, it } from "vitest";
import { buildNodeFamilies, nodeDisplayTitle, nodeMenuGroups, nodeProfileLabel, profileForAxis } from "./nodeCatalog";
import { graphClone, type GraphNodeType } from "./workflowGraph";

const type = (component_id: string, component_version = "1"): GraphNodeType => ({
  component_id, component_version, display_name: `Backend ${component_id} ${component_version}`, category: "Backend",
  config_schema: {}, default_config: {}, inputs: [], outputs: [], is_output: false, executable: true,
});
const versions = (id: string, values: string[]) => values.map(version => type(id, version));

describe("current node directory", () => {
  it("keeps only the current route for each protocol without mutating declarations", () => {
    const catalog = [...versions("models.source", ["4", "2", "1", "3"]),
      ...versions("agents.execute", ["8", "4", "3"]), type("plugin.text", "9")];
    const before = graphClone(catalog), families = buildNodeFamilies(catalog);
    expect(catalog).toEqual(before);
    expect(families).toHaveLength(3);
    const source = families.find(row => row.id === "models.source")!;
    expect(source.preferredKey).toBe("models.source@2");
    expect(source.profiles.map(row => row.key)).toEqual(["models.source@4", "models.source@2"]);
    const menu = nodeMenuGroups(families, true);
    expect(menu.flatMap(row => row.items).map(row => row.id)).toEqual(["models.source", "agents.execute", "plugin.text"]);
    expect(menu.every(row => row.items.every(item => item.disabled))).toBe(true);
  });
  it("offers only protocol selection, with no historical version choices", () => {
    for (const [id, current] of [["models.source", ["2", "4"]], ["models.chat", ["3", "4"]],
      ["agents.execute", ["4", "8"]]] as const) {
      const family = buildNodeFamilies(versions(id, [...current]))[0]!;
      expect(family.axes.map(row => row.id)).toEqual(["protocol"]);
      const deepseek = family.profiles[0]!;
      const gemini = profileForAxis(family, deepseek, "protocol", "gemini")!;
      expect(gemini.key).toBe(`${id}@${current[1]}`);
      expect(nodeProfileLabel(gemini.definition)).toBe("Gemini");
    }
  });
  it("removes retired families and old prompt/context declarations", () => {
    const families = buildNodeFamilies([...versions("context.merge", ["1", "2", "4"]),
      ...versions("prompts.item", ["1", "2"]), type("context.read"), type("agents.delta"),
      type("context.plan")]);
    expect(families.map(row => row.id)).toEqual(["prompts.item", "context.merge"]);
    expect(families.map(row => row.profiles.map(profile => profile.key)))
      .toEqual([["prompts.item@2"], ["context.merge@4"]]);
  });
  it("uses the latest installed external declaration and handles one installed protocol", () => {
    const families = buildNodeFamilies([...versions("plugin.node", ["7", "42"]), type("models.source", "4")]);
    expect(families.find(row => row.id === "plugin.node")!.profiles.map(row => row.key)).toEqual(["plugin.node@42"]);
    expect(families.find(row => row.id === "models.source")!.preferredKey).toBe("models.source@4");
    expect(buildNodeFamilies([type("models.source", "99")])).toEqual([]);
  });
  it("unifies default names and preserves user titles", () => {
    const definition = type("models.source", "2");
    const node = { node_binding_id: "id", component_id: definition.component_id, component_version: "2",
      title: definition.display_name, config: {}, position: { x: 0, y: 0 } };
    expect(nodeDisplayTitle(node, definition)).toBe("模型来源");
    expect(nodeDisplayTitle({ ...node, title: "我的模型" }, definition)).toBe("我的模型");
    expect(nodeDisplayTitle(node)).toBe(node.title);
  });
});
