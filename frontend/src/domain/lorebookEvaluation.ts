import { graphObject, graphUuid } from "./workflowGraph";
import { lorebookLimits } from "./lorebook";

export type LorebookEntryStatus = "disabled" | "probability_rejected" | "keyword_miss" | "activated";
export interface LorebookEntryEvaluation {
  entry_id: string;
  name: string;
  status: LorebookEntryStatus;
  activation_round: number | null;
  probability_passed: boolean | null;
  primary_matched: boolean | null;
  secondary_matched: boolean | null;
}
export interface LorebookEvaluation {
  kind: "lorebook_evaluation";
  recursive_rounds: number;
  entries: LorebookEntryEvaluation[];
}
export const lorebookEvaluationLabels: Record<LorebookEntryStatus, string> = {
  disabled: "已禁用", probability_rejected: "概率未通过", keyword_miss: "关键词未命中", activated: "已触发",
};
const round = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= 3;
const nullableBoolean = (value: unknown) => value === null || typeof value === "boolean";
export function lorebookEvaluation(value: unknown): LorebookEvaluation | null {
  if (!graphObject(value) || value.kind !== "lorebook_evaluation" || !round(value.recursive_rounds)
    || !Array.isArray(value.entries) || value.entries.length > lorebookLimits.entries
    || !value.entries.every(entry => graphObject(entry) && graphUuid(entry.entry_id) && typeof entry.name === "string"
      && Object.hasOwn(lorebookEvaluationLabels, String(entry.status))
      && (entry.activation_round === null || round(entry.activation_round))
      && (entry.status === "activated" ? entry.activation_round !== null : entry.activation_round === null)
      && [entry.probability_passed, entry.primary_matched, entry.secondary_matched].every(nullableBoolean))
    || new Set(value.entries.map(entry => entry.entry_id)).size !== value.entries.length) return null;
  return value as unknown as LorebookEvaluation;
}
