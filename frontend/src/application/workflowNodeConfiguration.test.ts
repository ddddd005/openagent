import { describe, expect, it, vi } from "vitest";
import { createNodeConfigurationAccess } from "./workflowNodeConfiguration";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import type { WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";

const request = (): WorkflowNodeConfigurationRequest => ({
  workflowId: crypto.randomUUID(), nodeId: crypto.randomUUID(), componentId: "models.source", componentVersion: "1",
  expectedConfig: { reference: {}, parameters: {} }, patch: { parameters: { model: "selected" } },
  extensionId: "workflow.models.node-fields", lifecycle: "origin",
});
describe("trusted package configuration access", () => {
  it("rejects asynchronous late edits after workflow/lifecycle changes or package unload", () => {
    const original = request(); let workflowId = original.workflowId, lifecycle = original.lifecycle;
    let extensions = modelFrontendExtensions;
    const patch = vi.fn(() => ({ workflowId: "actual-copy", nodeId: original.nodeId }));
    const configure = createNodeConfigurationAccess({ workflowId: () => workflowId, lifecycle: () => lifecycle,
      trustedExtensions: () => extensions, patch });
    expect(configure(original)).toEqual({ workflowId: "actual-copy", nodeId: original.nodeId });
    lifecycle = "new-generation"; expect(configure(original)).toBeNull();
    lifecycle = original.lifecycle; workflowId = crypto.randomUUID(); expect(configure(original)).toBeNull();
    workflowId = original.workflowId; extensions = []; expect(configure(original)).toBeNull();
    expect(patch).toHaveBeenCalledTimes(1); expect(patch.mock.calls[0]).toEqual([original]);
  });
  it("rejects mismatched declaration identities/components/versions and panels", () => {
    const original = request(), patch = vi.fn();
    const configure = createNodeConfigurationAccess({ workflowId: () => original.workflowId, lifecycle: () => original.lifecycle,
      trustedExtensions: () => modelFrontendExtensions, patch });
    expect(configure({ ...original, extensionId: "workflow.models.workbench-panel" })).toBeNull();
    expect(configure({ ...original, componentVersion: "2" })).toBeNull();
    expect(configure({ ...original, componentId: "agents.execute" })).toBeNull();
    expect(configure({ ...original, extensionId: "arbitrary" })).toBeNull();
    expect(patch).not.toHaveBeenCalled();
  });
});
