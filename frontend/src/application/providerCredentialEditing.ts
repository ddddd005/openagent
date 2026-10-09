import { computed, ref } from "vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { isCredentialRequest, providerConfigurationPorts, type CredentialRequest, type CredentialStatus } from "../adapters/providerConfigurationApi";
import type { LocalDraftRecord } from "../adapters/localDraftsApi";
import { graphObject } from "../domain/workflowGraph";
import type { CredentialReference } from "../domain/workflowModelResources";

interface CredentialIntent {
  request: CredentialRequest;
  remove: boolean;
  basis_reference: CredentialReference | null;
}
interface IntentChannel {
  read(refresh?: boolean): Promise<LocalDraftRecord | null>;
  write(value: unknown): Promise<LocalDraftRecord>;
}
export function createProviderCredentialEditor(channel: IntentChannel,
  saved: (reference: CredentialReference) => void, ports = providerConfigurationPorts) {
  const secret = ref(""), busy = ref(false), ready = ref(false), available = ref(false), error = ref("");
  const state = ref<CredentialStatus | null>(null), pending = ref<CredentialIntent | null>(null);
  const blocked = computed(() => busy.value || !ready.value || !!pending.value);
  let generation = 0, reference: CredentialReference | null = null;
  async function load(next: CredentialReference | null) {
    reference = next;
    const token = ++generation;
    secret.value = ""; state.value = null; pending.value = null; ready.value = false; busy.value = false;
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
      if (stored && !stored.deleted) {
        const value = stored.value;
        if (stored.format_version !== 1 || !graphObject(value) || Object.keys(value).length !== 3
          || !isCredentialRequest(value.request) || typeof value.remove !== "boolean"
          || value.basis_reference !== null && value.basis_reference !== "env:DEEPSEEK_API_KEY"
            && value.basis_reference !== "env:GEMINI_API_KEY"
            && !(typeof value.basis_reference === "string" && isCredentialRequest({
              ...value.request, credential_ref: value.basis_reference })))
          throw new Error("凭据原请求记录无法安全读取，未覆盖");
        pending.value = value as unknown as CredentialIntent;
        error.value = "原凭据请求待核实，Key 未重发";
      }
      ready.value = true;
    } catch (failure) {
      if (token === generation) error.value = failure instanceof Error ? failure.message : "凭据状态读取失败";
    }
  }
  async function accept(result: CredentialStatus, operation: CredentialIntent, token: number) {
    // A receipt proves this request, not the present generation after another tab's mutation.
    const current = await ports.status(operation.request.credential_ref);
    if (token !== generation) return;
    if (current.generation < result.generation)
      throw new WorkbenchApiError("unknown", "当前凭据状态落后于原回执，保留原请求");
    state.value = current;
    await channel.write(null);
    if (token !== generation) return;
    pending.value = null;
    if (current.state === "saved" && reference === operation.basis_reference)
      saved(operation.request.credential_ref);
    error.value = current.generation !== result.generation
      ? "原请求已核实，但凭据已被其他页面更新；当前状态已重新读取"
      : current.state === "saved"
        ? reference === operation.basis_reference ? "Key 已保存；供应商绑定仍须保存当前资源"
          : "Key 已保存，但当前引用已变化，未绑定到当前表单"
        : "Key 已删除，供应商引用已失效";
  }
  async function save(remove = false) {
    if (blocked.value || !available.value || !remove && !secret.value) return;
    if (remove && !reference?.startsWith("local:")) return;
    const token = generation;
    const target = reference?.startsWith("local:") ? reference as `local:${string}` : `local:${crypto.randomUUID()}` as const;
    const operation: CredentialIntent = { request: { credential_ref: target,
      expected_generation: target === state.value?.credential_ref ? state.value.generation : 0,
      idempotency_key: crypto.randomUUID() }, remove, basis_reference: reference };
    const input = secret.value;
    busy.value = true; error.value = "";
    try {
      await channel.write(operation);
      if (token !== generation) return;
      pending.value = operation;
      const result = await ports.save(operation.request, input, remove);
      if (token !== generation) return;
      secret.value = "";
      await accept(result, operation, token);
    } catch (failure) {
      if (token !== generation) return;
      secret.value = "";
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected" && failure.code !== "idempotency_conflict") {
        try {
          const current = await ports.status(target);
          if (token !== generation) return;
          await channel.write(null);
          if (token !== generation) return;
          pending.value = null; state.value = current;
        } catch { error.value = "拒绝结果尚未确认落盘，保留原请求"; return; }
      }
      error.value = failure instanceof Error ? failure.message : "Key 保存结果待核实";
    } finally { if (token === generation) busy.value = false; }
  }
  async function reconcile() {
    const operation = pending.value;
    if (!operation || busy.value) return;
    const token = generation;
    busy.value = true;
    try {
      const result = await ports.receipt(operation.request, operation.remove);
      if (token === generation) await accept(result, operation, token);
    } catch (failure) {
      if (token === generation) error.value = failure instanceof Error ? failure.message : "原 Key 请求仍待核实";
    } finally { if (token === generation) busy.value = false; }
  }
  function dispose() { generation++; secret.value = ""; }
  return { secret, busy, ready, available, error, state, pending, blocked, load, save, reconcile, dispose };
}
