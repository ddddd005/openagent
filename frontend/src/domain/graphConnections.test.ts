import { describe, expect, it } from "vitest";
import { graphConnectionIssue } from "./graphConnections";
import { graphClone, newGraph, type GraphNodeType } from "./workflowGraph";
const type: GraphNodeType = { component_id: "test", component_version: "1", display_name: "Test", category: "test",
  config_schema: {}, default_config: {}, executable: true, is_output: false,
  inputs: [{ port_id: "in", data_type: "TEXT", data_schema_version: 2, multiple: false, required: true }],
  outputs: [{ port_id: "out", data_type: "TEXT", data_schema_version: 2, multiple: false, required: true }] };
function fixture() {
  const document = newGraph("test");
  document.nodes = [1, 2, 3].map(value => ({ node_binding_id: String(value), component_id: "test", component_version: "1",
    title: "", config: {}, position: { x: 0, y: 0 } }));
  const edge = { source_node_id: "1", target_node_id: "2", source_port_id: "out", target_port_id: "in" };
  return { document, edge, catalog: [graphClone(type)] };
}
describe("shared connection diagnostics", () => {
  it("accepts compatible ports without mutating the graph", () => {
    const { document, edge, catalog } = fixture(), before = graphClone(document);
    expect(graphConnectionIssue(document, catalog, edge)).toBeNull(); expect(document).toEqual(before);
  });
  it("checks exact schema versions and missing dynamic modes", () => {
    const { document, edge, catalog } = fixture();
    catalog[0]!.outputs[0]!.data_schema_version = 1;
    expect(graphConnectionIssue(document, catalog, edge)).toContain("不兼容");
    catalog[0]!.port_modes = { other: { inputs: [], outputs: [] } };
    expect(graphConnectionIssue(document, catalog, edge)).toContain("模式");
  });
  it("checks effective dynamic ports, not the default port list", () => {
    const { document, edge, catalog } = fixture();
    catalog[0]!.port_modes = { active: { inputs: type.inputs, outputs: [] } };
    document.nodes.forEach(node => { node.config.mode = "active"; });
    expect(graphConnectionIssue(document, catalog, edge)).toContain("端口缺失");
  });
  it("rejects duplicate and single-source overwrites but allows multiple inputs", () => {
    const { document, edge, catalog } = fixture();
    document.edges.push({ ...edge, edge_id: "edge", order: 0 });
    expect(graphConnectionIssue(document, catalog, edge)).toContain("已存在");
    const next = { ...edge, source_node_id: "3" };
    expect(graphConnectionIssue(document, catalog, next)).toContain("一条");
    catalog[0]!.inputs[0]!.multiple = true;
    expect(graphConnectionIssue(document, catalog, next)).toBeNull();
  });
  it("rejects self, data and combined data/control cycles", () => {
    const { document, edge, catalog } = fixture();
    expect(graphConnectionIssue(document, catalog, { ...edge, target_node_id: "1" })).toContain("循环");
    document.edges.push({ ...edge, edge_id: "old", order: 0 });
    document.control_edges = [{ edge_id: "control", source_node_id: "2", target_node_id: "3" }];
    expect(graphConnectionIssue(document, catalog, { ...edge, source_node_id: "3", target_node_id: "1" })).toContain("循环");
  });
  it("diagnoses unavailable node versions", () => {
    const { document, edge } = fixture();
    expect(graphConnectionIssue(document, [], edge)).toContain("版本缺失");
  });
});
