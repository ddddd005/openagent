import { describe, expect, it, vi } from "vitest";
import { createWorkflowConfigurationApplication } from "./workflowConfiguration";
import { createWorkflowConfigurationCopy } from "./workflowConfigurationCopy";
import { createWorkflowManagementRequests } from "./workflowManagementRequests";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import type { RequestEvidenceRecord } from "../adapters/requestEvidenceApi";

function fixture(kind: "configuration" | "copy" | "management") {
  const owner = crypto.randomUUID(), key = crypto.randomUUID(), evidence = memoryRequestEvidence();
  const send = vi.fn(async (_request: any): Promise<any> => { throw new Error("Unexpected business send"); });
  if (kind === "configuration") {
    const request: GraphConfigurationUpdateRequest = { workflow_definition_id: owner, expected_edit_sequence: 0,
      idempotency_key: key, document: { schema_version: 3, workflow_definition_id: owner, revision: 1,
        name: "Configuration", nodes: [], edges: [], object_bindings: [] } };
    const result = { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
      source_workflow_definition_id: owner, workflow_definition_id: owner, definition_revision: 1,
      edit_sequence: 1, source_has_effective_session: false, changed_categories: ["fixed"], node_id_map: {},
      session_id: null, document: { ...request.document, edit_sequence: 1 } };
    send.mockResolvedValue(result);
    const receipt = vi.fn(async (): Promise<any> => result);
    // Install receipt reading in the same application, independently of dispatch.
    const receiptApp = createWorkflowConfigurationApplication({ evidence: evidence.ports, extensions: () => [],
      update: send, readReceipt: receipt });
    const entry = receiptApp.forWorkflow(owner);
    return { owner, evidence, send, initialize: entry.initialize,
      submit: () => entry.submit(request).then(() => true).catch(() => false),
      reconcile: () => entry.reconcile().then(() => true).catch(() => false), receipt, entry };
  }
  if (kind === "copy") {
    const target = crypto.randomUUID();
    const request = { source_workflow_definition_id: owner, expected_source_definition_revision: 1,
      expected_source_edit_sequence: 1, target_workflow_definition_id: target, target_name: "Copy", idempotency_key: key };
    const result = { schema_version: 1, kind: "workflow.configuration-copy-result", outcome: "copied",
      source_workflow_definition_id: owner, source_definition_revision: 1, source_edit_sequence: 1,
      workflow_definition_id: target, definition_revision: 1, edit_sequence: 1, session_id: null, node_id_map: {},
      document: { schema_version: 3, workflow_definition_id: target, revision: 1, edit_sequence: 1,
        name: "Copy", nodes: [], edges: [], object_bindings: [] } };
    send.mockResolvedValue(result);
    const receipt = vi.fn(async (): Promise<any> => result);
    const entry = createWorkflowConfigurationCopy({ evidence: evidence.ports, send, readReceipt: receipt }).forSource(owner);
    return { owner, evidence, send, initialize: entry.initialize,
      submit: () => entry.submit(request), reconcile: entry.reconcile, receipt, entry };
  }
  const result = { schema_version: 1, kind: "workflow.definition-management", workflow_definition_id: owner,
    revision: 1, archived: false, deleted: true };
  send.mockResolvedValue(result);
  const receipt = vi.fn(async (): Promise<any> => result);
  const entry = createWorkflowManagementRequests({ evidence: evidence.ports, send, readReceipt: receipt,
    accept: vi.fn() }).forWorkflow(owner);
  return { owner, evidence, send, initialize: entry.initialize,
    submit: () => entry.dispatch({ action: "definition-delete", path: `/api/graph/definitions/${owner}/delete`,
      body: { expected_revision: 1, idempotency_key: key } }).then(() => true).catch(() => false),
    reconcile: () => entry.reconcile(), receipt, entry };
}
describe("workflow frozen evidence consumers", () => {
  it.each(["configuration", "copy", "management"] as const)(
    "%s completes a lost resolve acknowledgement without sending business again", async kind => {
      const state = fixture(kind), resolve = state.evidence.ports.resolve;
      await state.initialize();
      state.evidence.ports.resolve = vi.fn(async request => {
        await resolve(request);
        throw new Error("resolve acknowledgement lost after commit");
      });
      expect(await state.submit()).toBe(false);
      expect(state.send).toHaveBeenCalledTimes(1);
      expect(state.entry.progress.value).toBe("closure-failed");
      expect(state.entry.locked.value).toBe(true);
      const verified = graphClone([...state.evidence.records.values()][0]!);
      expect(verified.resolution).toBe("verified");
      state.evidence.ports.resolve = vi.fn(async request => resolve(request));
      expect(await state.reconcile()).toBe(true);
      expect(state.receipt).toHaveBeenCalledTimes(1);
      expect(state.send).toHaveBeenCalledTimes(1);
      expect(state.entry.pending.value).toBeNull();
      expect(state.entry.locked.value).toBe(false);
      expect([...state.evidence.records.values()][0]).toEqual(verified);
    });
  it.each(["configuration", "copy", "management"] as const)(
    "%s keeps pending B after A resolve committed but its acknowledgement was lost", async kind => {
      const state = fixture(kind), resolve = state.evidence.ports.resolve;
      await state.initialize();
      state.evidence.ports.resolve = vi.fn(async request => {
        await resolve(request);
        throw new Error("resolve acknowledgement lost after commit");
      });
      expect(await state.submit()).toBe(false);
      const a = [...state.evidence.records.values()][0]!, keyB = crypto.randomUUID();
      const b = await state.evidence.ports.write({ owner_kind: a.owner_kind, owner_id: a.owner_id,
        operation: a.operation, idempotency_key: keyB, request: { ...a.request, idempotency_key: keyB } });
      state.evidence.ports.resolve = vi.fn(async request => resolve(request));
      expect(await state.reconcile()).toBe(false);
      expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
      expect([...state.evidence.records.values()].find(row => row.idempotency_key === keyB)).toEqual(b);
      expect(state.send).toHaveBeenCalledTimes(1);
      expect(state.entry.locked.value).toBe(true);
    });
  it.each(["configuration", "copy", "management"] as const)(
    "%s re-reads current evidence after a frozen A write reply and preserves new pending B", async kind => {
      const state = fixture(kind);
      await state.initialize();
      const write = state.evidence.ports.write, resolve = state.evidence.ports.resolve;
      let frozenA!: RequestEvidenceRecord, pendingB!: RequestEvidenceRecord;
      state.evidence.ports.write = vi.fn(async request => {
        frozenA = await write(request);
        await resolve({ operation: frozenA.operation, idempotency_key: frozenA.idempotency_key,
          request_digest: frozenA.request_digest, resolution: "verified" });
        const keyB = crypto.randomUUID();
        pendingB = await write({ ...request, idempotency_key: keyB,
          request: { ...request.request, idempotency_key: keyB } });
        return graphClone(frozenA);
      });
      expect(await state.submit()).toBe(false);
      expect(state.send).not.toHaveBeenCalled();
      expect(frozenA.resolution).toBe("pending");
      const records = await state.evidence.ports.read(pendingB.owner_kind, pendingB.owner_id);
      expect(records.find(row => row.idempotency_key === frozenA.idempotency_key)?.resolution).toBe("verified");
      expect(records.find(row => row.idempotency_key === pendingB.idempotency_key)).toEqual(pendingB);
      expect(state.evidence.ports.resolve).toHaveBeenCalledTimes(1);
      expect(state.entry.locked.value).toBe(true);
    });
  it.each(["configuration", "copy", "management"] as const)(
    "%s sends zero business requests if the current read fails after evidence write confirmation", async kind => {
      const state = fixture(kind);
      await state.initialize();
      const write = state.evidence.ports.write;
      state.evidence.ports.write = vi.fn(async request => {
        const record = await write(request);
        state.evidence.ports.read = vi.fn(async () => { throw new WorkbenchApiError("unknown", "current evidence unavailable"); });
        return record;
      });
      expect(await state.submit()).toBe(false);
      expect(state.send).not.toHaveBeenCalled();
      expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
      expect([...state.evidence.records.values()][0]?.resolution).toBe("pending");
      expect(state.entry.locked.value).toBe(true);
    });
});
