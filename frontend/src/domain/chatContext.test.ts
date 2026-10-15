import { describe, expect, it } from "vitest";
import { isChatContext } from "./chatContext";
import { graphClone } from "./workflowGraph";

function fixture() {
  return { rounds: [{ round_id: crypto.randomUUID(), base_checkpoint_id: null,
    submitted_input: "original input", candidates: [1, 2].map(index => ({
      candidate_id: crypto.randomUUID(), messages: [{ message_id: crypto.randomUUID(), role: "assistant",
        text: "reply " + index }], result_checkpoint_id: crypto.randomUUID(),
    })), selected_candidate_id: null as string | null }] };
}
describe("logical chat display contract", () => {
  it("accepts complete inline candidates with exact selection without artifact references", () => {
    const value = fixture(); value.rounds[0]!.selected_candidate_id = value.rounds[0]!.candidates[0]!.candidate_id;
    expect(isChatContext(value)).toBe(true); expect(isChatContext({ rounds: [] })).toBe(true);
    const before = graphClone(value); isChatContext(value); expect(value).toEqual(before);
  });
  it("rejects foreign selection, duplicated message identities and old reference content", () => {
    const value = fixture(); value.rounds[0]!.selected_candidate_id = crypto.randomUUID();
    expect(isChatContext(value)).toBe(false);
    value.rounds[0]!.selected_candidate_id = null;
    value.rounds[0]!.candidates[1]!.messages[0]!.message_id = value.rounds[0]!.candidates[0]!.messages[0]!.message_id;
    expect(isChatContext(value)).toBe(false);
    const old = { entries: [], view_ref: null };
    expect(isChatContext(old)).toBe(false);
  });
});
