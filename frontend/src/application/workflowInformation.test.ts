import { afterEach, describe, expect, it, vi } from "vitest";
import { graphClone } from "../domain/workflowGraph";
import type { GraphInformationBinding, GraphInformationPage } from "../domain/graphInformation";
import { createWorkflowInformationAccess, readGraphInformation, readGraphRegistrations } from "./workflowInformation";

function binding(): GraphInformationBinding {
  const reference = { source_id: "plugin.logs", exact_version: "1" };
  return { kind: "information_binding", registration_ref: reference, package_id: "plugin", package_version: "1.0.0",
    availability: "history", discover_public: false, read_public: false, generation: 2,
    owner: { workflow_session_id: crypto.randomUUID(), chain_run_id: crypto.randomUUID(),
      node_binding_id: crypto.randomUUID(), node_run_id: crypto.randomUUID() },
    declaration: { source_ref: reference, component_id: "plugin.node", component_version: "1", channel_id: "logs",
      format_id: "plugin.unknown-format", format_version: 9, source_scope: "live_and_history",
      discover_public: false, read_public: false, item_schema: {}, max_page_size: 3, max_page_bytes: 500 } };
}
function page(source: GraphInformationBinding): GraphInformationPage {
  return { schema_version: 1, source_ref: source.registration_ref, owner: source.owner, generation: source.generation,
    source_scope: "history", format_id: source.declaration.format_id, format_version: source.declaration.format_version,
    items: [{ unrecognized: { progress: 100, final_answer: "observation only" } }], next_cursor: "provider:next", status: "gap" };
}
const json = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => vi.unstubAllGlobals());

describe("registered information query boundaries", () => {
  it("lists one bounded metadata page without reading a source or starting a node", async () => {
    const source = binding();
    const fetcher = vi.fn(async () => json({ items: [source], next_cursor: "directory:next" }));
    vi.stubGlobal("fetch", fetcher);
    const parameters = { session_id: crypto.randomUUID(), chain_id: source.owner.chain_run_id,
      node_id: source.owner.node_binding_id, kind: "information_binding", limit: 50 };
    expect(await readGraphRegistrations(parameters)).toEqual({ items: [source], next_cursor: "directory:next" });
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "registration.list", parameters }),
    }));
  });

  it("keeps a child's authorization session distinct from all four original owner coordinates and generation", async () => {
    const source = binding(), childSession = crypto.randomUUID(), response = page(source);
    const fetcher = vi.fn(async () => json(response)); vi.stubGlobal("fetch", fetcher);
    expect(await readGraphInformation(childSession, source, "history", "opaque:old")).toEqual(response);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({
      body: JSON.stringify({ operation: "information.read", parameters: {
        session_id: childSession, reference: source.registration_ref, owner: source.owner, generation: 2,
        source_scope: "history", limit: 3, cursor: "opaque:old",
      } }),
    }));
    expect(response.status).toBe("gap");
    expect(response.items).toEqual([{ unrecognized: { progress: 100, final_answer: "observation only" } }]);
  });

  it.each(["owner", "generation", "source_ref", "format_version", "source_scope", "items"] as const)(
    "rejects a response that changes %s instead of falling back to another invocation", async field => {
      const source = binding(), response = page(source);
      if (field === "owner") response.owner = { ...source.owner, node_run_id: crypto.randomUUID() };
      else if (field === "generation") response.generation++;
      else if (field === "source_ref") response.source_ref = { ...response.source_ref, exact_version: "2" };
      else if (field === "format_version") response.format_version++;
      else if (field === "source_scope") response.source_scope = "live";
      else response.items = Array.from({ length: 4 }, () => ({}));
      vi.stubGlobal("fetch", vi.fn(async () => json(response)));
      await expect(readGraphInformation(crypto.randomUUID(), source, "history")).rejects.toThrow("不匹配");
    });

  it("preserves reset and accepts server-validated numeric byte boundaries without a different browser canonicalization", async () => {
    const source = binding(), response = page(source);
    response.status = "reset"; response.items = [0.00001];
    // Python json-v1 writes 1e-05 while a browser writes 0.00001. The router owns the declared byte bound.
    source.declaration.max_page_bytes = 52;
    vi.stubGlobal("fetch", vi.fn(async () => json(response)));
    expect((await readGraphInformation(crypto.randomUUID(), source, "history")).status).toBe("reset");
  });

  it("rejects a foreign binding in a filtered directory and oversized directory pages", async () => {
    const source = binding();
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [source], next_cursor: null })));
    await expect(readGraphRegistrations({ session_id: crypto.randomUUID(), chain_id: crypto.randomUUID() })).rejects.toThrow("范围不匹配");
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [source, source], next_cursor: null })));
    await expect(readGraphRegistrations({ limit: 1 })).rejects.toThrow("不匹配");
  });

  it("accepts unfamiliar registration categories as metadata and rejects unbound source bodies as bindings", async () => {
    const source = binding();
    const custom = { kind: "future_extension", registration_ref: { custom_id: "new" }, declaration: { shape: "unknown" },
      package_id: null, package_version: null, discover_public: true, read_public: false, availability: "registered" };
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [custom], next_cursor: null })));
    expect((await readGraphRegistrations()).items).toEqual([custom]);
    await expect(readGraphInformation(crypto.randomUUID(), { ...source, kind: "information_source" } as unknown as GraphInformationBinding,
      "history")).rejects.toThrow("未登记");
  });

  it("classifies lost read responses as unavailable and never retries or activates a node", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("offline")); vi.stubGlobal("fetch", fetcher);
    await expect(readGraphInformation(crypto.randomUUID(), binding(), "history")).rejects.toMatchObject({ kind: "unavailable" });
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("drops delayed pages after a session or revision basis changes, after disposal, or after a newer read", async () => {
    const source = binding(), response = page(source);
    let release!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { release = resolve; })));
    let basis = "session-one:r1";
    const access = createWorkflowInformationAccess(() => basis);
    const delayed = access.information(crypto.randomUUID(), source, "history");
    basis = "session-two:r2"; release(json(response));
    expect(await delayed).toBeNull();
    const disposed = access.information(crypto.randomUUID(), source, "history");
    access.invalidate(); release(json(response));
    expect(await disposed).toBeNull();
    const older = access.information(crypto.randomUUID(), source, "history");
    const releaseOlder = release;
    const newer = access.information(crypto.randomUUID(), graphClone(source), "history");
    release(json({ ...response, status: "ok" })); expect((await newer)?.status).toBe("ok");
    releaseOlder(json(response)); expect(await older).toBeNull();
  });
});
