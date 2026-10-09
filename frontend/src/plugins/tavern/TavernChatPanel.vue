<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Plus } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { createTavernDemo } from "./tavernDemo";
import { useProviderResources } from "../../application/workflowResources";
import { isCapacityModelSourceConfiguration, isModelCapacity, modelParameterDiagnostic,
  providerIdentity, sameProviderIdentity, type ProviderProtocol } from "../../domain/workflowModelResources";
import { providerConfigurationPorts } from "../../adapters/providerConfigurationApi";
import { useWorkbenchPersistenceStore } from "../../stores/workbenchPersistence";
import { graphClone } from "../../domain/workflowGraph";

const graph = useWorkflowGraphStore(), error = ref("");
const resources = useProviderResources(), persistence = typeof window !== "undefined" ? useWorkbenchPersistenceStore() : null;
const protocol = ref<ProviderProtocol>("chat"), selected = ref(""), busy = ref(false);
const configurations = computed(() => (graph.document?.nodes ?? []).flatMap(node => {
  if (node.component_id !== "models.source" || !["2", "4"].includes(node.component_version)
    || !isCapacityModelSourceConfiguration(node.config) || !isModelCapacity(node.config.capacity)) return [];
  const config = node.config, provider = resources.records.value.find(row => sameProviderIdentity(providerIdentity(row), config.reference));
  if (!provider?.value.enabled || !provider.value.credential_ref || provider.value.protocol !== protocol.value
    || node.component_version !== (protocol.value === "gemini" ? "4" : "2")
    || modelParameterDiagnostic(config.parameters, protocol.value)) return [];
  return [{ id: node.node_binding_id, title: node.title, config, provider }];
}));
async function create() {
  if (busy.value || graph.locked || graph.catalogLoading || graph.catalogError) return;
  error.value = "";
  busy.value = true;
  const workflowId = graph.document?.workflow_definition_id, fixedProtocol = protocol.value, selector = selected.value;
  const choice = selector === "unconfigured" ? null : selector ? configurations.value.find(row => row.id === selector)
    : configurations.value.length === 1 ? configurations.value[0] : null;
  const fixed = choice ? graphClone(choice) : null;
  try {
    if (selector && selector !== "unconfigured" && !choice)
      throw new Error("原模型配置已失效，请明确选择另一条或创建待配置流程");
    if (persistence && !await persistence.flushAll()) throw new Error("当前编辑尚未写入本机");
    if (workflowId !== graph.document?.workflow_definition_id || protocol.value !== fixedProtocol || selected.value !== selector) return;
    if (fixed) {
      const status = await providerConfigurationPorts.status(fixed.provider.value.credential_ref!);
      if (status.state !== "saved") throw new Error("所选模型凭据缺失，未创建可运行模板");
      if (workflowId !== graph.document?.workflow_definition_id || protocol.value !== fixedProtocol || selected.value !== selector
        || JSON.stringify(configurations.value.find(row => row.id === fixed.id)) !== JSON.stringify(fixed))
        throw new Error("模型配置依据已变化，请重新选择");
    }
    const modelConfig = fixed ? { reference: { ...fixed.config.reference },
      parameters: { ...fixed.config.parameters }, capacity: fixed.config.capacity } : undefined;
    const doc = createTavernDemo(graph.catalog, graph.executionPackageLock, modelConfig, fixedProtocol);
    if (!graph.createWorkflowDraft(doc, "酒馆独立聊天展示")) error.value = "当前不可创建酒馆草稿";
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "酒馆示例不可用"; }
  finally { busy.value = false; }
}
onMounted(() => { void resources.refresh(); });
</script>
<template>
  <section class="tavern-chat-panel" aria-label="酒馆聊天入口">
    <header><strong>酒馆聊天</strong></header>
    <label>模型协议<select v-model="protocol" aria-label="酒馆模型协议" :disabled="busy" @change="selected = ''"><option value="chat">DeepSeek / Chat</option><option value="gemini">Gemini</option></select></label>
    <label>模型配置<select v-model="selected" aria-label="酒馆预置模型配置" :disabled="busy">
      <option value="">{{ configurations.length === 1 ? '使用当前有效配置' : '模型与容量待配置' }}</option>
      <option v-for="choice in configurations" :key="choice.id" :value="choice.id">{{ choice.title }} · {{ choice.config.parameters.model }}</option>
      <option value="unconfigured">创建待配置流程</option>
    </select></label>
    <div class="chat-actions">
      <button type="button" title="创建酒馆示例" aria-label="创建酒馆示例"
        :disabled="busy || graph.locked || graph.catalogLoading || !!graph.catalogError" @click="create"><Plus :size="14" />新建</button>
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
