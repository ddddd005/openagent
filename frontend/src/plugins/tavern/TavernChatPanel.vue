<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { Plus } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { createTavernDemo } from "./tavernDemo";
import { useProviderResources } from "../../application/workflowResources";
import { useModelProfiles } from "../../application/modelProfiles";
import { isModelProfileIdentity, isProviderIdentity, providerIdentity, sameProviderIdentity,
  type ModelProfileIdentity, type ProviderProtocol, type ProviderIdentity } from "../../domain/workflowModelResources";
import { graphClone, graphObject, graphUuid } from "../../domain/workflowGraph";

const graph = useWorkflowGraphStore(), error = ref("");
const resources = useProviderResources();
const profiles = useModelProfiles();
const protocol = ref<ProviderProtocol>("chat"), selected = ref(""), busy = ref(false);
interface LegacyPreset {
  identity: ModelProfileIdentity;
  provider: ProviderIdentity;
  protocol: ProviderProtocol;
  modelLabel: string;
  title: string;
}
function legacyPreset(value: unknown): LegacyPreset | null {
  if (!graphObject(value) || value.envelope_version !== 1 || value.data_schema_version !== 1
    || value.type_id !== "workflow.model-profile" || typeof value.scope !== "string"
    || !value.scope.trim() || !graphUuid(value.resource_id) || !Number.isSafeInteger(value.update_sequence)
    || !graphObject(value.value) || typeof value.value.name !== "string"
    || value.value.enabled !== true || !["chat", "gemini"].includes(String(value.value.protocol))
    || !isProviderIdentity(value.value.provider) || !graphObject(value.value.parameters)
    || typeof value.value.parameters.model !== "string" || !value.value.parameters.model.trim()) return null;
  const identity = { envelope_version: 1 as const, scope: value.scope, type_id: "workflow.model-profile" as const,
    resource_id: value.resource_id };
  if (!isModelProfileIdentity(identity)) return null;
  return { identity, provider: value.value.provider, protocol: value.value.protocol as ProviderProtocol,
    modelLabel: value.value.parameters.model, title: value.value.name };
}
const configurations = computed(() => profiles.records.value.flatMap(config => {
  const legacy = legacyPreset(config);
  if (!legacy || legacy.protocol !== protocol.value) return [];
  const provider = resources.records.value.find(row => sameProviderIdentity(providerIdentity(row), legacy.provider));
  if (!provider?.value.enabled || !provider.value.credential_ref || provider.value.protocol !== protocol.value) return [];
  return [{ id: JSON.stringify([legacy.identity.scope, legacy.identity.resource_id]), ...legacy, provider }];
}));
const requiredOldVersions = computed(() => [
  ["models.source", protocol.value === "gemini" ? "6" : "5"],
  ["agents.execute", protocol.value === "gemini" ? "8" : "4"],
  ["context.merge", "4"],
]);
const missingOldNodes = computed(() => requiredOldVersions.value.filter(([component, version]) =>
  !graph.catalog.some(row => row.executable && row.component_id === component && row.component_version === version))
  .map(([component, version]) => `${component}@${version}`));
const unavailable = computed(() => {
  if (graph.catalogLoading) return "正在读取酒馆旧预置所需节点目录";
  if (graph.catalogError) return "酒馆旧预置不可用：节点目录无法核实";
  return missingOldNodes.value.length
    ? `酒馆旧预置不可用：缺少精确节点 ${missingOldNodes.value.join("、")}` : "";
});
const configurationNotice = computed(() => profiles.records.value.length > 0 && configurations.value.length === 0
  ? "新版全局模型配置尚未接入酒馆旧预置；未将其作为旧配置使用" : "");
const canCreate = computed(() => !busy.value && !unavailable.value
  && !graph.locked && !graph.catalogLoading && !graph.catalogError);
async function create() {
  if (!canCreate.value) return;
  error.value = "";
  busy.value = true;
  const workflowId = graph.document?.workflow_definition_id, fixedProtocol = protocol.value, selector = selected.value;
  const choice = selector === "unconfigured" ? null : selector ? configurations.value.find(row => row.id === selector)
    : configurations.value.length === 1 ? configurations.value[0] : null;
  const fixed = choice ? graphClone(choice) : null;
  try {
    if (!selector && configurations.value.length !== 1)
      throw new Error("请明确选择酒馆旧格式模型配置，或选择创建待配置流程");
    if (selector && selector !== "unconfigured" && !choice)
      throw new Error("原模型配置已失效，请明确选择另一条或创建待配置流程");
    if (workflowId !== graph.document?.workflow_definition_id || protocol.value !== fixedProtocol || selected.value !== selector) return;
    if (fixed && (workflowId !== graph.document?.workflow_definition_id || protocol.value !== fixedProtocol
      || selected.value !== selector || JSON.stringify(configurations.value.find(row => row.id === fixed.id)) !== JSON.stringify(fixed)))
      throw new Error("模型配置依据已变化，请重新选择");
    const modelConfig = fixed ? { reference: fixed.identity } : undefined;
    const doc = createTavernDemo(graph.catalog, graph.executionPackageLock, modelConfig, fixedProtocol);
    if (!graph.createWorkflowDraft(doc, "酒馆独立聊天展示")) error.value = "当前不可创建酒馆草稿";
    else {
      await nextTick();
      if (graph.document?.workflow_definition_id === doc.workflow_definition_id)
        graph.selectedNodeIds = doc.nodes.filter(node => node.component_id === "tavern.chat.presentation")
          .map(node => node.node_binding_id);
    }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "酒馆示例不可用"; }
  finally { busy.value = false; }
}
onMounted(() => { void resources.refresh(); void profiles.refresh(); });
</script>
<template>
  <section class="tavern-chat-panel" aria-label="酒馆聊天入口">
    <header><strong>酒馆工作流</strong></header>
    <label>模型协议<select v-model="protocol" aria-label="酒馆模型协议" :disabled="busy" @change="selected = ''"><option value="chat">DeepSeek / Chat</option><option value="gemini">Gemini</option></select></label>
    <label>模型配置<select v-model="selected" aria-label="酒馆预置模型配置" :disabled="busy">
      <option value="">{{ configurations.length === 1 ? '使用唯一可用配置' : '选择酒馆旧格式模型配置' }}</option>
      <option v-for="choice in configurations" :key="choice.id" :value="choice.id">{{ choice.title }} · {{ choice.modelLabel }}</option>
      <option value="unconfigured">创建待配置流程</option>
    </select></label>
    <p v-if="unavailable" role="status">{{ unavailable }}</p>
    <p v-else-if="configurationNotice" role="status">{{ configurationNotice }}</p>
    <div class="chat-actions">
      <button type="button" title="创建酒馆示例" aria-label="创建酒馆示例"
        :disabled="!canCreate" @click="create"><Plus :size="14" />创建酒馆工作流</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
  </section>
</template>
<style scoped>
.tavern-chat-panel { display:grid; gap:9px; min-width:0; padding:12px; border-top:1px solid #45454e; font-size:12px; }
header { display:flex; min-width:0; }
strong { font-weight:500; }
.chat-actions { display:flex; flex-wrap:wrap; gap:6px; min-width:0; }
button,a { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-height:28px; box-sizing:border-box; padding:5px 8px; border:1px solid #64776d; border-radius:3px; color:#dedee4; background:transparent; font:inherit; text-decoration:none; }
:disabled,[aria-disabled="true"] { opacity:.55; }
[aria-disabled="true"] { pointer-events:none; }
p { margin:0; overflow-wrap:anywhere; line-height:1.5; color:#a4abb4; }
.warning { color:#e4a0a0; }
label { display:grid; gap:5px; min-width:0; }
select { min-width:0; width:100%; padding:6px; background:#222226; border:1px solid #505059; border-radius:3px; color:inherit; }
</style>
