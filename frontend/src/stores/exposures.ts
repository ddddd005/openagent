import { computed, onScopeDispose, ref } from "vue";
import { defineStore } from "pinia";
import { AGENT_BINDINGS, WorkbenchApiError, workbenchRequest, type PublicSession } from "../adapters/workbenchApi";
import { clonePreparation } from "../domain/preparation";
import type { WorkflowEditGuard } from "../domain/workflowIdentity";
import {
  exposureSignature, isExposureConfiguration, isExposureMutation, isExposureRead, isExposureSnapshot,
  type ExposureConfiguration, type ExposureMutation, type ExposurePublication, type ExposureSnapshot,
} from "../domain/exposureConfiguration";
import {
  readPublicExposure,
  type ExposureField,
  type ExposureObservation,
  type ExposureRegistration,
} from "../domain/exposure";

export const useExposuresStore = defineStore("workbench-exposures", () => {
  const registrations = ref<ExposureRegistration[]>([]);
  const observations = ref<Record<string, ExposureObservation>>({});
  const publications = ref<ExposurePublication[]>([]);
  const pending = ref<ExposureMutation | null>(null);
  const busy = ref(false);
  const error = ref<WorkbenchApiError | null>(null);
  const locked = computed(() => busy.value || !!pending.value);
  let guard: (() => boolean) | null = null;
  let editGuard: WorkflowEditGuard | null = null;
  function setEditGuard(value: WorkflowEditGuard) { editGuard = value; }
  function cloneWorkflow(source: string, target: string) {
    registrations.value.push(...clonePreparation(rowsFor(source)).map(row => ({
      ...row, id: crypto.randomUUID(), workflowId: target,
    })));
  }
  function restoreWorkflowRegistrations(workflowId: string, values: ExposureRegistration[]) {
    mutationGeneration++;
    busy.value = false;
    registrations.value = [...registrations.value.filter(row => row.workflowId !== workflowId),
      ...clonePreparation(values).map(row => ({ ...row, workflowId }))];
    clearObservations();
  }
  function removeWorkflow(workflowId: string) {
    restoreWorkflowRegistrations(workflowId, []);
    publications.value = publications.value.filter(row => row.workflowId !== workflowId);
  }
  let observationGeneration = 0;
  let mutationGeneration = 0;
  let alive = true;
  const controller = new AbortController();
  onScopeDispose(() => { alive = false; observationGeneration++; mutationGeneration++; controller.abort(); });
  const rowsFor = (id: string) => registrations.value.filter((row) => row.workflowId === id);
  function referenceFor(id: string) {
    const publication = publications.value.find((row) => row.workflowId === id);
    return publication && !pending.value && publication.signature === exposureSignature(rowsFor(id))
      ? { config_id: publication.config_id, revision: publication.revision } : null;
  }

  function register(
    workflowId: string,
    stage: "A" | "B",
    publicName: string,
    kind: "state" | "result",
    fields: ExposureField[],
  ) {
    if (!publicName.trim()) throw new Error("公开名称不能为空");
    const allowed: ExposureField[] = kind === "state"
      ? ["status", "run_id", "revision", "budget"] : stage === "A" ? ["result_text", "delivered_text"] : ["delivered_text"];
    const safeFields = [...new Set(fields)].filter((field) => allowed.includes(field));
    if (!safeFields.length) throw new Error("至少选择一个公开字段");
    if (registrations.value.some((row) => row.workflowId === workflowId
      && row.publicName === publicName.trim())) throw new Error("公开名称已注册");
    const descriptor: ExposureRegistration = {
      schemaVersion: 1, id: crypto.randomUUID(), workflowId, stage,
      nodeBindingId: AGENT_BINDINGS[stage], publicName: publicName.trim(), kind,
      type: kind === "state" ? "public_agent_state" : stage === "A" ? "public_node_result" : "delivered_workflow_result",
      fields: safeFields,
    };
    if (editGuard && !editGuard(workflowId, id => register(id, stage, publicName, kind, fields))) return descriptor;
    registrations.value.push(descriptor);
    clearObservations();
    return descriptor;
  }

  function remove(id: string) {
    const row = registrations.value.find(row => row.id === id);
    if (row && editGuard && !editGuard(row.workflowId, target => {
      const copied = rowsFor(target).find(candidate => candidate.publicName === row.publicName);
      if (copied) remove(copied.id);
    })) return;
    registrations.value = registrations.value.filter((row) => row.id !== id);
    clearObservations();
  }

  function clearObservations() {
    observationGeneration++;
    observations.value = {};
  }

  function observe(workflowId: string, view: PublicSession) {
    observations.value = Object.fromEntries(
      registrations.value.filter((row) => row.workflowId === workflowId)
        .map((row) => [row.id, readPublicExposure(row, view)]),
    );
  }
  function restoreRegistrations(values: ExposureRegistration[]) {
    mutationGeneration++;
    busy.value = false;
    registrations.value = structuredClone(values);
    clearObservations();
  }
  function setPersistenceGuard(value: (() => boolean) | null) { guard = value; }
  async function dispatch(command: ExposureMutation) {
    if (!isExposureMutation(command)) throw new WorkbenchApiError("unavailable", "注册声明字段无效或超过容量");
    const generation = mutationGeneration;
    const fingerprint = JSON.stringify(command);
    const current = () => alive && generation === mutationGeneration && JSON.stringify(pending.value) === fingerprint;
    pending.value = clonePreparation(command);
    clearObservations();
    try {
      if (!guard || !guard()) throw new Error("persistence_failed");
    } catch {
      if (current()) pending.value = null;
      throw new WorkbenchApiError("unavailable", "原注册请求无法保存，未提交");
    }
    if (!current()) return;
    function clearPending() {
      pending.value = null;
      try {
        if (!guard!()) throw new Error("persistence_failed");
      } catch {
        if (alive && generation === mutationGeneration && pending.value === null)
          pending.value = clonePreparation(command);
        throw new WorkbenchApiError("unknown", "原注册请求的本机结算保存失败，保留请求等待核实");
      }
      return alive && generation === mutationGeneration && pending.value === null;
    }
    let record: ExposureConfiguration;
    try {
      const response = await workbenchRequest<unknown>("/api/exposure-configurations", {
        body: command, signal: controller.signal,
      });
      if (!current()) return;
      if (!isExposureConfiguration(response)
        || exposureSignature(response) !== exposureSignature(command.record))
        throw new WorkbenchApiError("unknown", "注册回执不匹配，保留原请求等待核实");
      record = response;
    } catch (failure) {
      if (!current()) return;
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected" && failure.code === "idempotency_conflict")
        throw new WorkbenchApiError("unknown", "原注册请求身份冲突，无法证明未发生，保留原请求等待核实", failure.status, failure.code);
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected" && !clearPending()) return;
      throw failure;
    }
    publications.value = [...publications.value.filter(row => row.workflowId !== record.workflow_id),
      { workflowId: record.workflow_id, config_id: record.config_id,
      revision: record.revision, signature: exposureSignature(record.registrations) }];
    if (!clearPending()) return;
    return record;
  }
  function issue(failure: unknown) {
    error.value = failure instanceof WorkbenchApiError ? failure
      : new WorkbenchApiError("unavailable", "注册声明不可用");
  }
  async function publish(id: string) {
    if (locked.value) return false;
    busy.value = true;
    error.value = null;
    const generation = mutationGeneration;
    const previous = publications.value.find((row) => row.workflowId === id);
    try {
      const result = await dispatch({
        record: { schema_version: 1, kind: "workflow_exposure_configuration",
          config_id: previous?.config_id ?? crypto.randomUUID(), workflow_id: id,
          revision: (previous?.revision ?? 0) + 1, registrations: clonePreparation(rowsFor(id)) },
        expected_revision: previous?.revision ?? 0, idempotency_key: crypto.randomUUID(),
      });
      return result !== undefined;
    } catch (failure) { if (alive && generation === mutationGeneration) issue(failure); return false; }
    finally { if (alive && generation === mutationGeneration) busy.value = false; }
  }
  async function reconcile() {
    if (!pending.value || busy.value) return;
    issue(new WorkbenchApiError("unknown", "旧注册请求缺少可核实的原应用身份，保留原请求且不重新提交"));
  }
  async function observePublished(id: string, view: PublicSession) {
    const reference = referenceFor(id);
    if (!reference) return;
    const generation = ++observationGeneration;
    observations.value = {};
    try {
      const response = await workbenchRequest<unknown>(
        `/api/sessions/${view.workflow_session_id}/exposures/${reference.config_id}/revisions/${reference.revision}`,
        { signal: controller.signal },
      );
      if (!alive || generation !== observationGeneration) return;
      if (!isExposureRead(response, reference, view))
        throw new WorkbenchApiError("unavailable", "注册读取的身份、字段或会话版本不匹配");
      if (response.availability === "unavailable")
        throw new WorkbenchApiError("unavailable", "注册声明版本已变化，旧引用已不可用");
      observations.value = Object.fromEntries(response.observations.map((row) => [row.registrationId, row]));
    } catch (failure) { if (alive && generation === observationGeneration) { observations.value = {}; issue(failure); } }
  }
  function exportConfiguration(): ExposureSnapshot {
    return clonePreparation({ schemaVersion: 1, publications: publications.value, pending: pending.value });
  }
  function restoreConfiguration(value: unknown) {
    if (!isExposureSnapshot(value)) return false;
    mutationGeneration++;
    busy.value = false;
    publications.value = clonePreparation(value.publications);
    pending.value = clonePreparation(value.pending);
    clearObservations();
    return true;
  }
  return { registrations, observations, publications, pending, busy, locked, error, register, remove,
    clearObservations, observe, observePublished, restoreRegistrations, referenceFor, publish, reconcile,
    setPersistenceGuard, exportConfiguration, restoreConfiguration, setEditGuard, cloneWorkflow,
    restoreWorkflowRegistrations, removeWorkflow };
});
