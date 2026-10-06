import { afterEach, describe, expect, it, vi } from "vitest";
import { createWorkflowFrontendDisplayAccess, readFrontendArtifact } from "./workflowFrontendDisplay";
import { frontendEntry, frontendObject } from "../testUtils/frontendFixture";
import { frontendDisplayEntries, type FrontendArtifact, type FrontendReadScope } from "../domain/frontendDisplay";
import { graphClone } from "../domain/workflowGraph";

function fixture() {
  const entry = frontendEntry(), object = frontendObject([entry]);
  const scope: FrontendReadScope = { sessionId: crypto.randomUUID(), objectKey: "frontend",
    nodeId: object.binding.readers[0], revisionId: object.revision_id };
  const artifact: FrontendArtifact = { schema_version: 1, kind: "workflow.object-artifact",
    workflow_session_id: scope.sessionId, object_key: scope.objectKey, revision_id: scope.revisionId,
    reference: graphClone(entry.source_ref), component_id: "tools.regex", component_version: "1",
    value: { schema_version: 2, kind: "workflow.text", text: "Accepted processed text" },
    producer: { workflow_session_id: crypto.randomUUID(), chain_run_id: crypto.randomUUID(),
      node_binding_id: crypto.randomUUID(), node_run_id: crypto.randomUUID() } };
  return { entry, object, scope, artifact };
}
const response = (value: unknown) => new Response(JSON.stringify(value));
function deferred() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>(release => { resolve = release; });
  return { promise, resolve };
}
afterEach(() => vi.unstubAllGlobals());

describe("formal frontend reference renderer", () => {
  it("reads exact immutable TEXT@2 through the frozen authorized object revision and preserves original producer", async () => {
    const { scope, entry, artifact } = fixture();
    const fetcher = vi.fn(async () => response(artifact)); vi.stubGlobal("fetch", fetcher);
    expect(await readFrontendArtifact(scope, entry.source_ref)).toEqual(artifact);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "object.artifact.read", parameters: {
        session_id: scope.sessionId, object_key: scope.objectKey, node_id: scope.nodeId,
        revision_id: scope.revisionId, reference: entry.source_ref,
      } }),
    }));
    expect(artifact.producer.workflow_session_id).not.toBe(scope.sessionId);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it.each(["workflow_session_id", "object_key", "revision_id", "reference", "producer", "value"] as const)(
    "rejects mismatched %s without a fallback or latest-output query", async key => {
      const { scope, entry, artifact } = fixture(); const value = graphClone(artifact) as unknown as Record<string, unknown>;
      if (key === "reference") value.reference = { scope: "artifact", output_id: crypto.randomUUID() };
      else if (key === "producer") value.producer = { ...artifact.producer, node_run_id: "latest" };
      else if (key === "value") value.value = { schema_version: 1, text: "guessed legacy text" };
      else value[key] = crypto.randomUUID();
      const fetcher = vi.fn(async () => response(value)); vi.stubGlobal("fetch", fetcher);
      await expect(readFrontendArtifact(scope, entry.source_ref)).rejects.toThrow("不匹配");
      expect(fetcher).toHaveBeenCalledOnce();
    });
  it("accepts only reference-only object entries and rejects inline text, duplicate entry IDs and unproven entries", () => {
    const { object, entry } = fixture();
    expect(frontendDisplayEntries(object)).toEqual([entry]);
    const modify = (patch: unknown) => frontendDisplayEntries({ ...object, value: patch });
    expect(modify({ entries: [{ ...entry, text: "inline message" }], view_ref: null })).toBeNull();
    expect(modify({ entries: [entry, entry], view_ref: (object.value as { view_ref: unknown }).view_ref })).toBeNull();
    expect(modify({ entries: [entry], view_ref: null })).toBeNull();
    expect(frontendDisplayEntries({ ...object, deleted: true })).toBeNull();
  });
  it("deduplicates identical source refs and keeps repeated role entries with one authoritative content read", async () => {
    const { scope, entry, artifact } = fixture(), other = { ...entry, entry_id: crypto.randomUUID(), role: "user" as const };
    const fetcher = vi.fn(async () => response(artifact)); vi.stubGlobal("fetch", fetcher);
    const access = createWorkflowFrontendDisplayAccess(() => "same");
    const result = await access.display(scope, [entry, other]);
    expect(result?.map(row => row.entry.role)).toEqual(["assistant", "user"]);
    expect(result?.map(row => row.artifact?.value.text)).toEqual([artifact.value.text, artifact.value.text]);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("shows failure per declared entry without replacing its source or retaining fabricated text", async () => {
    const { scope, entry } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ reason_code: "session_object_reference_denied",
      message: "Artifact is outside this authorized revision" }), { status: 403 })));
    const result = await createWorkflowFrontendDisplayAccess(() => "same").display(scope, [entry]);
    expect(result?.[0]).toMatchObject({ entry, artifact: null });
    expect(result?.[0].error).toBeTruthy();
  });
  it.each(["session", "object", "workflow", "return"] as const)(
    "fences delayed content when the %s basis changes", async change => {
      const { scope, entry, artifact } = fixture(), pending = deferred(); let basis = "original";
      vi.stubGlobal("fetch", vi.fn(() => pending.promise));
      const access = createWorkflowFrontendDisplayAccess(() => basis), reading = access.display(scope, [entry]);
      basis = change;
      if (change === "return") { access.invalidate(); basis = "original"; }
      pending.resolve(response(artifact)); expect(await reading).toBeNull();
    });
  it("lets only the newest explicit refresh win and freezes request inputs before awaiting", async () => {
    const { scope, entry, artifact } = fixture(), first = deferred(), latest = deferred();
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(latest.promise);
    vi.stubGlobal("fetch", fetcher);
    const access = createWorkflowFrontendDisplayAccess(() => "same");
    const old = access.display(scope, [entry]), current = access.display(scope, [entry]);
    scope.revisionId = crypto.randomUUID(); entry.source_ref.output_id = crypto.randomUUID();
    latest.resolve(response(artifact)); expect((await current)?.[0].artifact).toEqual(artifact);
    first.resolve(response(artifact)); expect(await old).toBeNull();
    const request = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(request.parameters.revision_id).toBe(artifact.revision_id);
    expect(request.parameters.reference).toEqual(artifact.reference);
  });
  it("invalidates delayed failures on unmount or source changes", async () => {
    const { scope, entry } = fixture(), pending = deferred(); vi.stubGlobal("fetch", vi.fn(() => pending.promise));
    const access = createWorkflowFrontendDisplayAccess(() => "same"), reading = access.display(scope, [entry]);
    access.invalidate(); pending.resolve(new Response("unavailable", { status: 503 }));
    expect(await reading).toBeNull();
  });
  it("limits unique artifact reads to four in flight and resolves all declared entries in their original order", async () => {
    const { scope, artifact } = fixture(), entries = Array.from({ length: 7 }, frontendEntry);
    const pending = entries.map(deferred); let inFlight = 0, maximum = 0;
    const fetcher = vi.fn((_path, init) => {
      const reference = JSON.parse(init.body).parameters.reference;
      const index = entries.findIndex(entry => entry.source_ref.output_id === reference.output_id);
      inFlight++; maximum = Math.max(maximum, inFlight);
      return pending[index].promise.finally(() => { inFlight--; });
    });
    vi.stubGlobal("fetch", fetcher);
    const access = createWorkflowFrontendDisplayAccess(() => "same"), reading = access.display(scope, entries);
    expect(fetcher).toHaveBeenCalledTimes(4);
    for (let index = 0; index < entries.length; index++) {
      pending[index].resolve(response({ ...artifact, reference: entries[index].source_ref }));
      if (index < 3) await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(index + 5));
    }
    const result = await reading;
    expect(maximum).toBe(4);
    expect(result?.map(row => row.entry)).toEqual(entries);
    expect(result?.every(row => row.artifact && !row.error)).toBe(true);
  });
  it("stops queued reads after the view is invalidated instead of requesting stale sources", async () => {
    const { scope, artifact } = fixture(), entries = Array.from({ length: 8 }, frontendEntry), pending = deferred();
    const fetcher = vi.fn(() => pending.promise); vi.stubGlobal("fetch", fetcher);
    const access = createWorkflowFrontendDisplayAccess(() => "same"), reading = access.display(scope, entries);
    expect(fetcher).toHaveBeenCalledTimes(4);
    access.invalidate(); pending.resolve(response(artifact));
    expect(await reading).toBeNull(); expect(fetcher).toHaveBeenCalledTimes(4);
  });
});
