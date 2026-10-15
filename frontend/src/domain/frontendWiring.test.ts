import { describe, expect, it } from "vitest";
import { editFrontendSource, frontendSources } from "./frontendWiring";
import { graphClone, newGraph, type GraphDocument, type GraphNodeType } from "./workflowGraph";
import { explicitFrontendGraph, frontendCatalog } from "../testUtils/frontendFixture";

const definition = (id: string) => frontendCatalog.find(row => row.component_id === id)!;
function addWriter(document: GraphDocument, type: GraphNodeType) {
  const id = crypto.randomUUID(), objectKey = document.object_bindings![0]!.object_key;
  document.nodes.push({ node_binding_id: id, component_id: type.component_id, component_version: "1",
    title: "Existing writer", position: { x: 0, y: 200 }, config: { object_key: objectKey } });
  document.object_bindings![0].readers.push(id); document.object_bindings![0].writers.push(id);
  document.execution_roots!.push(id);
  return id;
}
describe("existing explicit frontend source edits", () => {
  it("inspects a blank graph without creating nodes, bindings or edges", () => {
    const document = newGraph("Blank"), before = graphClone(document);
    expect(frontendSources(document, frontendCatalog)).toEqual([]);
    expect(document).toEqual(before);
  });
  it("keeps edge identity and order during an exact source replacement and rejects a cycle", () => {
    const { document, appendId } = explicitFrontendGraph();
    const source = { ...graphClone(document.nodes[0]), node_binding_id: crypto.randomUUID(), title: "Replacement" };
    document.nodes.push(source);
    const edge = document.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    const identity = edge.edge_id, order = edge.order;
    expect(editFrontendSource(document, frontendCatalog, appendId, identity,
      { nodeId: source.node_binding_id, portId: "output" })).toBe(true);
    expect(edge).toMatchObject({ edge_id: identity, order, source_node_id: source.node_binding_id });
    document.control_edges!.push({ edge_id: crypto.randomUUID(), source_node_id: appendId,
      target_node_id: source.node_binding_id });
    expect(() => editFrontendSource(document, frontendCatalog, appendId, identity,
      { nodeId: source.node_binding_id, portId: "output" })).toThrow("循环");
  });
  it("allows unordered declared object writers while retaining their permission checks", () => {
    const { document, appendId } = explicitFrontendGraph();
    const writer = { ...definition("frontend.state.append"), component_id: "plugin.writer", inputs: [], outputs: [] };
    const writerId = addWriter(document, writer);
    const source = { ...graphClone(document.nodes[0]), node_binding_id: crypto.randomUUID(), title: "Replacement" };
    document.nodes.push(source);
    const edge = document.edges.find(row => row.target_node_id === appendId && row.target_port_id === "content")!;
    expect(editFrontendSource(document, [...frontendCatalog, writer], appendId, edge.edge_id,
      { nodeId: source.node_binding_id, portId: "output" })).toBe(true);
    expect(document.control_edges).toEqual([]);
    document.object_bindings![0].writers = document.object_bindings![0].writers.filter(id => id !== writerId);
    expect(() => editFrontendSource(document, [...frontendCatalog, writer], appendId, edge.edge_id,
      { nodeId: source.node_binding_id, portId: "output" })).toThrow("权限");
  });
  it("rejects duplicate or incompatible source ports and ignores foreign node and edge identities", () => {
    const { document, sourceId, appendId } = explicitFrontendGraph();
    const before = graphClone(document);
    expect(() => editFrontendSource(document, frontendCatalog, appendId, null,
      { nodeId: sourceId, portId: "output" })).toThrow("已接入");
    expect(() => editFrontendSource(document, frontendCatalog, appendId, null,
      { nodeId: appendId, portId: "view" })).toThrow("TEXT@2");
    expect(editFrontendSource(document, frontendCatalog, crypto.randomUUID(), null, null)).toBe(false);
    expect(editFrontendSource(document, frontendCatalog, appendId, crypto.randomUUID(), null)).toBe(false);
    expect(document).toEqual(before);
  });
});
