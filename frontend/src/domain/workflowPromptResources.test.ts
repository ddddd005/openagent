import { describe, expect, it } from "vitest";
import { clonePromptResource, duplicatePromptResource, isCurrentPromptResource, isPromptIdentity, movePromptMember, newPromptMember, newPromptResource,
  promptIdentity, promptResourceLabel, promptResourceType, samePromptIdentity } from "./workflowPromptResources";

describe("current independent prompt resource contracts", () => {
  it("creates an empty workspace group without legacy name or kind fields", () => {
    const record = newPromptResource();
    expect(record).toMatchObject({ envelope_version: 1, scope: "workspace", type_id: promptResourceType,
      data_schema_version: 2, update_sequence: 1, value: { enabled: true, members: [] } });
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
    { data_schema_version: 3 }, { data_schema_version: "1" }, { update_sequence: 0 }, { update_sequence: -1 },
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
  it("requires explicit lifecycle fields in schema two without silently upgrading schema one", () => {
    const record = newPromptResource(2), member = newPromptMember("Persistent facts", 2);
    record.value.members = [member];
    expect(member).toMatchObject({ lifecycle: "per_request", compaction: "never" });
    expect(isCurrentPromptResource(record)).toBe(true);
    member.lifecycle = "context_once"; member.compaction = "allowed";
    expect(isCurrentPromptResource(record)).toBe(false);
    member.presentation.role = "user";
    expect(isCurrentPromptResource(record)).toBe(true);
    member.lifecycle = "per_request";
    expect(isCurrentPromptResource(record)).toBe(false);
    member.compaction = "never";
    expect(isCurrentPromptResource(record)).toBe(true);
    expect(isCurrentPromptResource({ ...record, data_schema_version: 1 })).toBe(false);
    record.value.members = [newPromptMember("", 1)];
    expect(isCurrentPromptResource(record)).toBe(false);
  });
  it.each([1, 2] as const)("duplicates schema %i into an independent group without upgrading or mutating the source", version => {
    const record = newPromptResource(version);
    record.scope = "project:story"; record.update_sequence = 9; record.value.enabled = false;
    const member = newPromptMember("Background", version);
    member.metadata = { unknown: { labels: ["retain", null, true] } };
    member.presentation = { role: "user", placement: "middle", depth: 2, order: -7, enabled: false };
    if (version === 2) { member.lifecycle = "context_once"; member.compaction = "allowed"; }
    record.value.members = [member, newPromptMember("Rule", version)];
    const original = clonePromptResource(record), copy = duplicatePromptResource(record);
    expect(isCurrentPromptResource(copy)).toBe(true);
    expect(copy.resource_id).not.toBe(record.resource_id);
    expect(copy.update_sequence).toBe(1);
    expect(copy.scope).toBe("project:story");
    expect(copy.data_schema_version).toBe(version);
    expect(copy.value.enabled).toBe(false);
    expect(copy.value.members.map(item => item.id)).not.toEqual(record.value.members.map(item => item.id));
    for (const [index, copied] of copy.value.members.entries()) {
      expect(record.value.members.some(item => item.id === copied.id)).toBe(false);
      expect({ ...copied, id: member.id }).toEqual({ ...record.value.members[index], id: member.id });
    }
    copy.value.members[0]!.text = "Edited copy";
    (copy.value.members[0]!.metadata.unknown as { labels: unknown[] }).labels.push(2);
    expect(record).toEqual(original);
  });
  it("duplicates empty groups and rejects malformed contracts before generating a draft", () => {
    const record = newPromptResource(2), copy = duplicatePromptResource(record);
    expect(copy.value.members).toEqual([]);
    expect(copy.resource_id).not.toBe(record.resource_id);
    expect(() => duplicatePromptResource({ ...record, update_sequence: 0 })).toThrow("contract is invalid");
    expect(() => duplicatePromptResource({ ...record, value: { ...record.value, name: "invalid" } } as typeof record))
      .toThrow("contract is invalid");
  });
  it("moves members with strictly increasing ranks without changing identity or other semantics", () => {
    const members = [newPromptMember("One", 2), newPromptMember("Two", 2), newPromptMember("Three", 2)];
    members.forEach((member, index) => { member.presentation.order = [-7, 3, 19][index]!; });
    members[1]!.presentation.role = "user";
    members[1]!.presentation.placement = "middle"; members[1]!.presentation.depth = 4;
    members[1]!.lifecycle = "context_once"; members[1]!.compaction = "allowed";
    members[1]!.metadata = { custom: ["preserved"] };
    const original = members.map(member => clonePromptResource({ ...newPromptResource(2), value: { enabled: true, members: [member] } }).value.members[0]!);
    expect(movePromptMember(members, 1, -1)).toBe(true);
    expect(members.map(member => member.id)).toEqual([original[1]!.id, original[0]!.id, original[2]!.id]);
    expect(members.map(member => member.presentation.order)).toEqual([-7, 3, 19]);
    for (const member of members) {
      const source = original.find(item => item.id === member.id)!;
      expect({ ...member, presentation: { ...member.presentation, order: source.presentation.order } }).toEqual(source);
    }
    expect(movePromptMember(members, 0, 1)).toBe(true);
    expect(members).toEqual(original);
  });
  it.each([[0, 0, 0], [9, -5, 4], [0, 1, 1]])(
    "normalizes ambiguous ranks %j only on an explicit move", (...ranks) => {
      const members = ranks.map((rank, index) => {
        const member = newPromptMember(String(index), 2); member.presentation.order = rank; return member;
      });
      const ids = members.map(member => member.id);
      expect(movePromptMember(members, 0, 1)).toBe(true);
      expect(members.map(member => member.id)).toEqual([ids[1], ids[0], ids[2]]);
      expect(members.map(member => member.presentation.order)).toEqual([0, 1, 2]);
    });
  it.each([[-1, 1], [0, -1], [1, 1], [2, -1], [0, 0], [0.5, 1], [0, NaN], [0, Infinity]])(
    "rejects an invalid move at %s with offset %s without changing members", (index, offset) => {
      const members = [newPromptMember("One"), newPromptMember("Two")];
      const original = structuredClone(members);
      expect(movePromptMember(members, index, offset)).toBe(false);
      expect(members).toEqual(original);
      expect(movePromptMember([], index, offset)).toBe(false);
    });
});
