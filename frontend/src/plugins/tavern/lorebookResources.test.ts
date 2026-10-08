import { describe, expect, it } from "vitest";
import { graphClone } from "../../domain/workflowGraph";
import { newLorebookEntry } from "../../domain/lorebook";
import { isCurrentLorebookResource, isLorebookActivationConfig, isLorebookIdentity,
  isLorebookReferenceConfig, lorebookIdentity, lorebookIdentityKey, newLorebookResource } from "./lorebookResources";

describe("global lorebook contracts", () => {
  it("retains resource and member identities independently of labels and session permissions", () => {
    const resource = newLorebookResource();
    resource.value.entries = [newLorebookEntry()];
    const original = graphClone(resource), identity = lorebookIdentity(resource);
    resource.value.name = "New name";
    resource.value.entries[0]!.name = "Member name";
    resource.value.entries[0]!.metadata = { preserved: [true, null, "text"] };
    expect(isCurrentLorebookResource(resource)).toBe(true);
    expect(lorebookIdentity(resource)).toEqual(identity);
    expect(resource.value.entries[0]!.id).toBe(original.value.entries[0]!.id);
    expect(isLorebookReferenceConfig({ reference: identity })).toBe(true);
    expect(isLorebookActivationConfig({ object_keys: ["variable/place"] })).toBe(true);
    expect(lorebookIdentityKey({ ...identity, scope: "project:one" })).not.toBe(lorebookIdentityKey(identity));
  });
  it.each(["object_keys", "scan_state", "lifecycle", "probability_result"])("rejects global %s", field => {
    const record = newLorebookResource();
    expect(isCurrentLorebookResource({ ...record, value: { ...record.value, [field]: [] } })).toBe(false);
  });
  it("rejects duplicate ids, malformed text, large resources and exact-schema escapes", () => {
    const record = newLorebookResource(), member = newLorebookEntry();
    record.value.entries = [member, member];
    expect(isCurrentLorebookResource(record)).toBe(false);
    record.value.entries = [member];
    member.text = "\ud800";
    expect(isCurrentLorebookResource(record)).toBe(false);
    member.text = "x".repeat(1_000_000);
    expect(isCurrentLorebookResource(record)).toBe(false);
    record.value.entries = [];
    expect(isCurrentLorebookResource({ ...record, data_schema_version: 2 })).toBe(false);
    expect(isCurrentLorebookResource({ ...record, update_sequence: 0 })).toBe(false);
    expect(isLorebookIdentity({ ...lorebookIdentity(record), type_id: "workflow.prompt-resource" })).toBe(false);
    expect(isLorebookIdentity({ ...lorebookIdentity(record), scope: "session" })).toBe(false);
    expect(isLorebookReferenceConfig({ reference: lorebookIdentity(record), object_keys: [] })).toBe(false);
    expect(isLorebookActivationConfig({ object_keys: ["variable/place", "variable/place"] })).toBe(false);
    expect(isLorebookActivationConfig({ object_keys: [], reference: lorebookIdentity(record) })).toBe(false);
  });
});
