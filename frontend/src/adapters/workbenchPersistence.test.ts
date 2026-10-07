import { describe, expect, it } from "vitest";
import { graphClone, newGraph, type GraphEntry } from "../domain/workflowGraph";
import { isSavedWorkbench, readWorkbench, writeWorkbench, type SavedWorkbench } from "./workbenchPersistence";

function fixture() {
  const document = newGraph("Current graph"), id = document.workflow_definition_id;
  const value: SavedWorkbench = { schemaVersion: 7, kind: "graph-workbench", revision: 1,
    savedAt: "2026-10-07T00:00:00.000Z", activeWorkflowId: id, selectedWorkflowId: id,
    catalog: [{ id, title: document.name, description: "", nodeCount: 0, state: "draft" }],
    graph: { schema_version: 1, entries: { [id]: { document, saved_revision: 0, session_id: null, pending: null } } } };
  return { value, id, document };
}
function memory(initial: string | null) {
  let raw = initial;
  return { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
}
function mixed(value: SavedWorkbench) {
  const documents = Object.fromEntries(Object.entries(value.graph.entries).map(([id, entry]) => [id, { document: entry.document }]));
  const entries = Object.fromEntries(Object.entries(value.graph.entries).map(([id, entry]) => {
    const { document: _document, ...metadata } = graphClone(entry);
    return [id, metadata];
  }));
  return { ...value, schemaVersion: 6, kind: "unified-workbench", documents,
    graph: { schema_version: 1, entries }, runtime: {}, registrations: [] };
}
describe("current-only workbench persistence", () => {
  it("roundtrips a current graph without requiring acceptance evidence or projecting fixed stages", () => {
    const { value } = fixture(), storage = memory(null);
    const raw = writeWorkbench(storage, value, null);
    expect(isSavedWorkbench(JSON.parse(raw))).toBe(true);
    expect(readWorkbench(storage)).toEqual({ value, raw, error: null, rewrite: false });
    expect(JSON.parse(raw)).not.toHaveProperty("preparations");
  });
  it("removes explicit compatibility rows and preserves every current metadata field and frozen pending body", () => {
    const { value, id, document } = fixture(), legacy = newGraph("Fixed test");
    const original: GraphEntry & { custom_metadata: unknown } = { document, saved_revision: 1,
      saved_document: graphClone(document), session_id: crypto.randomUUID(),
      pending: { action: "save", path: "/api/graph/definitions",
        body: { document: graphClone(document), expected_revision: 1, idempotency_key: crypto.randomUUID() } },
      external_inputs: { original: "retained" }, state_mappings: [],
      custom_metadata: { unrecognized: ["preserve", 7] } };
    value.graph.entries[id] = original;
    const prior = mixed(value);
    prior.catalog.push({ id: legacy.workflow_definition_id, title: "Fixed", description: "", nodeCount: 0, state: "draft" });
    (prior.documents as Record<string, unknown>)[legacy.workflow_definition_id] = { document: legacy, compatibility: { preparations: [] } };
    const loaded = readWorkbench(memory(JSON.stringify(prior)));
    expect(loaded.error).toBeNull(); expect(loaded.rewrite).toBe(true);
    expect(loaded.value?.catalog.map(row => row.id)).toEqual([id]);
    expect(loaded.value?.graph.entries[id]).toEqual(original);
  });
  it("removes explicitly locked compat graphs but preserves a current entry without any new evidence", () => {
    const { value, id } = fixture(), prior = mixed(value);
    const legacy = newGraph("Compat"); legacy.schema_version = 2;
    legacy.package_lock = [{ package_id: "workflow.compat", version: "1.0.0" }];
    legacy.object_bindings = []; legacy.execution_roots = []; legacy.control_edges = [];
    prior.catalog.push({ id: legacy.workflow_definition_id, title: legacy.name, description: "", nodeCount: 0 });
    prior.documents[legacy.workflow_definition_id] = { document: legacy };
    prior.graph.entries[legacy.workflow_definition_id] = { saved_revision: 0, session_id: null, pending: null };
    const loaded = readWorkbench(memory(JSON.stringify(prior)));
    expect(loaded.error).toBeNull();
    expect(Object.keys(loaded.value!.graph.entries)).toEqual([id]);
  });
  it("removes an exact retired component from a schema6 graph without a compatibility marker or package lock", () => {
    const { value, id } = fixture(), legacy = newGraph("Early compat graph");
    legacy.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "workflow.text",
      component_version: "1", title: "Retired text", position: { x: 0, y: 0 }, config: { text: "old" } });
    value.catalog.push({ id: legacy.workflow_definition_id, title: legacy.name, description: "", nodeCount: 1 });
    value.graph.entries[legacy.workflow_definition_id] = { document: legacy, saved_revision: 1,
      session_id: crypto.randomUUID(), pending: null };
    const loaded = readWorkbench(memory(JSON.stringify(mixed(value))));
    expect(loaded.error).toBeNull(); expect(loaded.rewrite).toBe(true);
    expect(loaded.value?.catalog.map(row => row.id)).toEqual([id]);
    expect(loaded.value?.graph.entries).toEqual({ [id]: value.graph.entries[id] });
    expect(isSavedWorkbench(value)).toBe(false);
  });
  it.each([
    [6, "saved_document"], [6, "pending_document"],
    [7, "saved_document"], [7, "pending_document"],
  ])("removes a schema%s current draft whose %s retains an exact retired definition", (schema, source) => {
    const { value, id, document } = fixture(), draft = newGraph("Current replacement draft");
    document.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "tools.text",
      component_version: "1", title: "Current text", position: { x: 0, y: 0 },
      config: { text: "workflow.text@1 workflow.compat legacy.custom" } });
    value.catalog[0].nodeCount = 1;
    value.graph.entries[id] = { document, saved_revision: 1, saved_document: graphClone(document),
      session_id: crypto.randomUUID(), pending: { action: "save", path: "/api/graph/definitions",
        body: { document: graphClone(document), expected_revision: 1, idempotency_key: crypto.randomUUID() } } };
    const original = graphClone(value.graph.entries[id]);
    const retired = graphClone(draft);
    retired.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "workflow.text",
      component_version: "1", title: "Retired text", position: { x: 0, y: 0 }, config: { text: "old" } });
    const retiringEntry: GraphEntry = { document: draft, saved_revision: 1,
      session_id: crypto.randomUUID(), pending: null };
    if (source === "saved_document") retiringEntry.saved_document = retired;
    else retiringEntry.pending = { action: "save", path: "/api/graph/definitions",
      body: { document: retired, expected_revision: 1, idempotency_key: crypto.randomUUID() } };
    value.catalog.push({ id: draft.workflow_definition_id, title: draft.name, description: "", nodeCount: 0 });
    value.graph.entries[draft.workflow_definition_id] = retiringEntry;
    value.activeWorkflowId = value.selectedWorkflowId = draft.workflow_definition_id;
    expect(isSavedWorkbench(value)).toBe(false);
    const loaded = readWorkbench(memory(JSON.stringify(schema === 6 ? mixed(value) : value)));
    expect(loaded.error).toBeNull(); expect(loaded.rewrite).toBe(true);
    expect(loaded.value?.catalog.map(row => row.id)).toEqual([id]);
    expect(loaded.value?.activeWorkflowId).toBe(id);
    expect(loaded.value?.selectedWorkflowId).toBe(id);
    expect(loaded.value?.graph.entries).toEqual({ [id]: original });
  });
  it.each([
    ["legacy.custom", "1"],
    ["legacy.agent", "1"],
    ["workflow.text", "999"],
    ["workflow.text", "01"],
    ["workflow.agent", "3"],
  ])("preserves unknown identity %s@%s with current metadata and the complete pending request", (component_id, component_version) => {
    const { value, id, document } = fixture();
    document.nodes.push({ node_binding_id: crypto.randomUUID(), component_id, component_version,
      title: "Trusted extension", position: { x: 0, y: 0 }, config: { custom: "preserved" } });
    value.catalog[0].nodeCount = 1;
    const original: GraphEntry & { custom_metadata: unknown } = { document, saved_revision: 1,
      saved_document: graphClone(document), session_id: crypto.randomUUID(),
      pending: { action: "save", path: "/api/graph/definitions",
        body: { document: graphClone(document), expected_revision: 1, idempotency_key: crypto.randomUUID() } },
      external_inputs: { original: "retained" }, state_mappings: [],
      custom_metadata: { unrecognized: ["preserve", 7] } };
    value.graph.entries[id] = original;
    const loaded = readWorkbench(memory(JSON.stringify(mixed(value))));
    expect(loaded.error).toBeNull(); expect(loaded.rewrite).toBe(true);
    expect(loaded.value?.graph.entries[id]).toEqual(original);
    expect(isSavedWorkbench(loaded.value)).toBe(true);
    const storage = memory(null), raw = writeWorkbench(storage, loaded.value!, null);
    expect(readWorkbench(storage)).toEqual({ value: loaded.value, raw, error: null, rewrite: false });
  });
  it("blocks unknown or malformed current rows instead of classifying them as disposable old data", () => {
    const { value, id } = fixture(), prior = mixed(value);
    prior.documents[id].document.workflow_definition_id = "invalid";
    const storage = memory(JSON.stringify(prior)), original = storage.getItem();
    expect(readWorkbench(storage).error).not.toBeNull(); expect(storage.getItem()).toBe(original);
    expect(readWorkbench(memory(JSON.stringify({ ...value, schemaVersion: 99 }))).error).not.toBeNull();
  });
  it("allows explicit fixed-workbench test storage to be replaced and retains CAS and capacity barriers", () => {
    const old = JSON.stringify({ schemaVersion: 5, kind: "fixed-workbench" });
    expect(readWorkbench(memory(old))).toEqual({ value: null, raw: old, error: null, rewrite: true });
    const { value } = fixture(), storage = memory(old);
    expect(() => writeWorkbench(storage, value, null)).toThrow("其他页面");
    value.catalog[0].description = "x".repeat(4_000_001);
    expect(() => writeWorkbench(storage, value, old)).toThrow("容量");
  });
});
