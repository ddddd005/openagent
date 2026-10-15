"use strict";

((root) => {
  const INPUT_UPDATE_MS = 200;
  const MAX_TEXT = 1000000;
  const chatCommands = new Set(["reroll", "select_candidate", "fork_floor"]);
  const commands = new Set(["patch_input", "start", "pause", "resume", "close", "fork_checkpoint", ...chatCommands]);
  const evidenceOperations = {
    "frontend-node.patch-input": "patch_input", "frontend-node.start": "start", "frontend-node.pause": "pause",
    "frontend-node.resume": "resume", "frontend-node.close": "close", "frontend-node.fork-checkpoint": "fork_checkpoint",
    "frontend-node.reroll": "reroll", "frontend-node.select-candidate": "select_candidate",
    "frontend-node.fork-floor": "fork_floor",
  };
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length
    && fields.every(field => Object.hasOwn(value, field));
  const uuid = value => typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const sequence = value => Number.isSafeInteger(value) && value >= 0;
  const key = value => typeof value === "string" && value.length > 0
    && value.length <= 128 && value.trim().length > 0;
  const text = value => typeof value === "string" && value.length <= MAX_TEXT;
  const clone = value => JSON.parse(JSON.stringify(value));
  const same = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((v, i) => same(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length
      && Object.keys(a).every(field => Object.hasOwn(b, field) && same(a[field], b[field]));
  };
  function require(condition, code) {
    if (!condition) { const failure = new Error(code); failure.code = code; throw failure; }
  }
  function owner(value) {
    return exact(value, ["workflow_definition_id", "definition_revision", "node_binding_id", "session_id"])
      && uuid(value.workflow_definition_id) && positive(value.definition_revision)
      && uuid(value.node_binding_id) && (value.session_id === null || uuid(value.session_id));
  }
  function parameters(operation, value) {
    if (!object(value) || !key(value.idempotency_key)) return false;
    if (operation === "patch_input") return exact(value, ["text", "expected_edit_sequence", "idempotency_key",
      ...(Object.hasOwn(value, "confirm_derivation") ? ["confirm_derivation"] : [])])
      && text(value.text) && sequence(value.expected_edit_sequence)
      && (!Object.hasOwn(value, "confirm_derivation") || typeof value.confirm_derivation === "boolean");
    if (operation === "fork_checkpoint") return exact(value, [
      "checkpoint_id", "expected_session_revision", "expected_data_revision", "expected_head_revision", "idempotency_key",
    ]) && uuid(value.checkpoint_id) && positive(value.expected_session_revision)
      && sequence(value.expected_data_revision) && positive(value.expected_head_revision);
    if (chatCommands.has(operation)) return exact(value, [
      "round_id", "expected_session_revision", "expected_data_revision", "expected_head_revision",
      "expected_object_revision", "idempotency_key", ...(operation === "select_candidate" ? ["candidate_id"] : []),
    ]) && uuid(value.round_id) && positive(value.expected_session_revision)
      && sequence(value.expected_data_revision) && positive(value.expected_head_revision)
      && positive(value.expected_object_revision) && (operation !== "select_candidate" || uuid(value.candidate_id));
    return commands.has(operation) && operation !== "patch_input"
      && exact(value, ["expected_session_revision", "idempotency_key"])
      && (value.expected_session_revision === null && operation === "start"
        || positive(value.expected_session_revision));
  }
  function run(value) {
    return value === null || exact(value, ["revision", "status", "can_start", "can_pause",
      "can_resume", "can_close", "can_fork",
      ...(Object.hasOwn(value ?? {}, "object_revision") ? ["data_revision", "head_revision", "object_revision"] : [])])
      && positive(value.revision) && typeof value.status === "string"
      && ["can_start", "can_pause", "can_resume", "can_close", "can_fork"]
        .every(field => typeof value[field] === "boolean")
      && (!Object.hasOwn(value, "object_revision") || sequence(value.data_revision)
        && positive(value.head_revision) && positive(value.object_revision));
  }
  function chatContext(value) {
    if (!exact(value, ["rounds"]) || !Array.isArray(value.rounds)) return false;
    const rounds = new Set(), candidates = new Set(), messages = new Set();
    for (const round of value.rounds) {
      if (!exact(round, ["round_id", "base_checkpoint_id", "submitted_input", "candidates", "selected_candidate_id"])
        || !uuid(round.round_id) || rounds.has(round.round_id)
        || !(round.base_checkpoint_id === null || uuid(round.base_checkpoint_id))
        || !(round.submitted_input === null || typeof round.submitted_input === "string")
        || !Array.isArray(round.candidates)) return false;
      rounds.add(round.round_id);
      const ownCandidates = new Set();
      for (const candidate of round.candidates) {
        if (!exact(candidate, ["candidate_id", "messages", "result_checkpoint_id"])
          || !uuid(candidate.candidate_id) || candidates.has(candidate.candidate_id)
          || !(candidate.result_checkpoint_id === null || uuid(candidate.result_checkpoint_id))
          || !Array.isArray(candidate.messages) || !candidate.messages.length) return false;
        candidates.add(candidate.candidate_id); ownCandidates.add(candidate.candidate_id);
        for (const message of candidate.messages) {
          if (!exact(message, ["message_id", "role", "text"]) || !uuid(message.message_id)
            || messages.has(message.message_id) || !["user", "assistant", "system"].includes(message.role)
            || typeof message.text !== "string") return false;
          messages.add(message.message_id);
        }
      }
      if (round.selected_candidate_id !== null && !ownCandidates.has(round.selected_candidate_id)) return false;
    }
    return true;
  }
  function snapshot(value, binding) {
    if (value === null) return true;
    if (!exact(value, ["schema_version", "kind", "owner", "basis", "chat"])
      || value.schema_version !== 2 || value.kind !== "workflow.frontend-node-snapshot"
      || !exact(value.owner, ["workflow_definition_id", "definition_revision", "node_binding_id",
        "workflow_session_id", "object_key"]) || value.owner.workflow_definition_id !== binding.workflow_definition_id
      || value.owner.definition_revision !== binding.definition_revision
      || value.owner.node_binding_id !== binding.node_binding_id
      || value.owner.workflow_session_id !== binding.session_id || binding.session_id === null
      || typeof value.owner.object_key !== "string" || !value.owner.object_key
      || !exact(value.basis, ["revision", "revision_id"]) || !positive(value.basis.revision)
      || !uuid(value.basis.revision_id)) return false;
    return chatContext(value.chat);
  }
  function evidence(value, binding) {
    const fields = ["schema_version", "kind", "operation", "idempotency_key", "owner_kind", "owner_id",
      "request", "request_digest", "resolution"];
    if (!exact(value, fields) || value.schema_version !== 1 || value.kind !== "workflow.request-evidence"
      || value.resolution !== "pending" || typeof value.owner_kind !== "string" || typeof value.owner_id !== "string"
      || typeof value.operation !== "string"
      || typeof value.request_digest !== "string" || !value.request_digest
      || !exact(value.request, ["owner", "parameters"]) || !same(value.request.owner, binding)) return null;
    const operation = evidenceOperations[value.operation];
    return commands.has(operation)
      && parameters(operation, value.request.parameters)
      && value.idempotency_key === value.request.parameters.idempotency_key
      ? { owner: clone(binding), operation, parameters: clone(value.request.parameters) } : null;
  }
  function phase(value, binding) {
    return value === null || exact(value, ["workflow_definition_id", "definition_revision", "workflow_session_id",
      "revision", "status", "active_chain_run_id"])
      && value.workflow_definition_id === binding.workflow_definition_id
      && value.definition_revision === binding.definition_revision && uuid(value.workflow_session_id)
      && (binding.session_id === null || value.workflow_session_id === binding.session_id)
      && positive(value.revision) && typeof value.status === "string"
      && (value.active_chain_run_id === null || uuid(value.active_chain_run_id));
  }
  function response(value, request) {
    require(exact(value, ["schema_version", "kind", "owner", "operation", "idempotency_key", "outcome", "result"])
      && value.schema_version === 1 && value.kind === "workflow.frontend-node-result"
      && same(value.owner, request.owner) && value.operation === request.operation
      && value.idempotency_key === request.parameters.idempotency_key
      && object(value.result) && value.outcome === value.result.outcome, "frontend_node_result_mismatch");
    const result = value.result, binding = request.owner, next = result.next_owner;
    require(next === null || owner(next) && next.node_binding_id === binding.node_binding_id,
      "frontend_node_result_mismatch");
    if (request.operation === "patch_input") {
      require(exact(result, ["outcome", "edit_sequence", "definition_revision", "text", "next_owner"])
        && ["saved", "unchanged", "confirmation_required", "derived", "unknown"].includes(result.outcome),
      "frontend_node_result_mismatch");
      if (result.outcome === "unknown") {
        require(result.edit_sequence === null && result.text === null
          && result.definition_revision === binding.definition_revision && next === null, "frontend_node_result_mismatch");
      } else {
        require(result.text === request.parameters.text && positive(result.edit_sequence)
          && positive(result.definition_revision), "frontend_node_result_mismatch");
        if (result.outcome === "confirmation_required") require(next === null
          && result.edit_sequence === request.parameters.expected_edit_sequence
          && result.definition_revision === binding.definition_revision, "frontend_node_result_mismatch");
        else if (result.outcome === "derived") require(next !== null && next.session_id === null
          && next.workflow_definition_id !== binding.workflow_definition_id
          && result.edit_sequence === 1 && next.definition_revision === result.definition_revision,
        "frontend_node_result_mismatch");
        else require(result.edit_sequence === request.parameters.expected_edit_sequence + (result.outcome === "saved" ? 1 : 0)
          && (next === null ? result.definition_revision === binding.definition_revision
            : next.workflow_definition_id === binding.workflow_definition_id && next.session_id === binding.session_id
              && next.definition_revision === result.definition_revision),
        "frontend_node_result_mismatch");
      }
    } else if (["fork_checkpoint", "fork_floor"].includes(request.operation)) {
      require(exact(result, ["outcome", "fork_result", "failure_phase", "error", "next_owner"])
        && ["succeeded", "failed", "unknown"].includes(result.outcome)
        && (result.error === null || object(result.error) && typeof result.error.reason_code === "string"),
      "frontend_node_result_mismatch");
      const target = result.fork_result;
      require(result.outcome === "succeeded" ? exact(target, [
        "target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id",
      ]) && Object.values(target).every(uuid) && target.target_session_id !== binding.session_id
        && result.failure_phase === null && result.error === null && next !== null
        && next.workflow_definition_id === binding.workflow_definition_id
        && next.definition_revision === binding.definition_revision
        && next.session_id === target.target_session_id
        : target === null && next === null && result.failure_phase === request.operation,
      "frontend_node_result_mismatch");
    } else {
      const fields = request.operation === "start" ? ["create_result", "start_result"] : ["control_result"];
      require(exact(result, ["outcome", ...fields, "failure_phase", "error", "next_owner"])
        && ["succeeded", "failed", "unknown"].includes(result.outcome)
        && fields.every(field => phase(result[field], binding))
        && (result.error === null || object(result.error) && typeof result.error.reason_code === "string"),
      "frontend_node_result_mismatch");
      const created = request.operation === "start" ? result.create_result : null;
      const accepted = request.operation === "start" ? result.start_result : result.control_result;
      if (request.operation === "start" && binding.session_id === null && accepted !== null)
        require(created !== null, "frontend_node_result_mismatch");
      require(result.outcome === "succeeded" ? accepted !== null && result.failure_phase === null && result.error === null
        : accepted === null && (request.operation === "start" ? ["create", "start"].includes(result.failure_phase)
          : result.failure_phase === request.operation), "frontend_node_result_mismatch");
      if (created !== null && accepted !== null) require(created.workflow_session_id === accepted.workflow_session_id,
        "frontend_node_result_mismatch");
      if (binding.session_id !== null) require(created === null && next === null, "frontend_node_result_mismatch");
      else {
        const actual = created ?? accepted;
        require(actual === null ? next === null : next !== null
          && next.workflow_definition_id === binding.workflow_definition_id
          && next.definition_revision === binding.definition_revision
          && next.session_id === actual.workflow_session_id, "frontend_node_result_mismatch");
      }
    }
    return clone(value);
  }

  class FrontendNodeClient {
    constructor({ owner: binding, port, keyFactory, onChange = () => {},
      timers = { setTimeout: (...args) => setTimeout(...args), clearTimeout: handle => clearTimeout(handle) } }) {
      require(owner(binding) && object(port) && typeof port.request === "function"
        && typeof keyFactory === "function" && typeof onChange === "function"
        && typeof timers.setTimeout === "function" && typeof timers.clearTimeout === "function", "frontend_node_invalid_client");
      this._owner = clone(binding); this._port = port; this._keyFactory = keyFactory;
      this._onChange = onChange; this._timers = timers; this._disposed = false;
      this._initialized = false; this._input = null; this._run = null; this._snapshot = null;
      this._text = ""; this._generation = 0; this._savedGeneration = 0; this._timer = null;
      this._pending = Object.fromEntries([...commands].map(name => [name, null]));
      this._busy = Object.fromEntries([...commands].map(name => [name, false]));
      this._status = Object.fromEntries([...commands].map(name => [name, "ready"]));
      this._errors = { ...Object.fromEntries([...commands].map(name => [name, ""])), state: "", checkpoints: "" };
      this._checkpoints = null; this._checkpoint = null; this._checkpointReading = 0;
      this._confirmation = null; this._nextOwner = null; this._lastResult = null;
      this._writing = null; this._sending = false; this._reading = 0;
    }
    view() {
      return clone({
        owner: this._owner, initialized: this._initialized, input: this._input, text: this._text,
        dirty: this._generation !== this._savedGeneration, run: this._run, snapshot: this._snapshot,
        pending: this._pending, busy: this._busy, status: this._status, errors: this._errors,
        confirmation: this._confirmation, next_owner: this._nextOwner, last_result: this._lastResult,
        sending: this._sending, closed: this._disposed,
        checkpoints: this._checkpoints, checkpoint: this._checkpoint,
      });
    }
    _notify() { if (!this._disposed) this._onChange(this.view()); }
    _open() { require(!this._disposed, "frontend_node_client_closed"); }
    _cancelTimer() { if (this._timer !== null) this._timers.clearTimeout(this._timer); this._timer = null; }
    async initialize() { return this.refresh(); }
    async refresh() {
      this._open();
      const reading = ++this._reading;
      try {
        const value = await this._port.request({ owner: clone(this._owner), operation: "state", parameters: {} });
        if (this._disposed || reading !== this._reading) return null;
        require(exact(value, ["schema_version", "kind", "owner", "input", "run", "snapshot", "pending"])
          && value.schema_version === 1 && value.kind === "workflow.frontend-node-state"
          && same(value.owner, this._owner)
          && (value.input === null || exact(value.input, ["text", "edit_sequence", "policy"])
            && text(value.input.text) && positive(value.input.edit_sequence)
            && ["fixed", "adjustable"].includes(value.input.policy))
          && run(value.run) && (this._owner.session_id === null ? value.run === null : value.run !== null)
          && snapshot(value.snapshot, this._owner) && Array.isArray(value.pending), "frontend_node_state_mismatch");
        const pending = value.pending.map(record => evidence(record, this._owner));
        require(pending.every(Boolean) && new Set(pending.map(row => row.operation)).size === pending.length,
          "frontend_node_pending_mismatch");
        require(pending.every(request => this._pending[request.operation] === null
          || same(this._pending[request.operation], request)), "frontend_node_pending_mismatch");
        const dirty = this._generation !== this._savedGeneration;
        if (this._initialized && this._input !== null && value.input !== null
          && this._input.edit_sequence === value.input.edit_sequence)
          require(same(this._input, value.input), "frontend_node_state_mismatch");
        if (!this._initialized || !dirty && (this._input === null || value.input === null
          || value.input.edit_sequence >= this._input.edit_sequence)) this._input = clone(value.input);
        else if (!same(this._input, value.input) && (this._input === null || value.input === null
          || value.input.edit_sequence > this._input.edit_sequence))
          this._errors.patch_input = "frontend_node_input_basis_changed";
        this._run = clone(value.run); this._snapshot = clone(value.snapshot);
        if (!this._initialized || !dirty) this._text = this._input?.text ?? "";
        for (const request of pending) {
          const operation = request.operation;
          if (!this._initialized && operation === "patch_input") {
            this._text = request.parameters.text; this._generation++;
          }
          this._pending[operation] = request; this._status[operation] = "unknown";
        }
        if (!this._pending.patch_input && this._status.patch_input === "rejected") this._status.patch_input = "ready";
        this._initialized = true; this._errors.state = ""; this._notify();
        return this.view();
      } catch (failure) {
        if (!this._disposed && reading === this._reading) {
          this._errors.state = failure.message ?? "frontend_node_state_unavailable"; this._notify();
        }
        throw failure;
      }
    }
    update(value) {
      this._open();
      require(this._initialized && this._input !== null && text(value), "frontend_node_input_unavailable");
      require(!this._sending && this._nextOwner === null, "frontend_node_input_locked");
      if (value === this._text) return this.view();
      this._text = value; this._generation++; this._confirmation = null;
      if (!this._pending.patch_input && this._status.patch_input !== "rejected") this._status.patch_input = "pending";
      this._cancelTimer();
      if (!this._pending.patch_input || this._busy.patch_input) {
        const generation = this._generation;
        this._timer = this._timers.setTimeout(() => {
          this._timer = null;
          void this._autoSave(generation).catch(() => {});
        }, INPUT_UPDATE_MS);
      }
      this._notify(); return this.view();
    }
    async _autoSave(generation) {
      if (this._writing) await this._writing;
      if (!this._disposed && generation === this._generation && !this._sending
        && this._status.patch_input === "pending") await this._saveOne();
    }
    _capture(operation, fields) {
      const parameters = { ...clone(fields), idempotency_key: this._keyFactory() };
      require(commands.has(operation) && parameters && key(parameters.idempotency_key),
        "frontend_node_invalid_request");
      return { owner: clone(this._owner), operation, parameters };
    }
    async _request(request, reconcile = false) {
      const operation = request.operation;
      this._pending[operation] = clone(request); this._busy[operation] = true;
      this._status[operation] = "saving"; this._errors[operation] = ""; this._notify();
      try {
        const receiptParameters = {
          operation, original_request: { owner: clone(request.owner), parameters: clone(request.parameters) },
        };
        const envelope = reconcile ? { owner: clone(this._owner), operation: "receipt",
          parameters: clone(receiptParameters) } : clone(request);
        const value = response(await this._port.request(envelope), request);
        this._lastResult = value;
        const result = value.result;
        this._nextOwner = clone(result.next_owner);
        if (result.outcome === "unknown") {
          this._status[operation] = "unknown";
          throw Object.assign(new Error("frontend_node_request_unknown"), { unknown: true });
        }
        if (result.outcome === "failed") {
          this._pending[operation] = null;
          this._status[operation] = "failed";
          this._errors[operation] = result.error?.reason_code ?? "frontend_node_request_failed";
          return clone(value);
        }
        if (reconcile) {
          const accepted = response(await this._port.request({ owner: clone(this._owner),
            operation: "accept_receipt", parameters: clone(receiptParameters) }), request);
          require(same(accepted, value), "frontend_node_receipt_acceptance_mismatch");
        }
        this._pending[operation] = null;
        this._status[operation] = result.outcome;
        if (operation === "patch_input") {
          if (result.outcome === "confirmation_required") this._confirmation = clone(request);
          else if (this._nextOwner === null) this._input = {
            text: result.text, edit_sequence: result.edit_sequence, policy: this._input.policy,
          };
        }
        return clone(value);
      } catch (failure) {
        if (this._pending[operation] !== null) {
          if (!reconcile && failure.definite === true
            && !["idempotency_conflict", "request_evidence_pending"].includes(failure.code ?? failure.reason_code)) {
            this._pending[operation] = null; this._status[operation] = "rejected";
          } else this._status[operation] = "unknown";
        }
        this._errors[operation] = failure.message ?? "frontend_node_request_unknown";
        throw failure;
      } finally { this._busy[operation] = false; this._notify(); }
    }
    async _saveOne() {
      this._open();
      require(this._initialized && this._input !== null && this._nextOwner === null
        && this._pending.patch_input === null && !this._confirmation
        && this._status.patch_input !== "rejected", "frontend_node_input_blocked");
      if (this._writing) return this._writing;
      if (this._generation === this._savedGeneration) return null;
      const generation = this._generation;
      const request = this._capture("patch_input", { text: this._text,
        expected_edit_sequence: this._input.edit_sequence });
      const writing = this._request(request).then(value => {
        if (["saved", "unchanged"].includes(value.result.outcome) && value.result.next_owner === null) {
          this._savedGeneration = generation;
          if (generation !== this._generation) this._status.patch_input = "pending";
        }
        this._notify(); return value;
      });
      this._writing = writing;
      try { return await writing; } finally { if (this._writing === writing) this._writing = null; }
    }
    async flush() {
      this._open(); this._cancelTimer();
      if (this._writing) await this._writing;
      require(this._initialized && this._input !== null && this._nextOwner === null
        && !this._pending.patch_input && !this._confirmation
        && this._status.patch_input !== "rejected", "frontend_node_input_blocked");
      while (this._generation !== this._savedGeneration) {
        await this._saveOne();
        require(!this._confirmation && this._nextOwner === null && !this._pending.patch_input,
          "frontend_node_input_blocked");
      }
      return this.view();
    }
    async send() {
      this._open();
      require(this._initialized && !this._sending && !this._pending.start
        && !this._busy.start && this._nextOwner === null, "frontend_node_start_blocked");
      this._sending = true; this._notify();
      try {
        if (this._input !== null) await this.flush();
        require(this._owner.session_id === null || this._run?.can_start, "frontend_node_start_unavailable");
        return await this._request(this._capture("start", {
          expected_session_revision: this._run?.revision ?? null,
        }));
      } finally { this._sending = false; this._notify(); }
    }
    async pause() {
      return this._control("pause");
    }
    async resume() {
      return this._control("resume");
    }
    async close() {
      return this._control("close");
    }
    async _control(operation) {
      this._open();
      require(this._initialized && this._run?.["can_" + operation] && this._owner.session_id !== null
        && !this._pending[operation] && !this._busy[operation] && this._nextOwner === null,
      "frontend_node_" + operation + "_unavailable");
      return this._request(this._capture(operation, { expected_session_revision: this._run.revision }));
    }
    async listCheckpoints() {
      this._open();
      require(this._initialized && this._owner.session_id !== null, "frontend_node_checkpoints_unavailable");
      const reading = ++this._checkpointReading;
      try {
        const value = await this._port.request({ owner: clone(this._owner), operation: "checkpoints", parameters: {} });
        if (this._disposed || reading !== this._checkpointReading) return null;
        require(exact(value, ["schema_version", "kind", "owner", "result"]) && value.schema_version === 1
          && value.kind === "workflow.frontend-node-checkpoints" && same(value.owner, this._owner)
          && exact(value.result, ["items", "basis"]) && Array.isArray(value.result.items)
          && exact(value.result.basis, ["revision", "data_revision", "head_revision"])
          && positive(value.result.basis.revision) && sequence(value.result.basis.data_revision)
          && positive(value.result.basis.head_revision), "frontend_node_checkpoints_mismatch");
        const seen = new Set();
        for (const item of value.result.items) {
          require(exact(item, ["checkpoint_id", "parent_checkpoint_id"]) && uuid(item.checkpoint_id)
            && !seen.has(item.checkpoint_id)
            && (seen.size === 0 ? item.parent_checkpoint_id === null
              : item.parent_checkpoint_id === value.result.items[seen.size - 1].checkpoint_id),
          "frontend_node_checkpoints_mismatch");
          seen.add(item.checkpoint_id);
        }
        this._checkpoints = clone(value.result); this._checkpoint = null;
        this._errors.checkpoints = ""; this._notify(); return clone(value);
      } catch (failure) {
        if (!this._disposed && reading === this._checkpointReading) {
          this._checkpoints = null; this._checkpoint = null;
          this._errors.checkpoints = failure.message; this._notify();
        }
        throw failure;
      }
    }
    async readCheckpoint(checkpointId) {
      this._open();
      require(this._initialized && this._owner.session_id !== null && uuid(checkpointId),
        "frontend_node_checkpoints_unavailable");
      const reading = ++this._checkpointReading;
      const value = await this._port.request({ owner: clone(this._owner),
        operation: "checkpoint", parameters: { checkpoint_id: checkpointId } });
      if (this._disposed || reading !== this._checkpointReading) return null;
      const point = value?.result;
      require(exact(value, ["schema_version", "kind", "owner", "result"]) && value.schema_version === 1
        && value.kind === "workflow.frontend-node-checkpoint" && same(value.owner, this._owner)
        && exact(point, ["schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
          "fixed_definition_revision", "parent_checkpoint_id"]) && positive(point.schema_version)
        && point.checkpoint_id === checkpointId && point.workflow_definition_id === this._owner.workflow_definition_id
        && point.workflow_session_id === this._owner.session_id
        && point.fixed_definition_revision === this._owner.definition_revision
        && (point.parent_checkpoint_id === null || uuid(point.parent_checkpoint_id)),
      "frontend_node_checkpoints_mismatch");
      this._checkpoint = clone(point); this._notify(); return clone(value);
    }
    async forkCheckpoint(checkpointId) {
      this._open();
      require(this._initialized && this._run?.can_fork && this._nextOwner === null && this._checkpoints
        && !this._sending && this._generation === this._savedGeneration
        && !Object.values(this._pending).some(Boolean) && !Object.values(this._busy).some(Boolean)
        && this._checkpoints.items.some(point => point.checkpoint_id === checkpointId),
      "frontend_node_fork_unavailable");
      const basis = this._checkpoints.basis;
      return this._request(this._capture("fork_checkpoint", { checkpoint_id: checkpointId,
        expected_session_revision: basis.revision, expected_data_revision: basis.data_revision,
        expected_head_revision: basis.head_revision }));
    }
    async reroll(roundId) {
      return this._chatAction("reroll", roundId);
    }
    async selectCandidate(roundId, candidateId) {
      return this._chatAction("select_candidate", roundId, candidateId);
    }
    async forkFloor(roundId) {
      return this._chatAction("fork_floor", roundId);
    }
    async _chatAction(operation, roundId, candidateId) {
      this._open();
      const rounds = this._snapshot?.schema_version === 2 ? this._snapshot.chat.rounds : [];
      const round = rounds.find(item => item.round_id === roundId);
      require(this._initialized && this._owner.session_id !== null && this._nextOwner === null
        && round && positive(this._run?.object_revision)
        && this._run.object_revision === this._snapshot.basis.revision
        && !this._sending && !this._confirmation && this._generation === this._savedGeneration
        && !Object.values(this._pending).some(Boolean) && !Object.values(this._busy).some(Boolean)
        && (operation === "fork_floor" ? this._run.can_fork : this._run.can_start && round === rounds.at(-1))
        && (operation !== "select_candidate" || round.candidates.some(candidate =>
          candidate.candidate_id === candidateId && candidate.result_checkpoint_id !== null)),
      "frontend_node_" + operation + "_unavailable");
      return this._request(this._capture(operation, {
        round_id: roundId, ...(operation === "select_candidate" ? { candidate_id: candidateId } : {}),
        expected_session_revision: this._run.revision, expected_data_revision: this._run.data_revision,
        expected_head_revision: this._run.head_revision, expected_object_revision: this._run.object_revision,
      }));
    }
    async confirm() {
      this._open();
      require(this._confirmation && !this._pending.patch_input && !this._busy.patch_input
        && this._nextOwner === null, "frontend_node_confirmation_unavailable");
      const ask = clone(this._confirmation);
      this._confirmation = null;
      return this._request(this._capture("patch_input", {
        text: ask.parameters.text, expected_edit_sequence: ask.parameters.expected_edit_sequence,
        confirm_derivation: true,
      }));
    }
    decline() {
      this._open();
      require(this._confirmation !== null, "frontend_node_confirmation_unavailable");
      this._confirmation = null; this._status.patch_input = "rejected"; this._notify();
    }
    async reconcile(operation) {
      this._open();
      require(commands.has(operation) && this._pending[operation] && !this._busy[operation],
        "frontend_node_receipt_unavailable");
      const request = clone(this._pending[operation]);
      const value = await this._request(request, true);
      if (operation === "patch_input" && ["saved", "unchanged"].includes(value.result.outcome)
        && value.result.next_owner === null) {
        if (this._text === request.parameters.text) this._savedGeneration = this._generation;
        else this._status.patch_input = "pending";
      }
      this._notify(); return value;
    }
    dispose() {
      this._cancelTimer(); this._disposed = true; this._reading++;
    }
  }
  root.FrontendNodeData = { snapshot, chatContext };
  root.FrontendNodeClient = FrontendNodeClient;
})(globalThis);
