import { describe, expect, it } from "vitest";
import { graphClone, graphSignature, isGraphCatalog, isGraphDocument, isGraphSession, newGraph, nodePorts, type GraphNodeType } from "./workflowGraph";

export const textType: GraphNodeType = { component_id: "workflow.text", component_version: "1", display_name: "文本",
  category: "内容", config_schema: { type: "object", properties: { text: { type: "string" } } }, default_config: { text: "" },
  inputs: [], outputs: [{ port_id: "output", data_type: "TEXT", required: true, multiple: false }], is_output: false, executable: true };
export const outputType: GraphNodeType = { ...textType, component_id: "workflow.output", display_name: "输出", is_output: true,
  default_config: { mode: "text" }, inputs: [{ port_id: "input", data_type: "TEXT", required: true, multiple: false }] };
describe("versioned workflow graph boundary", () => {
  it("compares JSON objects by content while retaining array order, types and graph identities", () => {
    const doc = newGraph("semantic receipt");
    doc.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "external.test", component_version: "1",
      title: "node", position: { x: 1, y: 2 }, config: { nested: { z: 7, a: [{ second: true, first: null }, "tail"] } } });
    doc.edges.push({ edge_id: crypto.randomUUID(), source_node_id: doc.nodes[0].node_binding_id, source_port_id: "out",
      target_node_id: doc.nodes[0].node_binding_id, target_port_id: "in", order: 0 });
    const reordered = { edges: doc.edges, nodes: [{ ...doc.nodes[0], config: {
      nested: { a: [{ first: null, second: true }, "tail"], z: 7 },
    } }], name: doc.name, revision: 99, workflow_definition_id: doc.workflow_definition_id, schema_version: 1 as const };
    expect(graphSignature(reordered)).toBe(graphSignature(doc));
    for (const change of [
      (copy: typeof doc) => { (copy.nodes[0].config.nested as { a: unknown[] }).a.reverse(); },
      (copy: typeof doc) => { (copy.nodes[0].config.nested as { z: unknown }).z = "7"; },
      (copy: typeof doc) => { copy.nodes[0].node_binding_id = crypto.randomUUID(); },
      (copy: typeof doc) => { copy.edges[0].target_port_id = "other"; },
      (copy: typeof doc) => { copy.workflow_definition_id = crypto.randomUUID(); },
      (copy: typeof doc) => { copy.nodes[0].title = "different content"; },
    ]) {
      const changed = graphClone(doc); change(changed);
      expect(graphSignature(changed)).not.toBe(graphSignature(doc));
    }
  });
  it("keeps unfamiliar node types, bad semantic config and stale ports without changing them", () => {
    const doc = newGraph("unknown plugin");
    const node = { node_binding_id: crypto.randomUUID(), component_id: "external.test", component_version: "7",
      config: { future_key: { preserve: true } }, title: "测试", position: { x: 1, y: 2 } };
    doc.nodes.push(node);
    doc.edges.push({ edge_id: crypto.randomUUID(), source_node_id: node.node_binding_id,
      source_port_id: "old", target_node_id: crypto.randomUUID(), target_port_id: "future", order: 0 });
    expect(isGraphDocument(doc)).toBe(true);
    expect(graphClone(doc)).toEqual(doc);
    const damaged = graphClone(doc); damaged.nodes.push(graphClone(node));
    expect(isGraphDocument(damaged)).toBe(false);
  });
  it("accepts independent registered types and uses declared mode ports", () => {
    const plugin = { ...textType, component_id: "independent.upper", display_name: "Upper" };
    expect(isGraphCatalog([textType, plugin])).toBe(true);
    const type = { ...plugin, port_modes: { prompt: { inputs: [], outputs: [{ port_id: "output", data_type: "PROMPT", required: true, multiple: false }] } } };
    expect(nodePorts(type, { node_binding_id: crypto.randomUUID(), component_id: plugin.component_id,
      component_version: "1", title: "", position: { x: 0, y: 0 }, config: { mode: "prompt" } }, "outputs")[0]?.data_type).toBe("PROMPT");
    expect(isGraphCatalog([textType, textType])).toBe(false);
  });
  it("decodes zero-node sessions without Agent, prompt, model or chat messages", () => {
    const sid = crypto.randomUUID(); const definition = crypto.randomUUID();
    const view = { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
      workflow_definition_id: definition, definition_revision: 1, revision: 1, data_revision: 0,
      head_revision: 1, status: "idle", can_submit: true, nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} } };
    expect(isGraphSession(view, definition)).toBe(true);
    expect(isGraphSession(view, crypto.randomUUID())).toBe(false);
  });
  it("preserves native Agent budget/archive boundaries and rejects malformed progress counters", () => {
    const view = { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
      workflow_definition_id: crypto.randomUUID(), definition_revision: 1, revision: 3, data_revision: 0,
      head_revision: 1, status: "budget_exhausted", can_submit: false, available_actions: ["resume", "extend_budget"],
      nodes: [{ node_binding_id: crypto.randomUUID(), label: "Agent", status: "budget_exhausted", run_id: crypto.randomUUID(),
        revision: 2, outputs: {}, diagnostic: null, budget: { max_model_requests: 8, max_model_attempts: 32,
          model_requests: 8, attempts: 8, accepted_messages: 8 } }], chains: [], outputs: [], data: { revision: 0, values: {} } };
    expect(isGraphSession(view)).toBe(true);
    view.status = "archive_failed"; expect(isGraphSession(view)).toBe(true);
    view.nodes[0].budget.attempts = -1; expect(isGraphSession(view)).toBe(false);
  });
});
