import { describe, expect, it } from "vitest";
import { clonePromptResource, isCurrentPromptResource, isPromptIdentity, newPromptMember, newPromptResource,
  promptIdentity, promptResourceLabel, promptResourceType, samePromptIdentity } from "./workflowPromptResources";

describe("current independent prompt resource contracts", () => {
  it("creates an empty workspace group without legacy name or kind fields", () => {
    const record = newPromptResource();
    expect(record).toMatchObject({ envelope_version: 1, scope: "workspace", type_id: promptResourceType,
      data_schema_version: 1, update_sequence: 1, value: { enabled: true, members: [] } });
    expect(isCurrentPromptResource(record)).toBe(true);
    expect(isPromptIdentity(promptIdentity(record))).toBe(true);
    expect(promptResourceLabel(record)).toBe(record.resource_id);
    expect(Object.keys(record.value)).toEqual(["enabled", "members"]);
    expect(newPromptMember()).toMatchObject({ text: "", metadata: {},
      presentation: { role: "system", placement: "before", depth: null, order: 0, enabled: true } });
  });
  it("derives the label from the first nonblank member body and preserves complete identities", () => {
    const record = newPromptResource();
    record.scope = "project";
    record.value.members = [newPromptMember(" \r\n "), newPromptMember("  First line\r\nSecond line")];
    record.value.members[1]!.metadata = { name: "not the label", nested: { flags: [true, null, 1.5] } };
    expect(promptResourceLabel(record)).toBe("First line");
    const identity = promptIdentity(record);
    expect(identity).toEqual({ envelope_version: 1, scope: "project", type_id: promptResourceType,
      resource_id: record.resource_id });
    expect(samePromptIdentity(identity, { ...identity })).toBe(true);
    expect(samePromptIdentity(identity, { ...identity, scope: "workspace" })).toBe(false);
    expect(samePromptIdentity(identity, { ...identity, type_id: "workflow.chat-provider" } as unknown as typeof identity)).toBe(false);
    expect(samePromptIdentity(identity, { ...identity, resource_id: crypto.randomUUID() })).toBe(false);
    const copy = clonePromptResource(record);
    copy.value.members[1]!.text = "edited";
    (copy.value.members[1]!.metadata.nested as { flags: unknown[] }).flags.push(false);
    expect(record.value.members[1]!.text).toBe("  First line\r\nSecond line");
    expect((record.value.members[1]!.metadata.nested as { flags: unknown[] }).flags).toHaveLength(3);
    expect(copy.scope).toBe("project");
  });
  it.each([
    { scope: "" }, { scope: " workspace" }, { scope: "workspace " }, { scope: "x".repeat(129) },
    { scope: "session" }, { scope: "artifact" }, { scope: 1 }, { envelope_version: 2 },
    { envelope_version: "1" }, { type_id: "workflow.global-content" }, { resource_id: "not-a-uuid" },
    { resource_id: "59e405d0-debd-1d7a-aa7f-ce6445d72665" }, { extra: true },
  ])("rejects invalid or incomplete identity fields %j", patch => {
    expect(isPromptIdentity({ ...promptIdentity(newPromptResource()), ...patch })).toBe(false);
  });
  it("requires every identity field and canonical UUID spelling", () => {
    const identity = promptIdentity(newPromptResource());
    for (const key of Object.keys(identity)) {
      const incomplete: Record<string, unknown> = { ...identity }; delete incomplete[key];
      expect(isPromptIdentity(incomplete)).toBe(false);
    }
    expect(isPromptIdentity({ ...identity, resource_id: identity.resource_id.toUpperCase() })).toBe(false);
    expect(isPromptIdentity({ ...identity, scope: "\ud800" })).toBe(false);
    expect(isPromptIdentity({ ...identity, scope: "\ud83d\ude00".repeat(128) })).toBe(true);
  });
  it.each([
    { data_schema_version: 2 }, { data_schema_version: "1" }, { update_sequence: 0 }, { update_sequence: -1 },
    { update_sequence: 1.5 }, { update_sequence: Number.MAX_SAFE_INTEGER + 1 }, { update_sequence: Infinity },
    { update_sequence: NaN }, { name: "legacy" }, { kind: "role_card" },
    { value: { enabled: true, members: [], name: "legacy" } },
    { value: { enabled: true, members: [], kind: "role_card" } },
    { value: { enabled: 1, members: [] } }, { value: { enabled: true } },
  ])("rejects unsupported resource envelopes and value fields %j", patch => {
    expect(isCurrentPromptResource({ ...newPromptResource(), ...patch })).toBe(false);
  });
  it("allows at most 1024 distinct members, including empty or disabled members", () => {
    const record = newPromptResource();
    record.value.enabled = false;
    record.value.members = Array.from({ length: 1024 }, () => newPromptMember());
    record.value.members[0]!.presentation.enabled = false;
    expect(isCurrentPromptResource(record)).toBe(true);
    record.value.members.push(newPromptMember());
    expect(isCurrentPromptResource(record)).toBe(false);
    record.value.members = [newPromptMember("one")];
    record.value.members.push({ ...record.value.members[0]!, text: "two" });
    expect(isCurrentPromptResource(record)).toBe(false);
  });
  it.each([
    { id: "not-a-uuid" }, { text: 1 }, { name: "legacy" }, { kind: "role_card" }, { metadata: null },
    { metadata: [] }, { presentation: { role: "system" } },
  ])("requires the exact member contract %j", patch => {
    const record = newPromptResource();
    record.value.members = [{ ...newPromptMember(), ...patch } as ReturnType<typeof newPromptMember>];
    expect(isCurrentPromptResource(record)).toBe(false);
  });
  it.each([
    { role: "tool" }, { role: ["system"] }, { placement: "unknown" }, { placement: ["before"] },
    { depth: 0 }, { order: 0.1 }, { order: Infinity }, { enabled: "true" }, { extra: true },
    { placement: "after", depth: 0 }, { placement: "middle", depth: null },
    { placement: "middle", depth: -1 }, { placement: "middle", depth: 0.5 },
  ])("rejects invalid presentation fields and placement-depth combinations %j", patch => {
    const record = newPromptResource(), member = newPromptMember();
    Object.assign(member.presentation, patch); record.value.members = [member];
    expect(isCurrentPromptResource(record)).toBe(false);
  });
  it("accepts middle placement only with a nonnegative safe integer depth", () => {
    const record = newPromptResource(), member = newPromptMember("body");
    member.presentation = { role: "assistant", placement: "middle", depth: 0, order: -7, enabled: false };
    record.value.members = [member];
    expect(isCurrentPromptResource(record)).toBe(true);
    member.id = member.id.toUpperCase();
    expect(isCurrentPromptResource(record)).toBe(false);
  });
  it("rejects non-JSON metadata instead of allowing serialization to drop or replace it", () => {
    const cycle: Record<string, unknown> = {}; cycle.self = cycle;
    const symbol = { [Symbol("hidden")]: true };
    const hidden = Object.defineProperty({}, "secret", { value: 1 });
    const getter = Object.defineProperty({}, "secret", { enumerable: true, get: () => 1 });
    const sparse: unknown[] = []; sparse.length = 1;
    const decorated: unknown[] = []; Object.assign(decorated, { extra: true });
    const invalid = [undefined, NaN, Infinity, 1n, () => {}, new Date(), new Map(), new Set(),
      cycle, symbol, hidden, getter, sparse, decorated, "\ud800"];
    const record = newPromptResource(), member = newPromptMember("body");
    record.value.members = [member];
    for (const value of invalid) {
      member.metadata = { value };
      expect(isCurrentPromptResource(record)).toBe(false);
    }
    member.metadata = { "\udc00": true };
    expect(isCurrentPromptResource(record)).toBe(false);
    member.metadata = { valid: "\ud83d\ude00", nested: { nil: null, flags: [false, 2, "body"] } };
    expect(isCurrentPromptResource(record)).toBe(true);
    member.text = "\udc00";
    expect(isCurrentPromptResource(record)).toBe(false);
  });
});
