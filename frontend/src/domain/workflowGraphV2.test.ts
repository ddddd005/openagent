import { describe, expect, it } from "vitest";
import { graphClone, graphEnumLabel, isGraphCatalog, isGraphDocument, isGraphObjectBindings,
  isGraphSession, newGraph, resolveGraphRunInputs, upgradeGraph,
  type GraphNodeType, type GraphRunDetail, type GraphSession } from "./workflowGraph";

function fixture() {
  const document = newGraph("v2 tools");
  document.nodes = [1, 2].map(index => ({ node_binding_id: crypto.randomUUID(),
    component_id: `plugin.node-${index}`, component_version: "1", title: `node ${index}`,
    position: { x: 100 * index, y: 0 }, config: {} }));
  upgradeGraph(document, [{ package_id: "plugin", version: "1.0.0" }]);
  const [source, target] = document.nodes.map(node => node.node_binding_id);
  document.execution_roots = [source];
  document.control_edges = [{ edge_id: crypto.randomUUID(), source_node_id: source, target_node_id: target }];
  document.object_bindings = [{ object_key: "shared/main", type_id: "plugin.scalar", schema_version: 1,
    scope: "shared", owner_node_id: null, readers: [source, target], writers: [source] }];
  return { document, source, target };
}

describe("v2 workflow document and registered object boundaries", () => {
  it("keeps v1 unchanged until an explicit v2 upgrade and retains the full v2 definition", () => {
    const old = newGraph("existing");
    expect(old.schema_version).toBe(1); expect(isGraphDocument(old)).toBe(true);
    const { document } = fixture();
    expect(isGraphDocument(document)).toBe(true);
    expect(graphClone(document)).toEqual(document);
    expect(document.package_lock).toEqual([{ package_id: "plugin", version: "1.0.0" }]);
    expect(isGraphDocument({ ...document, schema_version: 1 })).toBe(false);
    expect(isGraphDocument({ ...document, schema_version: "2" })).toBe(false);
    const noBindings = graphClone(document); delete noBindings.object_bindings;
    expect(isGraphDocument(noBindings)).toBe(false);
  });
  it("validates root membership and unique data/control edge identities without inventing cycle policy", () => {
    const { document, source, target } = fixture();
    expect(isGraphDocument({ ...document, execution_roots: [source, source] })).toBe(false);
    expect(isGraphDocument({ ...document, execution_roots: [crypto.randomUUID()] })).toBe(false);
    const malformed = graphClone(document);
    malformed.control_edges![0].source_node_id = crypto.randomUUID();
    expect(isGraphDocument(malformed)).toBe(false);
    document.edges.push({ edge_id: document.control_edges![0].edge_id, source_node_id: source,
      source_port_id: "out", target_node_id: target, target_port_id: "in", order: 0 });
    expect(isGraphDocument(document)).toBe(false);
    document.edges = [];
    document.control_edges!.push({ edge_id: crypto.randomUUID(), source_node_id: target, target_node_id: source });
    expect(isGraphDocument(document)).toBe(true); // The backend diagnoses runtime cycles.
  });
  it("adds newly loaded package identities without silently changing an existing exact version", () => {
    const { document } = fixture();
    upgradeGraph(document, [{ package_id: "plugin", version: "2.0.0" },
      { package_id: "new-package", version: "1.0.0" }]);
    expect(document.package_lock).toEqual([{ package_id: "plugin", version: "1.0.0" },
      { package_id: "new-package", version: "1.0.0" }]);
  });
  it("rejects duplicate keys, foreign nodes, extra binding fields and non-owner private permissions", () => {
    const { document, source, target } = fixture();
    const binding = document.object_bindings![0];
    expect(isGraphObjectBindings([binding, binding], document.nodes)).toBe(false);
    expect(isGraphObjectBindings([{ ...binding, writers: [crypto.randomUUID()] }], document.nodes)).toBe(false);
    expect(isGraphObjectBindings([{ ...binding, unintended_field: true }], document.nodes)).toBe(false);
    const privateBinding = { ...binding, scope: "private", owner_node_id: source, readers: [source], writers: [source] };
    expect(isGraphObjectBindings([privateBinding], document.nodes)).toBe(true);
    expect(isGraphObjectBindings([{ ...privateBinding, readers: [target] }], document.nodes)).toBe(false);
  });
  it("retains plugin metadata and exact port schema versions while validating input storage policies", () => {
    const type: GraphNodeType = { component_id: "plugin.writer", component_version: "1", display_name: "writer",
      category: "plugin", config_schema: {}, default_config: {}, inputs: [], outputs: [],
      is_output: false, executable: true, input_storage: "references", capabilities: ["objects:write"],
      plugin_metadata: { owner: "package" } };
    expect(isGraphCatalog([type])).toBe(true);
    expect(graphClone(type).plugin_metadata).toEqual({ owner: "package" });
    const port = { port_id: "input", data_type: "JSON", data_schema_version: 2, required: false, multiple: false };
    expect(isGraphCatalog([{ ...type, inputs: [port] }])).toBe(true);
    for (const data_schema_version of [0, -1, "2", true])
      expect(isGraphCatalog([{ ...type, inputs: [{ ...port, data_schema_version }] }])).toBe(false);
    expect(isGraphCatalog([{ ...type, input_storage: "cache" }])).toBe(false);
  });
  it("validates session objects independently of the historical variable projection", () => {
    const { document } = fixture();
    const binding = document.object_bindings![0];
    const session = { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
      workflow_definition_id: document.workflow_definition_id, definition_revision: 1, revision: 1,
      data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
      nodes: [], chains: [], outputs: [], data: { revision: 0, values: { old: { value: "legacy" } } },
      objects: { [binding.object_key]: { binding, revision_id: crypto.randomUUID(), revision: 2,
        type_id: binding.type_id, schema_version: 1, deleted: false, value: "current" } } } as GraphSession;
    expect(isGraphSession(session)).toBe(true);
    expect(session.objects![binding.object_key].value).toBe("current");
    expect(session.data.values.old).toEqual({ value: "legacy" });
    session.objects![binding.object_key].binding = { ...binding, schema_version: 2 };
    expect(isGraphSession(session)).toBe(false);
  });
  it("resolves history display from output IDs without adding cached input bodies to records", () => {
    const chain = crypto.randomUUID(); const source = crypto.randomUUID(); const node = crypto.randomUUID();
    const outputId = crypto.randomUUID(); const missing = crypto.randomUUID();
    const run = { run_id: crypto.randomUUID(), chain_run_id: chain, node_binding_id: node,
      workflow_session_id: source, status: "succeeded", input_storage: "references" as const,
      input_values: {}, input_refs: { input: [{ output_id: outputId }, { output_id: missing }] },
      output_refs: {}, reads: [], effects: [], diagnostic: null };
    const detail: GraphRunDetail = { chain: { chain_run_id: chain, status: "succeeded", revision: 1,
      targets: [node], completed_nodes: [node], node_run_ids: [run.run_id], diagnostic: null,
      workflow_session_id: source }, node_runs: [run],
      outputs: [{ output_id: outputId, node_binding_id: node, port_id: "output", chain_run_id: chain,
        payload: { schema_version: 2, kind: "workflow.text", text: "retained artifact" } }] };
    const original = JSON.stringify(detail);
    expect(resolveGraphRunInputs(detail, run)).toEqual({ input: [
      { output_id: outputId, availability: "produced", payload: detail.outputs[0].payload },
      { output_id: missing, availability: "unavailable" },
    ] });
    expect(JSON.stringify(detail)).toBe(original); expect(run.input_values).toEqual({});
  });
  it("labels declared enum choices generically and exposes both structural transform scopes", () => {
    expect(graphEnumLabel("scope", { enum: ["body", "all"] }, "body")).toBe("仅文本");
    expect(graphEnumLabel("schema_scope", { enum: ["body", "all"] }, "all")).toBe("全部");
    expect(graphEnumLabel("style", { enum: ["short", "long"], "x-enum-labels": { short: "简短" } }, "short")).toBe("简短");
    expect(graphEnumLabel("style", { enum: ["short", "long"], enumNames: ["简短", "完整"] }, "long")).toBe("完整");
  });
});
