import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { createPinia, disposePinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import { graphClone, isGraphDocument, type GraphEntry, type GraphNodeType } from "../../domain/workflowGraph";
import { frontendSession } from "../../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { useWorkspaceStore } from "../../stores/workspace";
import { chatTavernFrontendExtensions } from "../tavernFrontendManifest";
import { createWorkbenchFrontendHost } from "../../application/workflowFrontendPackage";
import { createTavernDemo } from "./tavernDemo";
import { tavernChatLaunch } from "./chatLaunch";

const packages = ["content", "tools", "prompts", "models", "agents", "context"]
  .map(name => ({ package_id: `workflow.${name}`, version: "1.0.0" }))
  .concat([{ package_id: "workflow.tavern", version: "1.2.0" }]);
const base = { display_name: "Node", category: "Test", config_schema: {}, default_config: {},
  inputs: [], outputs: [], is_output: false, executable: true };
const catalog: GraphNodeType[] = [
  ...["tavern.chat.output", "tavern.chat.append", "tavern.chat.presentation"].map(component_id => ({
    ...base, component_id, component_version: "1",
    outputs: component_id.endsWith("presentation") ? [{ port_id: "display", data_type: "TAVERN_CHAT_DISPLAY",
      data_schema_version: 1, required: true, multiple: false }] : [],
  })),
  ...["context.output", "context.assembly", "context.merge", "agents.execute"].map(component_id => ({
    ...base, component_id, component_version: "4",
  })),
  { ...base, component_id: "models.source", component_version: "2", default_config: {
    reference: { resource_id: "hidden-default" }, parameters: { model: "hidden-default" },
    capacity: { context_window_tokens: 0, output_reserve_tokens: 1024, summary_max_tokens: 128, max_cold_input_tokens: 0 },
  } },
  { ...base, component_id: "prompts.item", component_version: "2" },
  { ...base, component_id: "tools.current-input", component_version: "1", capabilities: ["external:read"] },
];
function fixture() {
  const document = createTavernDemo(catalog, packages);
  const entry: GraphEntry = { document, saved_document: graphClone(document), saved_revision: 1, session_id: null, pending: null };
  const launch = () => tavernChatLaunch("http://127.0.0.1:8765/", entry, catalog, packages, null);
  return { document, entry, launch };
}
afterEach(() => { vi.unstubAllGlobals(); });

describe("isolated tavern workbench launch and draft", () => {
  it("builds independent native normal-context and chat objects without generic frontend or context editors", () => {
    const before = graphClone(catalog), doc = createTavernDemo(catalog, packages);
    expect(isGraphDocument(doc)).toBe(true); expect(catalog).toEqual(before);
    expect(doc.nodes.some(node => node.component_id.startsWith("frontend."))).toBe(false);
    expect(doc.object_bindings!.map(row => [row.type_id, row.schema_version]))
      .toEqual([["workflow.effective-context", 4], ["workflow.tavern.chat-state", 1]]);
    const writer = doc.nodes.find(row => row.component_id === "context.merge")!;
    const chatRead = doc.nodes.find(row => row.title === "读取酒馆记录")!;
    expect(doc.control_edges).toContainEqual(expect.objectContaining({
      source_node_id: writer.node_binding_id, target_node_id: chatRead.node_binding_id,
    }));
    const presentation = doc.nodes.find(row => row.component_id === "tavern.chat.presentation")!;
    expect(presentation.public_outputs).toEqual(["display"]);
    expect(doc.execution_roots).toContain(presentation.node_binding_id);
    expect(doc.nodes.find(row => row.component_id === "models.source")!.config)
      .toMatchObject({ reference: { resource_id: "" }, parameters: { model: "" } });
    const next = createTavernDemo(catalog, packages);
    expect(next.workflow_definition_id).not.toBe(doc.workflow_definition_id);
    expect(next.nodes.every(node => !doc.nodes.some(old => old.node_binding_id === node.node_binding_id))).toBe(true);
  });
  it("refuses missing exact locks and nodes without falling back to generic frontend", () => {
    expect(() => createTavernDemo(catalog, packages.map(row => row.package_id === "workflow.tavern"
      ? { ...row, version: "1.1.0" } : row))).toThrow("workflow.tavern@1.2.0");
    expect(() => createTavernDemo(catalog.filter(row => row.component_id !== "context.merge"), packages))
      .toThrow("context.merge@4");
  });
  it("registers a detached explicit draft without publishing, replacing another workflow or starting a run", () => {
    const pinia = createPinia(); setActivePinia(pinia);
    try {
      const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
      graph.setPersistenceGuard(() => true); vi.stubGlobal("fetch", vi.fn());
      const old = graph.createWorkflow(), before = graphClone(graph.entries[old]);
      const doc = createTavernDemo(catalog, packages), id = graph.createWorkflowDraft(doc)!;
      expect(workspace.activeWorkflowId).toBe(id); expect(graph.entries[old]).toEqual(before);
      expect(graph.entries[id]).toMatchObject({ saved_revision: 0, session_id: null, pending: null });
      doc.name = "outside edit"; expect(graph.entries[id].document.name).toBe("酒馆聊天");
      expect(graph.createWorkflowDraft(doc)).toBeNull();
      expect(graph.createWorkflowDraft({ ...doc, workflow_definition_id: "invalid" })).toBeNull();
      expect(fetch).not.toHaveBeenCalled();
      expect(tavernChatLaunch("http://127.0.0.1:8765/", graph.entries[id], catalog, packages, null).href).toBeNull();
      expect(graph.entries[old]).toEqual(before);
    } finally { disposePinia(pinia); }
  });
  it("opens the saved snapshot and exact selected session without touching dirty draft configuration", () => {
    const { entry, launch } = fixture();
    entry.document.package_lock = []; entry.document.nodes = [];
    const before = graphClone(entry), href = launch().href!;
    const url = new URL(href);
    expect(url.pathname).toBe("/tavern/");
    expect([...url.searchParams]).toEqual([["graph_workflow", entry.saved_document!.workflow_definition_id]]);
    const session = frontendSession(entry.saved_document!.workflow_definition_id);
    session.definition_revision = 1; entry.session_id = session.workflow_session_id;
    const withSession = tavernChatLaunch("http://localhost:8899/", entry, catalog, packages, session);
    expect(new URL(withSession.href!).searchParams.get("graph_session")).toBe(session.workflow_session_id);
    entry.session_id = null; expect(entry).toEqual(before);
  });
  it.each(["pending", "unsaved", "missing-snapshot", "old-lock", "missing-port", "multiple-display", "multiple-input", "unknown-input"])(
    "diagnoses %s before creating a link or mutating the definition", reason => {
      const { entry } = fixture(), localCatalog = graphClone(catalog);
      if (reason === "pending") entry.pending = { action: "save", path: "/api/graph/definitions",
        body: { idempotency_key: crypto.randomUUID() } };
      if (reason === "unsaved") entry.saved_revision = 0;
      if (reason === "missing-snapshot") delete entry.saved_document;
      if (reason === "old-lock") entry.saved_document!.package_lock = [{ package_id: "workflow.tavern", version: "1.1.0" }];
      if (reason === "missing-port") entry.saved_document!.nodes.forEach(row => { row.public_outputs = []; });
      if (reason === "multiple-display") entry.saved_document!.nodes.push({
        ...graphClone(entry.saved_document!.nodes.find(row => row.component_id === "tavern.chat.presentation")!),
        node_binding_id: crypto.randomUUID(),
      });
      if (reason === "multiple-input") entry.saved_document!.nodes.push({ ...graphClone(entry.saved_document!.nodes[0]!),
        node_binding_id: crypto.randomUUID(), config: { input_name: "other" } });
      if (reason === "unknown-input") localCatalog.find(row => row.component_id === "prompts.item")!.capabilities = ["external:read"];
      const before = graphClone(entry), result = tavernChatLaunch("http://127.0.0.1:8765/", entry, localCatalog, packages, null);
      expect(result.href).toBeNull(); expect(result.diagnosis).toBeTruthy(); expect(entry).toEqual(before);
    });
  it.each(["https://example.invalid/", "file:///tmp/index.html", "http://user:pw@localhost/", "http://localhost/wrong/"])(
    "rejects unsupported host %s", base => {
      const { entry } = fixture(); expect(tavernChatLaunch(base, entry, catalog, packages, null).href).toBeNull();
    });
  it("rejects stale or foreign selected sessions and unavailable exact packages", () => {
    const { entry } = fixture(), session = frontendSession(entry.document.workflow_definition_id);
    entry.session_id = session.workflow_session_id;
    session.definition_revision = 2;
    expect(tavernChatLaunch("http://localhost/", entry, catalog, packages, session).href).toBeNull();
    expect(tavernChatLaunch("http://localhost/", entry, catalog, packages, null).href).toBeNull();
    entry.session_id = null;
    expect(tavernChatLaunch("http://localhost/", entry, catalog, [], null).href).toBeNull();
  });
  it("rejects malformed and noncanonical identities using the same page-query spelling as the host", () => {
    const { entry } = fixture();
    entry.session_id = "";
    expect(tavernChatLaunch("http://localhost/", entry, catalog, packages, null).href).toBeNull();
    entry.session_id = null;
    entry.saved_document!.workflow_definition_id = "ABCDEFAB-1234-4ABC-ABCD-123456ABCDEF";
    entry.document.workflow_definition_id = entry.saved_document!.workflow_definition_id;
    expect(tavernChatLaunch("http://localhost/", entry, catalog, packages, null).href).toBeNull();
  });
  it("registers only the exact1.2 workbench extensions, including isolated launcher", () => {
    const host = createWorkbenchFrontendHost(() => chatTavernFrontendExtensions, () => packages);
    expect(host.issues.value).toEqual([]); expect(host.extensions.value).toHaveLength(6);
    expect(host.extensions.value[5]!.declaration.extension_id).toBe("workflow.tavern.chat-launch");
    expect(createWorkbenchFrontendHost(() => chatTavernFrontendExtensions, () => [
      { package_id: "workflow.tavern", version: "1.1.0" },
    ]).extensions.value).toEqual([]);
  });
});

describe("limited shared content transport", () => {
  function core() {
    const scope: Record<string, any> = {};
    runInNewContext(readFileSync(new URL("../../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
    return scope.GraphChat;
  }
  const payload = { schema_version: 1, kind: "workflow.tavern-chat-display", entries: [
    { entry_id: "00000000-0000-4000-8000-000000000001", role: "assistant",
      source_ref: { scope: "artifact", output_id: "00000000-0000-4000-8000-000000000002" } },
  ] };
  it("validates only exact tavern type/version and preserves generic validation", () => {
    const api = core();
    expect(api.validateContent(payload, "TAVERN_CHAT_DISPLAY", 1)).toEqual(payload);
    expect(api.supportedContent("TAVERN_CHAT_DISPLAY", 1)).toBe(true);
    expect(api.supportedContent("TAVERN_CHAT_DISPLAY", 2)).toBe(false);
    expect(api.supportedContent("UNKNOWN_DISPLAY", 1)).toBe(false);
    expect(() => api.validateContent(payload, "UNKNOWN_DISPLAY", 1)).toThrow();
    expect(() => api.validateContent({ ...payload, entries: [...payload.entries, ...payload.entries] }, "TAVERN_CHAT_DISPLAY", 1)).toThrow();
    expect(() => api.validateContent({ ...payload, entries: [{ ...payload.entries[0], text: "forged" }] }, "TAVERN_CHAT_DISPLAY", 1)).toThrow();
    expect(() => api.validateContent(payload, "FRONTEND_DISPLAY", 1)).toThrow();
    expect(api.validateContent({ ...payload, kind: "workflow.frontend-display" }, "FRONTEND_DISPLAY", 1).entries).toEqual(payload.entries);
  });
  it("permits a tavern entry only from its declared current output and never accepts an arbitrary display", async () => {
    const api = core(), output = { availability: "produced", data_type: "TAVERN_CHAT_DISPLAY", data_schema_version: 1, payload };
    const client = Object.create(api.GraphChatClient.prototype);
    client.consumer = { workflow_session_id: "session", outputs: [output] };
    client.sessionId = "session"; client.publicHistory = []; client.sessionGeneration = 0;
    client.readOutputArtifact = vi.fn().mockResolvedValue({ text: "accepted" });
    await client.readDisplayEntry(output, payload.entries[0]);
    expect(client.readOutputArtifact).toHaveBeenCalledWith(output, payload.entries[0]!.source_ref);
    await expect(client.readDisplayEntry(output, { ...payload.entries[0], entry_id: crypto.randomUUID() })).rejects.toThrow("条目");
    const forged = { ...output, data_type: "UNKNOWN_DISPLAY" };
    client.consumer.outputs = [forged];
    await expect(client.readDisplayEntry(forged, payload.entries[0])).rejects.toThrow("类型");
    expect(client.readOutputArtifact).toHaveBeenCalledOnce();
  });
});
