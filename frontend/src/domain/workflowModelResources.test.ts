import { describe, expect, it } from "vitest";
import { isCurrentProvider, isModelSourceConfiguration, isCapacityModelSourceConfiguration,
  isModelCapacity, isModelParameters, newProvider, providerIdentity } from "./workflowModelResources";
import { geminiThinkingCapability, modelParameterDiagnostic } from "./workflowModelResources";
describe("current model resource contracts", () => {
  it("keeps model parameters on the source and rejects secrets or unsupported provider fields", () => {
    const record = newProvider();
    expect(isCurrentProvider(record)).toBe(true);
    expect(isCurrentProvider({ ...record, value: { ...record.value, api_key: "forbidden" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: "env:ARBITRARY" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, base_url: "http://remote.example" } })).toBe(false);
    expect(isModelSourceConfiguration({ reference: providerIdentity(record),
      parameters: { model: "chosen-model", thinking: "disabled", stream: false, max_tokens: 8192, temperature: 0 } })).toBe(true);
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
    const source = { reference: providerIdentity(newProvider()),
      parameters: { model: "test", thinking: "disabled", stream: false, max_tokens: 1024 }, capacity };
    expect(isCapacityModelSourceConfiguration(source)).toBe(true);
    expect(isModelSourceConfiguration(source)).toBe(false);
    expect(isCapacityModelSourceConfiguration({ ...source, parameters: { ...source.parameters, max_tokens: undefined } })).toBe(false);
    expect(isCapacityModelSourceConfiguration({ ...source, parameters: { ...source.parameters, max_tokens: 2048 } })).toBe(false);
    expect(isModelCapacity({ ...capacity, context_window_tokens: 128000, max_cold_input_tokens: 128000 })).toBe(true);
    expect(isModelCapacity({ ...capacity, context_window_tokens: 1024, max_cold_input_tokens: 1 })).toBe(false);
    expect(isModelCapacity({ ...capacity, summary_max_tokens: 1025 }, true)).toBe(false);
  });
  it("supports only protocol-matched controlled Gemini credentials", () => {
    const record = newProvider();
    record.value = { name: "Gemini", protocol: "gemini", enabled: true,
      base_url: "https://generativelanguage.googleapis.com/v1beta", credential_ref: "env:GEMINI_API_KEY" };
    expect(isCurrentProvider(record)).toBe(false);
    record.data_schema_version = 2;
    expect(isCurrentProvider(record)).toBe(true);
    expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: "env:DEEPSEEK_API_KEY" } })).toBe(false);
    expect(isCurrentProvider({ ...newProvider(), value: { ...newProvider().value, credential_ref: "env:GEMINI_API_KEY" } })).toBe(false);
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
