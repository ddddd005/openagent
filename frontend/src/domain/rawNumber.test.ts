import { describe, expect, it } from "vitest";
import { parseRawNumber } from "./rawNumber";

describe("raw numeric fields", () => {
  it.each(["-", "+", "1e", "1e-", ".", "0x20", " 1 ", "Infinity", "1e999"])("does not coerce incomplete or unsupported input %s", raw => {
    expect(parseRawNumber(raw).valid).toBe(false);
    expect(parseRawNumber(raw).value).toBeNull();
  });
  it.each([["-3", -3], ["1e2", 100], ["1.", 1], [".25", 0.25]] as const)("accepts complete decimal input %s", (raw, value) => {
    expect(parseRawNumber(raw)).toEqual({ valid: true, value, error: "" });
  });
  it("validates required, finite integer and range without changing the raw text", () => {
    expect(parseRawNumber("", { required: false })).toEqual({ valid: true, value: null, error: "" });
    expect(parseRawNumber("").valid).toBe(false);
    expect(parseRawNumber("0.5", { integer: true }).valid).toBe(false);
    expect(parseRawNumber("-1", { min: 0 }).valid).toBe(false);
    expect(parseRawNumber("101", { max: 100 }).valid).toBe(false);
  });
});
