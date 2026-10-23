import { describe, expect, it } from "vitest";
import { isCurrentProvider, isModelSourceConfiguration, isCurrentModelProfile, modelProfileDiagnostic,
  modelProfileIdentity, isModelCapacity, isModelRuntime, isModelThinking, newModelProfile, newProvider,
  providerIdentity, modelParameterDiagnostic } from "./workflowModelResources";
import type { CurrentModelProfile } from "./workflowModelResources";

function profile(): CurrentModelProfile {
  const value = newModelProfile(newProvider());
  value.value.name = "Named config";
  value.value.model.id = "chosen-model";
  value.value.model.capacity = { context_window_tokens: 128_000, input_limit_tokens: 120_000, max_output_tokens: 8192 };
  value.value.runtime.context_length_tokens = 64_000;
  value.value.runtime.output_limit_tokens = 4096;
  return value;
}

describe("current model resource contracts", () => {
  it("keeps the saved model profile strict and permits only its identity on workflow sources", () => {
    const record = newProvider();
    expect(record.value.credential_ref).toBeNull();
    expect(isCurrentProvider(record)).toBe(true);
    expect(isCurrentProvider({ ...record, value: { ...record.value, api_key: "forbidden" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, credential_ref: "env:ARBITRARY" } })).toBe(false);
    expect(isCurrentProvider({ ...record, value: { ...record.value, base_url: "http://remote.example" } })).toBe(false);
    const saved = profile();
    expect(saved.data_schema_version).toBe(2);
    expect(isCurrentModelProfile(saved)).toBe(true);
    expect(isModelSourceConfiguration({ reference: modelProfileIdentity(saved) })).toBe(true);
    expect(isModelSourceConfiguration({ reference: modelProfileIdentity(saved), runtime: saved.value.runtime })).toBe(false);
    expect(isModelSourceConfiguration({ reference: providerIdentity(record) })).toBe(false);
    expect(isModelSourceConfiguration({ reference: providerIdentity(record), parameters: {}, credential_ref: null })).toBe(false);
    expect(isCurrentModelProfile({ ...saved, data_schema_version: 1 })).toBe(false);
    expect(isCurrentModelProfile({ ...saved, value: { ...saved.value, protocol: "chat" } })).toBe(false);
    expect(isCurrentModelProfile({ ...saved, value: { ...saved.value, temperature: 0.5 } })).toBe(false);
    expect(isCurrentModelProfile({ ...saved, value: { ...saved.value,
      runtime: { ...saved.value.runtime, summary_output_limit_tokens: 1024 } } })).toBe(false);
  });

  it("uses only the four runtime values and rejects malformed thinking or capacity fields", () => {
    const saved = profile();
    expect(saved.value.runtime).toEqual({ thinking: "disabled", context_length_tokens: 64_000,
      compaction_threshold_bps: 8500, output_limit_tokens: 4096 });
    expect(newModelProfile(newProvider()).value.runtime).toEqual({
      thinking: "disabled", context_length_tokens: 300_000,
      compaction_threshold_bps: 8500, output_limit_tokens: 32_000,
    });
    expect(isModelRuntime({ ...saved.value.runtime, temperature: 0.5 })).toBe(false);
    expect(isModelRuntime({ ...saved.value.runtime, summary_output_limit_tokens: 1024 })).toBe(false);
    expect(isModelCapacity({ context_window_tokens: 0, input_limit_tokens: 0, max_output_tokens: 0 })).toBe(false);
    expect(isModelCapacity({ context_window_tokens: 0, input_limit_tokens: 0, max_output_tokens: 0 }, true)).toBe(true);
    expect(isModelCapacity({ context_window_tokens: 128_000, input_limit_tokens: 120_000, max_output_tokens: 8192 })).toBe(true);
    expect(isModelCapacity({ context_window_tokens: 128_000, input_limit_tokens: 120_000,
      max_output_tokens: 8192, max_cold_input_tokens: 120_000 })).toBe(false);
    expect(isModelThinking({ mode: "budget", budget: 2048, include_summary: true })).toBe(true);
    expect(isModelThinking({ mode: "budget", budget: 2048, include_summary: false })).toBe(false);
    expect(isModelThinking({ mode: "level", level: "high", include_summary: true, extra: true })).toBe(false);
    expect(modelProfileDiagnostic({ ...saved.value,
      runtime: { ...saved.value.runtime, context_length_tokens: 128_001 } })).toContain("真实模型窗口");
    expect(modelProfileDiagnostic({ ...saved.value,
      runtime: { ...saved.value.runtime, output_limit_tokens: 8193 } })).toContain("真实模型容量");
    expect(isCurrentModelProfile({ ...saved, value: { ...saved.value,
      runtime: { ...saved.value.runtime, compaction_threshold_bps: 10_001 } } })).toBe(false);
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

  it("validates thinking mode against the provider route without adding a profile field", () => {
    const thinking = { mode: "level" as const, level: "medium" as const, include_summary: true as const };
    expect(modelParameterDiagnostic("gemini-3-flash-preview", thinking, "gemini")).toBeNull();
    expect(modelParameterDiagnostic("gemini-3-flash-preview", thinking, "chat")).not.toBeNull();
    expect(modelParameterDiagnostic("gemini-3.8-flash", "disabled", "gemini")).toBeNull();
    expect(modelParameterDiagnostic("proxy-custom-flash", thinking, "gemini")).toBeNull();
    const budget = { mode: "budget" as const, budget: 512, include_summary: true as const };
    expect(modelParameterDiagnostic("gemini-2.5-flash-lite", budget, "gemini")).toBeNull();
    expect(modelParameterDiagnostic("gemini-2.5-flash-lite",
      { ...budget, budget: 65_536 }, "gemini")).toBeNull();
    expect(modelParameterDiagnostic("gemini-2.5-pro", "disabled", "gemini")).toBeNull();
    expect(modelParameterDiagnostic("gemini-3.1-pro-preview",
      { ...thinking, level: "minimal" }, "gemini")).toBeNull();
    expect(modelParameterDiagnostic("gemini-3.8-flash", { ...budget, budget: -2 }, "gemini")).not.toBeNull();
    expect(modelParameterDiagnostic(" gemini-3.8-flash", "disabled", "gemini")).not.toBeNull();
  });
});
