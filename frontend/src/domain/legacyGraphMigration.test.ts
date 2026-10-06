import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { migrateLegacyGraph } from "./legacyGraphMigration";
import { graphClone, isGraphDocument, type GraphDocument, type GraphNodeType } from "./workflowGraph";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";

export function migrationCatalog(): GraphNodeType[] {
  const port = (port_id: string, data_type = "PROMPT") => ({ port_id, data_type, required: true, multiple: false });
  const row = (name: string, inputs: string[], outputs: string[], version = "1"): GraphNodeType => ({
    component_id: `workflow.${name}`, component_version: version, display_name: name, category: "test",
    config_schema: { type: "object" }, default_config: {}, inputs: inputs.map(id => port(id)),
    outputs: outputs.map(id => port(id)), is_output: name === "output", executable: true,
  });
  return [row("agent", ["prompt", "model"], ["output", "output_json", "context_delta"], "2"), row("model-provider", [], ["model"], "2"),
    row("prompt-item", [], ["output"]), row("prompt-group", [], ["output"]), row("prompt-summary", ["input"], ["output"], "2"),
    row("tool", [], ["tool-descriptions", "tool-schemas"]), row("tool-summary", ["tool-descriptions", "tool-schemas"], ["tool-descriptions", "tool-schemas"]),
    row("context", [], ["output"]), row("current-input", [], ["output"]), row("prompt-assembly", ["input", "current_input"], ["output"]),
    row("global-content", [], ["output"]), row("regex", ["input"], ["output"]), row("output", ["input"], ["output"]),
    row("variable-register", ["text"], ["text"]), row("variable-assign", ["text"], ["text"]), row("session-data-write", ["text"], ["json"])];
}
export function legacyFixture(): GraphDocument {
  let raw: string | null = null;
  useWorkbenchPersistenceStore().initialize({ getItem: () => raw, setItem: (_key, value) => { raw = value; } });
  return JSON.parse(raw!).documents[MAIN_WORKFLOW_ID].document;
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkbenchPersistenceStore().$dispose(); useWorkflowGraphStore().$dispose(); });
describe("explicit immutable A/B migration", () => {
  it("converts the complete named graph, preserves prompts and tools, and rewires B through data dependencies", () => {
    const source = legacyFixture(); const before = graphClone(source);
    const result = migrateLegacyGraph(source, migrationCatalog(), crypto.randomUUID());
    expect(result.diagnostics).toEqual([]); expect(isGraphDocument(result.document)).toBe(true);
    expect(source).toEqual(before);
    expect(result.document.workflow_definition_id).not.toBe(source.workflow_definition_id);
    expect(result.document.nodes.every(node => node.component_id.startsWith("workflow."))).toBe(true);
    const prompts = source.nodes.filter(node => node.component_id === "legacy.preparation.prompt-item");
    for (const prompt of prompts) expect(result.document.nodes.find(node => node.node_binding_id === prompt.node_binding_id)?.config).toEqual(prompt.config);
    const a = source.nodes.find(node => node.component_id === "legacy.agent" && node.config.stage === "A")!;
    const bAssembly = source.nodes.find(node => node.component_id === "legacy.preparation.assemble" && node.config.targetStage === "B")!;
    expect(result.document.edges.some(edge => edge.source_node_id === a.node_binding_id
      && edge.target_node_id === bAssembly.node_binding_id && edge.target_port_id === "current_input")).toBe(true);
    expect(result.document.nodes.filter(node => node.component_id === "workflow.current-input")).toHaveLength(1);
    expect(result.mappings).toHaveLength(2);
    for (const context of result.document.nodes.filter(node => node.component_id === "workflow.context"))
      expect(result.mappings.some(mapping => mapping.target_node_id === context.config.source_node_id)).toBe(true);
    expect(result.document.edges.filter(edge => edge.source_port_id === "tool-schemas").length).toBeGreaterThan(0);
    const ordinals = result.document.edges.map(edge => `${edge.target_node_id}/${edge.target_port_id}/${edge.order}`);
    expect(new Set(ordinals).size).toBe(ordinals.length);
  });
  it("preserves unfamiliar nodes and rejects missing mappings instead of pretending conversion is complete", () => {
    const source = legacyFixture();
    source.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "legacy.future", component_version: "1",
      title: "future", position: { x: 1, y: 2 }, config: { retained: true } });
    source.edges[0].source_port_id = "unknown";
    const before = graphClone(source); const result = migrateLegacyGraph(source, migrationCatalog());
    expect(result.diagnostics.some(row => row.port_id === "unknown")).toBe(true);
    expect(result.diagnostics.some(row => row.message.includes("legacy.future"))).toBe(true);
    expect(source).toEqual(before);
  });
  it("refuses edited root semantics and cross-session or explicit historical context selections", () => {
    const source = legacyFixture();
    const input = source.nodes.find(node => node.component_id === "legacy.preparation.root-input")!;
    input.config.text = "custom root";
    const context = source.nodes.find(node => node.component_id === "legacy.preparation.context")!;
    context.config.reference = { workflowSessionId: crypto.randomUUID(), selectedChainId: crypto.randomUUID() };
    const result = migrateLegacyGraph(source, migrationCatalog(), crypto.randomUUID());
    expect(result.diagnostics.filter(row => row.node_id === input.node_binding_id).length).toBeGreaterThan(0);
    expect(result.diagnostics.filter(row => row.node_id === context.node_binding_id)).toHaveLength(2);
  });
  it("requires the exact registered version without changing frozen Agent v1 placeholders", () => {
    const source = legacyFixture(); const catalog = migrationCatalog();
    catalog.find(row => row.component_id === "workflow.agent")!.component_version = "1";
    const result = migrateLegacyGraph(source, catalog);
    expect(result.document.nodes.filter(node => node.component_id === "workflow.agent").every(node => node.component_version === "2")).toBe(true);
    expect(result.diagnostics.some(row => row.message.includes("尚未接入"))).toBe(true);
  });
  it("creates no history mappings without a source session", () => {
    expect(migrateLegacyGraph(legacyFixture(), migrationCatalog()).mappings).toEqual([]);
  });
  it("rejects old implicit writer effects until a real output dependency preserves their execution", () => {
    const source = legacyFixture();
    const writer = { node_binding_id: crypto.randomUUID(), component_id: "legacy.preparation.variable-register",
      component_version: "1", title: "变量注册", position: { x: 1, y: 2 },
      config: { name: "counter", valueType: "string", hasInitialValue: true, initialValue: "0" } };
    source.nodes.push(writer);
    const result = migrateLegacyGraph(source, migrationCatalog());
    expect(result.diagnostics.some(row => row.node_id === writer.node_binding_id && row.message.includes("隐式"))).toBe(true);
    const output = source.nodes.find(node => node.component_id === "legacy.output")!;
    source.edges = source.edges.filter(edge => edge.target_node_id !== output.node_binding_id);
    source.edges.push({ edge_id: crypto.randomUUID(), source_node_id: writer.node_binding_id, source_port_id: "text",
      target_node_id: output.node_binding_id, target_port_id: "in", order: 1 });
    const linked = migrateLegacyGraph(source, migrationCatalog());
    expect(linked.diagnostics).toEqual([]);
    expect(source.nodes.find(node => node.node_binding_id === writer.node_binding_id)?.component_id).toBe("legacy.preparation.variable-register");
  });
});
