import { computed, ref } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { isCredentialRequest, isProviderCredentialRequest, providerConfigurationPorts,
  type CredentialRequest, type CredentialStatus, type ProviderCredentialRequest,
  type ProviderCredentialResult } from "../adapters/providerConfigurationApi";
import { readCurrentProvider } from "../adapters/workflowResourcesApi";
import type { RequestEvidenceRecord } from "../adapters/requestEvidenceApi";
import { graphClone, graphJsonSignature, graphObject } from "../domain/workflowGraph";
import { type CredentialReference, type CurrentProvider, type ProviderIdentity } from "../domain/workflowModelResources";

export interface ProviderCredentialBasis {
  identity: ProviderIdentity; expected_sequence: number; provider_seed: ProviderCredentialRequest["provider_seed"];
}
export interface CredentialIntent {
  schema_version: 2; kind: "save" | "bind" | "delete";
  request: ProviderCredentialRequest | CredentialRequest;
  basis_reference: CredentialReference | null;
}
type CredentialMutationIntent = CredentialIntent & { kind: "save" | "delete" };
interface IntentChannel {
  read(refresh?: boolean): Promise<RequestEvidenceRecord | null>;
  write(value: CredentialIntent | null, formatVersion?: number): Promise<RequestEvidenceRecord>;
  archive?(value: CredentialIntent): Promise<void>;
}
const defaultPorts = { ...providerConfigurationPorts, provider: readCurrentProvider };
function validIntent(value: unknown): value is CredentialIntent {
  if (!graphObject(value) || Object.keys(value).length !== 4 || value.schema_version !== 2
    || !["save", "bind", "delete"].includes(String(value.kind))
    || value.basis_reference !== null && typeof value.basis_reference !== "string") return false;
  if (value.kind === "delete") return isCredentialRequest(value.request);
  return isProviderCredentialRequest(value.request)
    && (value.kind === "save" || value.request.expected_generation > 0);
}
function evidenceIntent(stored: RequestEvidenceRecord, reference: CredentialReference | null): CredentialIntent {
  const intent: CredentialIntent = { schema_version: 2,
    kind: stored.operation === "credential.delete" ? "delete"
      : stored.operation === "provider.credential.save" ? "save" : "bind",
    request: graphClone(stored.request) as unknown as ProviderCredentialRequest | CredentialRequest,
    basis_reference: reference };
  if (!validIntent(intent) || !/^[0-9a-f]{64}$/.test(stored.request_digest)
    || stored.idempotency_key !== intent.request.idempotency_key
    || !["credential.delete", "provider.credential.save", "provider.credential.bind"].includes(stored.operation)
    || stored.resolution !== "pending")
    throw new Error("原 Key 生效请求格式无法读取，未覆盖");
  return intent;
}
const resultReason: Record<string, string> = {
  provider_binding_conflict: "Key 已保存，但供应商配置写入冲突，保存未完成；原请求仍保留。",
  provider_binding_changed: "原绑定已核实，但供应商后来发生变化；请重新读取供应商。",
  provider_credential_changed: "原 Key 请求已核实，但 Key 随后被修改或删除；不能继续使用旧代次。",
  provider_binding_result_unknown: "Key 已保存，供应商绑定结果尚未确认；先核实原请求，未重新提交。",
  provider_credential_result_unknown: "Key 生效结果尚未确认；先核实原请求，未重新提交。",
  provider_credential_request_missing: "后端未找到原 Key 生效记录；保留原请求，可归档后解除本机锁定。",
  provider_binding_not_applied: "Key 已保存，但供应商配置尚未写入，保存未完成；请核实原请求。",
  credential_receipt_missing: "原 Key 的权威回执缺失，不能确认保存成功；保留原请求，核实私有库或归档后重新配置。",
  provider_binding_receipt_missing: "原供应商绑定的权威回执缺失，不能确认生效；保留原请求，核实或归档后重新配置。",
};
export function createProviderCredentialEditor(channel: IntentChannel,
  saved: (reference: CredentialReference, provider: CurrentProvider, request: ProviderCredentialRequest) => void | Promise<void>,
  ports = defaultPorts) {
  const secret = ref(""), busy = ref(false), ready = ref(false), available = ref(false), error = ref("");
  const state = ref<CredentialStatus | null>(null), pending = ref<CredentialIntent | null>(null);
  const outcome = ref<ProviderCredentialResult | null>(null);
  const blocked = computed(() => busy.value || !ready.value || !!pending.value);
  let generation = 0, reference: CredentialReference | null = null, basis: ProviderCredentialBasis | null = null;
  let evidence: RequestEvidenceRecord | null = null;
  async function load(next: CredentialReference | null, nextBasis: ProviderCredentialBasis) {
    reference = next; basis = graphClone(nextBasis);
    const token = ++generation;
    secret.value = ""; state.value = null; pending.value = null; evidence = null;
    outcome.value = null; ready.value = false; busy.value = false;
    error.value = "";
    try {
      const protection = await ports.protection();
      if (token !== generation) return;
      available.value = protection;
      if (next) {
        const status = await ports.status(next);
        if (token !== generation) return;
        state.value = status;
      }
      const stored = await channel.read(true);
      if (token !== generation) return;
      if (stored) {
        pending.value = evidenceIntent(stored, next); evidence = graphClone(stored);
        error.value = "原 Key 生效请求待核实，Key 未重发";
      }
      ready.value = true;
    } catch (failure) {
      if (token === generation) error.value = failure instanceof Error ? failure.message : "凭据状态读取失败";
    }
  }
  async function current(operation: CredentialIntent, token: number) {
    if (token !== generation || graphJsonSignature(pending.value) !== graphJsonSignature(operation)) return false;
    const stored = await channel.read(true);
    return token === generation && stored !== null && evidence !== null
      && stored.operation === evidence.operation && stored.idempotency_key === evidence.idempotency_key
      && stored.request_digest === evidence.request_digest
      && graphJsonSignature(stored.request) === graphJsonSignature(operation.request);
  }
  async function accept(result: ProviderCredentialResult | CredentialStatus, operation: CredentialIntent, token: number) {
    if (!await current(operation, token)) return;
    if (operation.kind === "delete") {
      const observed = await ports.status(operation.request.credential_ref);
      if (token !== generation) return;
      if (observed.generation < (result as CredentialStatus).generation)
        throw new WorkbenchApiError("unknown", "当前凭据状态落后于原删除回执，保留原请求");
      state.value = observed;
      await channel.write(null, 2);
      if (token !== generation) return;
      pending.value = null;
      error.value = observed.state === "missing" ? "Key 已删除；请在模型供应商中保存新 Key 后再运行"
        : "原删除请求已核实，但 Key 已被其他页面更新";
      return;
    }
    const binding = result as ProviderCredentialResult;
    outcome.value = binding;
    if (binding.credential) state.value = binding.credential;
    if (binding.outcome !== "saved") {
      error.value = resultReason[binding.reason_code] ?? "Key 生效步骤未完成，请核实原请求或归档解除锁定";
      return;
    }
    const request = operation.request as ProviderCredentialRequest;
    const observed = await ports.status(request.credential_ref);
    const provider = await ports.provider(request.identity);
    if (token !== generation) return;
    if (observed.generation !== binding.credential!.generation || observed.state !== "saved"
      || !provider || provider.update_sequence < binding.provider!.update_sequence
      || provider.value.credential_ref !== request.credential_ref
      || request.provider_seed !== null && graphJsonSignature(provider.value)
        !== graphJsonSignature({ ...request.provider_seed, credential_ref: request.credential_ref })) {
      outcome.value = { ...binding, outcome: "partial", reason_code: "provider_binding_changed" };
      error.value = "原请求已核实，但当前 Key 或供应商引用发生变化，请重新读取后处理";
      return;
    }
    if (!await current(operation, token)) return;
    state.value = observed;
    await saved(request.credential_ref, provider, request);
    if (!await current(operation, token)) return;
    await channel.write(null, 2);
    if (token !== generation) return;
    pending.value = null;
    if (token === generation) error.value = "供应商配置与 Key 已保存";
  }
  async function dispatch(operation: CredentialMutationIntent, input = "") {
    const token = generation;
    busy.value = true; error.value = ""; outcome.value = null;
    pending.value = operation; evidence = null;
    try {
      const stored = await channel.write(operation, 2);
      if (token !== generation) return;
      pending.value = operation; evidence = graphClone(stored);
      if (stored.operation !== (operation.kind === "delete" ? "credential.delete" : "provider.credential.save")
        || stored.idempotency_key !== operation.request.idempotency_key || stored.resolution !== "pending"
        || !/^[0-9a-f]{64}$/.test(stored.request_digest)
        || graphJsonSignature(stored.request) !== graphJsonSignature(operation.request)
        || !await current(operation, token))
        throw new WorkbenchApiError("unknown", "原 Key 请求证据未准确落盘，Key 未发送");
      const result = operation.kind === "delete" ? await ports.delete(operation.request as CredentialRequest, stored.request_digest)
        : await ports.saveBound(operation.request as ProviderCredentialRequest, input, stored.request_digest);
      if (token === generation) { secret.value = ""; await accept(result, operation, token); }
    } catch (failure) {
      if (token !== generation) return;
      secret.value = "";
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && failure.code === "request_evidence_pending" && evidence === null) {
        pending.value = null; ready.value = false;
        try {
          const stored = await channel.read(true);
          if (!stored) throw new Error("另一页面原 Key 请求证据无法读取，未覆盖");
          if (token !== generation) return;
          pending.value = evidenceIntent(stored, reference); evidence = graphClone(stored); ready.value = true;
          error.value = `另一页面原 Key 请求待核实 [${stored.idempotency_key}]；新 Key 未发送`;
        } catch (readFailure) {
          if (token === generation) error.value = readFailure instanceof Error ? readFailure.message : "原 Key 请求证据读取失败";
        }
        return;
      }
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected"
        && !["idempotency_conflict", "request_evidence_pending"].includes(failure.code ?? "")) {
        if (evidence === null) {
          pending.value = null;
          error.value = failure.message;
          return;
        }
        if (await current(operation, token)) {
          try { await channel.write(null, 2); if (token === generation) pending.value = null; }
          catch { error.value = "拒绝结果尚未确认落盘，保留原请求"; return; }
        }
      }
      error.value = failure instanceof Error ? failure.message : "Key 生效结果待核实";
    } finally { if (token === generation) busy.value = false; }
  }
  async function save(remove = false) {
    if (blocked.value || !available.value || !basis || !remove && !secret.value) return;
    if (remove && !reference?.startsWith("local:")) return;
    const target = reference?.startsWith("local:") ? reference as `local:${string}` : `local:${crypto.randomUUID()}` as const;
    const key: CredentialRequest = { credential_ref: target,
      expected_generation: target === state.value?.credential_ref ? state.value.generation : 0,
      idempotency_key: crypto.randomUUID() };
    const operation: CredentialMutationIntent = { schema_version: 2, kind: remove ? "delete" : "save",
      request: remove ? key : { ...key, ...graphClone(basis) }, basis_reference: reference };
    if (!validIntent(operation)) { error.value = "供应商字段尚未填写完整，Key 未发送"; return; }
    return dispatch(operation, secret.value);
  }
  async function reconcile() {
    const operation = pending.value;
    if (!operation || busy.value) return;
    const token = generation;
    busy.value = true;
    try {
      if (!evidence) {
        const stored = await channel.read(true);
        if (!stored || stored.operation !== (operation.kind === "delete" ? "credential.delete" : "provider.credential.save")
          || stored.idempotency_key !== operation.request.idempotency_key
          || !/^[0-9a-f]{64}$/.test(stored.request_digest)
          || graphJsonSignature(stored.request) !== graphJsonSignature(operation.request))
          throw new WorkbenchApiError("unknown", "准确原 Key 请求证据仍无法核实，未重新提交");
        if (token !== generation) return;
        evidence = graphClone(stored);
      }
      const result = operation.kind === "delete" ? await ports.receipt(operation.request as CredentialRequest)
        : await ports.boundReceipt(operation.request as ProviderCredentialRequest, operation.kind === "bind");
      if (token === generation) await accept(result, operation, token);
    } catch (failure) {
      if (token === generation) error.value = failure instanceof Error ? failure.message : "原 Key 生效请求仍待核实";
    } finally { if (token === generation) busy.value = false; }
  }
  async function archive(operation: CredentialIntent, token: number) {
    if (!channel.archive || !await current(operation, token)) throw new Error("原请求无法确认，未清理");
    await channel.archive(graphClone(operation));
    if (!await current(operation, token)) throw new Error("归档后原请求已变化，未清理");
  }
  async function abandon(confirmed: boolean) {
    if (!confirmed || busy.value || !pending.value) return;
    const token = generation, original = graphClone(pending.value);
    busy.value = true;
    try {
      await archive(original, token);
      await channel.write(null, 2);
      if (token !== generation) return;
      pending.value = null; outcome.value = null;
      error.value = "原 Key 请求已归档并解除锁定；不表示撤销远端效果，未重新提交";
    } catch (failure) {
      if (token === generation) error.value = failure instanceof Error ? failure.message : "归档失败，原请求仍保留";
    } finally { if (token === generation) busy.value = false; }
  }
  function dispose() { generation++; secret.value = ""; }
  function adoptSavedBasis(next: CredentialReference | null, nextBasis: ProviderCredentialBasis) {
    const request = pending.value?.request;
    if (!busy.value || pending.value?.kind === "delete" || !request || !isProviderCredentialRequest(request)
      || outcome.value?.outcome !== "saved" || next !== request.credential_ref
      || graphJsonSignature(nextBasis.identity) !== graphJsonSignature(request.identity)
      || nextBasis.expected_sequence !== outcome.value.provider?.update_sequence
      || request.provider_seed !== null
        && graphJsonSignature(nextBasis.provider_seed) !== graphJsonSignature(request.provider_seed)) return false;
    reference = next;
    basis = graphClone(nextBasis);
    return true;
  }
  function updateDraftBasis(next: CredentialReference | null, nextBasis: ProviderCredentialBasis) {
    if (!basis || busy.value || pending.value || next !== reference
      || graphJsonSignature(nextBasis.identity) !== graphJsonSignature(basis.identity)
      || nextBasis.expected_sequence !== basis.expected_sequence) return false;
    basis = graphClone(nextBasis);
    return true;
  }
  return { secret, busy, ready, available, error, state, pending, outcome, blocked,
    load, save, reconcile, abandon, dispose, adoptSavedBasis, updateDraftBasis };
}
