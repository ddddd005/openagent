<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Check } from "lucide-vue-next";
import { graphClone, type GraphDocument, type GraphNode } from "../domain/workflowGraph";
import { isCurrentPromptResource, type PromptMember } from "../domain/workflowPromptResources";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import PromptMembersEditor from "./PromptMembersEditor.vue";
const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk();
const members = ref<PromptMember[]>([]), valid = ref(true), error = ref(""), status = ref(""), applying = ref(false);
const basis = () => ({ workflowId: props.document.workflow_definition_id, nodeId: props.node.node_binding_id,
  version: props.node.component_version, config: props.node.config, lifecycle: sdk.lifecycle.value });
const supported = computed(() => isCurrentPromptResource({
  envelope_version: 1, scope: "workspace", type_id: "workflow.prompt-resource",
  resource_id: props.node.node_binding_id, data_schema_version: 2, update_sequence: 1,
  value: { enabled: true, members: props.node.config.members },
}));
const scope = computed(() => `node:${props.document.workflow_definition_id}:${props.node.node_binding_id}:prompt-members`);
const disabled = computed(() => props.disabled || sdk.locked.value || applying.value || !supported.value);
function current(request: { workflowId: string; nodeId: string; componentId: string; componentVersion: string;
  lifecycle: string }) {
  return sdk.workflowId.value === request.workflowId && props.document.workflow_definition_id === request.workflowId
    && props.node.node_binding_id === request.nodeId && props.node.component_id === request.componentId
    && props.node.component_version === request.componentVersion && sdk.lifecycle.value === request.lifecycle;
}
watch(() => [sdk.lifecycle.value, props.node.node_binding_id, props.node.component_version, props.node.config], () => {
  members.value = supported.value ? graphClone(props.node.config.members as PromptMember[]) : [];
  error.value = ""; status.value = "";
}, { immediate: true, deep: true });
async function apply() {
  if (disabled.value || !valid.value) return;
  const fixed = basis(), patch = { members: graphClone(members.value) };
  if (!isCurrentPromptResource({ envelope_version: 1, scope: "workspace", type_id: "workflow.prompt-resource",
    resource_id: props.node.node_binding_id, data_schema_version: 2, update_sequence: 1,
    value: { enabled: true, members: patch.members } })) { error.value = "条目正文或呈现字段无效，原文保留"; return; }
  applying.value = true;
  try {
    if (props.disabled || sdk.locked.value
      || JSON.stringify(members.value) !== JSON.stringify(patch.members)) { error.value = "配置或输入已变化，请重新确认"; return; }
    const request = { workflowId: fixed.workflowId, nodeId: fixed.nodeId,
      componentId: props.node.component_id, componentVersion: fixed.version, expectedConfig: graphClone(fixed.config),
      patch, extensionId: promptFrontendExtensions[2]!.extension_id, lifecycle: fixed.lifecycle };
    let result;
    try { result = await sdk.configureNode(request); }
    catch (failure) {
      if (current(request)) error.value = failure instanceof Error ? failure.message : "提示词配置提交失败";
      return;
    }
    if (!current(request)) return;
    error.value = result ? "" : "编辑依据已变化，未应用条目";
    status.value = result?.outcome === "cancelled" ? "已取消；工作流配置未修改"
      : result ? result.workflowId === fixed.workflowId
        ? "条目已保存到当前工作流" : "条目已保存到新工作流" : "";
  } finally { applying.value = false; }
}
</script>
<template>
  <form class="prompt-group-fields" aria-label="提示词内容组" @submit.prevent="apply">
    <PromptMembersEditor v-if="supported" :members="members" :version="2" :disabled="disabled" :identity="scope" @validity="valid = $event" />
    <p v-else role="status">提示词配置不兼容，原文保留，请在技术详情中核对。</p>
    <p v-if="error" role="status">{{ error }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <button type="submit" :disabled="disabled || !valid"><Check :size="14" />保存条目</button>
  </form>
</template>
<style scoped>
form { display:grid; gap:10px; min-width:0; } p { color:#e4a0a0; font-size:11px; overflow-wrap:anywhere; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; padding:6px; border:1px solid #64776d; border-radius:3px; font-size:12px; }
</style>
