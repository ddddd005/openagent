import { describe, expect, it } from "vitest";
import {
  assemblePreparationPreview, clonePreparation, compilePromptItems,
  createPreparationNode, newPreparationId, preparationPorts, validatePreparation,
  type PreparationDraft, type PreparationNode,
} from "./preparation";
import { createPreparationDraft } from "../fixtures/preparation";

function of<K extends PreparationNode["kind"]>(draft: PreparationDraft, kind: K) {
  return draft.nodes.find((node) => node.kind === kind) as Extract<PreparationNode, { kind: K }>;
}
function edge(draft: PreparationDraft, source: string, target: string,
  sourceHandle: string, targetHandle = sourceHandle) {
  draft.edges.push({ id: newPreparationId(), source, target, sourceHandle, targetHandle });
}
function insertPromptProcessor(draft: PreparationDraft, kind: "regex" | "variable-replace") {
  const item = of(draft, "prompt-item");
  const collect = of(draft, "prompt-collect");
  const processor = createPreparationNode(kind);
  draft.nodes.push(processor);
  draft.edges = draft.edges.filter((entry) => !(entry.source === item.id && entry.target === collect.id));
  collect.config.inputs = collect.config.inputs.map((id) => id === item.id ? processor.id : id);
  edge(draft, item.id, processor.id, "prompt");
  edge(draft, processor.id, collect.id, "prompt");
  return processor;
}

describe("incremental preparation configuration", () => {
  it("compiles a Python regex declaration without executing JavaScript regex or rewriting fixed definitions", () => {
    const draft = createPreparationDraft("main", "A");
    const node = insertPromptProcessor(draft, "regex");
    if (node.kind !== "regex") throw new Error("wrong kind");
    node.config.pattern = "(?P<word>draft)";
    node.config.replacement = "\\g<word>";
    node.config.flags = ["i", "m"];
    const before = clonePreparation(draft);
    const result = compilePromptItems(draft);
    expect(result.diagnostics.filter((item) => item.severity === "error")).toEqual([]);
    expect(result.config.schema_version).toBe(2);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    expect(result.config.preparation.nodes.find((item) => item.node_id === node.id)).toMatchObject({
      kind: "regex", config: { mode: "prompt", rule: {
        pattern: "(?P<word>draft)", replacement: "\\g<word>", flags: "im", mode: "all",
      } },
    });
    expect(result.items.every((item) => item.interpolation === "literal")).toBe(true);
    expect(result.config.preparation.nodes.filter((item) => item.kind === "prompt-source")).toHaveLength(3);
    expect(assemblePreparationPreview(draft).messages).toEqual([]);
    expect(assemblePreparationPreview(draft).diagnostics).toContainEqual(expect.objectContaining({
      code: "backend_preparation_required", severity: "warning",
    }));
    expect(draft).toEqual(before);
  });

  it("keeps mode-incompatible edges for diagnosis and declares correct input and output types", () => {
    const draft = createPreparationDraft("main", "A");
    const node = insertPromptProcessor(draft, "regex");
    if (node.kind !== "regex") throw new Error("wrong kind");
    const before = clonePreparation(draft.edges);
    node.config.mode = "text";
    expect(preparationPorts(node).map((port) => port.id)).toEqual(["text", "text"]);
    expect(draft.edges).toEqual(before);
    expect(validatePreparation(draft).filter((item) => item.code === "incompatible_port")).toHaveLength(2);
    expect(compilePromptItems(draft).items).toEqual([]);
  });

  it("supports Chinese typed declarations and distinguishes unassigned from an empty string", () => {
    const draft = createPreparationDraft("main", "A");
    const declaration = createPreparationNode("variable-register");
    if (declaration.kind !== "variable-register") throw new Error("wrong kind");
    declaration.config.name = "倒计时";
    draft.nodes.push(declaration);
    expect(validatePreparation(draft)).toEqual([]);
    const unassigned = compilePromptItems(draft);
    if (unassigned.config.schema_version !== 2) throw new Error("wrong version");
    expect(unassigned.config.preparation.nodes[0]).toMatchObject({
      kind: "variable-register", config: { name: "倒计时", type: "string" },
    });
    expect(unassigned.config.preparation.nodes[0].config).not.toHaveProperty("initial");
    declaration.config.hasInitialValue = true;
    declaration.config.initialValue = "";
    const assigned = compilePromptItems(draft);
    if (assigned.config.schema_version !== 2) throw new Error("wrong version");
    expect(assigned.config.preparation.nodes[0].config).toHaveProperty("initial", "");
    declaration.config.name = "{{倒计时}}";
    expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({
      code: "invalid_variable_name", nodeId: declaration.id,
    }));
  });

  it("declares the explicit countdown-to-text-to-b path once with exact dependencies", () => {
    const draft = createPreparationDraft("main", "A");
    const countdown = createPreparationNode("variable-register");
    const text = createPreparationNode("text");
    const replacement = createPreparationNode("variable-replace");
    const b = createPreparationNode("variable-register");
    const prompt = createPreparationNode("text-to-prompt");
    if (countdown.kind !== "variable-register" || text.kind !== "text"
      || replacement.kind !== "variable-replace" || b.kind !== "variable-register")
      throw new Error("wrong kind");
    countdown.config = { name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10 };
    text.config.text = "{{倒计时}}";
    replacement.config.mode = "text";
    b.config.name = "b";
    draft.nodes.push(countdown, text, replacement, b, prompt);
    edge(draft, text.id, replacement.id, "text");
    edge(draft, replacement.id, b.id, "text");
    edge(draft, b.id, prompt.id, "text");
    const collect = of(draft, "prompt-collect");
    collect.config.inputs.push(prompt.id);
    edge(draft, prompt.id, collect.id, "prompt");
    const result = compilePromptItems(draft);
    expect(result.diagnostics.filter((item) => item.severity === "error")).toEqual([]);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    const program = result.config.preparation;
    expect(program.nodes.find((node) => node.node_id === replacement.id)?.inputs).toEqual({ input: text.id });
    expect(program.nodes.find((node) => node.node_id === b.id)).toMatchObject({
      kind: "variable-register", inputs: { text: replacement.id }, config: { name: "b", type: "string" },
    });
    expect(new Set(program.nodes.map((node) => node.node_id)).size).toBe(program.nodes.length);
    const completed = new Set<string>();
    for (const node of program.nodes) {
      expect(Object.values(node.inputs).every((id) => completed.has(id))).toBe(true);
      completed.add(node.node_id);
    }
  });

  it("connects protected context through prompt-mode processing while excluding observed bodies from publication", () => {
    const draft = createPreparationDraft("main", "A");
    const context = of(draft, "context");
    const assembly = of(draft, "assemble");
    const replacement = createPreparationNode("variable-replace");
    draft.nodes.push(replacement);
    draft.edges = draft.edges.filter((entry) => entry.source !== context.id);
    edge(draft, context.id, replacement.id, "context", "prompt");
    edge(draft, replacement.id, assembly.id, "prompt", "context");
    context.config.reference = {
      workflowId: "main", stage: "A", workflowSessionId: "private",
      nodeBindingId: "binding", selectedChainId: "chain",
    };
    const result = compilePromptItems(draft);
    expect(result.diagnostics.filter((item) => item.severity === "error")).toEqual([]);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    expect(result.config.preparation.outputs.context).toBe(replacement.id);
    expect(JSON.stringify(result.config)).not.toContain("private");
    expect(result.config.preparation.nodes.find((node) => node.node_id === context.id)).toEqual({
      node_id: context.id, kind: "context-source", config: {}, inputs: {}, output_ports: ["context"],
    });
  });

  it("rejects cycles, conflicting declarations and numeric operations on strings", () => {
    const draft = createPreparationDraft("main", "A");
    const declaration = createPreparationNode("variable-register");
    const assignment = createPreparationNode("variable-assign");
    if (declaration.kind !== "variable-register" || assignment.kind !== "variable-assign")
      throw new Error("wrong kind");
    declaration.config.name = "b";
    assignment.config.name = "b";
    assignment.config.operation = "add";
    draft.nodes.push(declaration, assignment);
    expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({
      code: "invalid_variable_operation", nodeId: assignment.id,
    }));
    edge(draft, declaration.id, assignment.id, "text");
    edge(draft, assignment.id, declaration.id, "text");
    expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({ code: "preparation_cycle" }));
  });

  it("uses the runtime current input source rather than the cached textarea body", () => {
    const draft = createPreparationDraft("main", "A");
    const root = of(draft, "root-input");
    const converter = createPreparationNode("text-to-prompt");
    root.config.text = "preview-only";
    draft.nodes.push(converter);
    edge(draft, root.id, converter.id, "current-input", "text");
    const collect = of(draft, "prompt-collect");
    collect.config.inputs.push(converter.id);
    edge(draft, converter.id, collect.id, "prompt");
    const result = compilePromptItems(draft);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    expect(result.config.preparation.nodes.find((node) => node.node_id === root.id)?.config)
      .toEqual({ source: "root_input" });
    expect(JSON.stringify(result.config)).not.toContain("preview-only");
  });

  it("retains explicit text initialization as a valid side-effect branch without requiring duplicate material output", () => {
    const draft = createPreparationDraft("main", "A");
    const text = createPreparationNode("text");
    const declaration = createPreparationNode("variable-register");
    if (text.kind !== "text" || declaration.kind !== "variable-register") throw new Error("wrong kind");
    text.config.text = "10";
    declaration.config.name = "b";
    draft.nodes.push(text, declaration);
    edge(draft, text.id, declaration.id, "text");
    expect(validatePreparation(draft)).toEqual([]);
    const result = compilePromptItems(draft);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    expect(result.config.preparation.nodes.find((node) => node.node_id === declaration.id)?.inputs)
      .toEqual({ text: text.id });
  });

  it("keeps mixed context on the prompt path and rejects flattening or a mixed dedicated context output", () => {
    const draft = createPreparationDraft("main", "A");
    const context = of(draft, "context");
    const collect = of(draft, "prompt-collect");
    const assembly = of(draft, "assemble");
    draft.edges = draft.edges.filter((entry) => entry.source !== context.id);
    edge(draft, context.id, collect.id, "context", "prompt");
    collect.config.inputs.push(context.id);
    expect(validatePreparation(draft)).toEqual([]);
    const result = compilePromptItems(draft);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    expect(result.config.preparation.outputs.context).toBeNull();
    edge(draft, collect.id, assembly.id, "prompt", "context");
    expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({ code: "mixed_context_port" }));
    const converter = createPreparationNode("prompt-to-text");
    draft.nodes.push(converter);
    edge(draft, collect.id, converter.id, "prompt");
    expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({
      code: "protected_context_conversion", nodeId: converter.id,
    }));
  });

  it("declares collector input order explicitly rather than relying on object key insertion order", () => {
    const draft = createPreparationDraft("main", "A");
    const collect = of(draft, "prompt-collect");
    for (let index = 0; index < 12; index += 1) {
      const source = createPreparationNode("prompt-item");
      draft.nodes.push(source);
      collect.config.inputs.push(source.id);
      edge(draft, source.id, collect.id, "prompt");
    }
    const declaration = createPreparationNode("variable-register");
    if (declaration.kind !== "variable-register") throw new Error("wrong kind");
    declaration.config.name = "collector_order";
    draft.nodes.push(declaration);
    const result = compilePromptItems(draft);
    expect(result.diagnostics.filter((entry) => entry.severity === "error")).toEqual([]);
    if (result.config.schema_version !== 2) throw new Error("wrong version");
    const compiledCollect = result.config.preparation.nodes.find((node) => node.node_id === collect.id)!;
    const order = collect.config.inputs.map((_, index) => `input-${index}`);
    expect(compiledCollect.config.input_order).toEqual(order);
    expect([...Object.keys(compiledCollect.inputs)].sort()).not.toEqual(order);
    const canonicalInputs = Object.fromEntries(Object.entries(compiledCollect.inputs).sort(([left], [right]) =>
      left.localeCompare(right)));
    expect(order.map((key) => canonicalInputs[key])).toEqual(collect.config.inputs);
    const synthetic = result.config.preparation.nodes.find((node) => node.node_id.endsWith(":materials"))!;
    expect(synthetic.config.input_order).toEqual(["prompt", "tools"]);
  });

  it.each(["collector", "regex", "variable-replace"] as const)(
    "rejects global content mixed with protected context on the dedicated context output through %s",
    (path) => {
      const draft = createPreparationDraft("main", "A");
      const context = of(draft, "context");
      const collect = of(draft, "prompt-collect");
      const assembly = of(draft, "assemble");
      const global = createPreparationNode("global-content");
      draft.nodes = draft.nodes.filter((node) => node.kind !== "prompt-item" && node.kind !== "prompt-group");
      draft.edges = draft.edges.filter((entry) => entry.target !== collect.id && entry.source !== context.id);
      draft.nodes.push(global);
      collect.config.inputs = [context.id, global.id];
      edge(draft, context.id, collect.id, "context", "prompt");
      edge(draft, global.id, collect.id, "prompt");
      let output = collect.id;
      if (path !== "collector") {
        const processor = createPreparationNode(path);
        draft.nodes.push(processor);
        edge(draft, collect.id, processor.id, "prompt");
        output = processor.id;
        draft.edges = draft.edges.filter((entry) => !(entry.source === collect.id && entry.target === assembly.id));
        edge(draft, output, assembly.id, "prompt");
      }
      expect(validatePreparation(draft)).toEqual([]);
      expect(compilePromptItems(draft).diagnostics).toEqual([]);
      edge(draft, output, assembly.id, "prompt", "context");
      expect(validatePreparation(draft)).toContainEqual(expect.objectContaining({
        code: "mixed_context_port", nodeId: assembly.id,
      }));
      expect(compilePromptItems(draft).items).toEqual([]);
    },
  );
});
