import { describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import type { FrontendExtension } from "../domain/frontendExtensions";
import type { GraphConfigurationDocument, GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { createWorkflowConfigurationSdk, type WorkflowNodeConfigurationSubmission } from "./workflowFrontendSdk2";

function fixture() {
  const workflowId = crypto.randomUUID(), nodeId = crypto.randomUUID();
  const document: GraphConfigurationDocument = {
    schema_version: 3, workflow_definition_id: workflowId, revision: 1, edit_sequence: 1, name: "Editable",
    nodes: [{ node_binding_id: nodeId, component_id: "example.node", component_version: "1", title: "Node",
      position: { x: 0, y: 0 }, config: { text: "Before", fixed: true },
      configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }],
    edges: [], object_bindings: [],
  };
  const declaration: FrontendExtension = {
    schema_version: 3, host_protocol_version: 2, extension_id: "example.fields", kind: "field-editor",
    entrypoint: "example.workbench.fields", component_id: "example.node", component_version: "1",
    package_id: "example.package", package_version: "2.0.0",
    binding: { surface: "workbench", slot: "node-fields", target: { component_id: "example.node", component_version: "1" } },
  };
  const input: WorkflowNodeConfigurationSubmission = {
    workflowId, nodeId, componentId: "example.node", componentVersion: "1", extensionId: "example.fields",
    expectedEditSequence: 1, expectedConfig: graphClone(document.nodes[0]!.config),
    nextConfig: { text: "After", fixed: true }, nextPolicy: { schema_version: 1, prompt_mode: "fixed" },
  };
  const result = (request: GraphConfigurationUpdateRequest, target = workflowId): GraphConfigurationResult => ({
    schema_version: 1, kind: "workflow.configuration-result", outcome: target === workflowId ? "saved" : "derived",
    source_workflow_definition_id: workflowId, workflow_definition_id: target, definition_revision: target === workflowId ? 2 : 1,
    edit_sequence: target === workflowId ? 2 : 1, source_has_effective_session: target !== workflowId,
    changed_categories: ["fixed"], node_id_map: { [nodeId]: nodeId }, session_id: null,
    document: { ...graphClone(request.document), workflow_definition_id: target, revision: target === workflowId ? 2 : 1,
      edit_sequence: target === workflowId ? 2 : 1 },
  });
  let observed: GraphConfigurationDocument | null = document;
  const submit = vi.fn(async (request: GraphConfigurationUpdateRequest) => result(request)), accept = vi.fn();
  const sdk = createWorkflowConfigurationSdk({ document: () => observed, locked: () => false,
    extensions: () => [{ declaration, configurationFields: ["text"] }], submit, accept });
  return { workflowId, nodeId, document, declaration, input, result, submit, accept, sdk,
    select: (value: GraphConfigurationDocument | null) => { observed = value; } };
}
describe("uninstalled frontend host 2 configuration SDK", () => {
  it("awaits the formal save and returns its authoritative full result", async () => {
    const f = fixture();
    const value = await f.sdk.submitNodeConfiguration(f.input);
    expect(value.edit_sequence).toBe(2);
    expect(f.submit).toHaveBeenCalledTimes(1);
    const request = f.submit.mock.calls[0]![0];
    expect(request.expected_edit_sequence).toBe(1);
    expect(request.document.nodes[0]!.config.text).toBe("After");
    expect(f.document.nodes[0]!.config.text).toBe("Before");
    expect(f.accept).toHaveBeenCalledWith(value, request);
  });
  it("accepts a derived target by the captured source even after selection changes", async () => {
    const f = fixture(), target = crypto.randomUUID();
    f.submit.mockImplementation(async request => {
      f.select({ ...f.document, workflow_definition_id: crypto.randomUUID() });
      return f.result(request, target);
    });
    const value = await f.sdk.submitNodeConfiguration(f.input);
    expect(value.workflow_definition_id).toBe(target);
    expect(f.accept.mock.calls[0]![1].workflow_definition_id).toBe(f.workflowId);
    expect(value.session_id).toBeNull();
  });
  it.each(["stale", "other-node", "old-host", "unowned-field", "policy-in-fields"] as const)(
    "rejects %s before submitting", async reason => {
      const f = fixture();
      if (reason === "stale") f.input.expectedEditSequence = 2;
      if (reason === "other-node") f.input.nodeId = crypto.randomUUID();
      if (reason === "old-host") { f.declaration.schema_version = 2; f.declaration.host_protocol_version = 1; }
      if (reason === "unowned-field") f.input.nextConfig.fixed = false;
      if (reason === "policy-in-fields") f.input.nextPolicy.prompt_mode = "adjustable";
      await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "rejected" });
      expect(f.submit).not.toHaveBeenCalled(); expect(f.accept).not.toHaveBeenCalled();
    });
  it("limits a registered node action to the same business configuration", async () => {
    const f = fixture();
    f.declaration.kind = "node-action"; f.declaration.binding.slot = "node-actions";
    f.input.nextPolicy.prompt_mode = "adjustable";
    await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "rejected" });
    expect(f.submit).not.toHaveBeenCalled();
  });
  it("does not infer save success or mutate the editor after an unknown outcome", async () => {
    const f = fixture();
    f.submit.mockRejectedValue(new WorkbenchApiError("unknown", "No receipt"));
    await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "unknown" });
    expect(f.accept).not.toHaveBeenCalled();
    expect(f.document.nodes[0]!.config.text).toBe("Before");
  });
  it("serializes configuration submissions for the same workflow", async () => {
    const f = fixture();
    let complete!: (value: GraphConfigurationResult) => void;
    f.submit.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    const first = f.sdk.submitNodeConfiguration(f.input);
    await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({ kind: "rejected" });
    complete(f.result(f.submit.mock.calls[0]![0]));
    await first;
    expect(f.submit).toHaveBeenCalledTimes(1);
  });
  it.each([NaN, Infinity, undefined])("rejects non-JSON config %s without normalizing it into a write", async value => {
    const f = fixture();
    f.input.nextConfig.text = value;
    await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({
      kind: "rejected", code: "graph_invalid_document",
    });
    expect(f.submit).not.toHaveBeenCalled();
  });
  it("rejects a custom serialization hook without executing it", async () => {
    const f = fixture(), toJSON = vi.fn(() => ({ text: "Converted", fixed: true }));
    f.input.nextConfig.toJSON = toJSON;
    await expect(f.sdk.submitNodeConfiguration(f.input)).rejects.toMatchObject({
      kind: "rejected", code: "graph_invalid_document",
    });
    expect(toJSON).not.toHaveBeenCalled(); expect(f.submit).not.toHaveBeenCalled();
  });
});
