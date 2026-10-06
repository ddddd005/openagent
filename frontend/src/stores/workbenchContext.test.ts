import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { compilePromptItems, validatePreparation } from "../domain/preparation";
import { contextId, contextPreviewFixture, contextReadFixture, contextScope } from "../adapters/workbenchContext.fixtures";
import { usePreparationStore } from "./preparation";
import { useWorkbenchContextStore } from "./workbenchContext";

const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => vi.unstubAllGlobals());
describe("workbench context observations", () => {
  it("loads backend floors without creating edit history, dirty state or blocking subsequent starts", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(contextReadFixture())));
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    const revision = preparation.getDraft("main", "A").revision;
    await store.refresh();
    expect(store.read?.turns).toHaveLength(1);
    const draft = preparation.getDraft("main", "A");
    const node = draft.nodes.find((entry) => entry.kind === "context")!;
    expect(node.kind === "context" && node.config.floors).toHaveLength(2);
    expect(draft.revision).toBe(revision);
    expect(preparation.isDirty("main", "A")).toBe(false);
    expect(preparation.canUndo("main", "A")).toBe(false);
    expect(validatePreparation(draft)).toEqual([]);
    expect(compilePromptItems(draft).diagnostics.some((entry) => entry.severity === "error")).toBe(false);
  });
  it("drops delayed reads on session, stage, head or view changes and clears on leaving", async () => {
    const waiting = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(waiting.promise));
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    const loading = store.refresh();
    store.activate({ ...contextScope(), headCommitId: contextId(50), refRevision: 4 });
    waiting.resolve(response(contextReadFixture()));
    await loading;
    expect(store.read).toBeNull();
    expect(store.error).toBeNull();
    expect(preparation.getDraft("main", "A").nodes.find((node) => node.kind === "context")!.config)
      .toMatchObject({ floors: [], reference: null });
    store.leave();
    expect(store.scope).toBeNull();
    expect(store.loading).toBeNull();
  });
  it("rejects a response after editing without overwriting node configuration or notifying stale errors", async () => {
    const waiting = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(waiting.promise));
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    const loading = store.refresh();
    preparation.setRootText("main", "A", "edited meanwhile");
    waiting.resolve(response(contextReadFixture()));
    await loading;
    expect(store.read).toBeNull();
    expect(store.error).toBeNull();
    expect(preparation.getRootText("main", "A")).toBe("edited meanwhile");
    expect(store.loading).toBeNull();
  });
  it("previews inline prompt definitions with A input without caching the ephemeral root and invalidates after edits", async () => {
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    preparation.setRootText("main", "A", "next");
    const fetcher = vi.fn().mockImplementation((_path, options) => {
      const preview = contextPreviewFixture(contextScope(), "next");
      if (preview.status === "ready")
        preview.preparation.config.prompt_config = JSON.parse(options.body).prompt_config;
      return Promise.resolve(response(preview));
    });
    vi.stubGlobal("fetch", fetcher);
    await store.preview();
    expect(store.serverPreview?.status).toBe("ready");
    const sent = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(Object.keys(sent.prompt_config).sort()).toEqual(["config", "groups", "items"]);
    expect(sent.text).toBe("next");
    expect(sent).not.toHaveProperty("messages");
    expect(preparation.getDraft("main", "A").nodes.find((node) => node.kind === "context")!.config)
      .toMatchObject({ reference: null, floors: [] });
    preparation.setRootText("main", "A", "changed");
    expect(store.serverPreview).toBeNull();
  });
  it("marks B as pending without filling its current input from old A results", async () => {
    const target = contextScope("B");
    const fetcher = vi.fn().mockResolvedValue(response(contextPreviewFixture(target)));
    vi.stubGlobal("fetch", fetcher);
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(target);
    await store.preview();
    expect(JSON.parse(fetcher.mock.calls[0][1].body).text).toBeNull();
    expect(store.serverPreview).toMatchObject({ status: "pending", input: null, preparation: null });
    expect(preparation.getRootText("main", "B")).toBe("");
  });
  it("suppresses delayed preview after edits and classification stays readonly unavailable", async () => {
    const waiting = deferred<Response>();
    const fetcher = vi.fn().mockReturnValueOnce(waiting.promise).mockRejectedValueOnce(new Error("offline"));
    vi.stubGlobal("fetch", fetcher);
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    preparation.setRootText("main", "A", "next");
    const loading = store.preview();
    preparation.setRootText("main", "A", "edited");
    waiting.resolve(response(contextPreviewFixture()));
    await loading;
    expect(store.serverPreview).toBeNull();
    expect(store.error).toBeNull();
    await store.refresh();
    expect(store.error?.kind).toBe("unavailable");
    expect(store.loading).toBeNull();
    store.clearError(store.error!.sequence);
    expect(store.error).toBeNull();
  });
  it("refuses fabricated scope or B custom input before issuing a request", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate({ ...contextScope(), headCommitId: null });
    await store.refresh();
    expect(fetcher).not.toHaveBeenCalled();
    expect(store.error?.kind).toBe("unavailable");
    store.activate(contextScope("B"));
    preparation.setRootText("main", "B", "fake old output");
    await store.preview();
    expect(fetcher).not.toHaveBeenCalled();
    expect(store.error?.reason).toContain("不能预填");
  });
  it("retains a redacted node diagnostic after dismissing the toast, until the draft changes", async () => {
    const store = useWorkbenchContextStore();
    const preparation = usePreparationStore();
    store.activate(contextScope());
    preparation.setRootText("main", "A", "next");
    const nodeId = preparation.addNode("main", "A", "regex")!;
    const draft = preparation.getDraft("main", "A");
    const item = draft.nodes.find((node) => node.kind === "prompt-item")!;
    const collector = draft.nodes.find((node) => node.kind === "prompt-collect")!;
    const edge = draft.edges.find((entry) => entry.source === item.id && entry.target === collector.id)!;
    preparation.removeEdge("main", "A", edge.id);
    preparation.connect("main", "A", {
      source: item.id, target: nodeId, sourceHandle: "prompt", targetHandle: "prompt",
    });
    preparation.connect("main", "A", {
      source: nodeId, target: collector.id, sourceHandle: "prompt", targetHandle: "prompt",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: {
        reason_code: "invalid_regex_rule",
        diagnostic: { code: "invalid_regex_rule", node_id: nodeId },
        message: "private backend exception text",
      },
    }), { status: 400 })));
    await store.preview();
    expect(store.error?.reason).toContain("invalid_regex_rule");
    expect(store.error?.reason).not.toContain("private backend");
    expect(store.diagnostics).toEqual([expect.objectContaining({
      nodeId, code: "invalid_regex_rule", severity: "error",
    })]);
    store.clearError(store.error!.sequence);
    expect(store.error).toBeNull();
    expect(store.diagnostics).toHaveLength(1);
    preparation.setRootText("main", "A", "changed");
    expect(store.diagnostics).toEqual([]);
  });
});
