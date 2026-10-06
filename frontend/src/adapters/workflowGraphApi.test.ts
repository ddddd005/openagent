import { afterEach, describe, expect, it, vi } from "vitest";
import { readGraphArchive, readGraphCatalog, readGraphCatalogDetails, readGraphRun, readGraphSessions } from "./workflowGraphApi";
import { workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";
afterEach(() => vi.unstubAllGlobals());
describe("generic node run history decoding", () => {
  it("keeps pure UI packages in the loaded directory while exposing the separate execution subset", async () => {
    const business = { package_id: "workflow.frontend-business", version: "1.0.0" };
    const ui = { package_id: "workflow.frontend", version: "1.0.0" };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ schema_version: 2,
      node_types: [], data_types: [], package_lock: [business, ui], execution_package_lock: [business],
      frontend_extensions: workflowFrontendExtensions }))));
    const result = await readGraphCatalogDetails();
    expect(result.package_lock).toEqual([business, ui]); expect(result.execution_package_lock).toEqual([business]);
    expect(result.frontend_extensions).toEqual(workflowFrontendExtensions);
  });
  it.each(["missing", "version", "duplicate", "invalid"])("rejects an execution lock with %s entries", async mode => {
    const loaded = { package_id: "business", version: "1.0.0" };
    const required: any = mode === "missing" ? [{ package_id: "unknown", version: "1.0.0" }]
      : mode === "version" ? [{ ...loaded, version: "2.0.0" }] : mode === "duplicate" ? [loaded, loaded] : {};
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ schema_version: 2,
      node_types: [], data_types: [], package_lock: [loaded], execution_package_lock: required }))));
    await expect(readGraphCatalogDetails()).rejects.toThrow("执行包目录");
  });
  it("requests the complete v2 catalog with output-free object writers and plugin metadata", async () => {
    const type = { component_id: "plugin.register", component_version: "1", display_name: "register",
      category: "plugin", config_schema: {}, default_config: {}, inputs: [], outputs: [],
      is_output: false, executable: true, input_storage: "references", capabilities: ["objects:write"],
      plugin_metadata: { label: "retained" } };
    const response = { schema_version: 2, node_types: [type],
      package_lock: [{ package_id: "plugin", version: "1.0.0" }],
      data_types: [{ type_id: "plugin.object", schema_version: 1, scope: "session", schema: {} }] };
    const fetcher = vi.fn(async (_path: string) => new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetcher);
    expect(await readGraphCatalog()).toEqual([type]);
    expect(await readGraphCatalogDetails()).toEqual({
      node_types: [type], package_lock: response.package_lock, execution_package_lock: response.package_lock,
      data_types: response.data_types, frontend_extensions: [],
    });
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "catalog.node-types", parameters: { protocol_version: 2 } }),
    }));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ schema_version: 1, node_types: [type] }))));
    await expect(readGraphCatalog()).rejects.toThrow("契约无效");
  });
  it("retains v2 UI declarations and rejects old v1 hints instead of enabling local UI", async () => {
    const response = { schema_version: 2, node_types: [], package_lock: [], data_types: [],
      frontend_extensions: workflowFrontendExtensions };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(response))));
    expect((await readGraphCatalogDetails()).frontend_extensions).toEqual(workflowFrontendExtensions);
    const invalid = { ...response, frontend_extensions: [{ schema_version: 1, kind: "renderer",
      extension_id: "workflow.frontend.session-object" }] };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(invalid))));
    await expect(readGraphCatalogDetails()).rejects.toThrow("前端扩展目录契约无效");
  });
  it("uses the definition-scoped session directory endpoint", async () => {
    const definition = crypto.randomUUID();
    const fetcher = vi.fn(async (_path:string) => new Response("[]")); vi.stubGlobal("fetch",fetcher);
    expect(await readGraphSessions(definition)).toEqual([]);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "definition.sessions", parameters: { workflow_definition_id: definition } }),
    }));
  });
  it("preserves immutable original ownership when reading a copied session's frozen ancestor", async () => {
    const sid = crypto.randomUUID(); const source = crypto.randomUUID(); const chain = crypto.randomUUID();
    const node = crypto.randomUUID(); const record = { chain:{ chain_run_id:chain,workflow_session_id:source,status:"succeeded",definition_revision:1 },
      node_runs:[{ run_id:crypto.randomUUID(),chain_run_id:chain,workflow_session_id:source,node_binding_id:node,
        status:"succeeded",input_values:{ input:"text" },input_refs:{},output_refs:{},reads:[{ revision:2 }],effects:[],diagnostic:null }], outputs:[] };
    const fetcher = vi.fn(async (_path:string) => new Response(JSON.stringify(record))); vi.stubGlobal("fetch",fetcher);
    const result = await readGraphRun(sid,chain);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "run.read", parameters: { session_id: sid, chain_id: chain } }),
    }));
    expect(result.node_runs[0].workflow_session_id).toBe(source); expect(result.node_runs[0].reads).toEqual([{ revision:2 }]);
  });
  it("rejects mismatched chain and node output identities", async () => {
    const chain = crypto.randomUUID();
    vi.stubGlobal("fetch",vi.fn(async () => new Response(JSON.stringify({ chain:{ chain_run_id:chain,status:"succeeded" },
      node_runs:[{ run_id:crypto.randomUUID(),node_binding_id:crypto.randomUUID(),chain_run_id:crypto.randomUUID() }],outputs:[] }))));
    await expect(readGraphRun(crypto.randomUUID(),chain)).rejects.toThrow("不匹配");
  });
  it("accepts reference-only v4 history and rejects inline duplicate input bodies", async () => {
    const chain = crypto.randomUUID(); const session = crypto.randomUUID(); const node = crypto.randomUUID();
    const outputId = crypto.randomUUID();
    const row = { schema_version: 4, input_storage: "references",
      run_id: crypto.randomUUID(), chain_run_id: chain, workflow_session_id: session,
      node_binding_id: node, status: "succeeded", input_values: {},
      input_refs: { input: [{ output_id: outputId }] }, output_refs: {}, reads: [], effects: [],
      control_refs: [{ edge_id: crypto.randomUUID(), run_id: crypto.randomUUID() }], diagnostic: null };
    const response = { chain: { chain_run_id: chain, status: "succeeded" }, node_runs: [row],
      outputs: [{ output_id: outputId, node_binding_id: node, chain_run_id: chain, port_id: "output",
        payload: { schema_version: 2, kind: "workflow.text", text: "artifact" } }] };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(response))));
    expect((await readGraphRun(session, chain)).node_runs[0].input_values).toEqual({});
    Object.assign(row.input_values, { input: response.outputs[0].payload });
    await expect(readGraphRun(session, chain)).rejects.toThrow("不匹配");
  });
  it("reads a frozen legacy archive without replacing its original ownership", async () => {
    const session = crypto.randomUUID(); const turn = crypto.randomUUID(); const source = crypto.randomUUID();
    const record = { turn: { turn_id: turn, workflow_session_id: source },
      root: [{ message_id: crypto.randomUUID(), role: "user", blocks: [{ type: "text", text: "root" }] }], snapshot: { workflow_session_id: source } };
    const fetcher = vi.fn(async (_path: string) => new Response(JSON.stringify(record))); vi.stubGlobal("fetch", fetcher);
    expect((await readGraphArchive(session, turn)).turn.workflow_session_id).toBe(source);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "archive.read", parameters: { session_id: session, archive_id: turn } }),
    }));
    record.turn.turn_id = crypto.randomUUID();
    await expect(readGraphArchive(session, turn)).rejects.toThrow("不匹配");
  });
});
