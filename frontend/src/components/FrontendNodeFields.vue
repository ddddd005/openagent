<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ExternalLink } from "lucide-vue-next";
import { frontendNodeInputs, frontendNodeLaunchTarget, openFrontendNodeEntry } from "../application/frontendNodeEntry";
import { graphClone, graphJsonSignature, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { frontendNodeFieldsExtensionForVersion } from "../plugins/workflowFrontendManifest";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

const props = defineProps<{ node: GraphNode; document: GraphDocument; catalog: GraphNodeType[]; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), graph = useWorkflowGraphStore();
const error = ref(""), saving = ref(false), opening = ref(false);
const entry = computed(() => graph.entries[sdk.workflowId.value]);
const owned = computed(() => sdk.document.value === props.document && entry.value?.document === props.document);
const inputs = computed(() => frontendNodeInputs(props.document, props.catalog));
const selected = computed(() => typeof props.node.config.input_node_id === "string" ? props.node.config.input_node_id : "");
const invalidInput = computed(() => selected.value !== "" && !inputs.value.some(node => node.node_binding_id === selected.value));
const editingLocked = computed(() => props.disabled || sdk.locked.value || saving.value || !owned.value);
const unresolved = computed(() => graph.configurationPendingRequests().some(row => row.workflowId === sdk.workflowId.value)
  || graph.managementPendingRequests().some(row => row.workflowId === sdk.workflowId.value && row.pending));
const launch = computed(() => owned.value
  ? frontendNodeLaunchTarget(entry.value, { node: props.node, document: props.document }, sdk.session.value, unresolved.value)
  : { owner: null, diagnosis: "前端节点不属于当前工作流" });
let generation = 0, alive = true;
watch(() => [props.node.node_binding_id, props.document.workflow_definition_id, sdk.lifecycle.value], () => {
  generation++; error.value = ""; saving.value = false; opening.value = false;
});
onBeforeUnmount(() => { alive = false; generation++; });
async function selectInput(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (editingLocked.value || value !== "" && !inputs.value.some(node => node.node_binding_id === value)) return;
  const token = generation, node = props.node;
  const extension = frontendNodeFieldsExtensionForVersion(node.component_version);
  if (node.component_id !== "frontend.node" || !extension) return;
  saving.value = true; error.value = "";
  try {
    const result = await sdk.configureNode({
      workflowId: sdk.workflowId.value, nodeId: node.node_binding_id,
      componentId: node.component_id, componentVersion: node.component_version,
      expectedConfig: graphClone(node.config), patch: { input_node_id: value },
      extensionId: extension.extension_id, lifecycle: sdk.lifecycle.value,
    });
    if (alive && token === generation && (!result || result.outcome === "cancelled"))
      error.value = "输入绑定未保存";
  } catch (failure) {
    if (alive && token === generation) error.value = failure instanceof Error ? failure.message : "输入绑定保存失败";
  } finally {
    if (alive && token === generation) saving.value = false;
  }
}
async function open() {
  const owner = launch.value.owner;
  if (!owner || opening.value) return;
  const token = generation, signature = graphJsonSignature(owner);
  opening.value = true; error.value = "";
  try {
    await openFrontendNodeEntry(owner, {
      current: () => alive && token === generation && graphJsonSignature(launch.value.owner) === signature,
      openBlank: () => window.open("about:blank", "_blank"),
    });
  } catch (failure) {
    if (alive && token === generation) error.value = failure instanceof Error ? failure.message : "前端节点入口不可用";
  } finally {
    if (alive && token === generation) opening.value = false;
  }
}
</script>
<template>
  <section class="frontend-node-fields" aria-label="前端节点入口与输入绑定">
    <label>
      <span>输入节点</span>
      <select :value="selected" :disabled="editingLocked" aria-label="前端节点输入绑定" @change="selectInput">
        <option value="">未绑定输入</option>
        <option v-if="invalidInput" :value="selected" disabled>缺失或不适用的输入 · {{ selected }}</option>
        <option v-for="input in inputs" :key="input.node_binding_id" :value="input.node_binding_id">
          {{ input.title }} · {{ input.node_binding_id }}
        </option>
      </select>
    </label>
    <small v-if="invalidInput" role="status">当前绑定不是此工作流中的可编辑输入节点</small>
    <button type="button" :disabled="opening || !launch.owner" aria-label="打开前端节点"
      :title="launch.diagnosis" @click="open"><ExternalLink :size="15" />打开前端节点</button>
    <small v-if="!launch.owner" role="status">{{ launch.diagnosis }}</small>
    <small v-if="error" role="status">{{ error }}</small>
  </section>
</template>
<style scoped>
.frontend-node-fields { display:grid; gap:8px; min-width:0; font-size:12px; }
label { display:grid; gap:5px; min-width:0; }
select { width:100%; min-width:0; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:inherit; }
button { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:30px; padding:6px 9px; border:1px solid #64776d; border-radius:3px; color:inherit; background:transparent; font:inherit; }
button:disabled { opacity:.5; }
small { color:#e4a0a0; font-size:11px; overflow-wrap:anywhere; }
</style>
