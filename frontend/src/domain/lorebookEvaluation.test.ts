import { describe, expect, it } from "vitest";
import { lorebookEvaluation, lorebookEvaluationLabels } from "./lorebookEvaluation";
const diagnostic = () => ({ entry_id: crypto.randomUUID(), name: "山谷", status: "activated",
  activation_round: 0, probability_passed: true, primary_matched: true, secondary_matched: false });
describe("bounded lorebook evaluation facts", () => {
  it("recognizes the actual read record and labels all four activation results", () => {
    const record = { kind: "lorebook_evaluation", recursive_rounds: 3,
      input_ref: { scope: "artifact", output_id: crypto.randomUUID() }, entries: [diagnostic()] };
    expect(lorebookEvaluation(record)).toEqual(record);
    expect(lorebookEvaluationLabels).toEqual({ disabled: "已禁用", probability_rejected: "概率未通过",
      keyword_miss: "关键词未命中", activated: "已触发" });
    expect(lorebookEvaluation({ ...record, entries: [] })).toEqual({ ...record, entries: [] });
  });
  it.each([
    { kind: "other" }, { recursive_rounds: 4 }, { recursive_rounds: -1 },
    { entries: [{ ...diagnostic(), activation_round: null }] },
    { entries: [{ ...diagnostic(), status: "probability_rejected", activation_round: 0 }] },
    { entries: [{ ...diagnostic(), status: "unknown" }] },
    { entries: [{ ...diagnostic(), primary_matched: "true" }] },
    { entries: [{ ...diagnostic(), entry_id: "invalid" }] },
  ])("does not manufacture results from malformed evidence %#", patch => {
    expect(lorebookEvaluation({ kind: "lorebook_evaluation", recursive_rounds: 0, entries: [diagnostic()], ...patch })).toBeNull();
  });
  it("rejects duplicate entry identities and unbounded diagnostic lists", () => {
    const entry = diagnostic();
    expect(lorebookEvaluation({ kind: "lorebook_evaluation", recursive_rounds: 0, entries: [entry, entry] })).toBeNull();
    expect(lorebookEvaluation({ kind: "lorebook_evaluation", recursive_rounds: 0,
      entries: Array.from({ length: 1025 }, diagnostic) })).toBeNull();
  });
});
