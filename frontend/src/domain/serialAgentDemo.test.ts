import { describe, expect, it } from "vitest";
import { createCompactingSerialAgentDemo, createSerialAgentDemo } from "./serialAgentDemo";
import { graphClone, isGraphDocument, type GraphNodeType } from "./workflowGraph";
import { frontendCatalog } from "../testUtils/frontendFixture";

const packageLock = ["content", "tools", "prompts", "models", "agents", "context", "frontend-business"]
  .map(name => ({ package_id: `workflow.${name}`, version: "1.0.0" }));
const base = { component_version: "1", display_name: "Node", category: "Test", config_schema: {},
  default_config: {}, inputs: [], outputs: [], is_output: false, executable: true };
const catalog: GraphNodeType[] = [...graphClone(frontendCatalog),
  { ...base, component_id: "models.source", component_version: "2", default_config: {
    reference: { envelope_version: 1, scope: "workspace", type_id: "workflow.chat-provider", resource_id: "default-provider" },
    parameters: { model: "default-model", thinking: "disabled", stream: false } } },
  { ...base, component_id: "prompts.item", component_version: "2", default_config: { id: "00000000-0000-4000-8000-000000000001",
    text: "", presentation: { role: "system", placement: "before", depth: null, order: 0, enabled: true }, metadata: {},
    lifecycle: "per_request", compaction: "never" } },
  { ...base, component_id: "prompts.source", component_version: "2" },
  { ...base, component_id: "context.output", component_version: "4" },
  { ...base, component_id: "context.assembly", component_version: "4" },
  { ...base, component_id: "context.merge", component_version: "4" },
  { ...base, component_id: "agents.execute", component_version: "4" },
];
const model = { reference: { envelope_version: 1, scope: "workspace", type_id: "workflow.chat-provider", resource_id: "selected-provider" },
  parameters: { model: "selected-model", thinking: "disabled", stream: false } };

describe("serial Agent example", () => {
  it("keeps original input as both roots and uses A output as explicit non-system B material", () => {
    const before = graphClone(catalog), doc = createSerialAgentDemo(catalog, packageLock, model);
    expect(isGraphDocument(doc)).toBe(true); expect(catalog).toEqual(before);
    const input = doc.nodes.find(row => row.component_id === "tools.current-input")!;
    const handoff = doc.nodes.find(row => row.component_id === "prompts.source")!;
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
        type_id: "workflow.effective-context", schema_version: 4,
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
      .toThrow("context.merge@4");
    expect(() => createSerialAgentDemo(catalog, packageLock.filter(row => row.package_id !== "workflow.frontend-business"), model))
      .toThrow("workflow.frontend-business");
  });
});

describe("native compacting serial example", () => {
  const capacity = { context_window_tokens: 0, output_reserve_tokens: 1024, summary_max_tokens: 128,
    max_cold_input_tokens: 0 };
  const nativeCatalog: GraphNodeType[] = [...catalog.filter(row => !row.component_id.startsWith("models.")
    && !row.component_id.startsWith("prompts.") && !row.component_id.startsWith("context.")
    && row.component_id !== "agents.execute"),
    { ...base, component_id: "models.source", component_version: "2", default_config: {
      reference: model.reference, parameters: { ...model.parameters, max_tokens: 1024 }, capacity } },
    { ...base, component_id: "prompts.item", component_version: "2", default_config: {
      ...(catalog.find(row => row.component_id === "prompts.item")!.default_config),
      lifecycle: "per_request", compaction: "never" } },
    { ...base, component_id: "prompts.source", component_version: "2", default_config: {
      value: { schema_version: 1, kind: "workflow.prompt-materials", items: [] }, member_id: crypto.randomUUID(),
      presentation: {}, lifecycle: "per_request", compaction: "never" } },
    ...["context.output", "context.assembly", "context.merge", "agents.execute"].map(component_id =>
      ({ ...base, component_id, component_version: "4" })),
    { ...base, component_id: "agents.compaction-policy", default_config: {
      enabled: true, trigger_tokens: 0, keep_depth: 2, summary_prompt: "", target_tokens: null } },
  ];
  it("uses only explicitly versioned native paths and leaves provider capacity unconfigured", () => {
    const doc = createCompactingSerialAgentDemo(nativeCatalog, packageLock);
    expect(isGraphDocument(doc)).toBe(true);
    expect(doc.nodes.find(row => row.component_id === "models.source")!).toMatchObject({
      component_version: "2", config: { capacity, reference: { resource_id: "" }, parameters: { model: "", max_tokens: 1024 } } });
    expect(doc.nodes.filter(row => row.component_id.startsWith("context."))).toHaveLength(6);
    expect(doc.nodes.filter(row => row.component_id.startsWith("context.")).every(row => row.component_version === "4")).toBe(true);
    expect(doc.nodes.filter(row => row.component_id === "agents.execute").every(row => row.component_version === "4")).toBe(true);
    expect(doc.object_bindings!.filter(row => row.type_id === "workflow.effective-context")
      .map(row => row.schema_version)).toEqual([4, 4]);
    expect(doc.nodes.some(row => row.component_id === "tools.text-to-prompt")).toBe(false);
    expect(doc.nodes.find(row => row.title === "A 本轮分析材料")!).toMatchObject({
      component_id: "prompts.source", component_version: "2",
      config: { lifecycle: "per_request", compaction: "never", presentation: { role: "user", placement: "after" } } });
  });
  it("wires independent policies and exact capacity configuration without sharing mutable drafts", () => {
    const configured = { ...model, parameters: { ...model.parameters, max_tokens: 1024 },
      capacity: { ...capacity, context_window_tokens: 128000, max_cold_input_tokens: 128000 } };
    const doc = createCompactingSerialAgentDemo(nativeCatalog, packageLock, configured);
    expect(doc.nodes.find(row => row.component_id === "models.source")!.config).toEqual(configured);
    const policies = doc.nodes.filter(row => row.component_id === "agents.compaction-policy");
    expect(policies).toHaveLength(2);
    for (const label of ["A", "B"]) {
      expect(doc.edges).toContainEqual(expect.objectContaining({
        source_node_id: policies.find(row => row.title === `${label} 精简策略`)!.node_binding_id,
        target_node_id: doc.nodes.find(row => row.title === `Agent ${label}`)!.node_binding_id,
        target_port_id: "compaction_policy" }));
    }
    configured.capacity.summary_max_tokens = 42;
    expect((doc.nodes.find(row => row.component_id === "models.source")!.config.capacity as typeof capacity)
      .summary_max_tokens).toBe(128);
    expect(() => createCompactingSerialAgentDemo(catalog, packageLock)).toThrow("agents.compaction-policy@1");
  });
});
