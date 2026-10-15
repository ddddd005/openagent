"use strict";

((root) => {
  const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";
  const uuid = value => typeof value === "string" && new RegExp("^" + UUID + "$").test(value);
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length
    && fields.every(field => Object.hasOwn(value, field));
  const copy = value => JSON.parse(JSON.stringify(value));
  const sameOwner = (a, b) => validOwner(a) && validOwner(b)
    && ["workflow_definition_id", "definition_revision", "node_binding_id", "session_id"].every(key => a[key] === b[key]);
  function validOwner(value) {
    return exact(value, ["workflow_definition_id", "definition_revision", "node_binding_id", "session_id"])
      && uuid(value.workflow_definition_id) && positive(value.definition_revision)
      && uuid(value.node_binding_id) && (value.session_id === null || uuid(value.session_id));
  }
  function fail(code) { throw Object.assign(new Error(code), { code }); }
  function ownerPath(value) {
    if (!validOwner(value)) fail("frontend_node_invalid_binding");
    return [value.workflow_definition_id, value.definition_revision, value.node_binding_id, value.session_id ?? "none"].join("/");
  }
  function parseOwnerPath(path) {
    const match = new RegExp("^/frontend-node/(" + UUID + ")/([1-9][0-9]*)/(" + UUID + ")/(none|" + UUID + ")/$").exec(path);
    if (!match || !positive(Number(match[2]))) fail("frontend_node_invalid_binding");
    return { workflow_definition_id: match[1], definition_revision: Number(match[2]),
      node_binding_id: match[3], session_id: match[4] === "none" ? null : match[4] };
  }
  function urlsForOwner(origin, owner) {
    const url = new URL(origin);
    if (!["http:", "https:"].includes(url.protocol) || url.origin !== origin
      || url.username || url.password) fail("frontend_node_invalid_origin");
    const identity = ownerPath(owner), prefix = origin + "/api/graph/frontend-nodes/" + identity;
    return { page_url: origin + "/frontend-node/" + identity + "/",
      channel_url: prefix + "/channel", events_url: prefix + "/events", entry_url: prefix + "/entry" };
  }
  function validateSnapshot(value, owner) {
    if (!exact(value, ["schema_version", "kind", "owner", "basis", "chat"])
      || value.schema_version !== 2 || value.kind !== "workflow.frontend-node-snapshot"
      || !root.FrontendNodeData?.snapshot(value, owner)) fail("frontend_node_snapshot_mismatch");
    return value;
  }
  async function jsonRequest(fetcher, url, options = {}) {
    let response, value;
    try {
      response = await fetcher(url, { ...options, credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { "Content-Type": "application/json" } });
      value = await response.json();
    } catch { fail("frontend_node_response_unknown"); }
    if (!response.ok) {
      const detail = object(value?.error) ? value.error : null;
      const code = detail?.reason_code ?? detail?.code;
      const valid = typeof code === "string" && /^[a-z][a-z0-9_]{0,127}$/.test(code)
        && typeof detail?.message === "string" && detail.message.length > 0;
      const diagnosed = valid && Array.isArray(detail.diagnostics)
        && detail.diagnostics.some(item => object(item) && (item.code === code || item.reason_code === code));
      const uncertain = ["idempotency_conflict", "request_evidence_pending", "request_evidence_conflict",
        "frontend_node_result_mismatch", "frontend_node_dispatch_unknown", "workflow_operation_dispatch_unknown"].includes(code);
      const failure = new Error(valid && response.status < 500 ? detail.message : "frontend_node_response_unknown");
      failure.code = valid && response.status < 500 ? code : "frontend_node_response_unknown";
      failure.definite = diagnosed && response.status >= 400 && response.status < 500 && !uncertain;
      throw failure;
    }
    if (response.status !== 200 || !object(value)) fail("frontend_node_response_unknown");
    return value;
  }

  class Host {
    constructor({ origin, fetcher, eventSourceFactory, clientFactory = options => new root.FrontendNodeClient(options),
      keyFactory, onChange = () => {}, onBinding = () => {} }) {
      this._origin = origin; this._fetcher = fetcher; this._eventSourceFactory = eventSourceFactory;
      this._clientFactory = clientFactory; this._keyFactory = keyFactory;
      this._onChange = onChange; this._onBinding = onBinding;
      this._record = null; this._generation = 0; this._disposed = false;
      this._connection = "disconnected"; this._error = ""; this._notice = "";
    }
    view() {
      return { client: this._record?.client?.view() ?? null, owner: this._record ? copy(this._record.owner) : null,
        connection: this._connection, error: this._error, notice: this._notice };
    }
    _notify() { if (!this._disposed) this._onChange(copy(this.view())); }
    _current(record) { return !this._disposed && this._record === record; }
    _close(record) { record?.stream?.close(); record?.client?.dispose(); }
    async bind(owner) {
      if (this._disposed || !validOwner(owner)) fail("frontend_node_invalid_binding");
      this._close(this._record);
      const record = { owner: copy(owner), urls: urlsForOwner(this._origin, owner), generation: ++this._generation,
        stream: null, client: null, refresh: null, refreshAgain: false, seenBasis: null };
      this._record = record; this._connection = "connecting"; this._error = ""; this._notify();
      try {
        const entry = await jsonRequest(this._fetcher, record.urls.entry_url);
        if (!this._current(record)) return null;
        if (!exact(entry, ["schema_version", "kind", "owner", "page_url", "channel_url", "events_url"])
          || entry.schema_version !== 1 || entry.kind !== "workflow.frontend-node-entry"
          || !sameOwner(entry.owner, owner)
          || ["page_url", "channel_url", "events_url"].some(field => entry[field] !== record.urls[field]))
          fail("frontend_node_entry_mismatch");
        const port = { request: async envelope => {
          if (!sameOwner(envelope?.owner, record.owner)
            || !["state", "patch_input", "start", "pause", "resume", "close", "checkpoints", "checkpoint",
              "fork_checkpoint", "reroll", "select_candidate", "fork_floor", "receipt", "accept_receipt"].includes(envelope?.operation))
            fail("frontend_node_owner_mismatch");
          return jsonRequest(this._fetcher, record.urls.channel_url, { method: "POST", body: JSON.stringify(envelope) });
        } };
        record.client = this._clientFactory({ owner: copy(owner), port, keyFactory: this._keyFactory,
          onChange: () => { if (this._current(record)) this._notify(); } });
        await record.client.initialize();
        if (!this._current(record)) return null;
        this._onBinding(copy(owner), record.urls.page_url);
        this._openEvents(record); this._notify();
        return this.view();
      } catch (failure) {
        if (this._current(record)) {
          this._connection = "disconnected"; this._error = failure.message; this._notify();
        }
        throw failure;
      }
    }
    _streamFailure(record, message) {
      if (!this._current(record)) return;
      record.stream?.close(); record.stream = null;
      this._connection = "disconnected"; this._error = message; this._notify();
    }
    _openEvents(record) {
      record.stream?.close();
      this._connection = "connecting";
      const stream = this._eventSourceFactory(record.urls.events_url);
      record.stream = stream;
      stream.onopen = () => {
        if (this._current(record) && record.stream === stream) {
          this._connection = "connected"; this._error = ""; this._notify();
        }
      };
      stream.onerror = () => {
        if (record.stream === stream) this._streamFailure(record, "frontend_node_events_disconnected");
      };
      const accept = (name, event) => {
        if (!this._current(record) || record.stream !== stream) return;
        try {
          const value = JSON.parse(event.data);
          if (name === "snapshot") {
            validateSnapshot(value, record.owner);
            const current = record.client.view().snapshot?.basis;
            if (current && value.basis.revision < current.revision) return;
            if (current && value.basis.revision === current.revision
              && value.basis.revision_id !== current.revision_id) fail("frontend_node_snapshot_mismatch");
            if (value.basis.revision_id === record.seenBasis
              || value.basis.revision_id === current?.revision_id) return;
            record.seenBasis = value.basis.revision_id;
          } else if (!exact(value, ["schema_version", "kind", "owner"]) || value.schema_version !== 1
            || value.kind !== "workflow.frontend-node-state-changed" || !sameOwner(value.owner, record.owner))
            fail("frontend_node_state_event_mismatch");
          void this._refresh(record).catch(() => {});
        } catch (failure) { this._streamFailure(record, failure.message); }
      };
      stream.addEventListener("snapshot", event => accept("snapshot", event));
      stream.addEventListener("state_changed", event => accept("state_changed", event));
    }
    async _refresh(record) {
      if (!this._current(record) || !record.client) return null;
      if (record.refresh) { record.refreshAgain = true; return record.refresh; }
      const reading = (async () => {
        do {
          record.refreshAgain = false;
          await record.client.refresh();
        } while (this._current(record) && record.refreshAgain);
        return this.view();
      })();
      record.refresh = reading;
      try { return await reading; }
      catch (failure) {
        if (this._current(record)) { this._error = failure.message; this._notify(); }
        throw failure;
      } finally { if (record.refresh === reading) record.refresh = null; }
    }
    async refresh() {
      const record = this._record;
      if (!record) fail("frontend_node_invalid_binding");
      if (!record?.client) return this.bind(record.owner);
      const result = await this._refresh(record);
      if (this._current(record) && !record.stream) this._openEvents(record);
      return result;
    }
    update(text) {
      if (!this._record?.client) fail("frontend_node_input_unavailable");
      this._error = ""; return this._record.client.update(text);
    }
    async action(action, operation) {
      const record = this._record;
      if (!record?.client || !["send", "pause", "resume", "close", "listCheckpoints", "readCheckpoint",
        "forkCheckpoint", "reroll", "selectCandidate", "forkFloor", "flush", "confirm", "decline", "reconcile"].includes(action))
        fail("frontend_node_action_unavailable");
      this._error = ""; this._notice = "";
      try {
        const arguments_ = action === "selectCandidate" ? [operation?.round_id, operation?.candidate_id]
          : ["reconcile", "readCheckpoint", "forkCheckpoint", "reroll", "forkFloor"].includes(action) ? [operation] : [];
        const result = await record.client[action](...arguments_);
        if (!this._current(record)) return result;
        if (result?.operation === "start" && result.outcome === "succeeded"
          && record.owner.session_id === null && result.result.next_owner) {
          await this.bind(result.result.next_owner);
        } else if (["pause", "resume", "close", "send", "reroll", "selectCandidate"].includes(action)
          && record.client.view().next_owner === null)
          await this._refresh(record);
        if (this._current(record)) this._notify();
        return result;
      } catch (failure) {
        if (this._current(record)) {
          const current = record.client.view(), saved = current.last_result;
          const needsBinding = failure.code === "frontend_node_input_blocked"
            && current.next_owner !== null && saved?.operation === "patch_input"
            && ["saved", "unchanged"].includes(saved.outcome)
            && current.pending.patch_input === null
            && ["saved", "unchanged"].includes(current.status.patch_input);
          this._error = needsBinding ? "" : failure.message;
          if (needsBinding) this._notice = "输入已保存，请接续新绑定；本轮尚未启动。";
          this._notify();
        }
        throw failure;
      }
    }
    async continueBinding() {
      const current = this._record?.client?.view(), next = current?.next_owner;
      if (!next) fail("frontend_node_next_binding_unavailable");
      this._notice = current.last_result?.result?.failure_phase ? "已接续实际创建的会话，未重新启动" : "已接续新绑定，尚未启动";
      return this.bind(next);
    }
    dispose() {
      this._disposed = true; this._generation++; this._close(this._record);
    }
  }
  root.FrontendNodePage = { Host, parseOwnerPath, urlsForOwner };
})(globalThis);
