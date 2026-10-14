import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { readLegacyWorkbenchEvidence, type LegacyPendingCommand } from "../adapters/workbenchPersistence";
import { isLocalDraftRecord, localDraftPorts, workbenchDraftAddress,
  type LocalDraftPorts } from "../adapters/localDraftsApi";
import { readGraphReceipt } from "../adapters/workflowApplicationApi";
import { readGraphDefinition } from "../adapters/workflowGraphApi";
import { readGraphCommandReceipt, type GraphReceipt } from "../application/workflowCommands";
import { graphJsonSignature } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkflowGraphStore } from "./workflowGraph";

const retired = "整图及表单编辑草稿持久化已退役；正式配置保存暂不可用";
function receiptSummary(receipt: GraphReceipt) {
  if (receipt.kind === "definition") return `原定义回执已匹配，修订 ${receipt.document.revision}`;
  if (receipt.kind === "management") return receipt.result.deleted ? "原归档/删除回执已匹配" : "原管理回执已匹配";
  return `原会话回执已匹配，${receipt.session.workflow_session_id}`;
}

export const useWorkbenchPersistenceStore = defineStore("workbench-persistence", () => {
  const workspace = useWorkspaceStore();
  const graph = useWorkflowGraphStore();
  const status = ref<"loading" | "ready" | "saved" | "pending" | "blocked">("loading");
  const error = ref<string | null>(null);
  const savedAt = ref<string | null>(null);
  const ready = ref(false);
  const conflictCopyKey = ref<string | null>(null);
  const pendingCopy = computed(() => workspace.workflows.find(row => row.copyPending) ?? null);
  const legacyRequests = ref<LegacyPendingCommand[]>([]);
  const unreadableLegacyRequests = ref(0);
  const unreadableLegacyRecord = ref(false);
  const preferredActiveWorkflowId = ref<string | null>(null);
  const preferredSelectedWorkflowId = ref<string | null>(null);
  const hasUnsaved = computed(() => false);
  let initialization: Promise<void> | null = null;

  async function initialize(_target?: unknown, ports: Pick<LocalDraftPorts, "read"> = localDraftPorts): Promise<void> {
    if (initialization) return initialization;
    initialization = (async () => {
      status.value = "loading";
      try {
        const record = await ports.read(workbenchDraftAddress);
        if (record && !record.deleted) {
          if (!isLocalDraftRecord(record) || record.format_version !== 1
            || graphJsonSignature(record.address) !== graphJsonSignature(workbenchDraftAddress))
            throw new Error("旧工作台原请求记录身份无效；未改写原记录");
          const evidence = readLegacyWorkbenchEvidence(record.value);
          legacyRequests.value = evidence.pending;
          unreadableLegacyRequests.value = evidence.unreadablePending;
          unreadableLegacyRecord.value = evidence.unreadableRecord;
          preferredActiveWorkflowId.value = evidence.activeWorkflowId;
          preferredSelectedWorkflowId.value = evidence.selectedWorkflowId;
          if (evidence.unreadablePending)
            error.value = `${evidence.unreadablePending} 条旧原请求无法安全解析；原记录保留，未重发`;
        }
      } catch (failure) {
        error.value = failure instanceof Error
          ? `旧工作台原请求读取失败；未恢复编辑草稿或发送请求：${failure.message}`
          : "旧工作台原请求读取失败；未恢复编辑草稿或发送请求";
      } finally {
        status.value = "ready";
        ready.value = true;
      }
    })();
    return initialization;
  }

  function selectLoadedWorkflow() {
    const rows = workspace.workflows.filter(row => !row.copyPending);
    if (!rows.length) return false;
    const active = rows.find(row => row.id === preferredActiveWorkflowId.value)
      ?? rows.find(row => !row.archived && !row.deleted) ?? rows[0]!;
    if (workspace.activeWorkflowId !== active.id) workspace.openWorkflow(active.id);
    const selected = rows.find(row => row.id === preferredSelectedWorkflowId.value) ?? active;
    workspace.selectWorkflow(selected.id);
    return true;
  }

  async function save(): Promise<boolean> {
    status.value = "blocked";
    error.value = retired;
    return false;
  }
  async function flushAll(): Promise<boolean> {
    error.value = retired;
    return false;
  }
  async function saveWorkflow(): Promise<boolean> { return await save(); }
  async function discardDraft(): Promise<boolean> {
    graph.discardDraft();
    return true;
  }
  async function reconcileCopy() {
    if (pendingCopy.value) await graph.reconcile(pendingCopy.value.id);
  }

  async function verifyLegacyRequest(key: string) {
    const request = legacyRequests.value.find(row => row.key === key);
    if (!request || request.status === "checking") return false;
    request.status = "checking";
    request.error = undefined;
    try {
      const receipt = await readGraphCommandReceipt(request.command, request.basis, {
        readReceipt: readGraphReceipt,
        readDefinition: readGraphDefinition,
      });
      request.status = "verified";
      request.result = receiptSummary(receipt);
      return true;
    } catch (failure) {
      request.status = "unknown";
      request.error = failure instanceof Error ? failure.message : "原请求仍待核实；原记录保留";
      return false;
    }
  }

  return {
    status, error, savedAt, ready, hasUnsaved, conflictCopyKey, pendingCopy, legacyRequests,
    unreadableLegacyRequests, unreadableLegacyRecord,
    preferredActiveWorkflowId, preferredSelectedWorkflowId,
    initialize, selectLoadedWorkflow, verifyLegacyRequest,
    save, flushAll, saveWorkflow, discardDraft, reconcileCopy,
  };
});
