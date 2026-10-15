import { describe, expect, it } from "vitest";
import { variableRegistrationChange, variableRegistrationIssues } from "./workflowVariableRegistration";

const config = { name: "value", value_type: "string", has_initial: false, initial_value: null };

describe("variable registration configuration", () => {
  it.each(["value", "_value2", "计数器2", "变量_甲"])("accepts the registered identifier %s", name => {
    expect(variableRegistrationIssues({ ...config, name })).toEqual({});
  });
  it.each(["", "1", "2value", "a b", "a-b", "{{value}}", "a".repeat(129)])(
    "reports an invalid identifier before registration: %s", name => {
      expect(variableRegistrationIssues({ ...config, name }).name).toContain("不能以数字开头");
    });
  it.each([
    ["string", ""], ["string", "123"], ["integer", 0], ["number", 0.5], ["boolean", false],
  ])("accepts an explicit %s initial value without changing its type", (value_type, initial_value) => {
    expect(variableRegistrationIssues({ ...config, value_type, initial_value, has_initial: true })).toEqual({});
  });
  it.each([
    ["string", null], ["string", 1], ["integer", true], ["integer", 1.5],
    ["integer", Number.MAX_SAFE_INTEGER + 1], ["number", "1"], ["number", Infinity], ["boolean", 0],
  ])("reports a mismatching %s initial value", (value_type, initial_value) => {
    expect(variableRegistrationIssues({ ...config, value_type, initial_value, has_initial: true }).initial_value)
      .toBeTruthy();
  });
  it.each([["string", ""], ["integer", 0], ["number", 0], ["boolean", false]])(
    "initializes a usable %s value only when the user enables the initial value", (value_type, initial_value) => {
      const before = { ...config, value_type }, next = { ...before, has_initial: true };
      expect(variableRegistrationChange(before, next)).toEqual({ ...next, initial_value });
      expect(before.initial_value).toBeNull();
      expect(next.initial_value).toBeNull();
    });
  it("preserves valid zero, false and empty string values and does not silently repair saved invalid values", () => {
    for (const [value_type, initial_value] of [["string", ""], ["integer", 0], ["boolean", false]]) {
      const next = { ...config, value_type, has_initial: true, initial_value };
      expect(variableRegistrationChange(config, next)).toBe(next);
    }
    const invalid = { ...config, has_initial: true };
    expect(variableRegistrationChange(invalid, { ...invalid, name: "renamed" }).initial_value).toBeNull();
  });
  it("changes the initial value control type without coercing a value from the previous type", () => {
    const previous = { ...config, has_initial: true, initial_value: "text" };
    expect(variableRegistrationChange(previous, { ...previous, value_type: "integer" }).initial_value).toBe(0);
    expect(previous.initial_value).toBe("text");
  });
});
