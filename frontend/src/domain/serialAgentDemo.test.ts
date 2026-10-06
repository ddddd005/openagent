import { describe, expect, it } from "vitest";
import { createSerialAgentDemo } from "./serialAgentDemo";
import { graphClone, isGraphDocument, type GraphNodeType } from "./workflowGraph";
import { frontendCatalog } from "../testUtils/frontendFixture";

const packageLock = ["content", "tools", "prompts", "models", "agents", "context", "frontend-business"]
  .map(name => ({ package_id: `workflow.${name}`, version: "1.0.0" }));
const base = { component_version: "1", display_name: "Node", category: "Test", config_schema: {},
  default_config: {}, inputs: [], outputs: [], is_output: false, executable: true };
const catalog: GraphNodeType[] = [...graphClone(frontendCatalog),
  { ...base, component_id: "models.source", default_config: {
    reference: { envelope_version: 1, scope: "workspace", type_id: "workflow.chat-provider", resource_id: "default-provider" },
    parameters: { model: "default-model", thinking: "disabled", stream: false } } },
  { ...base, component_id: "prompts.item", default_config: { id: "00000000-0000-4000-8000-000000000001",
    text: "", presentation: { role: "system", placement: "before", depth: null, order: 0, enabled: true }, metadata: {} } },
  { ...base, component_id: "tools.text-to-prompt" },
  { ...base, component_id: "context.output", component_version: "2" },
  { ...base, component_id: "context.assembly", component_version: "3", default_config: { character_budget: 100_000 } },
  { ...base, component_id: "context.merge", component_version: "2" },
  { ...base, component_id: "agents.execute", component_version: "3" },
];
const model = { reference: { envelope_version: 1, scope: "workspace", type_id: "workflow.chat-provider", resource_id: "selected-provider" },
  parameters: { model: "selected-model", thinking: "disabled", stream: false } };

describe("serial Agent example", () => {
  it("keeps original input as both roots and uses A output as explicit non-system B material", () => {
    const before = graphClone(catalog), doc = createSerialAgentDemo(catalog, packageLock, model);
    expect(isGraphDocument(doc)).toBe(true); expect(catalog).toEqual(before);
    const input = doc.nodes.find(row => row.component_id === "tools.current-input")!;
    const handoff = doc.nodes.find(row => row.component_id === "tools.text-to-prompt")!;
    const a = doc.nodes.find(row => row.title === "Agent A")!, b = doc.nodes.find(row => row.title === "B 装配")!;
    expect(doc.edges.filter(row => row.source_node_id === input.node_binding_id && row.target_port_id === "current_input")).toHaveLength(2);
    expect(doc.edges).toContainEqual(expect.objectContaining({ source_node_id: a.node_binding_id,
      source_port_id: "result", target_node_id: handoff.node_binding_id, target_port_id: "input" }));
    expect(doc.edges).toContainEqual(expect.objectContaining({ source_node_id: handoff.node_binding_id,
      target_node_id: b.node_binding_id, target_port_id: "materials", order: 1 }));
    expect(handoff.config.presentation).toEqual({ role: "user", placement: "after", depth: null, order: 0, enabled: true });
    expect(doc.nodes.find(row => row.component_id === "models.source")!.config).toEqual(model);
    model.parameters.model = "changed";
    expect((doc.nodes.find(row => row.component_id === "models.source")!.config.parameters as Record<string, unknown>).model).toBe("selected-model");
    model.parameters.model = "selected-model";
  });
  it("authorizes separate bound objects and shares each exact view between assembly and merge", () => {
    const doc = createSerialAgentDemo(catalog, packageLock, model);
    for (const label of ["A", "B"]) {
      const execute = doc.nodes.find(row => row.title === `Agent ${label}`)!;
      const read = doc.nodes.find(row => row.title === `${label} 上下文`)!;
      const merge = doc.nodes.find(row => row.title === `${label} 上下文写回`)!;
      const assembly = doc.nodes.find(row => row.title === `${label} 装配`)!;
      expect(read.config).toEqual(merge.config);
      expect(read.config.agent_node_id).toBe(execute.node_binding_id);
      expect(doc.object_bindings!.find(row => row.object_key === read.config.object_key)).toMatchObject({
        type_id: "workflow.effective-context", schema_version: 3,
        readers: [read.node_binding_id, merge.node_binding_id], writers: [merge.node_binding_id] });
      expect(doc.edges.filter(row => row.source_node_id === read.node_binding_id).map(row => [row.source_port_id, row.target_node_id]))
        .toEqual([["output", assembly.node_binding_id], ["output", merge.node_binding_id]]);
      expect(doc.execution_roots).toContain(merge.node_binding_id);
    }
  });
  it("orders writes after B success, rereads between appends and exposes only final presentation", () => {
    const doc = createSerialAgentDemo(catalog, packageLock, model);
    const names = new Map(doc.nodes.map(row => [row.node_binding_id, row.title]));
    expect(doc.control_edges!.map(row => [names.get(row.source_node_id), names.get(row.target_node_id)]))
      .toEqual([["Agent B", "A 上下文写回"], ["A 上下文写回", "B 上下文写回"],
        ["B 上下文写回", "读取聊天记录"], ["追加用户输入", "重新读取聊天记录"]]);
    const assistant = doc.nodes.find(row => row.title === "追加最终回答")!;
    const fresh = doc.nodes.find(row => row.title === "重新读取聊天记录")!;
    expect(doc.edges).toContainEqual(expect.objectContaining({ source_node_id: fresh.node_binding_id,
      target_node_id: assistant.node_binding_id, target_port_id: "view" }));
    const published = doc.nodes.filter(row => row.public_outputs?.length);
    expect(published).toHaveLength(1); expect(published[0].public_outputs).toEqual(["display"]);
    expect(doc.execution_roots).toContain(published[0].node_binding_id);
  });
  it("creates independent identities while remaining stable under ordinary persistence", () => {
    const a = createSerialAgentDemo(catalog, packageLock, model), b = createSerialAgentDemo(catalog, packageLock, model);
    expect(a.workflow_definition_id).not.toBe(b.workflow_definition_id);
    const ids = new Set(a.nodes.map(row => row.node_binding_id));
    expect(b.nodes.every(row => !ids.has(row.node_binding_id))).toBe(true);
    expect(JSON.parse(JSON.stringify(a))).toEqual(a);
    expect(new Set([...a.nodes.map(row => row.node_binding_id), ...a.edges.map(row => row.edge_id),
      ...a.control_edges!.map(row => row.edge_id)]).size).toBe(a.nodes.length + a.edges.length + a.control_edges!.length);
  });
  it("creates an explicitly unconfigured model without inheriting a hidden default resource", () => {
    const doc = createSerialAgentDemo(catalog, packageLock);
    expect(doc.nodes.find(row => row.component_id === "models.source")!.config).toMatchObject({
      reference: { resource_id: "" }, parameters: { model: "" } });
  });
  it("rejects missing exact versions and package locks instead of falling back", () => {
    expect(() => createSerialAgentDemo(catalog.filter(row => row.component_id !== "context.merge"), packageLock, model))
      .toThrow("context.merge@2");
    expect(() => createSerialAgentDemo(catalog, packageLock.filter(row => row.package_id !== "workflow.frontend-business"), model))
      .toThrow("workflow.frontend-business");
  });
});
