import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone, newGraph, type GraphDocument } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { editableInputType } from "../testUtils/editableInputFixture";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkflowGraphStore } from "./workflowGraph";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });

async function setup(existing?: "fixed" | "adjustable" | "missing") {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  const fixedType = { ...graphClone(editableInputType), component_id: "tools.text",
    adjustable_configuration: undefined, default_configuration_policy: undefined };
  graph.catalog = [graphClone(editableInputType), fixedType];
  const requests: GraphConfigurationUpdateRequest[] = [];
  const application = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => [],
    update: async request => {
      requests.push(graphClone(request));
      const revision = request.expected_edit_sequence === 0 ? 1 : request.document.revision + 1;
      const editSequence = request.expected_edit_sequence + 1;
      const document = { ...graphClone(request.document), revision, edit_sequence: editSequence };
      const result: GraphConfigurationResult = { schema_version: 1, kind: "workflow.configuration-result",
        outcome: "saved", source_workflow_definition_id: request.workflow_definition_id,
        workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
        edit_sequence: editSequence, source_has_effective_session: false, changed_categories: ["fixed"],
        node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
        session_id: null, document };
      return result;
    },
  });
  let id: string;
  if (existing) {
    const document: GraphDocument = newGraph("Existing input");
    document.nodes = [{ node_binding_id: crypto.randomUUID(), component_id: editableInputType.component_id,
      component_version: "1", title: "Existing", config: { text: "original" }, position: { x: 0, y: 0 },
      ...(existing === "missing" ? {} : { configuration_policy: { schema_version: 1, prompt_mode: existing } }) }];
    id = graph.createWorkflow();
    graph.entries[id].document = { ...document, workflow_definition_id: id };
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    graph.installWorkflowConfigurationApplication(application);
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    graph.patchNode(document.nodes[0]!.node_binding_id, { title: "Existing input" });
  } else {
    graph.installWorkflowConfigurationApplication(application);
    id = graph.createWorkflow();
  }
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  expect(requests).toHaveLength(1);
  return { graph, id, requests, fixedType };
}
async function saved(graph: ReturnType<typeof useWorkflowGraphStore>) {
  await vi.waitFor(() => expect(graph.locked).toBe(false));
}

describe("editable input through trusted configuration declarations", () => {
  it("uses the exact installed default for new input and keeps undeclared types fixed", async () => {
    const { graph, requests } = await setup();
    const input = graph.addNode("tools.editable-input@1", { x: 0, y: 0 })!;
    await saved(graph);
    expect(graph.document!.nodes.find(node => node.node_binding_id === input)).toMatchObject({
      config: { text: "" }, configuration_policy: { schema_version: 1, prompt_mode: "adjustable" },
    });
    graph.addNode("tools.text@1", { x: 200, y: 0 });
    await saved(graph);
    expect(requests.at(-1)!.document.nodes.at(-1)!.configuration_policy.prompt_mode).toBe("fixed");
    expect(graph.catalog[0]!.default_configuration_policy!.prompt_mode).toBe("adjustable");
  });

  it.each(["fixed", "missing"] as const)("keeps existing %s policy fixed instead of applying the new default", async mode => {
    const { graph, requests } = await setup(mode);
    expect(graph.document!.nodes[0]!.configuration_policy!.prompt_mode).toBe("fixed");
    expect(requests[0]!.document.nodes[0]!.config.text).toBe("original");
  });

  it.each(["fixed", "adjustable"] as const)("preserves selected %s policy and text when duplicating", async mode => {
    const { graph } = await setup(mode);
    graph.selectedNodeIds = [graph.document!.nodes[0]!.node_binding_id];
    graph.duplicateSelection(); await saved(graph);
    expect(graph.document!.nodes).toHaveLength(2);
    expect(graph.document!.nodes[1]!.configuration_policy!.prompt_mode).toBe(mode);
    expect(graph.document!.nodes[1]!.config.text).toBe("original");
  });

  it("saves both policy switches and ordinary text edits through formal configuration", async () => {
    const { graph, id, requests } = await setup("fixed");
    const nodeId = graph.document!.nodes[0]!.node_binding_id;
    expect(graph.patchNodeConfigurationPolicy(nodeId, "adjustable")).toBe(id); await saved(graph);
    expect(requests.at(-1)!.document.nodes[0]!.configuration_policy.prompt_mode).toBe("adjustable");
    expect(graph.patchNode(nodeId, { config: { text: "  untouched whitespace\nnext line  " } })).toBe(id);
    await saved(graph);
    expect(requests.at(-1)!.document.nodes[0]!.config).toEqual({ text: "  untouched whitespace\nnext line  " });
    expect(graph.patchNodeConfigurationPolicy(nodeId, "fixed")).toBe(id); await saved(graph);
    expect(requests.at(-1)!.document.nodes[0]!.configuration_policy.prompt_mode).toBe("fixed");
  });

  it.each(["missing", "malformed", "another-version"] as const)(
    "rejects a strategy switch when the trusted declaration is %s", async reason => {
      const { graph, requests } = await setup("fixed");
      if (reason === "missing") delete graph.catalog[0]!.adjustable_configuration;
      if (reason === "malformed") graph.catalog[0]!.adjustable_configuration = { policy: "input" } as never;
      if (reason === "another-version") graph.catalog[0]!.component_version = "2";
      const before = graphClone(graph.document);
      expect(graph.patchNodeConfigurationPolicy(graph.document!.nodes[0]!.node_binding_id, "adjustable")).toBeNull();
      expect(requests).toHaveLength(1); expect(graph.document).toEqual(before);
    });

  it("keeps explicit fixed on replacement and returns unsupported adjustable replacements to fixed", async () => {
    const { graph, id } = await setup("fixed"), nodeId = graph.document!.nodes[0]!.node_binding_id;
    expect(graph.replaceNode(nodeId, "tools.editable-input@1")).toBe(id); await saved(graph);
    expect(graph.document!.nodes[0]!.configuration_policy!.prompt_mode).toBe("fixed");
    graph.patchNodeConfigurationPolicy(nodeId, "adjustable"); await saved(graph);
    graph.patchNode(nodeId, { config: { text: "replace this config" } }); await saved(graph);
    expect(graph.replaceNode(nodeId, "tools.editable-input@1")).toBe(id); await saved(graph);
    expect(graph.document!.nodes[0]!.configuration_policy!.prompt_mode).toBe("adjustable");
    expect(graph.document!.nodes[0]!.config.text).toBe("");
    graph.replaceNode(nodeId, "tools.text@1"); await saved(graph);
    expect(graph.document!.nodes[0]!.configuration_policy!.prompt_mode).toBe("fixed");
  });

  it("uses the replacement definition default only when the old node has no selected policy", async () => {
    const { graph } = await setup("fixed"), nodeId = graph.document!.nodes[0]!.node_binding_id;
    delete graph.document!.nodes[0]!.configuration_policy;
    graph.replaceNode(nodeId, "tools.editable-input@1"); await saved(graph);
    expect(graph.document!.nodes[0]!.configuration_policy!.prompt_mode).toBe("adjustable");
  });
});
