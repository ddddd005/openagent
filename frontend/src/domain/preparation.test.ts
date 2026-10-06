import { describe, expect, it } from "vitest";
import {
  assemblePreparationPreview,
  clonePreparation,
  compilePromptItems,
  createPromptMember,
  defaultRolePlacement,
  flattenPreparedItems,
  getContextCollection,
  getPromptCollection,
  getToolDefinition,
  newPreparationId,
  preparationPorts,
  PREPARATION_LIMITS,
  transformPreparedCollection,
  validatePreparation,
  validateRolePlacement,
  type ContextReference,
  type PreparationDraft,
  type PreparationNode,
  type PreparedMaterial,
} from "./preparation";
import { createPreparationDraft } from "../fixtures/preparation";

function nodeOf<K extends PreparationNode["kind"]>(
  draft: PreparationDraft, kind: K,
): Extract<PreparationNode, { kind: K }> {
  return draft.nodes.find((node) => node.kind === kind) as Extract<PreparationNode, { kind: K }>;
}
function basicDraft() {
  const draft = createPreparationDraft("frontend:main-test", "A");
  nodeOf(draft, "root-input").config.text = "current";
  nodeOf(draft, "prompt-group").config.enabled = false;
  nodeOf(draft, "tool").config.description.enabled = false;
  nodeOf(draft, "tool").config.schema.enabled = false;
  return draft;
}
function addContext(draft: PreparationDraft) {
  const node = nodeOf(draft, "context");
  const reference: ContextReference = {
    workflowId: draft.workflowId, stage: draft.stage,
    workflowSessionId: newPreparationId(), nodeBindingId: newPreparationId(),
    selectedChainId: newPreparationId(),
  };
  const item = (
    role: PreparedMaterial["role"], text: string,
    floorId: string, floorKind: "root" | "delta", messageId = newPreparationId(),
  ): PreparedMaterial => ({
    id: messageId, name: text, role, presentation: null, declarationIndex: 0,
    payload: { kind: "text", text },
    source: {
      kind: "context", nodeId: node.id,
      context: { reference, floorId, floorKind, messageId },
    },
  });
  const rootId = newPreparationId();
  const deltaId = newPreparationId();
  node.config = {
    reference, source: "backend-read",
    floors: [
      { id: rootId, kind: "root", items: [item("user", "previous", rootId, "root")] },
      { id: deltaId, kind: "delta", items: [item("assistant", "answer", deltaId, "delta")] },
    ],
  };
  return { node, item, reference, rootId, deltaId };
}

describe("preparation model", () => {
  it("seeds seven base node types plus a separate root and a fully valid fixed pipeline", () => {
    const draft = createPreparationDraft("test", "A");
    expect(new Set(draft.nodes.map((node) => node.kind)).size).toBe(8);
    expect(validatePreparation(draft)).toEqual([]);
    expect(preparationPorts("context")).toEqual([expect.objectContaining({
      id: "context", direction: "output",
    })]);
  });

  it("validates shared role and depth semantics, preserving invalid numbers when cloned", () => {
    expect(validateRolePlacement(defaultRolePlacement())).toEqual([]);
    expect(defaultRolePlacement({ placement: "after", depth: 99 }).depth).toBeNull();
    expect(validateRolePlacement(defaultRolePlacement({ placement: "middle", depth: -1 }))[0].code).toBe("invalid_depth");
    const invalid = clonePreparation(defaultRolePlacement({ order: NaN }));
    expect(Number.isNaN(invalid.order)).toBe(true);
    expect(validateRolePlacement(invalid)[0].code).toBe("invalid_order");
    expect(validateRolePlacement({ ...defaultRolePlacement(), role: "tool" } as never)[0].code).toBe("invalid_role");
  });

  it("retains group member identity and text duplicates with stable declaration order", () => {
    const draft = basicDraft();
    const group = nodeOf(draft, "prompt-group");
    group.config.enabled = true;
    group.config.members.push(createPromptMember());
    for (const member of group.config.members) member.text = "same";
    const materials = flattenPreparedItems(draft);
    expect(materials).toHaveLength(3);
    expect(materials[1].payload).toEqual(materials[2].payload);
    expect(materials[1].id).not.toBe(materials[2].id);
    expect(materials[1].source.groupInstanceId).toBe(group.id);
    expect(materials.map((item) => item.declarationIndex)).toEqual([0, 1, 2]);
    materials[1].source.groupId = "mutated";
    expect(group.config.groupId).not.toBe("mutated");
  });

  it("keeps description/schema identity paired to the exact immutable registered native tool", () => {
    const draft = createPreparationDraft("test", "A");
    const materials = flattenPreparedItems(draft).filter((item) => item.source.kind.startsWith("tool-"));
    expect(materials).toHaveLength(2);
    expect(materials[0].source.toolRef).toEqual(materials[1].source.toolRef);
    const definition = getToolDefinition({ name: "inspect_text", version: "1" })!;
    expect(definition.description).toBe("Count characters and lines in a text.");
    expect(definition.parametersSchema).toEqual({
      type: "object",
      properties: { text: { type: "string", description: "Text to inspect" } },
      required: ["text"], additionalProperties: false,
    });
    expect(Object.isFrozen(definition.parametersSchema)).toBe(true);
    expect(getToolDefinition({ name: "inspect_text", version: "2" })).toBeNull();
    const tool = nodeOf(draft, "tool");
    draft.edges = draft.edges.filter((edge) => !(edge.source === tool.id && edge.sourceHandle === "tool-schemas"));
    expect(validatePreparation(draft).some((error) => error.code === "tool_pair_mismatch")).toBe(true);
  });

  it("uses declared input/member order for ties and never uses canvas coordinates", () => {
    const draft = basicDraft();
    const item = nodeOf(draft, "prompt-item");
    const group = nodeOf(draft, "prompt-group");
    const collect = nodeOf(draft, "prompt-collect");
    item.config.text = "standalone";
    group.config.enabled = true;
    group.config.members.push(createPromptMember());
    group.config.members.forEach((member, index) => {
      member.text = `member-${index}`;
      member.presentation.order = 0;
    });
    collect.config.inputs = [group.id, item.id];
    const before = assemblePreparationPreview(draft).messages.map((message) => message.text);
    expect(before).toEqual(["member-0", "member-1", "standalone", "current"]);
    item.position = { x: -1000, y: -1000 };
    group.position = { x: 1000, y: 1000 };
    expect(assemblePreparationPreview(draft).messages.map((message) => message.text)).toEqual(before);
  });

  it.each([
    ["before", null, ["prompt", "previous", "answer", "current"]],
    ["middle", 0, ["previous", "answer", "current", "prompt"]],
    ["middle", 1, ["previous", "answer", "prompt", "current"]],
    ["middle", 2, ["previous", "prompt", "answer", "current"]],
    ["middle", 99, ["prompt", "previous", "answer", "current"]],
    ["after", null, ["previous", "answer", "current", "prompt"]],
  ] as const)("places %s depth %s only at complete logical floor boundaries", (placement, depth, texts) => {
    const draft = basicDraft();
    addContext(draft);
    const prompt = nodeOf(draft, "prompt-item");
    prompt.config.text = "prompt";
    prompt.config.presentation = defaultRolePlacement({ placement, depth });
    const preview = assemblePreparationPreview(draft);
    expect(preview.diagnostics).toEqual([]);
    expect(preview.messages.map((message) => message.text)).toEqual(texts);
  });

  it("inserts outside protected tool-call/result pairs and rejects incomplete protocol floors", () => {
    const draft = basicDraft();
    const { node, item, deltaId } = addContext(draft);
    const call = item("assistant", "call", deltaId, "delta");
    const result = item("tool", "result", deltaId, "delta");
    call.payload = {
      kind: "protected-message", message: {
        messageId: call.id, role: "assistant", source: { kind: "model" },
        blocks: [{ kind: "tool_call", tool_call_id: "call-1", tool_name: "inspect_text" }],
      },
    };
    result.payload = {
      kind: "protected-message", message: {
        messageId: result.id, role: "tool", source: { kind: "tool" },
        blocks: [{ kind: "tool_result", tool_call_id: "call-1", content: { characters: 1 } }],
      },
    };
    node.config.floors[1].items = [call, result];
    const prompt = nodeOf(draft, "prompt-item");
    prompt.config.presentation = defaultRolePlacement({ placement: "middle", depth: 1 });
    const messages = assemblePreparationPreview(draft).messages;
    expect(messages.findIndex((message) => message.id === result.id))
      .toBe(messages.findIndex((message) => message.id === call.id) + 1);
    node.config.floors[1].items.pop();
    expect(validatePreparation(draft).some((error) => error.code === "tool_protocol")).toBe(true);
  });

  it("rejects duplicated or cross-scope context rather than pretending it is canonical history", () => {
    const draft = basicDraft();
    const { node } = addContext(draft);
    node.config.floors[1].items.push(clonePreparation(node.config.floors[1].items[0]));
    expect(validatePreparation(draft).some((error) => error.code === "duplicate_context")).toBe(true);
    node.config.reference!.stage = "B";
    expect(validatePreparation(draft).some((error) => error.code === "context_scope_mismatch")).toBe(true);
  });
  it("accepts multiple text-block materials of one root but rejects mixed original-message origins", () => {
    const draft = basicDraft();
    const { node } = addContext(draft);
    const first = node.config.floors[0].items[0];
    const message = {
      schema_version: 1 as const, message_id: first.id, role: "user" as const,
      source: { kind: "human", visible_message_id: newPreparationId() },
      blocks: [{ kind: "text", text: "previous" }, { kind: "text", text: "second" }],
    };
    first.id += ":0";
    first.source.context!.blockIndex = 0;
    first.source.context!.canonicalMessage = message;
    const second = clonePreparation(first);
    second.id = `${message.message_id}:1`;
    second.payload = { kind: "text", text: "second" };
    second.source.context!.blockIndex = 1;
    node.config.floors[0].items.push(second);
    expect(validatePreparation(draft)).toEqual([]);
    (second.source.context!.canonicalMessage!.source as Record<string, unknown>).visible_message_id = newPreparationId();
    expect(validatePreparation(draft).some((entry) => entry.code === "duplicate_context")).toBe(true);
  });

  it("shares one versioned collection with text processing while protected canonical records stay unchanged", () => {
    const draft = basicDraft();
    const { node } = addContext(draft);
    const protectedFinal = node.config.floors[1].items[0];
    protectedFinal.payload = { kind: "structured-final", value: { text: "answer" }, displayText: "answer" };
    const contexts = getContextCollection(draft);
    const prompts = getPromptCollection(draft);
    expect(contexts.kind).toBe(prompts.kind);
    const original = clonePreparation(contexts);
    const derived = transformPreparedCollection(contexts, "test-upper", (text) => text.toUpperCase());
    expect(derived.items[0].payload).toEqual({ kind: "text", text: "PREVIOUS" });
    expect(derived.items[0].source.context).toEqual(original.items[0].source.context);
    expect(derived.items[0].source.derivation?.sourceInstanceId).toBe(original.items[0].id);
    expect(derived.items[1]).toEqual(original.items[1]);
    expect(Object.isFrozen(derived.items[1].payload)).toBe(true);
    expect(contexts).toEqual(original);
  });

  it("bounds input/output preview and derived transformations", () => {
    const draft = basicDraft();
    nodeOf(draft, "prompt-item").config.text = "a".repeat(100);
    const preview = assemblePreparationPreview(draft, { ...PREPARATION_LIMITS, maxTotalChars: 50 });
    expect(preview.messages).toEqual([]);
    expect(preview.diagnostics.some((entry) => entry.code === "input_limit")).toBe(true);
    const materials = getPromptCollection(draft);
    expect(() => transformPreparedCollection(materials, "too-large", () => "b".repeat(200), {
      ...PREPARATION_LIMITS, maxTotalChars: 150,
    })).toThrow("Derived text limit exceeded");
  });

  it("compiles literal exact backend v1 records with group references and null non-middle depth", () => {
    const draft = createPreparationDraft("test", "A");
    const compiled = compilePromptItems(draft);
    expect(compiled.diagnostics.filter((error) => error.severity === "error")).toEqual([]);
    expect(compiled.items).toHaveLength(4);
    expect(compiled.groups).toHaveLength(1);
    expect(compiled.config.inputs.map((input) => input.kind)).toEqual(["item", "group", "item", "item"]);
    expect(compiled.items.every((item) => item.interpolation === "literal" && item.depth === null)).toBe(true);
    expect(compiled.groups[0].members[0].item_id).toBe(compiled.items[1].item_id);
    expect(compiled.tools).toEqual([{ name: "inspect_text", version: "1" }]);
    expect(JSON.stringify(compiled.config)).not.toContain("preparation-collection");
  });

  it("rejects unsupported runtime topology while omitting read caches from prompt compilation", () => {
    const draft = basicDraft();
    const context = nodeOf(draft, "context");
    draft.edges = draft.edges.filter((edge) => edge.source !== context.id);
    const unsupported = compilePromptItems(draft);
    expect(unsupported.diagnostics.some((error) => error.code === "required_runtime_input")).toBe(true);
    expect(unsupported.items).toEqual([]);
    const full = basicDraft();
    addContext(full);
    const before = clonePreparation(full);
    const compiled = compilePromptItems(full);
    expect(compiled.diagnostics.some((error) => error.severity === "error")).toBe(false);
    expect(JSON.stringify(compiled.config)).not.toContain("workflowSessionId");
    expect(full).toEqual(before);
  });

  it("rejects conflicting definition identities instead of rebasing different revisions to one record", () => {
    const draft = createPreparationDraft("test", "A");
    const item = nodeOf(draft, "prompt-item");
    const member = nodeOf(draft, "prompt-group").config.members[0];
    member.itemId = item.config.itemId;
    member.revision = item.config.revision + 1;
    expect(compilePromptItems(draft).diagnostics.some((entry) => entry.code === "conflicting_definition")).toBe(true);
  });
});
