import { describe, expect, it } from "vitest";
import { isCurrentProvider, isModelSourceConfiguration, isModelParameters, newProvider, providerIdentity } from "./workflowModelResources";
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
});
