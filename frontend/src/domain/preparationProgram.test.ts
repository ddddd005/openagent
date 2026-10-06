import { describe, expect, it } from "vitest";
import { compilePromptItems, createPreparationNode, newPreparationId } from "./preparation";
import { createPreparationDraft } from "../fixtures/preparation";
import { isBackendPreparationProgram } from "./preparationProgram";
import { createPublication } from "../stores/workbenchRuntime";
import { validCompiledPromptRecord } from "../adapters/workbenchSessions";

function incrementalPlan() {
  const draft = createPreparationDraft("main", "A");
  const item = draft.nodes.find((node) => node.kind === "prompt-item")!;
  const collector = draft.nodes.find((node) => node.kind === "prompt-collect")!;
  const regex = createPreparationNode("regex");
  if (regex.kind !== "regex" || collector.kind !== "prompt-collect") throw new Error("Invalid fixture");
  regex.config.mode = "prompt";
  regex.config.pattern = "draft";
  regex.config.replacement = "answer";
  draft.nodes.push(regex);
  draft.edges = draft.edges.filter((edge) => !(edge.source === item.id && edge.target === collector.id));
  draft.edges.push(
    { id: newPreparationId(), source: item.id, target: regex.id, sourceHandle: "prompt", targetHandle: "prompt" },
    { id: newPreparationId(), source: regex.id, target: collector.id, sourceHandle: "prompt", targetHandle: "prompt" },
  );
  collector.config.inputs = collector.config.inputs.map((id) => id === item.id ? regex.id : id);
  return compilePromptItems(draft);
}

describe("versioned preparation publication", () => {
  it("publishes a valid bounded v2 program without rewriting its stable instance dependencies", () => {
    const plan = incrementalPlan();
    expect(plan.diagnostics.filter((row) => row.severity === "error")).toEqual([]);
    expect(plan.config.schema_version).toBe(2);
    if (plan.config.schema_version !== 2) throw new Error("Not incremental");
    expect(isBackendPreparationProgram(plan.config.preparation)).toBe(true);
    expect(validCompiledPromptRecord(plan.config, "config")).toBe(true);
    const published = createPublication(plan);
    expect(published.config.schema_version).toBe(2);
    if (published.config.schema_version !== 2) throw new Error("Missing program");
    expect(published.config.preparation).toEqual(plan.config.preparation);
    expect(published.config.config_id).not.toBe(plan.config.config_id);
    expect(published.items.every((item) => item.interpolation === "literal" && item.revision === 1)).toBe(true);
  });
  it("retains old fixed v1 records and rejects unknown versions or unbounded executable payloads", () => {
    const fixed = compilePromptItems(createPreparationDraft("main", "A"));
    expect(fixed.config.schema_version).toBe(1);
    expect(validCompiledPromptRecord(fixed.config, "config")).toBe(true);
    expect(validCompiledPromptRecord({ ...fixed.config, schema_version: 3 }, "config")).toBe(false);
    const plan = incrementalPlan();
    expect(validCompiledPromptRecord({ ...plan.config, preparation: { kind: "script", code: "eval()" } }, "config")).toBe(false);
  });
  it("rejects duplicate nodes, forward dependencies, cycles, unknown config fields and invalid output references", () => {
    const plan = incrementalPlan();
    if (plan.config.schema_version !== 2) throw new Error("Not incremental");
    const source = plan.config.preparation;
    const mutate = (change: (program: typeof source) => void) => {
      const program = structuredClone(source);
      change(program);
      return program;
    };
    expect(isBackendPreparationProgram(mutate((program) => program.nodes.push(program.nodes[0])))).toBe(false);
    expect(isBackendPreparationProgram(mutate((program) => {
      program.nodes[0].inputs.input = program.nodes.at(-1)!.node_id;
    }))).toBe(false);
    expect(isBackendPreparationProgram(mutate((program) => { program.nodes[0].config.script = "unknown"; }))).toBe(false);
    expect(isBackendPreparationProgram(mutate((program) => { program.outputs.context = newPreparationId(); }))).toBe(false);
  });
  it("requires an exact explicit collector input order, independent of JSON object key ordering", () => {
    const plan = incrementalPlan();
    if (plan.config.schema_version !== 2) throw new Error("Not incremental");
    const program = structuredClone(plan.config.preparation);
    const collector = program.nodes.find((node) => node.kind === "prompt-collector"
      && Object.keys(node.inputs).length === 2)!;
    const keys = Object.keys(collector.inputs);
    collector.config.input_order = [...keys].reverse();
    expect(isBackendPreparationProgram(program)).toBe(true);
    collector.config.input_order = [keys[0], keys[0]];
    expect(isBackendPreparationProgram(program)).toBe(false);
    collector.config.input_order = [keys[0], "missing"];
    expect(isBackendPreparationProgram(program)).toBe(false);
    delete collector.config.input_order;
    expect(isBackendPreparationProgram(program)).toBe(false);
  });
});
