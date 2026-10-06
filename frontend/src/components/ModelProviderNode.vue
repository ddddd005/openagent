<script setup lang="ts">
import { computed, onMounted } from "vue";
import { Handle, Position } from "@vue-flow/core";
import { Cpu, RefreshCw, Save, Trash2, Unplug } from "lucide-vue-next";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { useWorkspaceStore } from "../stores/workspace";
import {
  CHAT_CAPABILITIES, MODEL_DIAGNOSTIC_MESSAGES, modelParameterDiagnostics,
  type ModelConfigurationField, type ModelProviderNode,
} from "../domain/modelConfiguration";

const props = defineProps<{ id: string; selected?: boolean; data: { node: ModelProviderNode } }>();
const models = useModelConfigurationStore();
const workspace = useWorkspaceStore();
const node = computed(() => props.data.node);
const savedRevision = computed(() => models.getDraft(workspace.activeWorkflowId)?.backendRevision ?? 0);
const saved = computed(() => models.isDraftSaved(workspace.activeWorkflowId));
const dependencies = computed(() => models.getDraft(workspace.activeWorkflowId)?.edges.filter((edge) => edge.source === props.id) ?? []);
const choices = computed(() => models.providers.filter((provider) => provider.enabled));
const exactChoice = computed(() => node.value.provider_ref
  ? `${node.value.provider_ref.provider_id}:${node.value.provider_ref.revision}` : "");
const choiceExists = computed(() => choices.value.some((provider) => `${provider.provider_id}:${provider.revision}` === exactChoice.value));
const fields = computed(() => modelParameterDiagnostics(node.value.parameters));
const fieldError = (field: ModelConfigurationField) => fields.value.find((row) => row.field === field);
const providerState = computed(() => {
  const reference = node.value.provider_ref;
  if (!reference) return { code: "provider_missing", message: "未选择供应商" };
  if (!models.providersLoaded) return null;
  const current = models.providers.find((provider) => provider.provider_id === reference.provider_id);
  if (!current) return { code: "provider_missing", message: MODEL_DIAGNOSTIC_MESSAGES.provider_missing };
  if (!current.enabled) return { code: "provider_unavailable", message: MODEL_DIAGNOSTIC_MESSAGES.provider_unavailable };
  if (!current.credential_ref) return { code: "credential_reference_missing", message: MODEL_DIAGNOSTIC_MESSAGES.credential_reference_missing };
  if (current.revision !== reference.revision)
    return { code: "provider_historical_reference", message: `绑定 r${reference.revision} · 最新 r${current.revision}` };
  return null;
});
const nodeDiagnostics = computed(() => models.diagnostics.filter((row) => row.target === props.id
  && row.code !== providerState.value?.code));
function chooseProvider(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  const provider = choices.value.find((row) => `${row.provider_id}:${row.revision}` === value);
  models.updateNode(workspace.activeWorkflowId, props.id, {
    provider_ref: provider ? { provider_id: provider.provider_id, revision: provider.revision } : null,
  });
}
function parameter(key: "max_tokens" | "temperature", event: Event) {
  const value = (event.target as HTMLInputElement).value;
  const parameters = { ...node.value.parameters };
  if (value === "") delete parameters[key];
  else if (Number.isFinite(Number(value))) parameters[key] = Number(value);
  models.updateNode(workspace.activeWorkflowId, props.id, { parameters });
}
onMounted(() => { if (!models.providersLoaded && !models.loading) void models.loadProviders(); });
</script>

<template>
  <article class="model-provider-node" :class="{ 'is-selected': selected }" aria-label="模型提供">
    <header><Cpu :size="16" /><strong>模型提供</strong><span>Chat</span><button class="nodrag" type="button" title="删除模型节点" aria-label="删除模型节点" @click.stop="models.removeNode(workspace.activeWorkflowId, id)"><Trash2 :size="13" /></button></header>
    <div class="model-fields nodrag nowheel">
      <label>供应商
        <select :value="exactChoice" :aria-invalid="!!providerState && providerState.code !== 'provider_historical_reference'" @change="chooseProvider">
          <option value="">未选择</option>
          <option v-if="exactChoice && !choiceExists" :value="exactChoice">已绑定引用 · r{{ node.provider_ref?.revision }}</option>
          <option v-for="provider in choices" :key="provider.provider_id" :value="`${provider.provider_id}:${provider.revision}`">{{ provider.name }} · r{{ provider.revision }}</option>
        </select>
        <small v-if="providerState" class="model-diagnostic" :class="{ 'historical-reference': providerState.code === 'provider_historical_reference' }">{{ providerState.message }} [{{ providerState.code }}]</small>
      </label>
      <label>模型<input :value="node.parameters.model" @input="models.updateNode(workspace.activeWorkflowId, id, { parameters: { ...node.parameters, model: ($event.target as HTMLInputElement).value } })" maxlength="256" :aria-invalid="!!fieldError('model')" :aria-describedby="fieldError('model') ? `${id}-model-error` : undefined" />
        <small v-if="fieldError('model')" :id="`${id}-model-error`" class="model-diagnostic">{{ fieldError('model')?.message }} [{{ fieldError('model')?.code }}]</small>
      </label>
      <div class="model-numbers">
        <label>max_tokens<input type="number" min="1" :max="CHAT_CAPABILITIES.maxTokens" step="1" :value="node.parameters.max_tokens ?? ''" :aria-invalid="!!fieldError('max_tokens')" :aria-describedby="fieldError('max_tokens') ? `${id}-tokens-error` : undefined" @input="parameter('max_tokens', $event)" /></label>
        <label>temperature<input type="number" min="0" max="2" step="0.1" :value="node.parameters.temperature ?? ''" :aria-invalid="!!fieldError('temperature')" :aria-describedby="fieldError('temperature') ? `${id}-temperature-error` : undefined" @input="parameter('temperature', $event)" /></label>
      </div>
      <small v-if="fieldError('max_tokens')" :id="`${id}-tokens-error`" class="model-diagnostic">{{ fieldError('max_tokens')?.message }} [{{ fieldError('max_tokens')?.code }}]</small>
      <small v-if="fieldError('temperature')" :id="`${id}-temperature-error`" class="model-diagnostic">{{ fieldError('temperature')?.message }} [{{ fieldError('temperature')?.code }}]</small>
      <small v-for="diagnostic in nodeDiagnostics" :key="diagnostic.code" class="model-diagnostic">{{ MODEL_DIAGNOSTIC_MESSAGES[diagnostic.code] }} [{{ diagnostic.code }}]</small>
      <div class="model-capabilities" aria-label="模型能力"><span>非流式</span><span>thinking: disabled</span></div>
      <div v-for="edge in dependencies" :key="edge.id" class="model-dependency">
        <span>Agent {{ edge.target_binding_id === AGENT_BINDINGS.A ? 'A' : 'B' }}</span>
        <button type="button" title="断开模型连接" aria-label="断开模型连接" @click.stop="models.disconnect(workspace.activeWorkflowId, edge.id)"><Unplug :size="13" /></button>
      </div>
    </div>
    <div class="model-output"><span>模型</span><Handle id="model-out" type="source" :position="Position.Right" :connectable="true" aria-label="模型提供输出" /></div>
    <footer class="nodrag nowheel">
      <button type="button" title="保存模型配置到后端" aria-label="保存模型配置到后端" :disabled="models.locked" @click.stop="models.saveModels(workspace.activeWorkflowId)"><Save :size="14" /></button>
      <button type="button" title="刷新供应商" aria-label="刷新供应商" :disabled="models.loading" @click.stop="models.loadProviders()"><RefreshCw :size="14" /></button>
      <span v-if="savedRevision" class="model-saved-revision" title="最近后端保存修订">r{{ savedRevision }}</span>
      <span>{{ saved ? '已保存' : savedRevision ? '待保存' : '未保存' }}</span>
    </footer>
  </article>
</template>

<style scoped>
.model-provider-node { box-sizing: border-box; width: 260px; color: #dce1e7; border: 1px solid #5b5968; border-top: 2px solid #c39dbe; border-radius: 6px; background: #2b2930; }
.model-provider-node.is-selected { box-shadow: 0 0 0 1px #c39dbe; }
header { display: flex; align-items: center; gap: 8px; height: 35px; padding: 0 10px; border-bottom: 1px solid #46414b; font-size: 12px; }
header svg { color: #c39dbe; }
header span { margin-left: auto; color: #b4a4b3; font-size: 10px; }
header button, footer button, .model-dependency button { display: grid; place-items: center; width: 25px; height: 25px; padding: 0; border-radius: 3px; }
.model-fields { display: grid; gap: 8px; padding: 10px; }
.model-fields label { display: grid; gap: 5px; color: #b9b4c1; font-size: 10px; }
.model-fields input, .model-fields select { box-sizing: border-box; width: 100%; min-width: 0; height: 27px; padding: 0 6px; border: 1px solid #595361; border-radius: 3px; background: #212025; color: #e2dfe5; font: inherit; }
.model-numbers { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
.model-diagnostic { color: #f4b4ae; font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.model-diagnostic.historical-reference { color: #d9bf93; }
.model-capabilities { display: flex; flex-wrap: wrap; gap: 5px 12px; color: #a8bdb6; font-size: 10px; }
.model-fields input[aria-invalid="true"], .model-fields select[aria-invalid="true"] { border-color: #bd827c; }
.model-output { position: relative; height: 25px; padding-right: 13px; text-align: right; font-size: 10px; color: #d0a8c7; }
.model-output :deep(.vue-flow__handle) { top: 8px; right: -5px; width: 8px; height: 8px; background: #c39dbe; }
.model-dependency { display: flex; align-items: center; justify-content: space-between; color: #c4bcc7; font-size: 10px; }
footer { display: flex; align-items: center; gap: 5px; height: 33px; padding: 0 8px; border-top: 1px solid #46414b; }
footer span { margin-left: auto; color: #d9bf93; font-size: 10px; }
footer .model-saved-revision { margin-left: 0; color: #b5c7ba; }
</style>
