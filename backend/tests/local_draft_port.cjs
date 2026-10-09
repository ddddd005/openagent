"use strict";

const assert = require("node:assert/strict");
const clone = value => JSON.parse(JSON.stringify(value));

function createLocalDraftPort({ beforeSave, afterSave } = {}) {
  const records = new Map(), receipts = new Map(), calls = [];
  const identity = address => JSON.stringify(address);
  const port = {
    async read(address) {
      calls.push({ kind: "read", address: clone(address) });
      return clone(records.get(identity(address)) ?? null);
    },
    async save(parameters, reconcile = false) {
      calls.push({ kind: reconcile ? "receipt" : "save", parameters: clone(parameters) });
      const prior = receipts.get(parameters.idempotency_key);
      if (prior) {
        assert.deepEqual(parameters, prior.parameters, "Draft idempotency body changed");
        return clone(prior.record);
      }
      if (reconcile) throw new Error("Local draft receipt remains unknown");
      await beforeSave?.(clone(parameters));
      const key = identity(parameters.address), sequence = records.get(key)?.sequence ?? 0;
      if (sequence !== parameters.expected_sequence) {
        const error = new Error("Local draft CAS conflict");
        error.definite = true;
        throw error;
      }
      const record = { schema_version: 1, kind: "workflow.local-draft", address: clone(parameters.address),
        format_version: parameters.format_version, sequence: sequence + 1,
        updated_at: "2026-10-09T00:00:00.000Z", value: clone(parameters.value),
        deleted: parameters.value === null };
      records.set(key, record);
      receipts.set(parameters.idempotency_key, { parameters: clone(parameters), record: clone(record) });
      await afterSave?.(clone(parameters), clone(record));
      return clone(record);
    },
  };
  return {
    port, calls,
    value(key, workflow) {
      const row = [...records.values()].find(record => record.address.key === key
        && record.address.workflow_definition_id === workflow);
      return clone(row?.deleted ? null : row?.value ?? null);
    },
    async operation(name, parameters, reconcile = false) {
      const operation = name.replace(/^consumer\./, "");
      if (operation === "draft.read") return port.read(parameters.address);
      if (operation === "draft.list") return { schema_version: 1, kind: "workflow.local-drafts",
        records: [...records.values()].filter(record =>
          record.address.scope === (name.startsWith("consumer.") ? "consumer" : "management")
          && (!parameters.workflow_definition_id || record.address.workflow_definition_id === parameters.workflow_definition_id)
          && (!parameters.kind || record.address.kind === parameters.kind)).map(clone) };
      assert.equal(operation, "draft.save");
      const record = await port.save(parameters, reconcile);
      const receipt = { schema_version: 1, kind: "workflow.application-receipt",
        operation: name, operation_scope: record.address.scope, idempotency_key: parameters.idempotency_key,
        request_sha256: "a".repeat(64), authority: "service_receipt", target: {},
        accepted: { draft_address: clone(record.address), draft_sequence: record.sequence,
          draft_format_version: record.format_version, deleted: record.deleted } };
      return reconcile ? { schema_version: 1, kind: "workflow.application-receipt-read",
        outcome: "matched", reason_code: "receipt_matched", receipt, result: record }
        : { schema_version: 1, kind: "workflow.application-command", receipt, result: record };
    },
  };
}

module.exports = { createLocalDraftPort };
