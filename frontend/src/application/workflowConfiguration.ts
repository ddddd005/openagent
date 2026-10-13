import { computed, readonly, ref, shallowReactive, shallowRef } from "vue";
import { requestEvidencePorts, isRequestEvidenceRecord,
  type RequestEvidencePorts, type RequestEvidenceRecord } from "../adapters/requestEvidenceApi";
import { updateGraphConfiguration, readGraphConfigurationReceipt } from "../adapters/workflowConfigurationApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphJsonSignature, graphUuid } from "../domain/workflowGraph";
import { isGraphConfigurationDocument, isGraphConfigurationResult, isGraphConfigurationUpdateRequest,
  type GraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { createWorkflowConfigurationSdk,
  type WorkflowConfigurationSubmissionPorts } from "../plugins/workflowFrontendSdk2";

export interface WorkflowConfigurationSelection {
  workflowId: string;
  sessionId: string | null;
}
// The backend answered a fixed edit on a workflow with an effective session by
// asking for confirmation and writing nothing. This is the in-memory ask an
// upper layer must present to the user; nothing here is a durable draft.
export interface WorkflowConfigurationConfirmation {
  request: GraphConfigurationUpdateRequest;
}
export interface WorkflowConfigurationApplicationPorts {
  extensions: WorkflowConfigurationSubmissionPorts["extensions"];
  evidence?: RequestEvidencePorts;
  update?: (request: GraphConfigurationUpdateRequest) => Promise<GraphConfigurationResult>;
  readReceipt?: (request: GraphConfigurationUpdateRequest) => Promise<GraphConfigurationResult>;
}

const operation = "definition.update", ownerKind = "workflow-configuration";
const same = (left: unknown, right: unknown) => graphJsonSignature(left) === graphJsonSignature(right);
function failure(kind: "rejected" | "unknown" | "unavailable", message: string, code: string) {
  return new WorkbenchApiError(kind, message, undefined, code);
}

// This application is uninstalled; the production workbench still owns host 1.
export function createWorkflowConfigurationApplication(ports: WorkflowConfigurationApplicationPorts) {
  const evidence = ports.evidence ?? requestEvidencePorts;
  const update = ports.update ?? updateGraphConfiguration, readReceipt = ports.readReceipt ?? readGraphConfigurationReceipt;
  const documents = shallowReactive(new Map<string, GraphConfigurationDocument>());
  const selection = shallowRef<WorkflowConfigurationSelection | null>(null);
  let selectionSequence = 0;
  const current = computed(() => {
    const document = selection.value && documents.get(selection.value.workflowId);
    return document ? graphClone(document) : null;
  });
  const directory = computed(() => [...documents.values()].map(document => ({
    workflowId: document.workflow_definition_id, name: document.name, revision: document.revision,
    editSequence: document.edit_sequence, nodeCount: document.nodes.length,
  })));
  function observe(document: GraphConfigurationDocument) {
    if (!isGraphConfigurationDocument(document))
      throw failure("unavailable", "正式工作流配置格式无效", "graph_invalid_document");
    const prior = documents.get(document.workflow_definition_id);
    if (prior && prior.edit_sequence > document.edit_sequence) return;
    if (prior && prior.edit_sequence === document.edit_sequence && !same(prior, document))
      throw failure("unavailable", "同一配置序号的正式正文不同", "graph_configuration_basis_mismatch");
    documents.set(document.workflow_definition_id, graphClone(document));
  }
  function select(workflowId: string, sessionId: string | null = null) {
    if (!graphUuid(workflowId) || sessionId !== null && !graphUuid(sessionId))
      throw failure("rejected", "工作流或会话选择身份无效", "invalid_request");
    selectionSequence++;
    selection.value = { workflowId, sessionId };
  }
  function accept(result: GraphConfigurationResult, request: GraphConfigurationUpdateRequest,
    selectedAtSubmission: number | null = null) {
    if (!isGraphConfigurationResult(result, request))
      throw failure("unknown", "正式配置回执与原请求不匹配", "graph_configuration_receipt_invalid");
    // A confirmation question writes nothing: the proposed document is not the
    // formal current and the source workflow stays selected.
    if (result.outcome === "confirmation_required") return;
    observe(result.document);
    if (result.outcome === "derived" && selectedAtSubmission === selectionSequence
      && selection.value?.workflowId === request.workflow_definition_id)
      select(result.workflow_definition_id, null);
  }
  const entries = new Map<string, ReturnType<typeof createEntry>>();
  function createEntry(workflowId: string) {
    const pending = shallowRef<GraphConfigurationUpdateRequest | null>(null);
    const confirmation = shallowRef<WorkflowConfigurationConfirmation | null>(null);
    const lastResult = shallowRef<GraphConfigurationResult | null>(null);
    const error = ref(""), busy = ref(false), initialized = ref(false);
    const locked = computed(() => !initialized.value || busy.value || pending.value !== null);
    let record: RequestEvidenceRecord | null = null, initializing: Promise<boolean> | null = null;
    function checkedRecord(value: unknown): RequestEvidenceRecord {
      if (!isRequestEvidenceRecord(value) || value.owner_kind !== ownerKind || value.owner_id !== workflowId
        || value.operation !== operation || !isGraphConfigurationUpdateRequest(value.request)
        || value.request.workflow_definition_id !== workflowId)
        throw failure("unknown", "工作流原请求证据的身份或正文无效", "graph_configuration_evidence_invalid");
      return graphClone(value);
    }
    async function initialize(): Promise<boolean> {
      if (initialized.value) return true;
      if (initializing) return initializing;
      initializing = (async () => {
        try {
          const records = await evidence.read(ownerKind, workflowId);
          if (!Array.isArray(records) || !records.every(value => isRequestEvidenceRecord(value)
            && value.owner_kind === ownerKind && value.owner_id === workflowId)
            || records.filter(value => value.resolution === "pending").length > 1)
            throw failure("unknown", "工作流原请求证据无法核对", "graph_configuration_evidence_invalid");
          const durable = records.find(value => value.resolution === "pending");
          if (durable) {
            const captured = checkedRecord(durable);
            if (pending.value && !same(pending.value, captured.request))
              throw failure("unknown", "待核实证据与捕获原请求不同", "graph_configuration_evidence_conflict");
            record = captured;
            pending.value = graphClone(captured.request) as unknown as GraphConfigurationUpdateRequest;
          } else if (pending.value) {
            throw failure("unknown", "原请求证据写入仍待核实，业务未自动发送", "graph_configuration_evidence_unresolved");
          }
          initialized.value = true;
          error.value = "";
          return true;
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : "工作流原请求读取失败";
          throw cause;
        } finally { initializing = null; }
      })();
      return initializing;
    }
    async function resolveOriginal() {
      if (!record || !pending.value || !same(record.request, pending.value))
        throw failure("unknown", "原请求证据不完整，未结案", "graph_configuration_evidence_invalid");
      const resolved = checkedRecord(await evidence.resolve({ operation, idempotency_key: record.idempotency_key,
        request_digest: record.request_digest, resolution: "verified" }));
      if (resolved.request_digest !== record.request_digest || resolved.resolution !== "verified"
        || !same(resolved.request, record.request))
        throw failure("unknown", "原请求证据结案回执不同", "graph_configuration_evidence_invalid");
      record = null;
      pending.value = null;
    }
    async function finish(result: GraphConfigurationResult, request: GraphConfigurationUpdateRequest, selected: number) {
      if (!isGraphConfigurationResult(result, request))
        throw failure("unknown", "正式配置回执与原请求不匹配", "graph_configuration_receipt_invalid");
      lastResult.value = graphClone(result);
      try { accept(result, request, selected); }
      catch (cause) {
        error.value = `配置正式结果已确认，当前视图接纳失败：${cause instanceof Error ? cause.message : "接纳失败"}`;
        throw cause;
      }
      try { await resolveOriginal(); }
      catch (cause) {
        const verb = result.outcome === "derived" ? "派生"
          : result.outcome === "unchanged" ? "确认"
            : result.outcome === "confirmation_required" ? "请求确认" : "保存";
        error.value = `配置已正式${verb}，原请求证据结案失败：${
          cause instanceof Error ? cause.message : "持久化失败"}`;
        throw cause;
      }
      error.value = "";
      // Nothing was written for this outcome; expose the ask instead.
      if (result.outcome === "confirmation_required") confirmation.value = { request: graphClone(request) };
      return graphClone(result);
    }
    async function submit(request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult> {
      if (!isGraphConfigurationUpdateRequest(request) || request.workflow_definition_id !== workflowId)
        throw failure("rejected", "工作流配置原请求无效", "graph_invalid_document");
      const original = graphClone(request);
      await initialize();
      if (locked.value) throw failure("rejected", "工作流原请求尚未结束", "graph_configuration_pending");
      const prior = documents.get(workflowId);
      if (prior ? original.expected_edit_sequence !== prior.edit_sequence || original.document.revision !== prior.revision
        : original.expected_edit_sequence !== 0)
        throw failure("rejected", "配置提交依据已变化", "stale_edit_sequence");
      const selected = selectionSequence;
      // A submission supersedes any earlier unanswered question.
      confirmation.value = null;
      busy.value = true; error.value = ""; lastResult.value = null;
      try {
        try {
          record = checkedRecord(await evidence.write({ operation, idempotency_key: original.idempotency_key,
            owner_kind: ownerKind, owner_id: workflowId, request: { ...graphClone(original) } }));
          if (record.resolution !== "pending" || !same(record.request, original))
            throw failure("unknown", "保存的原请求证据不同，业务未提交", "graph_configuration_evidence_invalid");
        } catch (cause) {
          if (!(cause instanceof WorkbenchApiError && cause.kind === "rejected"
            && !["idempotency_conflict", "request_evidence_pending"].includes(cause.code ?? ""))) {
            pending.value = original;
            initialized.value = false;
          }
          error.value = `无法确认原请求证据，配置未发送：${cause instanceof Error ? cause.message : "持久化失败"}`;
          throw cause;
        }
        pending.value = graphClone(original);
        let result: GraphConfigurationResult;
        try { result = await update(graphClone(original)); }
        catch (cause) {
          error.value = cause instanceof Error ? cause.message : "配置提交失败";
          if (cause instanceof WorkbenchApiError && cause.kind === "rejected" && cause.code !== "idempotency_conflict") {
            try { await resolveOriginal(); }
            catch (resolutionFailure) {
              error.value += `；原请求证据结案失败：${resolutionFailure instanceof Error ? resolutionFailure.message : "持久化失败"}`;
              throw resolutionFailure;
            }
          }
          throw cause;
        }
        return await finish(result, original, selected);
      } catch (cause) {
        if (!error.value) error.value = cause instanceof Error ? cause.message : "配置回执接纳失败";
        throw cause;
      } finally { busy.value = false; }
    }
    function reject(): void {
      if (!confirmation.value)
        throw failure("rejected", "没有待确认的派生请求", "graph_configuration_confirmation_missing");
      // The user declined: nothing was written for the ask and nothing is written now.
      confirmation.value = null;
    }
    async function confirm(): Promise<GraphConfigurationResult> {
      const ask = confirmation.value;
      if (!ask) throw failure("rejected", "没有待确认的派生请求", "graph_configuration_confirmation_missing");
      // Consent is the only path to a derivation write; it uses a fresh
      // original request with its own evidence and idempotency key.
      const confirmed: GraphConfigurationUpdateRequest = { ...graphClone(ask.request),
        confirm_derivation: true, idempotency_key: crypto.randomUUID() };
      if (!isGraphConfigurationUpdateRequest(confirmed))
        throw failure("rejected", "确认请求格式无效", "graph_invalid_document");
      return submit(confirmed);
    }
    async function reconcile(): Promise<GraphConfigurationResult> {
      await initialize();
      if (busy.value || !pending.value)
        throw failure("rejected", "没有可核实的工作流原请求", "graph_configuration_receipt_missing");
      const original = graphClone(pending.value), selected = selectionSequence;
      busy.value = true; error.value = "";
      try { return await finish(await readReceipt(graphClone(original)), original, selected); }
      catch (cause) {
        if (!error.value) error.value = cause instanceof Error ? cause.message : "原配置回执核实失败";
        throw cause;
      } finally { busy.value = false; }
    }
    return { pending: readonly(pending), confirmation: readonly(confirmation), lastResult: readonly(lastResult),
      error: readonly(error), busy: readonly(busy), initialized: readonly(initialized), locked,
      initialize, submit, confirm, reject, reconcile };
  }
  function forWorkflow(workflowId: string) {
    if (!graphUuid(workflowId) || workflowId !== workflowId.toLowerCase())
      throw failure("rejected", "工作流配置身份无效", "invalid_request");
    let entry = entries.get(workflowId);
    if (!entry) { entry = createEntry(workflowId); entries.set(workflowId, entry); }
    return entry;
  }
  const sdk = createWorkflowConfigurationSdk({ document: () => current.value,
    locked: workflowId => forWorkflow(workflowId).locked.value, extensions: ports.extensions,
    submit: request => forWorkflow(request.workflow_definition_id).submit(request),
    accept: (result, request) => accept(result, request),
    confirmDerivation: workflowId => forWorkflow(workflowId).confirm(),
    declineDerivation: workflowId => forWorkflow(workflowId).reject() });
  return { current, directory, selection: computed(() => selection.value ? { ...selection.value } : null),
    document: (workflowId: string) => {
      const document = documents.get(workflowId);
      return document ? graphClone(document) : null;
    }, observe, select, forWorkflow, sdk };
}
