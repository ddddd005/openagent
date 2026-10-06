<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { frontendSources, frontendStateType, frontendWiringUnavailable, type FrontendRole } from "../domain/frontendWiring";
const sdk = useWorkflowFrontendSdk();
const graph = computed(() => ({ document: sdk.document.value, catalog: sdk.catalog.value,
  packageLock: sdk.packages.value, locked: sdk.locked.value }));
const emit = defineEmits<{ connected: [] }>();
const selected = ref("");
const role = ref<FrontendRole>("assistant");
const objectKey = ref("frontend");
const sources = computed(() => graph.value.document ? frontendSources(graph.value.document, graph.value.catalog) : []);
const unavailable = computed(() => frontendWiringUnavailable(graph.value.catalog, graph.value.packageLock));
const bindings = computed(() => graph.value.document?.object_bindings?.filter(binding => binding.type_id === frontendStateType
  && binding.schema_version === 1 && binding.scope === "shared") ?? []);
const sourceKey = (id: string, port: string) => JSON.stringify([id, port]);
const source = computed(() => sources.value.find(row => sourceKey(row.node.node_binding_id, row.port.port_id) === selected.value));
watch(() => sdk.lifecycle.value, () => { selected.value = ""; objectKey.value = "frontend"; });
function connect() {
  if (!source.value?.compatible) return;
  if (sdk.attachDisplay({ sourceNodeId: source.value.node.node_binding_id,
    sourcePortId: source.value.port.port_id, objectKey: objectKey.value, role: role.value })) emit("connected");
}
</script>
<template>
  <section class="frontend-wiring" aria-label="接入前端展示">
    <p>选择需要保存并展示的文本输出。此操作加入读取、追加和公开展示节点、对象绑定及必要接线，可以一次撤销。</p>
    <p v-if="unavailable" class="warning" role="status">{{ unavailable }}</p>
    <label>源节点 / 输出端口<select v-model="selected" :disabled="graph.locked">
      <option value="">选择 TEXT@2 输出</option>
      <option v-for="row in sources" :key="sourceKey(row.node.node_binding_id, row.port.port_id)"
        :value="sourceKey(row.node.node_binding_id, row.port.port_id)" :disabled="!row.compatible">
        {{ row.node.title }} / {{ row.port.port_id }} · {{ row.port.data_type }}@{{ row.port.data_schema_version ?? 1 }}{{ row.compatible ? '' : '（不兼容）' }}
      </option>
    </select></label>
    <p v-if="!sources.some(row => row.compatible)">当前图没有 TEXT@2 输出。可先从节点菜单添加支持 TEXT@2 的输入或文本节点。</p>
    <label>展示角色<select v-model="role" :disabled="graph.locked"><option value="user">用户</option><option value="assistant">助手</option><option value="system">系统</option></select></label>
    <label>会话对象键<input v-model="objectKey" list="frontend-object-keys" :disabled="graph.locked" maxlength="128" /></label>
    <datalist id="frontend-object-keys"><option v-for="binding in bindings" :key="binding.object_key" :value="binding.object_key" /></datalist>
    <p>同一对象的多种角色请分别接入。后续追加读取前次已接纳状态；展示内容由图执行保存。</p>
    <button type="button" :disabled="graph.locked || !!unavailable || !source?.compatible || !objectKey.trim()" @click="connect">接入前端展示</button>
  </section>
</template>
<style scoped>
.frontend-wiring { display:grid; gap:12px; font-size:12px; }
p { margin:0; color:#b2b8b6; line-height:1.65; }
label { display:grid; gap:5px; }
input,select { width:100%; min-width:0; padding:6px; box-sizing:border-box; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; }
button { padding:7px; border:1px solid #64776d; border-radius:3px; } button:disabled { opacity:.45; }
.warning { color:#e4a0a0; overflow-wrap:anywhere; }
</style>
