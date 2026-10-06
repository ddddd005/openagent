import { describe, expect, it } from "vitest";
import { graphClone, isGraphDocument, isGraphEventBindings, newGraph, remapGraphEventBindings, upgradeGraph } from "./workflowGraph";

function fixture() {
  const document = upgradeGraph(newGraph("events"));
  const id = crypto.randomUUID();
  document.nodes.push({ node_binding_id: id, component_id: "plugin.local", component_version: "1",
    title: "Local", position: { x: 0, y: 0 }, config: {} });
  const binding = { event_id: "edit-state", schema_version: 1, display_name: "Edit state",
    audience: "consumer" as const, target_node_ids: [id], payload_schema: { type: "object" as const,
      properties: { edit: { type: "string" } }, required: ["edit"], additionalProperties: false } };
  document.event_bindings = [binding]; return { document, binding, id };
}
describe("graph v2 local event declarations", () => {
  it("accepts an event-only graph with explicit empty ordinary roots and distinct versions of one ID", () => {
    const { document, binding } = fixture();
    document.event_bindings!.push({ ...graphClone(binding), schema_version: 2 });
    expect(document.execution_roots).toEqual([]); expect(isGraphDocument(document)).toBe(true);
    delete document.execution_roots; expect(isGraphDocument(document)).toBe(false);
    document.schema_version = 1; expect(isGraphDocument(document)).toBe(false);
  });
  it("rejects duplicate identity pairs, missing/duplicate targets and malformed local schemas", () => {
    const { document, binding } = fixture();
    expect(isGraphEventBindings([binding, binding], document.nodes)).toBe(false);
    for (const target_node_ids of [[], [crypto.randomUUID()], [binding.target_node_ids[0], binding.target_node_ids[0]]])
      expect(isGraphEventBindings([{ ...binding, target_node_ids }], document.nodes)).toBe(false);
    for (const payload_schema of [{ type: "string" }, { type: "object", $ref: "https://host/schema" },
      { type: "object", properties: { edit: { $dynamicRef: "file:///private" } } }])
      expect(isGraphEventBindings([{ ...binding, payload_schema }], document.nodes)).toBe(false);
    expect(isGraphEventBindings([{ ...binding, payload_schema: { type: "object", $defs: { edit: { type: "string" } },
      properties: { edit: { $ref: "#/$defs/edit" } } } }], document.nodes)).toBe(true);
    expect(isGraphEventBindings([{ ...binding, undocumented: true }], document.nodes)).toBe(false);
    expect(isGraphEventBindings([{ ...binding, payload_schema: { type: "object",
      properties: { literal: { const: { $ref: "https://literal-data" }, default: { $dynamicRef: "not-a-schema" },
        examples: [{ $recursiveRef: "literal" }] } } } }], document.nodes)).toBe(true);
  });
  it("remaps graph-copy targets without mutating original schemas or event identities", () => {
    const { binding, id } = fixture(), next = crypto.randomUUID();
    const result = remapGraphEventBindings([binding], new Map([[id, next]]));
    expect(result[0]).toEqual({ ...binding, target_node_ids: [next] });
    expect(binding.target_node_ids).toEqual([id]); expect(result[0].payload_schema).not.toBe(binding.payload_schema);
  });
});
