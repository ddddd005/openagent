import { describe, expect, it } from "vitest";
import { graphClone, isGraphDocument, newGraph, upgradeGraph, type GraphDocument } from "./workflowGraph";
import { isGraphConfigurationDocument, isGraphConfigurationUpdateDocument } from "./workflowConfiguration";

function fixture(): GraphDocument {
  return { schema_version: 3, workflow_definition_id: crypto.randomUUID(), revision: 4, edit_sequence: 7,
    name: "Current configuration", object_bindings: [],
    package_lock: [{ package_id: "missing.package", version: "9.0.0" }],
    nodes: [{ node_binding_id: crypto.randomUUID(), component_id: "missing.node", component_version: "9",
      title: "Unavailable", position: { x: 1, y: -2 }, config: {},
      configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }], edges: [] };
}

describe("schema 3 storage boundary", () => {
  it("preserves missing packages, incomplete config and stale data ports without compiling", () => {
    const value = fixture();
    value.edges.push({ edge_id: crypto.randomUUID(), source_node_id: value.nodes[0].node_binding_id,
      source_port_id: "old-port", target_node_id: crypto.randomUUID(), target_port_id: "unconnected", order: 0 });
    const original = graphClone(value);
    expect(isGraphDocument(value)).toBe(true);
    expect(isGraphConfigurationDocument(value)).toBe(true);
    expect(value).toEqual(original);
    expect(value.nodes[0].config).toEqual({});
  });
  it("distinguishes immutable seed, initial editing document and formal current projection", () => {
    const value = fixture();
    delete value.edit_sequence;
    expect(isGraphDocument(value)).toBe(true);
    expect(isGraphConfigurationUpdateDocument(value)).toBe(true);
    expect(isGraphConfigurationDocument(value)).toBe(false);
    value.edit_sequence = 0;
    expect(isGraphConfigurationUpdateDocument(value)).toBe(true);
    expect(isGraphConfigurationDocument(value)).toBe(false);
    value.edit_sequence = 1;
    expect(isGraphConfigurationDocument(value)).toBe(true);
  });
  it("does not upgrade existing graphs or downgrade schema 3 through the old upgrade helper", () => {
    const old = newGraph("old");
    const before = graphClone(old);
    expect(isGraphDocument(old)).toBe(true);
    expect(isGraphConfigurationUpdateDocument(old)).toBe(false);
    expect(old).toEqual(before);
    const current = fixture(), original = graphClone(current);
    expect(() => upgradeGraph(current)).toThrow("Schema 3");
    expect(current).toEqual(original);
  });
  it.each([
    (value: GraphDocument) => { delete value.nodes[0].configuration_policy; },
    (value: GraphDocument) => { value.nodes[0].configuration_policy = { schema_version: 2, prompt_mode: "fixed" } as never; },
    (value: GraphDocument) => { value.nodes[0].configuration_policy = { schema_version: 1, prompt_mode: "other" } as never; },
    (value: GraphDocument) => { Object.assign(value.nodes[0].configuration_policy!, { fields: ["text"] }); },
    (value: GraphDocument) => { Object.assign(value, { declared_adjustable: ["text"] }); },
    (value: GraphDocument) => { Object.assign(value.nodes[0], { executable: true }); },
    (value: GraphDocument) => { Object.assign(value.nodes[0].position, { z: 1 }); },
    (value: GraphDocument) => { Object.assign(value.package_lock![0], { installed: true }); },
    (value: GraphDocument) => { value.execution_roots = null as never; },
    (value: GraphDocument) => { value.control_edges = null as never; },
    (value: GraphDocument) => { value.edit_sequence = -1; },
    (value: GraphDocument) => { value.edit_sequence = Number.MAX_SAFE_INTEGER + 1; },
    (value: GraphDocument) => { value.workflow_definition_id = value.workflow_definition_id.toUpperCase(); },
    (value: GraphDocument) => { value.nodes[0].public_outputs = ["output", "output"]; },
  ])("rejects policy, identity and storage envelope violations %#", change => {
    const value = fixture();
    change(value);
    expect(isGraphDocument(value)).toBe(false);
  });
  it("uses backend graph limits, UTF-8 wire limits and JSON business values", () => {
    const value = fixture();
    const node = graphClone(value.nodes[0]);
    value.nodes = Array.from({ length: 513 }, () => ({ ...node, node_binding_id: crypto.randomUUID() }));
    expect(isGraphDocument(value)).toBe(false);
    value.nodes = [node];
    value.nodes[0].config = { text: "\u4e2d".repeat(1_334_000) };
    expect(isGraphDocument(value)).toBe(false);
    value.nodes[0].config = { value: undefined };
    expect(isGraphDocument(value)).toBe(false);
    value.nodes[0].config = { callback: () => true };
    expect(isGraphDocument(value)).toBe(false);
    value.nodes[0].config = { literal_null: null, list: [1, false, ""] };
    value.nodes[0].title = "\ud83d\ude00".repeat(128);
    expect(isGraphDocument(value)).toBe(true);
  });
  it("retains owner/control integrity without enforcing an execution cycle policy", () => {
    const value = fixture(), id = value.nodes[0].node_binding_id;
    value.object_bindings = [{ object_key: "shared/context", type_id: "missing.context", schema_version: 1,
      scope: "shared", owner_node_id: null, readers: [id], writers: [id], default_value: null }];
    value.control_edges = [{ edge_id: crypto.randomUUID(), source_node_id: id, target_node_id: id }];
    expect(isGraphDocument(value)).toBe(true);
    value.object_bindings[0].readers.push(crypto.randomUUID());
    expect(isGraphDocument(value)).toBe(false);
  });
  it("counts package, object and event names by backend Unicode code points", () => {
    const value = fixture(), id = value.nodes[0].node_binding_id, name = "\ud83d\ude00".repeat(128);
    value.package_lock = [{ package_id: name, version: name }];
    value.object_bindings = [{ object_key: name, type_id: name, schema_version: 1,
      scope: "shared", owner_node_id: null, readers: [id], writers: [] }];
    value.execution_roots = [];
    value.event_bindings = [{ event_id: name, schema_version: 1, display_name: name,
      audience: "consumer", target_node_ids: [id], payload_schema: { type: "object" } }];
    expect(isGraphDocument(value)).toBe(true);
    value.event_bindings[0].display_name += "\ud83d\ude00";
    expect(isGraphDocument(value)).toBe(false);
  });
  it.each([
    { text: "\ud800" }, { text: "\udc00" }, { text: "\ud800x" }, { "\ud800": "value" },
  ])("rejects strings and object keys that cannot encode as backend UTF-8 %#", config => {
    const value = fixture();
    value.nodes[0].config = config;
    expect(isGraphDocument(value)).toBe(false);
  });
});
