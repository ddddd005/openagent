import { afterEach, describe, expect, it, vi } from "vitest";
import { newGraph } from "../domain/workflowGraph";
import { readGraphInputDeclarations } from "./graphInputDeclarationsApi";
afterEach(() => vi.unstubAllGlobals());
describe("draft input declaration query", () => {
  it("keeps declarations when compilation is incomplete and uses a management read only", async () => {
    const doc = newGraph("incomplete"), id = crypto.randomUUID();
    doc.nodes.push({ node_binding_id: id, component_id: "plugin", component_version: "1", title: "", position: { x: 0, y: 0 }, config: {} });
    const value = { schema_version: 1, kind: "workflow.input-declarations", workflow_definition_id: doc.workflow_definition_id,
      inputs: [{ name: "message", data_type: "TEXT", required: true, conflict: false, node_ids: [id] }],
      required_for_run: null, diagnostics: [{ reason_code: "missing_input", message: "Incomplete graph" }] };
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify(value))); vi.stubGlobal("fetch", fetch);
    expect(await readGraphInputDeclarations(doc)).toEqual(value);
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/graph/queries");
  });
  it("rejects wrong workflow, undeclared required names and reserved inputs", async () => {
    const doc = newGraph("test");
    for (const patch of [{ workflow_definition_id: crypto.randomUUID() }, { required_for_run: ["other"] },
      { inputs: [{ name: "_workflow_frozen_resources", data_type: "TEXT", required: true, conflict: false, node_ids: [] }] }]) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
        schema_version: 1, kind: "workflow.input-declarations", workflow_definition_id: doc.workflow_definition_id,
        inputs: [], required_for_run: [], diagnostics: [], ...patch,
      }))));
      await expect(readGraphInputDeclarations(doc)).rejects.toThrow("输入声明无效");
    }
  });
});
