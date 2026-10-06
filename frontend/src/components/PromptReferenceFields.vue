<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import { usePromptResources } from "../application/promptResources";
import { graphClone, graphObject, type GraphDocument, type GraphNode } from "../domain/workflowGraph";
import { isCurrentPromptResource, isPromptIdentity, promptIdentity, promptResourceLabel,
  samePromptIdentity, type PromptIdentity } from "../domain/workflowPromptResources";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), resources = usePromptResources();
const records = resources.records, loading = resources.loading, resourceError = resources.error;
const selected = ref(""), status = ref(""), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const identityKey = (identity: PromptIdentity) => JSON.stringify([
  identity.envelope_version, identity.scope, identity.type_id, identity.resource_id,
]);
const reference = computed(() => graphObject(props.node.config) && Object.keys(props.node.config).length === 1
  && isPromptIdentity(props.node.config.reference) ? props.node.config.reference : null);
const chosen = computed(() => records.value.find(record => isCurrentPromptResource(record)
  && identityKey(promptIdentity(record)) === selected.value));
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value || !reference.value);
const diagnosis = computed(() => {
  if (!reference.value) return "提示词引用配置无效";
  const record = records.value.find(row => isCurrentPromptResource(row)
    && samePromptIdentity(promptIdentity(row), reference.value!));
  if (!record) return "所选提示词资源缺失或尚未读取";
  if (!record.value.enabled) return "所选提示词资源已停用";
  return `当前资源 · ${record.scope} · s${record.update_sequence}`;
});
watch(() => [sdk.workflowId.value, sdk.lifecycle.value, props.node.node_binding_id,
  props.node.component_id, props.node.component_version, props.node.config], () => {
  status.value = "";
  selected.value = reference.value ? identityKey(reference.value) : "";
  basis.value = {
    workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {},
    extensionId: promptFrontendExtensions[1]!.extension_id, lifecycle: sdk.lifecycle.value,
  };
}, { immediate: true, deep: true });
function apply() {
  if (disabled.value || !basis.value) { status.value = "提示词引用配置无效或当前不可编辑"; return; }
  if (!chosen.value) { status.value = "请先选择已有提示词资源"; return; }
  const result = sdk.configureNode({
    ...graphClone(basis.value), patch: { reference: promptIdentity(chosen.value) },
  });
  status.value = result ? result.workflowId === basis.value.workflowId ? "提示词引用已更新" : "提示词引用已写入新副本"
    : "配置依据已变化或当前不可编辑，请重新选择节点";
}
onMounted(() => { void resources.refresh(); });
</script>

<template>
  <form class="prompt-reference-fields" aria-label="提示词引用配置" @submit.prevent="apply">
    <label>提示词当前资源
      <div class="resource-row">
        <select v-model="selected" :disabled="disabled" aria-label="提示词当前资源">
          <option value="">选择提示词资源</option>
          <option v-if="selected && !chosen" :value="selected">缺失引用 · {{ reference?.scope }} · {{ reference?.resource_id }}</option>
          <option v-for="record in records" :key="identityKey(promptIdentity(record))"
            :value="identityKey(promptIdentity(record))">
            {{ promptResourceLabel(record) }} · {{ record.scope }} · s{{ record.update_sequence }}{{ record.value.enabled ? '' : '（已停用）' }}
          </option>
        </select>
        <button type="button" title="刷新提示词资源" aria-label="刷新提示词资源" :disabled="loading"
          @click="resources.refresh()"><RefreshCw :size="14" /></button>
      </div>
    </label>
    <p role="status">{{ diagnosis }}</p>
    <p v-if="resourceError" class="warning" role="status">{{ resourceError }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <button type="submit" :disabled="disabled || !chosen"><Save :size="14" />应用提示词引用</button>
  </form>
</template>

<style scoped>
.prompt-reference-fields { display:grid; gap:10px; min-width:0; font-size:12px; }
label { display:grid; gap:5px; min-width:0; }
.resource-row { display:flex; gap:5px; min-width:0; }
select { flex:1; width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
button { display:flex; align-items:center; justify-content:center; gap:6px; padding:6px; border:1px solid #64776d; border-radius:3px; }
.resource-row button { flex:0 0 28px; width:28px; height:28px; }
p { margin:0; color:#b9bec6; overflow-wrap:anywhere; }
.warning { color:#e4a0a0; }
:disabled { opacity:.55; }
</style>
