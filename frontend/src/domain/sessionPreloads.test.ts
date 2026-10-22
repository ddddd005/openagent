import { describe, expect, it } from "vitest";
import { graphClone, isGraphDocument, newGraph } from "./workflowGraph";
import { preloadTypeIssue, preloadValueIssue, sessionPreloadsIssue } from "./sessionPreloads";

import { preloadVariableType, preloadPlainType, preloadBinding, preloadVariableValue } from "../testUtils/sessionPreloadFixture";

describe("session preload declarations", () => {
  it("keeps absence and accepts only schema3 strict entries with one binding authority", () => {
    const old = newGraph("Preloads");
    expect(Object.hasOwn(graphClone(old), "session_preloads")).toBe(false);
    expect(isGraphDocument(old)).toBe(true);
    const document = { ...old, schema_version: 3, object_bindings: [preloadBinding],
      session_preloads: [{ object_key: preloadBinding.object_key, value: { label: "seed" } }] };
    expect(isGraphDocument(document)).toBe(true);
    for (const patch of [
      { schema_version: 2 }, { session_preloads: [...document.session_preloads, ...document.session_preloads] },
      { object_bindings: [] }, { object_bindings: [{ ...preloadBinding, default_value: null }] },
      { session_preloads: [{ ...document.session_preloads[0], unexpected: true }] },
    ]) expect(isGraphDocument({ ...document, ...patch })).toBe(false);
  });
  it("admits exact variables and plain types without defaults and rejects special callbacks", () => {
    expect(preloadTypeIssue(preloadVariableType)).toBeNull();
    expect(preloadTypeIssue(preloadPlainType)).toBeNull();
    expect(sessionPreloadsIssue([preloadBinding], [{ object_key: preloadBinding.object_key, value: { label: "seed" } }],
      [preloadPlainType])).toBeNull();
    for (const flag of ["has_validator", "has_references", "has_reference_mapper", "has_migrator",
      "has_write_validator", "has_artifact_bindings", "has_public_references", "has_content_transformer"])
      expect(preloadTypeIssue({ ...preloadPlainType, [flag]: true })).not.toBeNull();
    expect(preloadTypeIssue({ ...preloadVariableType, schema_version: 2 })).not.toBeNull();
    expect(preloadTypeIssue({ ...preloadPlainType, checkpoint_admission: false })).not.toBeNull();
    for (const type_id of ["workflow.variable", "workflow.chat-context", "workflow.effective-context"])
      expect(preloadTypeIssue({ ...preloadPlainType, type_id, schema_version: 9 })).not.toBeNull();
    for (const keyword of ["$ref", "$dynamicRef", "$recursiveRef"]) {
      expect(preloadTypeIssue({ ...preloadPlainType, schema: {
        type: "object", properties: { field: { [keyword]: "file:///schema.json" } },
      } })).not.toBeNull();
      expect(preloadTypeIssue({ ...preloadPlainType, schema: { [keyword]: "#/$defs/value" } })).toBeNull();
    }
    expect(preloadTypeIssue({ ...preloadPlainType, schema: {
      type: "object", default: { $ref: "ordinary business data" },
    } })).toBeNull();
  });
  it("requires registered complete scalar values while preserving unassigned variables", () => {
    expect(preloadValueIssue(preloadVariableType, preloadVariableValue)).toBeNull();
    expect(preloadValueIssue(preloadVariableType, { ...preloadVariableValue, assigned: false, value: null })).toBeNull();
    for (const patch of [{ registered: false }, { name: "bad name" }, { value: "7" }, { assigned: false },
      { value: Number.MAX_SAFE_INTEGER + 1 }])
      expect(preloadValueIssue(preloadVariableType, { ...preloadVariableValue, ...patch })).not.toBeNull();
  });
});
