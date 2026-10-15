import { describe, expect, it } from "vitest";
import { buildNodeFamilies, nodeDisplayTitle, nodeMenuGroups, nodeProfileLabel } from "./nodeCatalog";
import { graphClone, type GraphNodeType } from "./workflowGraph";
import { editableInputType } from "../testUtils/editableInputFixture";

const type = (component_id: string, component_version = "1"): GraphNodeType => ({
  component_id, component_version, display_name: `Backend ${component_id} ${component_version}`, category: "Backend",
  config_schema: {}, default_config: {}, inputs: [], outputs: [], is_output: false, executable: true,
});
const versions = (id: string, values: string[]) => values.map(version => type(id, version));

describe("current node directory", () => {
  it("offers the editable input contract in basic tools without changing current-input", () => {
    const families = buildNodeFamilies([type("tools.current-input"), graphClone(editableInputType)]);
    expect(families.find(row => row.id === "tools.editable-input")).toMatchObject({
      preferredKey: "tools.editable-input@1", profiles: [{ definition: editableInputType }],
    });
    expect(nodeMenuGroups(families).find(row => row.id === "basic")!.items).toEqual([
      { id: "tools.current-input", label: "当前输入", disabled: false },
      { id: "tools.editable-input", label: "可编辑输入", disabled: false },
    ]);
  });
  it("offers the whole frontend node without removing the existing explicit frontend nodes", () => {
    const catalog = ["frontend.state.output", "frontend.state.append", "frontend.presentation", "frontend.node"].map(id => type(id));
    const families = buildNodeFamilies(catalog);
    expect(nodeMenuGroups(families).find(row => row.id === "presentation")!.items.map(row => [row.id, row.label]))
      .toEqual([["frontend.state.output", "读取展示记录"], ["frontend.state.append", "追加展示记录"],
        ["frontend.presentation", "展示输出"], ["frontend.node", "前端节点"]]);
    expect(families.find(row => row.id === "frontend.node")!.preferredKey).toBe("frontend.node@1");
  });
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
  it("offers non-working variable registration in the variable menu using its current declaration", () => {
    const registration = { ...type("tools.variable-register", "2"), execution_class: "non_working" as const };
    for (const catalog of [[registration], [type("tools.variable-register", "1"), registration]]) {
      const families = buildNodeFamilies(catalog), family = families.find(row => row.id === registration.component_id)!;
      expect(family.preferredKey).toBe("tools.variable-register@2");
      expect(family.profiles.map(row => row.key)).toEqual(["tools.variable-register@2"]);
      expect(family.profiles[0]!.definition.execution_class).toBe("non_working");
      expect(nodeMenuGroups(families).find(row => row.id === "variables")!.items)
        .toContainEqual({ id: "tools.variable-register", label: "变量注册", disabled: false });
    }
  });
  it("does not offer retired variable registration when the current declaration is missing", () => {
    expect(buildNodeFamilies([type("tools.variable-register", "1")])).toEqual([]);
  });
  it("removes retired families and old prompt/context declarations", () => {
    const families = buildNodeFamilies([...versions("context.merge", ["1", "2", "6"]),
      ...versions("prompts.item", ["1", "2", "3"]), type("context.read"), type("agents.delta"),
      type("context.plan")]);
    expect(families.map(row => row.id)).toEqual(["prompts.item", "context.merge"]);
    expect(families.map(row => row.profiles.map(profile => profile.key)))
      .toEqual([["prompts.item@3"], ["context.merge@6"]]);
  });
  it("keeps world-book families while refusing the retired Tavern chat families", () => {
    const catalog = ["lorebook.item", "lorebook.group", "lorebook.global-reference", "lorebook.global-activate",
      "tavern.chat.output", "tavern.chat.append", "tavern.chat.presentation"].map(id => type(id));
    const before = graphClone(catalog), families = buildNodeFamilies(catalog);
    expect(catalog).toEqual(before);
    expect(families.map(row => row.id)).toEqual([
      "lorebook.item", "lorebook.group", "lorebook.global-reference", "lorebook.global-activate",
    ]);
    expect(nodeMenuGroups(families)).toMatchObject([{ id: "tavern", label: "世界书" }]);
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
