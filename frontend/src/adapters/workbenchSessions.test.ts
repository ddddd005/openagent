import { afterEach, describe, expect, it, vi } from "vitest";
import {
  decodeSessionList,
  decodeSessionSelection,
  decodeWorkbenchSession,
  isExactPromptSelection,
  listWorkbenchSessions,
  validCompiledPromptRecord,
} from "./workbenchSessions";
import { AGENT_BINDINGS } from "./workbenchApi";
import { compilePromptItems } from "../domain/preparation";
import { createPreparationDraft } from "../fixtures/preparation";
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
function session() {
  return {
    workflow_session_id: id(1), revision: 1, ref_revision: 1, head_commit_id: id(9),
    mode: "offline", can_submit: true, available_actions: [], can_select_candidates: true,
    nodes: [AGENT_BINDINGS.A, AGENT_BINDINGS.B, "7be319b8-30bd-4674-b7bf-d1cf54a1a10a"]
      .map(node_binding_id => ({ node_binding_id, label: "node", status: "idle", run_id: null, revision: null })),
    chains: [{ chain_run_id: id(2), status: "succeeded" }],
    messages: [{
      visible_message_id: id(3), role: "assistant", payload: { text: "selected" }, sequence: 2, chain_run_id: id(2),
      reply_candidates: [
        { candidate_id: id(4), chain_run_id: id(2), payload: { text: "selected" }, selected: true },
        { candidate_id: id(5), chain_run_id: id(6), payload: { text: "alternative" }, selected: false },
      ],
    }],
  };
}
afterEach(() => vi.unstubAllGlobals());
describe("workbench session and candidate projections", () => {
  it("validates session list and active-selection authority without creating sessions", async () => {
    const rows = [{ workflow_session_id: id(1), created_at: "2026-10-01T11:20:00.000Z", mode: "offline" }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(rows))));
    expect(await listWorkbenchSessions()).toEqual(rows);
    expect(decodeSessionSelection({ active_workflow_session_id: null, revision: 0 })).toEqual({
      active_workflow_session_id: null, revision: 0,
    });
    expect(() => decodeSessionList([...rows, ...rows])).toThrow("身份");
    expect(() => decodeSessionSelection({ active_workflow_session_id: id(1), revision: -1 })).toThrow();
    expect(() => decodeSessionList([{ ...rows[0], mode: "unknown" }])).toThrow();
  });
  it("retains optional candidate extensions while preserving strict core projection validation", () => {
    const value = session();
    expect(decodeWorkbenchSession(value, id(1)).messages[0].reply_candidates).toHaveLength(2);
    value.nodes[0].run_id = "private" as never;
    expect(() => decodeWorkbenchSession(value, id(1))).toThrow("公开投影");
  });
  it.each(["duplicate", "missing-selected", "wrong-payload", "wrong-chain", "unknown-fields", "bad-flag"])(
    "rejects %s candidate projection", kind => {
      const value = session();
      const rows = value.messages[0].reply_candidates;
      if (kind === "duplicate") rows[1].candidate_id = rows[0].candidate_id;
      if (kind === "missing-selected") rows[0].selected = false;
      if (kind === "wrong-payload") rows[0].payload.text = "changed";
      if (kind === "wrong-chain") rows[0].chain_run_id = id(99);
      if (kind === "unknown-fields") Object.assign(rows[0], { run_id: id(99) });
      if (kind === "bad-flag") value.can_select_candidates = "yes" as never;
      expect(() => decodeWorkbenchSession(value, id(1))).toThrow();
    },
  );
  it("validates exact prompt selection and compiler-owned records for restored pending publications", () => {
    const compiled = compilePromptItems(createPreparationDraft("main", "A"));
    expect(validCompiledPromptRecord(compiled.items[0], "item")).toBe(true);
    expect(validCompiledPromptRecord(compiled.groups[0], "group")).toBe(true);
    expect(validCompiledPromptRecord(compiled.config, "config")).toBe(true);
    expect(validCompiledPromptRecord({ ...compiled.items[0], secret: "private" }, "item")).toBe(false);
    expect(validCompiledPromptRecord(compiled.items[0], "config")).toBe(false);
    expect(isExactPromptSelection({
      schema_version: 1, kind: "workflow_prompt_selection",
      nodes: { A: { config_id: id(1), revision: 1 }, B: { config_id: id(2), revision: 2 } },
    })).toBe(true);
  });
});
