import { describe, expect, it } from "vitest";
import { availableControlLabels, closeRunConsequences } from "./runClosePresentation";

describe("explicit close control presentation", () => {
  it("names available actions and states that closing does not clear every run lock", () => {
    expect(availableControlLabels(["pause", "close", "custom_action"])).toEqual([
      "暂停（pause）", "关闭本次运行（close）", "custom_action（custom_action）",
    ]);
    expect(availableControlLabels(undefined)).toEqual([]);
    expect(closeRunConsequences.join(" ")).toContain("不等于清空所有运行占用");
    expect(closeRunConsequences.join(" ")).toContain("仍可能被拒绝或要求派生新工作流");
    expect(closeRunConsequences.join(" ")).toContain("不会自动关闭");
  });
});
