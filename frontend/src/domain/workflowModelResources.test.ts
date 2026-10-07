import { describe, expect, it } from "vitest";
import { isCurrentProvider, isModelSourceConfiguration, isCapacityModelSourceConfiguration,
  isModelCapacity, isModelParameters, newProvider, providerIdentity } from "./workflowModelResources";
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
});
