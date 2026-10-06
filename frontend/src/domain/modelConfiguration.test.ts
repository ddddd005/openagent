import { describe, expect, it } from "vitest";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { createWorkspaceDrafts, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import {
  dependencyTarget, isCanvasModelConnection, isConfigurationMutation,
  isChatProvider, isModelConfiguration, isModelConfigurationSnapshot, modelParameterDiagnostics,
  modelPublication, providerFieldDiagnostics, CHAT_CAPABILITIES, type ChatProvider, type ModelDraft,
} from "./modelConfiguration";

const NODE = "00000000-0000-4000-8000-000000000101";
const EDGE = "00000000-0000-4000-8000-000000000102";
function draft(): ModelDraft {
  return {
    workflowId: MAIN_WORKFLOW_ID, configId: "00000000-0000-4000-8000-000000000103",
    backendRevision: 0,
    nodes: [{ id: NODE, position: { x: 10, y: -10 }, provider_ref: null, parameters: { model: "deepseek-flash", max_tokens: 2048 } }],
    edges: [{ id: EDGE, source: NODE, target_binding_id: AGENT_BINDINGS.A }],
  };
}
function provider(): ChatProvider {
  return {
    schema_version: 1, kind: "chat_provider", provider_id: NODE, revision: 1,
    name: "DeepSeek", protocol: "chat", base_url: "https://api.deepseek.com",
    credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
  };
}
describe("model graph contracts", () => {
  it("preserves primary and model edges when Vue Flow revalidates restored edges", () => {
    const primary = createWorkspaceDrafts()[MAIN_WORKFLOW_ID].edges;
    const model = draft();
    expect(primary.every((edge) => isCanvasModelConnection(model, primary, edge))).toBe(true);
    expect(isCanvasModelConnection(model, primary, {
      id: EDGE, source: NODE, target: dependencyTarget("A"), sourceHandle: "model-out", targetHandle: "model-in",
    })).toBe(true);
    expect(isCanvasModelConnection(model, primary, { ...primary[0], target: "foreign" })).toBe(false);
  });
  it("refuses duplicate new connections and interactive rewiring of the business flow", () => {
    const model = draft();
    const connection = { source: NODE, target: dependencyTarget("A"), sourceHandle: "model-out", targetHandle: "model-in" };
    expect(isCanvasModelConnection(model, [], connection)).toBe(false);
    expect(isCanvasModelConnection(model, [], { ...connection, target: dependencyTarget("B") })).toBe(true);
    expect(isCanvasModelConnection(model, [], { ...connection, targetHandle: "in" })).toBe(false);
    const business = createWorkspaceDrafts()[MAIN_WORKFLOW_ID].edges[0];
    expect(isCanvasModelConnection(model, [business], { ...business, id: undefined })).toBe(false);
  });
  it("keeps editable invalid parameter values while refusing publishing, private fields or ambiguous dependencies", () => {
    const value = draft();
    value.nodes[0].parameters.temperature = -1;
    expect(isModelConfigurationSnapshot({ schemaVersion: 1, drafts: [value], pending: null })).toBe(true);
    expect(isModelConfiguration(modelPublication(value))).toBe(false);
    delete value.nodes[0].parameters.temperature;
    expect(isModelConfiguration(modelPublication(value))).toBe(true);
    expect(isModelConfiguration({ ...modelPublication(value), api_key: "PRIVATE" })).toBe(false);
    value.edges.push({ ...value.edges[0], id: crypto.randomUUID() });
    expect(isModelConfiguration(modelPublication(value))).toBe(false);
  });
  it("validates pending request revision and identity for exact replay", () => {
    const record = modelPublication(draft());
    const pending = {
      path: "/api/model-configurations/model",
      body: { record, expected_revision: 0, idempotency_key: crypto.randomUUID() },
    };
    expect(isConfigurationMutation(pending)).toBe(true);
    expect(isConfigurationMutation({ ...pending, body: { ...pending.body, expected_revision: 1 } })).toBe(false);
    expect(isConfigurationMutation({ ...pending, body: { ...pending.body, idempotency_key: "unrestorable" } })).toBe(false);
  });
  it.each([
    "https://name:PRIVATE@host.test", "https://@host.test", "https://host.test?key=PRIVATE",
    "https://host.test?", "https://host.test#", "https://host.test\\path", "https://host.test:0",
    "https:///host.test", "https:host.test", "http://host.test", "http://127.1",
    "https://host.test/\u007f", "https://host.test/\n",
  ])("rejects ambiguous or unsafe Chat address %s without echoing it", (address) => {
    const value = { ...provider(), base_url: address };
    expect(isChatProvider(value)).toBe(false);
    const errors = providerFieldDiagnostics(value);
    expect(errors.map((row) => row.code)).toEqual(["provider_address_invalid"]);
    expect(JSON.stringify(errors)).not.toContain("PRIVATE");
  });
  it.each([
    "https://api.deepseek.com", "https://api.deepseek.com/v1/",
    "HTTPS://api.deepseek.com", "http://localhost:8000/v1", "http://127.0.0.1:8000",
    "http://[::1]:8000/v1",
  ])("accepts existing HTTPS and literal loopback addresses %s", (address) => {
    const value = { ...provider(), base_url: address };
    expect(isChatProvider(value)).toBe(true);
    expect(providerFieldDiagnostics(value)).toEqual([]);
  });
  it("distinguishes empty names, missing reference and invalid reference without accepting plaintext", () => {
    expect(providerFieldDiagnostics({ ...provider(), name: " " })[0]?.field).toBe("name");
    expect(isChatProvider({ ...provider(), name: "PRIVATE\n" })).toBe(false);
    expect(providerFieldDiagnostics({ ...provider(), credential_ref: null })).toEqual([]);
    const value = { ...provider(), credential_ref: "PRIVATE" } as unknown as ChatProvider;
    expect(providerFieldDiagnostics(value)[0]?.code).toBe("credential_reference_invalid");
    expect(JSON.stringify(providerFieldDiagnostics(value))).not.toContain("PRIVATE");
    expect(isChatProvider(value)).toBe(false);
  });
  it.each([
    ["max_tokens", 0, "model_max_tokens_invalid"],
    ["max_tokens", 8193, "model_max_tokens_invalid"],
    ["max_tokens", 2.5, "model_max_tokens_invalid"],
    ["temperature", -0.1, "model_temperature_invalid"],
    ["temperature", 2.1, "model_temperature_invalid"],
    ["temperature", Number.NaN, "model_temperature_invalid"],
    ["model", "PRIVATE\n", "model_name_invalid"],
  ])("diagnoses actual Chat parameter limits for %s", (key, value, code) => {
    const errors = modelParameterDiagnostics({ model: "deepseek-flash", [key]: value });
    expect(errors.map((row) => row.code)).toEqual([code]);
    expect(JSON.stringify(errors)).not.toContain("PRIVATE");
  });
  it("keeps omitted parameter semantics and old stored requests separately from adapter compatibility", () => {
    expect(modelParameterDiagnostics({ model: "deepseek-flash" })).toEqual([]);
    expect(modelParameterDiagnostics({ model: "deepseek-flash", max_tokens: 8192, temperature: 2 })).toEqual([]);
    const value = draft();
    value.nodes[0].parameters.max_tokens = 9000;
    expect(isModelConfiguration(modelPublication(value))).toBe(true);
    expect(isConfigurationMutation({
      path: "/api/model-configurations/model",
      body: { record: modelPublication(value), expected_revision: 0, idempotency_key: crypto.randomUUID() },
    })).toBe(true);
    expect(modelParameterDiagnostics(value.nodes[0].parameters)[0]?.code).toBe("model_max_tokens_invalid");
    expect(CHAT_CAPABILITIES).toMatchObject({ maxTokens: 8192, streaming: false, thinking: "disabled" });
  });
});
