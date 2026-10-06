import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { graphClone } from "../domain/workflowGraph";
import { workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock, frontendSession } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });
describe("frontend catalog fail-closed lifecycle", () => {
  it("locks only execution packages in explicit graph wiring and retains the graph when UI is disabled", async () => {
    const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
    const packet = { schema_version: 2, node_types: frontendCatalog, data_types: [],
      package_lock: frontendProjectLock, execution_package_lock: frontendExecutionLock,
      frontend_extensions: workflowFrontendExtensions };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(packet)));
    vi.stubGlobal("fetch", fetcher);
    await graph.loadCatalog(); graph.createWorkflow();
    const input = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
    expect(graph.attachFrontendDisplay({ sourceNodeId: input, sourcePortId: "output", objectKey: "frontend", role: "user" })).toBeTruthy();
    expect(graph.packageLock).toEqual(frontendProjectLock);
    expect(graph.document!.package_lock).toEqual(frontendExecutionLock);
    expect(graph.document!.package_lock!.some(pkg => pkg.package_id === "workflow.frontend")).toBe(false);
    const before = graphClone(graph.document);
    fetcher.mockImplementation(async () => new Response(JSON.stringify({ ...packet,
      package_lock: frontendExecutionLock, frontend_extensions: [] })));
    await graph.loadCatalog();
    expect(graph.frontendExtensions).toEqual([]); expect(graph.executionPackageLock).toEqual(frontendExecutionLock);
    expect(graph.document).toEqual(before);
  });
  it("unloads the old UI before refreshing and retains graph/data when loading fails", async () => {
    const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
    const id = graph.createWorkflow(), session = frontendSession(id);
    graph.entries[id].session_id = session.workflow_session_id; graph.views[session.workflow_session_id] = session;
    graph.frontendExtensions = graphClone(workflowFrontendExtensions);
    const document = graphClone(graph.document), data = graphClone(graph.session);
    let fail!: (reason: Error) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((_resolve, reject) => { fail = reject; })));
    const reading = graph.loadCatalog();
    expect(graph.frontendExtensions).toEqual([]);
    fail(new Error("catalog unavailable")); await reading;
    expect(graph.frontendExtensions).toEqual([]); expect(graph.catalogError).toBeTruthy();
    expect(graph.document).toEqual(document); expect(graph.session).toEqual(data);
  });
  it("replaces extensions with the current headless directory and does not retain stale UI", async () => {
    const graph = useWorkflowGraphStore(); graph.frontendExtensions = graphClone(workflowFrontendExtensions);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 2, node_types: [], package_lock: [], data_types: [], frontend_extensions: [],
    }))));
    await graph.loadCatalog(); expect(graph.frontendExtensions).toEqual([]); expect(graph.catalogError).toBeNull();
  });
  it("cannot resurrect extensions after host disposal", async () => {
    const graph = useWorkflowGraphStore(); graph.frontendExtensions = graphClone(workflowFrontendExtensions);
    let complete!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => { complete = resolve; })));
    const reading = graph.loadCatalog(); graph.$dispose();
    complete(new Response(JSON.stringify({ schema_version: 2, node_types: [], package_lock: [], data_types: [],
      frontend_extensions: workflowFrontendExtensions })));
    await reading; expect(graph.frontendExtensions).toEqual([]);
  });
});
