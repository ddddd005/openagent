<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { isGraphObjectBindings, type GraphDocument, type GraphDataType } from "../domain/workflowGraph";
const props = defineProps<{ document: GraphDocument; types: GraphDataType[]; disabled?: boolean }>();
const emit = defineEmits<{ change: [value: unknown] }>();
const draft = ref("[]");
const error = ref<string | null>(null);
const selectedType = ref("");
const types = computed(() => props.types.filter(type => type.scope === "session"));
const typeKey = (type: GraphDataType) => `${type.type_id}@${type.schema_version}`;
watch(() => [props.document.workflow_definition_id, JSON.stringify(props.document.object_bindings ?? [])],
  () => { draft.value = JSON.stringify(props.document.object_bindings ?? [], null, 2); error.value = null; },
  { immediate: true });
function parsed() {
  const value: unknown = JSON.parse(draft.value);
  if (!isGraphObjectBindings(value, props.document.nodes))
    throw new Error("检查对象键、类型版本、节点 UUID 与权限；私有对象仅允许所有者读取和写入。");
  return value;
}
function apply() {
  try { const value = parsed(); error.value = null; emit("change", value); }
  catch (failure) { error.value = failure instanceof Error ? failure.message : "请输入有效的绑定 JSON"; }
}
function add() {
  const type = types.value.find(type => typeKey(type) === selectedType.value);
  if (!type) return;
  try {
    const bindings = parsed();
    let index = bindings.length + 1;
    while (bindings.some(binding => binding.object_key === `shared/object-${index}`)) index++;
    bindings.push({ object_key: `shared/object-${index}`, type_id: type.type_id,
      schema_version: type.schema_version, scope: "shared", owner_node_id: null, readers: [], writers: [] });
    draft.value = JSON.stringify(bindings, null, 2); error.value = null;
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "先修正绑定 JSON"; }
}
</script>

<template>
  <section class="graph-object-bindings" aria-label="会话对象绑定编辑">
    <h3>会话对象绑定</h3>
    <p>绑定声明对象类型和节点权限。节点配置中的对象键须与此处一致；保存工作流后生效。</p>
    <label>添加已加载的会话类型<select v-model="selectedType" :disabled="disabled">
      <option value="">选择类型</option><option v-for="type in types" :key="typeKey(type)" :value="typeKey(type)">{{ typeKey(type) }}</option>
    </select></label>
    <button type="button" :disabled="disabled || !selectedType" @click="add">加入绑定草稿</button>
    <label>对象绑定 JSON<textarea v-model="draft" rows="16" spellcheck="false" :disabled="disabled" /></label>
    <p>readers / writers 填节点 UUID；private 的 owner_node_id 与权限必须属于同一个节点。新私有绑定按声明的默认值初始化。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button type="button" :disabled="disabled" @click="apply">校验并应用到工作流</button>
    <h3>节点 UUID</h3>
    <label v-for="node in document.nodes" :key="node.node_binding_id">{{ node.title }}
      <input :value="node.node_binding_id" readonly @focus="($event.target as HTMLInputElement).select()" />
    </label>
    <p v-if="!document.nodes.length">先添加节点，再配置权限。</p>
  </section>
</template>

<style scoped>
.graph-object-bindings { display:grid; gap:10px; min-width:0; }
h3 { font-size:12px; font-weight:500; margin:0; }
p { margin:0; font-size:11px; line-height:1.6; color:#b2b8b6; overflow-wrap:anywhere; }
label { display:grid; gap:5px; font-size:12px; min-width:0; }
input,select,textarea { width:100%; min-width:0; box-sizing:border-box; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:12px Consolas,monospace; }
textarea { resize:vertical; }
button { font-size:12px; padding:6px; border:1px solid #505059; border-radius:3px; }
:disabled { opacity:.5; }
.error { color:#e4a0a0; }
</style>
