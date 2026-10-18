import { expect, vi } from "vitest";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { useWorkflowGraphStore } from "../stores/workflowGraph";
import type { memoryRequestEvidence } from "./requestEvidence";

export function debtCleanupDerivationResult(request: GraphConfigurationUpdateRequest,
  targetId: string): GraphConfigurationResult {
  const derived = request.confirm_derivation === true;
  const workflowId = derived ? targetId : request.workflow_definition_id;
  const revision = derived ? 1 : request.document.revision;
  const editSequence = derived ? 1 : request.expected_edit_sequence;
  const document = { ...graphClone(request.document), workflow_definition_id: workflowId,
    revision, edit_sequence: editSequence };
  return {
    schema_version: 1, kind: "workflow.configuration-result",
    outcome: derived ? "derived" : "confirmation_required",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: workflowId, definition_revision: revision, edit_sequence: editSequence,
    source_has_effective_session: true, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document,
  };
}
export function installDebtCleanupConfiguration(graph: ReturnType<typeof useWorkflowGraphStore>,
  evidence: ReturnType<typeof memoryRequestEvidence>) {
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult> => {
    const revision = request.expected_edit_sequence === 0 ? 1 : request.document.revision + 1;
    const document = { ...graphClone(request.document), revision, edit_sequence: request.expected_edit_sequence + 1 };
    return {
      schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
      source_workflow_definition_id: request.workflow_definition_id,
      workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
      edit_sequence: document.edit_sequence, source_has_effective_session: false, changed_categories: ["fixed"],
      node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
      session_id: null, document,
    };
  });
  const application = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => [], update,
    readReceipt: vi.fn(async () => { throw new Error("No configuration receipt read expected"); }),
  });
  graph.installWorkflowConfigurationApplication(application, { seed: true });
  async function settled(workflowId: string) {
    const channel = application.forWorkflow(workflowId);
    await channel.initialize();
    await vi.waitFor(() => {
      expect(channel.lastResult.value).not.toBeNull();
      expect(channel.locked.value).toBe(false);
      expect(graph.entries[workflowId]?.document).toEqual(channel.lastResult.value!.document);
      expect(graph.locked).toBe(false);
    });
  }
  return { application, update, settled };
}
