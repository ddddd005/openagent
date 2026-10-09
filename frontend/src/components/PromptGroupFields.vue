<script setup lang="ts">
import { computed, ref } from "vue";
import { Save } from "lucide-vue-next";
import { graphClone, graphObject, type GraphDocument, type GraphNode } from "../domain/workflowGraph";
import { isCurrentPromptResource, type PromptMember } from "../domain/workflowPromptResources";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import { useFormDraftsStore } from "../stores/formDrafts";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import PromptMembersEditor from "./PromptMembersEditor.vue";
import FormDraftNotice from "./FormDraftNotice.vue";
const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), forms = typeof window === "undefined" ? null : useFormDraftsStore();
const members = ref<PromptMember[]>([]), valid = ref(true), error = ref(""), applying = ref(false);
const editor = ref<{ adoptBasis(value: unknown): boolean } | null>(null);
const basis = () => ({ workflowId: props.document.workflow_definition_id, nodeId: props.node.node_binding_id,
  version: props.node.component_version, config: props.node.config, lifecycle: sdk.lifecycle.value });
const supported = computed(() => isCurrentPromptResource({
  envelope_version: 1, scope: "workspace", type_id: "workflow.prompt-resource",
  resource_id: props.node.node_binding_id, data_schema_version: 2, update_sequence: 1,
  value: { enabled: true, members: props.node.config.members },
}));
const draft = useBoundFormDraft({
  id: () => `node:${props.document.workflow_definition_id}:${props.node.node_binding_id}:prompt-members`,
  basis, capture: () => members.value,
  reset: () => { members.value = supported.value ? graphClone(props.node.config.members as PromptMember[]) : []; },
  restore: value => {
    if (!Array.isArray(value) || !value.every(row => graphObject(row))) throw new Error("提示词条目草稿格式不兼容");
    members.value = graphClone(value) as PromptMember[];
  },
});
const scope = computed(() => `node:${props.document.workflow_definition_id}:${props.node.node_binding_id}:prompt-members`);
const disabled = computed(() => props.disabled || sdk.locked.value || applying.value || draft.loading.value || draft.conflict.value || !supported.value);
async function apply() {
  if (disabled.value || !valid.value) return;
  const fixed = basis(), context = draft.context.value, patch = { members: graphClone(members.value) };
  if (!isCurrentPromptResource({ envelope_version: 1, scope: "workspace", type_id: "workflow.prompt-resource",
    resource_id: props.node.node_binding_id, data_schema_version: 2, update_sequence: 1,
    value: { enabled: true, members: patch.members } })) { error.value = "条目正文或呈现字段无效，原文保留"; return; }
  applying.value = true;
  try {
    if (!await draft.flush() || forms && !await forms.flushAll()) { error.value = "条目草稿尚未写入本机"; return; }
    if (context !== draft.context.value || props.disabled || sdk.locked.value
      || JSON.stringify(members.value) !== JSON.stringify(patch.members)) { error.value = "配置或输入已变化，请重新确认"; return; }
    const result = sdk.configureNode({ workflowId: sdk.workflowId.value, nodeId: fixed.nodeId,
      componentId: props.node.component_id, componentVersion: fixed.version, expectedConfig: graphClone(fixed.config),
      patch, extensionId: promptFrontendExtensions[2]!.extension_id, lifecycle: fixed.lifecycle });
    const nextBasis = { ...fixed, config: { ...fixed.config, ...patch } };
    const adopted = !result || draft.adoptBasis(nextBasis) && (editor.value?.adoptBasis(nextBasis) ?? true);
    error.value = result ? adopted ? "" : "条目已应用，草稿依据尚未同步，原草稿保留" : "编辑依据已变化，未应用条目";
  } finally { applying.value = false; }
}
</script>
<template>
  <form class="prompt-group-fields" aria-label="提示词内容组" @submit.prevent="apply">
    <FormDraftNotice :message="draft.message.value" :conflict="draft.conflict.value" @restore="draft.restoreConflict" @discard="draft.discardConflict" />
    <PromptMembersEditor v-if="supported" ref="editor" :members="members" :version="2" :disabled="disabled" :draft-scope="scope" :draft-basis="basis()" @validity="valid = $event" />
    <p v-else role="status">提示词配置不兼容，原文保留，请在技术详情中核对。</p>
    <p v-if="error" role="status">{{ error }}</p>
    <button type="submit" :disabled="disabled || !valid"><Save :size="14" />应用条目</button>
  </form>
</template>
<style scoped>
form { display:grid; gap:10px; min-width:0; } p { color:#e4a0a0; font-size:11px; overflow-wrap:anywhere; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; padding:6px; border:1px solid #64776d; border-radius:3px; font-size:12px; }
</style>
