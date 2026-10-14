import { describe, expect, it } from "vitest";
import { editFrontendSource, frontendSources, frontendWiringUnavailable, wireFrontendDisplay } from "./frontendWiring";
import { graphClone, isGraphDocument, newGraph, type GraphDocument, type GraphNodeType } from "./workflowGraph";
import { frontendCatalog, frontendLock } from "../testUtils/frontendFixture";

function fixture() {
  const document = newGraph("Explicit frontend");
  const source = { node_binding_id: crypto.randomUUID(), component_id: "tools.current-input", component_version: "1",
    title: "Source", position: { x: 0, y: 0 }, config: {} };
  const registrar = crypto.randomUUID();
  document.nodes.push(source, { node_binding_id: registrar, component_id: "tools.output", component_version: "2",
    title: "Test output", position: { x: 0, y: 100 }, config: { mode: "text" } });
  document.object_bindings!.push({ object_key: "item/output-test", type_id: "workflow.frontend-state",
    schema_version: 1, scope: "shared", owner_node_id: null, readers: [], writers: [],
    registrar_node_id: registrar, default_value: { entries: [], view_ref: null } });
  return { document, source, request: { sourceNodeId: source.node_binding_id, sourcePortId: "output",
    objectKey: "item/output-test", role: "user" as const } };
}
const wire = (document: GraphDocument, request: Parameters<typeof wireFrontendDisplay>[3], catalog = frontendCatalog) =>
  wireFrontendDisplay(document, catalog, frontendLock, request);
const definition = (componentId: string) => frontendCatalog.find(type =>
  type.component_id === componentId && type.component_version === "1")!;
function addWriter(document: GraphDocument, type: GraphNodeType, active: boolean) {
  const id = crypto.randomUUID();
  const objectKey = document.object_bindings![0]!.object_key;
  document.nodes.push({ node_binding_id: id, component_id: type.component_id, component_version: "1",
    title: "Old writer", position: { x: 0, y: 200 }, config: { object_key: objectKey } });
  document.object_bindings![0].readers.push(id); document.object_bindings![0].writers.push(id);
  if (active) document.execution_roots!.push(id);
  return id;
}
describe("explicit frontend recommendation", () => {
  it("does not alter a blank graph or create anything while inspecting sources and availability", () => {
    const document = newGraph("Blank"), before = graphClone(document);
    expect(frontendSources(document, frontendCatalog)).toEqual([]);
    expect(frontendWiringUnavailable(frontendCatalog, frontendLock)).toBeNull();
    expect(document).toEqual(before);
  });
  it("uses the exact registered item for read → append → public display", () => {
    const { document, request, source } = fixture();
    const [read, append, presentation] = wire(document, request);
    expect(document.execution_roots).toEqual([presentation]);
    expect(document.control_edges).toEqual([]);
    expect(document.edges.map(edge => [edge.source_node_id, edge.source_port_id, edge.target_node_id, edge.target_port_id])).toEqual([
      [read, "view", append, "view"], [source.node_binding_id, "output", append, "content"], [append, "view", presentation, "view"],
    ]);
    expect(document.nodes.find(node => node.node_binding_id === presentation)?.public_outputs).toEqual(["display"]);
    expect(document.object_bindings![0]).toMatchObject({ readers: [read, append], writers: [append],
      scope: "shared", type_id: "workflow.frontend-state", schema_version: 1,
      registrar_node_id: document.nodes[1]!.node_binding_id, default_value: { entries: [], view_ref: null } });
    expect(document.package_lock).toEqual(frontendLock); expect(isGraphDocument(document)).toBe(true);
  });
  it("requires an existing exact item binding instead of inventing a fixed key", () => {
    const { document, request } = fixture();
    document.object_bindings = [];
    const before = graphClone(document);
    expect(() => wire(document, request)).toThrow("已登记");
    expect(document).toEqual(before);
  });
  it("extends one shared object without adding an access order and reuses the presentation", () => {
    const { document, request } = fixture();
    const [firstRead, firstAppend, presentation] = wire(document, request);
    const userControlEdge = { edge_id: crypto.randomUUID(), source_node_id: document.nodes[0]!.node_binding_id,
      target_node_id: firstRead };
    document.control_edges!.push(userControlEdge);
    const oldEdges = graphClone(document.edges.filter(edge => edge.target_node_id !== presentation));
    const [secondRead, secondAppend, reused] = wire(document, { ...request, role: "assistant" });
    expect(reused).toBe(presentation);
    expect(document.nodes.filter(node => node.component_id === "frontend.presentation")).toHaveLength(1);
    expect(document.execution_roots).toEqual([presentation]);
    expect(document.control_edges).toEqual([userControlEdge]);
    expect(document.edges.filter(edge => edge.target_node_id === presentation)).toMatchObject([
      { source_node_id: secondAppend, source_port_id: "view" },
    ]);
    expect(document.edges).toEqual(expect.arrayContaining(oldEdges));
    expect(document.edges.some(edge => edge.source_node_id === firstAppend && edge.target_node_id === secondAppend)).toBe(false);
    expect(document.object_bindings![0].readers).toEqual([firstRead, firstAppend, secondRead, secondAppend]);
  });
  it("does not activate an isolated configured writer merely because it is authorized for the object", () => {
    const { document, request } = fixture(); wire(document, request);
    const writer = { ...definition("frontend.state.output"), component_id: "plugin.writer", inputs: [], outputs: [],
      object_accesses: [{ ...definition("frontend.state.append").object_accesses![0] }] };
    const dormant = addWriter(document, writer, false);
    const [, , presentation] = wire(document, request, [...frontendCatalog, writer]);
    expect(document.execution_roots).toEqual([presentation]);
    expect(document.control_edges!.some(edge => edge.source_node_id === dormant)).toBe(false);
    expect(document.nodes.some(node => node.node_binding_id === dormant)).toBe(true);
  });
  it("does not treat an authorization-only node as an actual object writer", () => {
    const { document, request } = fixture(); wire(document, request);
    const unrelated = { ...definition("tools.current-input"), component_id: "plugin.unrelated", outputs: [] };
    const id = addWriter(document, unrelated, true);
    expect(() => wire(document, request, [...frontendCatalog, unrelated])).not.toThrow();
    expect(document.control_edges!.some(edge => edge.source_node_id === id)).toBe(false);
  });
  it("allows unordered active object writers without inventing an execution order", () => {
    const { document, request } = fixture(); wire(document, request);
    const writer = { ...definition("frontend.state.append"), component_id: "plugin.writer", inputs: [], outputs: [] };
    addWriter(document, writer, true);
    expect(() => wire(document, request, [...frontendCatalog, writer])).not.toThrow();
    expect(document.control_edges).toEqual([]);
  });
  it.each(["permission", "type", "missing-definition"] as const)(
    "rejects a reused object with an invalid active access (%s) before editing the graph", failure => {
      const { document, request } = fixture(); wire(document, request);
      let catalog: GraphNodeType[] = frontendCatalog, message = "";
      const binding = document.object_bindings![0]!;
      if (failure === "permission") {
        const originalAppend = document.nodes.find(node => node.component_id === "frontend.state.append")!;
        binding.writers = binding.writers.filter(id => id !== originalAppend.node_binding_id);
        message = "权限";
      } else if (failure === "type") {
        catalog = frontendCatalog.map(type => type.component_id === "frontend.state.append"
          ? { ...type, object_accesses: type.object_accesses!.map(access => ({ ...access, type_id: "workflow.other-state" })) }
          : type);
        message = "绑定不匹配";
      } else {
        const writer = { ...definition("frontend.state.append"), component_id: "plugin.writer", inputs: [], outputs: [] };
        addWriter(document, writer, true);
        message = "类型缺失";
      }
      const before = graphClone(document);

      expect(() => wire(document, request, catalog)).toThrow(message);
      expect(document).toEqual(before);
    },
  );
  it("rejects a source downstream of the reused presentation instead of making a cycle", () => {
    const { document, request } = fixture(); const [, , presentation] = wire(document, request);
    const bridge = { ...definition("tools.regex"), component_id: "plugin.bridge",
      inputs: [{ ...definition("frontend.presentation").outputs[0], port_id: "input" }] };
    const id = crypto.randomUUID(); document.nodes.push({ node_binding_id: id, component_id: bridge.component_id,
      component_version: "1", title: "Bridge", position: { x: 0, y: 0 }, config: {} });
    document.edges.push({ edge_id: crypto.randomUUID(), source_node_id: presentation, source_port_id: "display",
      target_node_id: id, target_port_id: "input", order: 0 });
    expect(() => wire(document, { ...request, sourceNodeId: id }, [...frontendCatalog, bridge])).toThrow("循环");
  });
  it("rejects dormant or multiple existing public presentations rather than silently creating another", () => {
    const { document, request } = fixture(); const [, , presentation] = wire(document, request);
    document.execution_roots = [];
    expect(() => wire(document, request)).toThrow("未启用");
    document.execution_roots = [presentation];
    const duplicate = { ...graphClone(document.nodes.find(node => node.node_binding_id === presentation)!), node_binding_id: crypto.randomUUID() };
    document.nodes.push(duplicate);
    document.edges.push({ ...graphClone(document.edges.find(edge => edge.target_node_id === presentation)!),
      edge_id: crypto.randomUUID(), target_node_id: duplicate.node_binding_id });
    expect(() => wire(document, request)).toThrow("多个公开");
  });
  it("rejects missing package versions and incompatible source ports explicitly", () => {
    const { document, request } = fixture();
    const before = graphClone(document);
    expect(() => wireFrontendDisplay(document, frontendCatalog, [], request)).toThrow("packages.configure");
    expect(() => wire(document, request, frontendCatalog.filter(type => type.component_id !== "frontend.presentation"))).toThrow("未加载");
    const catalog = frontendCatalog.map(type => type.component_id === "tools.current-input"
      ? { ...type, outputs: [{ ...type.outputs[0], data_schema_version: 1 }] } : type);
    expect(() => wire(document, request, catalog)).toThrow("TEXT@2");
    expect(document).toEqual(before);
  });
  it("keeps existing edge identity/order during source replacement and rejects a cycle", () => {
    const { document, request } = fixture(); const [, append] = wire(document, request);
    const source = { ...graphClone(document.nodes[0]), node_binding_id: crypto.randomUUID(), title: "Replacement" };
    document.nodes.push(source);
    const edge = document.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;
    const identity = edge.edge_id, order = edge.order;
    expect(editFrontendSource(document, frontendCatalog, append, identity, { nodeId: source.node_binding_id, portId: "output" })).toBe(true);
    expect(edge).toMatchObject({ edge_id: identity, order, source_node_id: source.node_binding_id });
    document.control_edges!.push({ edge_id: crypto.randomUUID(), source_node_id: append, target_node_id: source.node_binding_id });
    expect(() => editFrontendSource(document, frontendCatalog, append, identity, { nodeId: source.node_binding_id, portId: "output" })).toThrow("循环");
  });
  it("allows source edits with unordered object accesses while retaining permission checks", () => {
    const { document, request } = fixture(); const [, append] = wire(document, request);
    const writer = { ...definition("frontend.state.append"), component_id: "plugin.writer", inputs: [], outputs: [] };
    const writerId = addWriter(document, writer, true);
    const source = { ...graphClone(document.nodes[0]), node_binding_id: crypto.randomUUID(), title: "Replacement" };
    document.nodes.push(source);
    const content = document.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;

    expect(editFrontendSource(document, [...frontendCatalog, writer], append, content.edge_id,
      { nodeId: source.node_binding_id, portId: "output" })).toBe(true);
    expect(document.edges.find(edge => edge.edge_id === content.edge_id)?.source_node_id).toBe(source.node_binding_id);
    expect(document.control_edges).toEqual([]);

    document.object_bindings![0].writers = document.object_bindings![0].writers.filter(id => id !== writerId);
    expect(() => editFrontendSource(document, [...frontendCatalog, writer], append, content.edge_id,
      { nodeId: source.node_binding_id, portId: "output" })).toThrow("权限");
  });
});
