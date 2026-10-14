import { describe, expect, it, vi } from "vitest";
import { createNodeConfigurationAccess } from "./workflowNodeConfiguration";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import type { WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import { modelProfileIdentity, newModelProfile, newProvider } from "../domain/workflowModelResources";

const request = (): WorkflowNodeConfigurationRequest => ({
  workflowId: crypto.randomUUID(), nodeId: crypto.randomUUID(), componentId: "models.source", componentVersion: "5",
  expectedConfig: {}, patch: { reference: modelProfileIdentity(newModelProfile(newProvider())) },
  extensionId: "workflow.models.node-fields-v7", lifecycle: "origin",
});
describe("trusted package configuration access", () => {
  it("rejects asynchronous late edits after workflow/lifecycle changes or package unload", () => {
    const original = request(); let workflowId = original.workflowId, lifecycle = original.lifecycle;
    let extensions = modelFrontendExtensions;
    const patch = vi.fn(async (_request: WorkflowNodeConfigurationRequest) =>
      ({ workflowId: "actual-copy", nodeId: original.nodeId, outcome: "saved" as const }));
    const configure = createNodeConfigurationAccess({ workflowId: () => workflowId, lifecycle: () => lifecycle,
      trustedExtensions: () => extensions, patch });
    return configure(original).then(result => {
      expect(result).toEqual({ workflowId: "actual-copy", nodeId: original.nodeId, outcome: "saved" });
      lifecycle = "new-generation"; return configure(original);
    }).then(result => {
      expect(result).toBeNull();
      lifecycle = original.lifecycle; workflowId = crypto.randomUUID(); return configure(original);
    }).then(result => {
      expect(result).toBeNull();
      workflowId = original.workflowId; extensions = []; return configure(original);
    }).then(result => {
      expect(result).toBeNull();
      expect(patch).toHaveBeenCalledTimes(1); expect(patch.mock.calls[0]![0]).toEqual(original);
    });
  });
  it("rejects mismatched declaration identities/components/versions and panels", () => {
    const original = request(), patch = vi.fn(async () => null);
    const configure = createNodeConfigurationAccess({ workflowId: () => original.workflowId, lifecycle: () => original.lifecycle,
      trustedExtensions: () => modelFrontendExtensions, patch });
    return Promise.all([
      configure({ ...original, extensionId: "workflow.models.workbench-panel" }),
      configure({ ...original, componentVersion: "4" }),
      configure({ ...original, componentId: "agents.execute" }),
      configure({ ...original, extensionId: "arbitrary" }),
    ]).then(results => {
      expect(results).toEqual([null, null, null, null]);
      expect(patch).not.toHaveBeenCalled();
    });
  });
});
