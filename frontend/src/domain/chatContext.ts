import { graphObject, graphUuid, type GraphSessionObject } from "./workflowGraph";

export interface ChatMessage { message_id: string; role: "user" | "assistant" | "system"; text: string }
export interface ChatCandidate { candidate_id: string; messages: ChatMessage[]; result_checkpoint_id: string | null }
export interface ChatRound {
  round_id: string;
  base_checkpoint_id: string | null;
  submitted_input: string | null;
  candidates: ChatCandidate[];
  selected_candidate_id: string | null;
}
export interface ChatContext { rounds: ChatRound[] }
const exactUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
const fields = (value: unknown, names: string[]): value is Record<string, unknown> =>
  graphObject(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
const checkpoint = (value: unknown) => value === null || exactUuid(value);
export function isChatContext(value: unknown): value is ChatContext {
  if (!fields(value, ["rounds"]) || !Array.isArray(value.rounds)) return false;
  const rounds = new Set<string>(), candidates = new Set<string>(), messages = new Set<string>();
  for (const round of value.rounds) {
    if (!fields(round, ["round_id", "base_checkpoint_id", "submitted_input", "candidates", "selected_candidate_id"])
      || !exactUuid(round.round_id) || rounds.has(round.round_id)
      || !checkpoint(round.base_checkpoint_id) || round.submitted_input !== null && typeof round.submitted_input !== "string"
      || !Array.isArray(round.candidates)) return false;
    rounds.add(round.round_id);
    const ownCandidates = new Set<string>();
    for (const candidate of round.candidates) {
      if (!fields(candidate, ["candidate_id", "messages", "result_checkpoint_id"])
        || !exactUuid(candidate.candidate_id) || candidates.has(candidate.candidate_id)
        || !checkpoint(candidate.result_checkpoint_id) || !Array.isArray(candidate.messages)
        || !candidate.messages.length) return false;
      candidates.add(candidate.candidate_id); ownCandidates.add(candidate.candidate_id);
      for (const message of candidate.messages) {
        if (!fields(message, ["message_id", "role", "text"]) || !exactUuid(message.message_id)
          || messages.has(message.message_id) || !["user", "assistant", "system"].includes(String(message.role))
          || typeof message.text !== "string") return false;
        messages.add(message.message_id);
      }
    }
    if (round.selected_candidate_id !== null
      && (typeof round.selected_candidate_id !== "string" || !ownCandidates.has(round.selected_candidate_id))) return false;
  }
  return true;
}
export function chatContextValue(object: GraphSessionObject): ChatContext | null {
  return object.type_id === "workflow.chat-context" && object.schema_version === 1
    && !object.deleted && isChatContext(object.value) ? object.value : null;
}
