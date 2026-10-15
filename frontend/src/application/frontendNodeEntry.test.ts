import { afterEach, describe, expect, it, vi } from "vitest";
import { frontendNodeInputs, frontendNodeLaunchTarget, openFrontendNodeEntry, readFrontendNodeEntry,
  validateFrontendNodeEntry, type FrontendNodeEntry, type FrontendNodeOwner } from "./frontendNodeEntry";
import { graphClone } from "../domain/workflowGraph";
import { frontendNodeFixture } from "../testUtils/frontendNodeFixture";
import { frontendSession } from "../testUtils/frontendFixture";

function fixture() {
  const value = frontendNodeFixture();
  const target = { node: value.nodes[0]!, document: value.document };
  const launch = () => frontendNodeLaunchTarget(value.entry, target, null);
  const owner = launch().owner!;
  return { ...value, target, launch, owner };
}
function entry(owner: FrontendNodeOwner, origin = "http://127.0.0.1:8765"): FrontendNodeEntry {
  const path = `${owner.workflow_definition_id}/${owner.definition_revision}/${owner.node_binding_id}/${owner.session_id ?? "none"}`;
  return { schema_version: 1, kind: "workflow.frontend-node-entry", owner: graphClone(owner),
    page_url: `${origin}/frontend-node/${path}/`,
    channel_url: `${origin}/api/graph/frontend-nodes/${path}/channel`,
    events_url: `${origin}/api/graph/frontend-nodes/${path}/events` };
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("frontend node saved entry", () => {
  it("opens a precisely saved version 2 node while rejecting a version 1 saved identity", () => {
    const f = frontendNodeFixture("2"), target = { node: f.nodes[0]!, document: f.document };
    expect(frontendNodeLaunchTarget(f.entry, target, null).owner?.node_binding_id).toBe(f.nodes[0]!.node_binding_id);
    f.entry.saved_document!.nodes.find(node => node.node_binding_id === f.nodes[0]!.node_binding_id)!.component_version = "1";
    expect(frontendNodeLaunchTarget(f.entry, target, null).owner).toBeNull();
  });
  it("derives a separate exact owner for each clicked saved node with no session or global mutation", () => {
    const f = fixture(), before = graphClone(f.entry);
    const other = frontendNodeLaunchTarget(f.entry, { node: f.nodes[1]!, document: f.document }, null);
    expect(f.owner).toEqual({ workflow_definition_id: f.document.workflow_definition_id,
      definition_revision: 1, node_binding_id: f.nodes[0]!.node_binding_id, session_id: null });
    expect(other.owner!.node_binding_id).toBe(f.nodes[1]!.node_binding_id);
    expect(f.entry).toEqual(before);
  });
  it.each(["unsaved", "missing-snapshot", "missing-node", "changed-config", "wrong-version", "pending", "wrong-workflow"])(
    "refuses %s without creating a session or changing the graph", reason => {
      const f = fixture();
      if (reason === "unsaved") f.entry.saved_revision = 0;
      if (reason === "missing-snapshot") delete f.entry.saved_document;
      if (reason === "missing-node") f.entry.saved_document!.nodes = [];
      if (reason === "changed-config") f.target.node.config.input_node_id = f.inputs[0]!.node_binding_id;
      if (reason === "wrong-version") f.target.node.component_version = "2";
      if (reason === "pending") f.entry.pending = { action: "start", path: "/unused", body: {} };
      if (reason === "wrong-workflow") f.entry.saved_document!.workflow_definition_id = crypto.randomUUID();
      const before = graphClone(f.entry), result = f.launch();
      expect(result.owner).toBeNull(); expect(result.diagnosis).toBeTruthy(); expect(f.entry).toEqual(before);
    });
  it("keeps running sessions openable but refuses foreign, stale, readonly and archived sessions", () => {
    const f = fixture(), session = frontendSession(f.document.workflow_definition_id);
    f.entry.session_id = session.workflow_session_id;
    session.status = "running"; session.can_submit = false;
    const launch = () => frontendNodeLaunchTarget(f.entry, f.target, session);
    expect(launch().owner!.session_id).toBe(session.workflow_session_id);
    session.definition_revision = 2; expect(launch().owner).toBeNull();
    session.definition_revision = 1; session.workflow_definition_id = crypto.randomUUID();
    expect(launch().owner).toBeNull();
    session.workflow_definition_id = f.document.workflow_definition_id; session.readonly = true;
    expect(launch().owner).toBeNull();
    session.readonly = false; session.archived = true; expect(launch().owner).toBeNull();
  });
  it("blocks unresolved requests and filters input bindings by the exact installed contract", () => {
    const f = fixture();
    expect(frontendNodeLaunchTarget(f.entry, f.target, null, true).owner).toBeNull();
    f.document.nodes.push({ ...graphClone(f.inputs[0]!), node_binding_id: crypto.randomUUID(),
      component_id: "tools.current-input" });
    expect(frontendNodeInputs(f.document, f.catalog).map(node => node.node_binding_id))
      .toEqual(f.inputs.map(node => node.node_binding_id));
    expect(frontendNodeInputs(f.document, [])).toEqual([]);
    expect(frontendNodeInputs(f.document, f.catalog.map(type => ({ ...type, executable: false })))).toEqual([]);
  });
  it("only fetches the exact read-only entry and accepts a backend origin different from the dev proxy", async () => {
    const { owner } = fixture();
    const response = entry(owner, "http://localhost:8899");
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetcher);
    expect(await readFrontendNodeEntry(owner)).toEqual(response);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(
      `/api/graph/frontend-nodes/${owner.workflow_definition_id}/1/${owner.node_binding_id}/none/entry`,
      expect.objectContaining({ method: "GET", cache: "no-store" }));
    expect(fetcher.mock.calls[0]![1]).not.toHaveProperty("body");
  });
  it.each(["owner", "remote", "credentials", "origin", "path", "query", "hash", "extra", "https"])(
    "rejects mismatched %s response information", reason => {
      const { owner } = fixture(), response = entry(owner);
      if (reason === "owner") response.owner.node_binding_id = crypto.randomUUID();
      if (reason === "remote") response.page_url = response.page_url.replace("127.0.0.1", "example.invalid");
      if (reason === "credentials") response.page_url = response.page_url.replace("http://", "http://user:password@");
      if (reason === "origin") response.events_url = response.events_url.replace(":8765", ":9999");
      if (reason === "path") response.channel_url = response.channel_url.replace("/channel", "/commands");
      if (reason === "query") response.page_url += "?arbitrary=true";
      if (reason === "hash") response.page_url += "#other";
      if (reason === "extra") Object.assign(response, { arbitrary: true });
      if (reason === "https") response.page_url = response.page_url.replace("http:", "https:");
      expect(() => validateFrontendNodeEntry(response, owner)).toThrow("归属或地址");
    });
  it("opens only the verified page with opener cleared and never dispatches a command", async () => {
    const { owner } = fixture(), response = entry(owner);
    const popup = { opener: {}, location: { href: "about:blank" }, close: vi.fn() };
    const openBlank = vi.fn(() => popup), read = vi.fn(async () => response);
    await openFrontendNodeEntry(owner, { current: () => true, openBlank, read });
    expect(openBlank).toHaveBeenCalledOnce(); expect(read).toHaveBeenCalledWith(owner);
    expect(popup.opener).toBeNull(); expect(popup.location.href).toBe(response.page_url);
    expect(popup.close).not.toHaveBeenCalled();
  });
  it("closes a provisional popup on a late identity change instead of opening a different node", async () => {
    const { owner } = fixture(), response = entry(owner);
    const popup = { opener: {}, location: { href: "about:blank" }, close: vi.fn() };
    let current = true;
    const read = vi.fn(async () => { current = false; return response; });
    await expect(openFrontendNodeEntry(owner, { current: () => current, openBlank: () => popup, read }))
      .rejects.toThrow("迟到入口");
    expect(popup.location.href).toBe("about:blank"); expect(popup.close).toHaveBeenCalledOnce();
  });
  it("does not query when the window is blocked or the captured binding is already stale", async () => {
    const { owner } = fixture(), read = vi.fn();
    await expect(openFrontendNodeEntry(owner, { current: () => true, openBlank: () => null, read }))
      .rejects.toThrow("阻止");
    const openBlank = vi.fn();
    await expect(openFrontendNodeEntry(owner, { current: () => false, openBlank, read })).rejects.toThrow("已变化");
    expect(read).not.toHaveBeenCalled(); expect(openBlank).not.toHaveBeenCalled();
  });
});
