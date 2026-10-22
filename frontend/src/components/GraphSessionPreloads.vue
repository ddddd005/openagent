<script setup lang="ts">
import { computed, ref } from "vue";
import { Plus, Trash2, Save } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphClone, graphObject, type GraphObjectBinding } from "../domain/workflowGraph";
import { isPreloadVariable, preloadTypeIssue, preloadTypeKey, preloadValueIssue } from "../domain/sessionPreloads";
import { useVolatileFormState } from "../composables/useVolatileFormState";

const graph = useWorkflowGraphStore();
const selected = ref<string | null>(null), editing = ref(false), key = ref(""), typeKey = ref("");
const scope = ref<"shared" | "private">("shared"), owner = ref("");
const readers = ref<string[]>([]), writers = ref<string[]>([]);
const name = ref(""), scalarType = ref("string"), assigned = ref(true), scalar = ref(""), booleanValue = ref(false);
const json = ref("{}"), error = ref("");
const types = computed(() => graph.dataTypes.filter(type => !preloadTypeIssue(type)));
const type = computed(() => types.value.find(type => preloadTypeKey(type) === typeKey.value));
const variable = computed(() => !!type.value && isPreloadVariable(type.value));
const entries = computed(() => graph.document?.session_preloads ?? []);
const nodes = computed(() => graph.document?.nodes ?? []);
const locked = computed(() => graph.editorLocked);
function reset() {
  selected.value = null; editing.value = false; error.value = "";
}
useVolatileFormState({
  id: () => graph.document?.workflow_definition_id ?? "",
  basis: () => ({ session: graph.active?.session_id, preloads: graph.document?.session_preloads,
    bindings: graph.document?.object_bindings }),
  reset,
});
function load(objectKey: string) {
  const entry = entries.value.find(entry => entry.object_key === objectKey);
  const binding = graph.document?.object_bindings?.find(binding => binding.object_key === objectKey);
  if (!entry || !binding) return;
  selected.value = objectKey; editing.value = true; key.value = objectKey;
  typeKey.value = preloadTypeKey(binding); scope.value = binding.scope; owner.value = binding.owner_node_id ?? "";
  readers.value = [...binding.readers]; writers.value = [...binding.writers];
  json.value = JSON.stringify(entry.value, null, 2); error.value = "";
  if (graphObject(entry.value) && isPreloadVariable(binding)) {
    name.value = String(entry.value.name ?? ""); scalarType.value = String(entry.value.value_type ?? "string");
    assigned.value = entry.value.assigned === true;
    scalar.value = entry.value.value == null ? "" : String(entry.value.value);
    booleanValue.value = entry.value.value === true;
  }
}
function add() {
  if (locked.value) return;
  selected.value = null; editing.value = true; key.value = ""; typeKey.value = types.value[0] ? preloadTypeKey(types.value[0]) : "";
  scope.value = "shared"; owner.value = ""; readers.value = []; writers.value = [];
  name.value = ""; scalarType.value = "string"; assigned.value = true; scalar.value = "";
  booleanValue.value = false; json.value = "{}"; error.value = "";
}
function changeType() {
  const selectedType = type.value;
  if (!selectedType) return;
  json.value = selectedType.has_default ? JSON.stringify(selectedType.default_value, null, 2) : "{}";
}
function save() {
  if (!editing.value || locked.value || !type.value) return;
  try {
    let value: unknown;
    if (variable.value) {
      const initial = !assigned.value ? null : scalarType.value === "boolean" ? booleanValue.value
        : scalarType.value === "string" ? scalar.value : String(scalar.value).trim() ? Number(scalar.value) : NaN;
      value = { registered: true, name: name.value, value_type: scalarType.value, assigned: assigned.value, value: initial };
    } else value = JSON.parse(json.value);
    const issue = preloadValueIssue(type.value, value);
    if (issue) throw new Error(issue);
    const binding: GraphObjectBinding = { object_key: key.value, type_id: type.value.type_id,
      schema_version: type.value.schema_version, scope: scope.value,
      owner_node_id: scope.value === "private" ? owner.value : null,
      readers: scope.value === "private" ? readers.value.filter(id => id === owner.value) : [...readers.value],
      writers: scope.value === "private" ? writers.value.filter(id => id === owner.value) : [...writers.value] };
    if (!graph.setSessionPreload(binding, graphClone(value), selected.value ?? undefined))
      throw new Error("无法应用预加载项，请检查对象键、权限和工作流状态。");
    error.value = "";
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "初值无效。"; }
}
function remove() {
  if (locked.value || !selected.value) return;
  graph.removeSessionPreload(selected.value);
}
</script>

<template>
  <section class="session-preloads" aria-label="会话预加载数据">
    <header><h3>会话预加载数据</h3><button type="button" aria-label="新增预加载项" title="新增预加载项" :disabled="locked || !types.length" @click="add"><Plus :size="16" /></button></header>
    <p v-if="locked" role="status">当前工作流只读或请求待核实。</p>
    <p v-if="!entries.length">暂无预加载数据。</p>
    <p v-if="!types.length">没有符合预加载准入的已加载会话类型。</p>
    <div class="preload-layout">
      <nav aria-label="预加载列表"><button v-for="entry in entries" :key="entry.object_key" type="button"
        :aria-pressed="selected === entry.object_key" @click="load(entry.object_key)">{{ entry.object_key }}</button></nav>
      <form v-if="editing" @submit.prevent="save">
        <label>对象键<input v-model="key" aria-label="预加载对象键" :disabled="locked" :readonly="selected !== null" required maxlength="128" /></label>
        <label>准确类型<select v-model="typeKey" aria-label="预加载类型" :disabled="locked" @change="changeType">
          <option v-for="item in types" :key="preloadTypeKey(item)" :value="preloadTypeKey(item)">{{ preloadTypeKey(item) }}</option>
        </select></label>
        <p v-if="!type" role="alert">此项的准确类型当前不可用于预加载。</p>
        <template v-if="variable">
          <label>变量名称<input v-model="name" aria-label="变量名称" :disabled="locked" required maxlength="128" /></label>
          <label>标量类型<select v-model="scalarType" aria-label="变量标量类型" :disabled="locked">
            <option value="string">string</option><option value="integer">integer</option><option value="number">number</option><option value="boolean">boolean</option>
          </select></label>
          <label class="check"><input v-model="assigned" type="checkbox" aria-label="赋初值" :disabled="locked" />赋初值</label>
          <label v-if="assigned && scalarType === 'boolean'" class="check"><input v-model="booleanValue" type="checkbox" aria-label="变量布尔初值" :disabled="locked" />true</label>
          <label v-else-if="assigned">初值<textarea v-if="scalarType === 'string'" v-model="scalar" aria-label="变量初值" :disabled="locked" /><input v-else v-model="scalar" aria-label="变量初值" type="number" :step="scalarType === 'integer' ? 1 : 'any'" :disabled="locked" /></label>
        </template>
        <label v-else>初始 JSON<textarea v-model="json" aria-label="预加载 JSON 初值" rows="8" :disabled="locked" spellcheck="false" /></label>
        <label>归属<select v-model="scope" aria-label="预加载归属" :disabled="locked"><option value="shared">shared</option><option value="private">private</option></select></label>
        <label v-if="scope === 'private'">所有者<select v-model="owner" aria-label="预加载所有者" :disabled="locked" required><option value="">选择节点</option><option v-for="node in nodes" :key="node.node_binding_id" :value="node.node_binding_id">{{ node.title }} · {{ node.node_binding_id }}</option></select></label>
        <fieldset :disabled="locked"><legend>节点权限</legend>
          <div v-for="node in nodes.filter(node => scope === 'shared' || node.node_binding_id === owner)" :key="node.node_binding_id" class="acl">
            <span>{{ node.title }}</span><label class="check"><input v-model="readers" type="checkbox" :value="node.node_binding_id" :aria-label="`${node.title} 读取`" />读取</label><label class="check"><input v-model="writers" type="checkbox" :value="node.node_binding_id" :aria-label="`${node.title} 写入`" />写入</label>
          </div>
        </fieldset>
        <p v-if="error" role="alert" class="error">{{ error }}</p>
        <footer><button type="submit" :disabled="locked || !type" aria-label="保存预加载项"><Save :size="15" />保存</button><button v-if="selected" type="button" :disabled="locked" aria-label="删除预加载项" title="删除预加载项" @click="remove"><Trash2 :size="15" /></button></footer>
      </form>
    </div>
  </section>
</template>

<style scoped>
.session-preloads { padding:16px; overflow:auto; width:100%; box-sizing:border-box; color:#dedee4; }
header,footer,.acl { display:flex; align-items:center; gap:12px; }
header { justify-content:space-between; } h3 { margin:0; font-size:14px; }
p { font-size:12px; color:#b2b8b6; } .preload-layout { display:grid; grid-template-columns:minmax(140px,240px) minmax(0,640px); gap:20px; margin-top:16px; }
nav,form,label { display:grid; gap:8px; min-width:0; } nav { align-content:start; } form { gap:14px; }
label,legend,.acl { font-size:12px; } .check { display:flex; align-items:center; gap:6px; }
input,select,textarea { min-width:0; width:100%; box-sizing:border-box; padding:7px; background:#222226; color:inherit; border:1px solid #505059; border-radius:3px; }
input[type=checkbox] { width:auto; } textarea { resize:vertical; } button { display:flex; align-items:center; gap:6px; padding:7px; border:1px solid #505059; border-radius:3px; overflow-wrap:anywhere; text-align:left; }
nav button[aria-pressed=true] { border-color:#a4c8b3; } fieldset { min-width:0; border:1px solid #505059; display:grid; gap:10px; } .acl span { flex:1; overflow-wrap:anywhere; } .error { color:#e4a0a0; } :disabled { opacity:.5; }
@media(max-width:640px) { .preload-layout { grid-template-columns:minmax(0,1fr); } }
</style>
