import { afterEach, describe, expect, it, vi } from "vitest";
import { compilePromptItems } from "../domain/preparation";
import { createPreparationDraft } from "../fixtures/preparation";
import { contextScope } from "./workbenchContext.fixtures";
import {
  decodeWorkbenchVariables, decodeWorkbenchVariableWrite, readWorkbenchVariables, validVariableValue,
} from "./workbenchVariables";

const SID = contextScope().sessionId;
const projection = () => ({
  schema_version: 1, kind: "workflow_variable_read", workflow_session_id: SID, revision: 0,
  values: [
    { name: "倒计时", type: "integer", assigned: true, source: "default", value: 10 },
    { name: "b", type: "string", assigned: false, source: "unassigned" },
    { name: "空内容", type: "string", assigned: true, source: "assignment", value: "" },
  ],
});
afterEach(() => vi.unstubAllGlobals());
describe("session variable projection", () => {
  it("keeps Chinese names, unassigned values and empty text distinct", () => {
    const value = decodeWorkbenchVariables(projection(), SID);
    expect(value.values[0].value).toBe(10);
    expect(value.values[1]).not.toHaveProperty("value");
    expect(value.values[2].value).toBe("");
    expect(validVariableValue("10", "integer")).toBe(false);
    expect(validVariableValue(1.5, "integer")).toBe(false);
    expect(validVariableValue(Infinity, "number")).toBe(false);
  });
  it("rejects wrong-session, duplicate, untyped and private fields", () => {
    expect(() => decodeWorkbenchVariables(projection(), crypto.randomUUID())).toThrow("归属");
    const duplicate = projection();
    duplicate.values.push(duplicate.values[0]);
    expect(() => decodeWorkbenchVariables(duplicate, SID)).toThrow();
    const untyped = projection();
    (untyped.values[0] as Record<string, unknown>).value = "10";
    expect(() => decodeWorkbenchVariables(untyped, SID)).toThrow();
    expect(() => decodeWorkbenchVariables({ ...projection(), credential: "private" }, SID)).toThrow();
  });
  it("reads an inline definition using revision fences with no variable writes", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(projection())));
    vi.stubGlobal("fetch", fetcher);
    const plan = compilePromptItems(createPreparationDraft("main", "A"));
    await readWorkbenchVariables(contextScope(), { items: plan.items, groups: plan.groups, config: plan.config });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe(`/api/sessions/${SID}/variables/read`);
    const body = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(body).toMatchObject({
      expected_session_revision: contextScope().sessionRevision,
      expected_ref_revision: contextScope().refRevision,
      expected_head_commit_id: contextScope().headCommitId,
      prompt_config: { config: plan.config },
    });
    expect(body).not.toHaveProperty("idempotency_key");
    expect(body).not.toHaveProperty("values");
  });
  it("requires a write receipt for the exact original request, while allowing revision gaps", () => {
    const request = {
      idempotency_key: crypto.randomUUID(), name: "倒计时", expected_variable_revision: 0, value: 9,
    };
    const value = {
      ...projection(), revision: 3,
      values: [{ name: "倒计时", type: "integer", assigned: true, source: "assignment", value: 9 }],
      write_receipt: {
        idempotency_key: request.idempotency_key, name: request.name, expected_variable_revision: 0,
      },
    };
    expect(decodeWorkbenchVariableWrite(value, SID, request).revision).toBe(3);
    expect(() => decodeWorkbenchVariables(value, SID)).toThrow();
    expect(() => decodeWorkbenchVariableWrite({
      ...value, write_receipt: { ...value.write_receipt, idempotency_key: crypto.randomUUID() },
    }, SID, request)).toThrow("原请求");
    expect(() => decodeWorkbenchVariableWrite({
      ...value, write_receipt: { ...value.write_receipt, expected_variable_revision: 1 },
    }, SID, request)).toThrow("原请求");
  });
});
