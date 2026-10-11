import { describe, expect, it } from "vitest";
import { isCurrentProvider, isModelSourceConfiguration, isCurrentModelProfile, modelProfileDiagnostic, modelProfileIdentity,
  isModelCapacity, isModelParameters, newModelProfile, newProvider, providerIdentity } from "./workflowModelResources";
import { geminiThinkingCapability, modelParameterDiagnostic } from "./workflowModelResources";
describe("current model resource contracts", () => {
  it("keeps model parameters globally and permits only profile identity on workflow sources", () => {
    const record = newProvider();
    expect(record.value.credential_ref).toBeNull();
    expect(isCurrentProvider(record)).toBe(true);
    expect(isCurrentProvider({ ...record, value: { ...record.value, api_key: "forbidden" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: "env:ARBITRARY" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, base_url: "http://remote.example" } })).toBe(false);
    const profile = newModelProfile(record);
    expect(isModelSourceConfiguration({ reference: modelProfileIdentity(profile) })).toBe(true);
    expect(isModelSourceConfiguration({ reference: modelProfileIdentity(profile), parameters: { model: "chosen-model" } })).toBe(false);
    expect(isModelSourceConfiguration({ reference: providerIdentity(record) })).toBe(false);
    expect(isModelSourceConfiguration({ reference: providerIdentity(record), parameters: {}, credential_ref: record.value.credential_ref })).toBe(false);
  });
  it.each([{ model: "" }, { max_tokens: 8193 }, { max_tokens: 1.5 }, { temperature: NaN },
    { temperature: 3 }, { stream: true }, { thinking: "enabled" }, { api_key: "forbidden" }])("rejects invalid source parameters %j", patch => {
    expect(isModelParameters({ model: "chosen-model", thinking: "disabled", stream: false, ...patch })).toBe(false);
  });
  it("permits unconfigured placeholders only as drafts and requires explicit output budgets", () => {
    const capacity = { context_window_tokens: 0, output_reserve_tokens: 1024,
      summary_max_tokens: 128, max_cold_input_tokens: 0 };
    expect(isModelCapacity(capacity)).toBe(false);
    expect(isModelCapacity(capacity, true)).toBe(true);
    const profile = newModelProfile(newProvider());
    profile.value.name = "Named config";
    profile.value.parameters.model = "test";
    expect(isCurrentModelProfile(profile)).toBe(false);
    profile.value.capacity = { ...capacity, context_window_tokens: 128000, max_cold_input_tokens: 128000 };
    expect(isCurrentModelProfile(profile)).toBe(true);
    expect(isCurrentModelProfile({ ...profile, value: { ...profile.value, api_key: "forbidden" } })).toBe(false);
    expect(modelProfileDiagnostic({ ...profile.value, parameters: { ...profile.value.parameters, max_tokens: 2048 } }))
      .toContain("输出预留");
    expect(isModelCapacity({ ...capacity, context_window_tokens: 128000, max_cold_input_tokens: 128000 })).toBe(true);
    expect(isModelCapacity({ ...capacity, context_window_tokens: 1024, max_cold_input_tokens: 1 })).toBe(false);
    expect(isModelCapacity({ ...capacity, summary_max_tokens: 1025 }, true)).toBe(false);
  });
  it.each(["chat", "gemini"] as const)("accepts only saved local Key references or null for %s", protocol => {
    const record = newProvider();
    record.value = { name: "Provider", protocol, enabled: true,
      base_url: "https://example.test", credential_ref: `local:${crypto.randomUUID()}` };
    expect(isCurrentProvider(record)).toBe(true);
    expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: null } })).toBe(true);
    expect(isCurrentProvider({ ...record, data_schema_version: 2 })).toBe(false);
    for (const reference of ["env:DEEPSEEK_API_KEY", "env:GEMINI_API_KEY", "local:not-a-uuid"])
      expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: reference } })).toBe(false);
    expect(isCurrentProvider({ ...newProvider(), data_schema_version: 2 })).toBe(false);
  });
  it("validates explicit budgets and levels without guessing unknown model capabilities", () => {
    const parameters = { model: "gemini-3.1-pro-preview", stream: false as const,
      thinking: { mode: "level" as const, level: "low" as const, include_summary: true } };
    expect(isModelParameters(parameters)).toBe(true);
    expect(modelParameterDiagnostic(parameters, "gemini")).toBeNull();
    expect(modelParameterDiagnostic(parameters, "chat")).not.toBeNull();
    expect(modelParameterDiagnostic({ ...parameters, thinking: "disabled" }, "gemini")).not.toBeNull();
    expect(modelParameterDiagnostic({ ...parameters, model: "gemini-unknown" }, "gemini")).not.toBeNull();
    expect(modelParameterDiagnostic({ ...parameters, thinking: { ...parameters.thinking, level: "minimal" } }, "gemini")).not.toBeNull();
    const budget = { model: "gemini-2.5-flash-lite", stream: false as const,
      thinking: { mode: "budget" as const, budget: 512, include_summary: false } };
    expect(modelParameterDiagnostic(budget, "gemini")).toBeNull();
    for (const value of [-1, 0, 24576]) expect(modelParameterDiagnostic({
      ...budget, thinking: { ...budget.thinking, budget: value } }, "gemini")).toBeNull();
    for (const value of [1, 511, 24577]) expect(modelParameterDiagnostic({
      ...budget, thinking: { ...budget.thinking, budget: value } }, "gemini")).not.toBeNull();
    expect(isModelParameters({ ...budget, thinking: { ...budget.thinking, budget: -2 } })).toBe(false);
    expect(isModelParameters({ ...budget, thinking: { ...budget.thinking, level: "high" } })).toBe(false);
    expect(geminiThinkingCapability("toString")).toBeNull();
  });
});
