import { afterEach, describe, expect, it, vi } from "vitest";
import { listCurrentPromptResources, readCurrentPromptResource, saveCurrentPromptResource,
  type PromptSaveRequest } from "./promptResourcesApi";
import { newPromptMember, newPromptResource, promptIdentity } from "../domain/workflowPromptResources";
import { newProvider } from "../domain/workflowModelResources";

afterEach(() => vi.unstubAllGlobals());
function resultFor(request: PromptSaveRequest) {
  return { reference: promptIdentity(request.record), update_sequence: request.record.update_sequence, deleted: false };
}
function envelope(request: PromptSaveRequest, result: unknown = resultFor(request), receipt: Record<string, unknown> = {}) {
  return { schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "resource.save",
      operation_scope: "management", idempotency_key: request.idempotency_key,
      authority: "service_receipt", request_sha256: "a".repeat(64), target: {}, accepted: result, ...receipt } };
}
describe("public current independent prompt resource API", () => {
  it("queries the prompt type only and retains same-UUID records in distinct scopes", async () => {
    const workspace = newPromptResource(), project = { ...newPromptResource(), scope: "project", resource_id: workspace.resource_id };
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      const request = JSON.parse(String(options.body));
      return new Response(JSON.stringify(request.operation === "resource.list" ? [workspace, project] : project));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await listCurrentPromptResources()).toEqual([workspace, project]);
    expect(await readCurrentPromptResource(promptIdentity(project))).toEqual(project);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/queries");
    expect(JSON.parse(String(fetcher.mock.calls[0]![1].body))).toEqual({
      operation: "resource.list", parameters: { type_id: "workflow.prompt-resource" } });
    expect(JSON.parse(String(fetcher.mock.calls[1]![1].body))).toEqual({
      operation: "resource.read", parameters: { identity: promptIdentity(project) } });
  });
  it.each([1, 2] as const)("reads and lists supported prompt schema %s without rewriting it", async version => {
    const record = newPromptResource(version);
    record.value.members = [newPromptMember("Supported prompt member", version)];
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      const request = JSON.parse(String(options.body));
      return new Response(JSON.stringify(request.operation === "resource.list" ? [record] : record));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await readCurrentPromptResource(promptIdentity(record))).toEqual(record);
    expect(await listCurrentPromptResources()).toEqual([record]);
    expect(record.data_schema_version).toBe(version);
    expect(fetcher.mock.calls.map(([, options]) => JSON.parse(String(options.body)).operation))
      .toEqual(["resource.read", "resource.list"]);
  });
  it.each(["scope", "type_id", "resource_id", "data_schema_version"])("rejects a read with mismatched %s", async field => {
    const record = newPromptResource();
    const values: Record<string, unknown> = { scope: "project", type_id: "workflow.chat-provider",
      resource_id: crypto.randomUUID(), data_schema_version: 99 };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...record, [field]: values[field] }))));
    await expect(readCurrentPromptResource(promptIdentity(record))).rejects.toMatchObject({ kind: "unavailable" });
  });
  it("accepts a missing record but rejects invalid read identities before dispatch", async () => {
    const fetcher = vi.fn(async () => new Response("null")); vi.stubGlobal("fetch", fetcher);
    const identity = promptIdentity(newPromptResource());
    expect(await readCurrentPromptResource(identity)).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
    await expect(readCurrentPromptResource({ ...identity, type_id: "workflow.chat-provider" } as unknown as typeof identity))
      .rejects.toMatchObject({ kind: "rejected" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it.each([
    { label: "provider", value: () => [newProvider()] },
    { label: "mixed types", value: () => [newPromptResource(), newProvider()] },
    { label: "schema", value: () => [{ ...newPromptResource(), data_schema_version: 99 }] },
    { label: "non-array", value: () => newPromptResource() },
  ])("rejects an invalid list: $label", async ({ value }) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value()))));
    await expect(listCurrentPromptResources()).rejects.toMatchObject({ kind: "unavailable" });
  });
  it("saves empty or populated groups through the named command with complete original identity", async () => {
    const record = newPromptResource(); record.scope = "project";
    const request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      expect(JSON.parse(String(options.body))).toEqual({ operation: "resource.save", parameters: request });
      return new Response(JSON.stringify(envelope(request)));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await saveCurrentPromptResource(request)).toEqual(resultFor(request));
    record.value.members = [newPromptMember("body")];
    record.value.members[0]!.metadata = { nested: { source: "kept", flags: [true, null] } };
    expect(await saveCurrentPromptResource(request)).toEqual(resultFor(request));
    expect(fetcher.mock.calls.every(call => call[0] === "/api/graph/commands")).toBe(true);
  });
  it.each(["scope", "type", "uuid", "sequence", "deleted", "envelope", "extra"])(
    "treats a mismatched resource receipt (%s) as unknown", async field => {
      const record = newPromptResource(), request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
      const result = resultFor(request);
      const wrong: Record<string, unknown> = { ...result };
      if (field === "scope") wrong.reference = { ...result.reference, scope: "project" };
      if (field === "type") wrong.reference = { ...result.reference, type_id: "workflow.chat-provider" };
      if (field === "uuid") wrong.reference = { ...result.reference, resource_id: crypto.randomUUID() };
      if (field === "sequence") wrong.update_sequence = 2;
      if (field === "deleted") wrong.deleted = true;
      if (field === "envelope") wrong.reference = { ...result.reference, envelope_version: 2 };
      if (field === "extra") wrong.extra = true;
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(envelope(request, wrong)))));
      await expect(saveCurrentPromptResource(request)).rejects.toMatchObject({ kind: "unknown" });
    });
  it.each(["idempotency_key", "operation", "accepted"])("checks the application receipt %s", async field => {
    const record = newPromptResource(), request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const wrong: Record<string, unknown> = { idempotency_key: crypto.randomUUID(), operation: "resource.delete",
      accepted: { ...resultFor(request), update_sequence: 2 } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(envelope(request, resultFor(request),
      { [field]: wrong[field] })))));
    await expect(saveCurrentPromptResource(request)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("rejects malformed or lossy requests before dispatch and preserves CAS rejection kind", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const request: PromptSaveRequest = { record: newPromptResource(), expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const invalid = [{ ...request, expected_sequence: -1 }, { ...request, expected_sequence: 0.5 },
      { ...request, expected_sequence: 1 }, { ...request, idempotency_key: "not-a-uuid" },
      { ...request, idempotency_key: request.idempotency_key.toUpperCase() }, { ...request, extra: true },
      { ...request, record: { ...request.record, type_id: "workflow.chat-provider" } }];
    const symbol = { ...request, [Symbol("hidden")]: true };
    for (const value of [...invalid, symbol]) {
      await expect(saveCurrentPromptResource(value as PromptSaveRequest)).rejects.toMatchObject({ kind: "rejected" });
    }
    expect(fetcher).not.toHaveBeenCalled();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "stale_revision" } }), { status: 409 })));
    await expect(saveCurrentPromptResource(request)).rejects.toMatchObject({ kind: "rejected", code: "stale_revision" });
  });
});
