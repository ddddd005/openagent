<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Plus, RefreshCw, Save, X } from "lucide-vue-next";
import LorebookEntries from "./LorebookEntries.vue";
import { useLorebookResources } from "./lorebookResourceController";
import { cloneLorebookResource, isCurrentLorebookResource, lorebookIdentity, lorebookIdentityKey,
  lorebookResourceLabel, newLorebookResource, type CurrentLorebookResource } from "./lorebookResources";

const resources = useLorebookResources();
const records = resources.records, loading = resources.loading, locked = resources.locked;
const error = resources.error, pending = resources.pending, busy = resources.busy;
const form = ref<CurrentLorebookResource | null>(null), expectedSequence = ref(0), formError = ref("");
function edit(record: CurrentLorebookResource | null) {
  if (locked.value) return;
  form.value = record ? cloneLorebookResource(record) : newLorebookResource();
  expectedSequence.value = record?.update_sequence ?? 0;
  formError.value = "";
}
async function save() {
  if (!form.value || locked.value) return;
  if (!isCurrentLorebookResource(form.value)) {
    formError.value = "Lorebook 条目、字段或资源大小无效，尚未提交"; return;
  }
  formError.value = "";
  if (await resources.save(cloneLorebookResource(form.value), expectedSequence.value)) form.value = null;
}
async function reconcile() {
  if (await resources.reconcile()) form.value = null;
}
onMounted(() => { void resources.refresh(); });
</script>

<template>
  <section class="current-lorebook-panel" aria-label="全局 Lorebook 资源">
    <header>
      <strong>Lorebook · 全局资源</strong>
      <button type="button" title="刷新 Lorebook 资源" aria-label="刷新 Lorebook 资源" :disabled="loading"
        @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增 Lorebook 资源" aria-label="新增 Lorebook 资源" :disabled="locked"
        @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length">暂无全局 Lorebook</p>
    <ul>
      <li v-for="record in records" :key="lorebookIdentityKey(lorebookIdentity(record))">
        <button type="button" :disabled="locked" @click="edit(record)">
          <strong :title="lorebookResourceLabel(record)">{{ lorebookResourceLabel(record) }}</strong>
          <span>{{ record.value.enabled ? '启用' : '已停用' }} · s{{ record.update_sequence }}</span>
          <small>{{ record.scope }} · {{ record.resource_id.slice(0, 8) }} · {{ record.value.entries.length }} 个条目</small>
        </button>
      </li>
    </ul>
    <form v-if="form" aria-label="全局 Lorebook 表单" @submit.prevent="save">
      <header>
        <strong>{{ expectedSequence ? '更新全局 Lorebook' : '新增全局 Lorebook' }}</strong>
        <button type="button" title="取消 Lorebook 编辑" aria-label="取消 Lorebook 编辑" :disabled="locked"
          @click="form = null"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked">
        <label>范围<output>{{ form.scope }}</output></label>
        <label>资源 UUID<output>{{ form.resource_id }}</output></label>
        <label>名称<input v-model="form.value.name" type="text" maxlength="128" aria-label="Lorebook 资源名称" /></label>
        <label class="toggle"><input v-model="form.value.enabled" type="checkbox" />启用资源</label>
        <LorebookEntries v-model="form.value.entries" :disabled="locked" />
      </fieldset>
      <p v-if="formError" class="warning" role="status">{{ formError }}</p>
      <button type="submit" :disabled="locked"><Save :size="14" />保存全局 Lorebook</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>Lorebook 资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="reconcile"><RefreshCw :size="14" />核实原 Lorebook 请求</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
  </section>
</template>

<style scoped>
.current-lorebook-panel { display:grid; gap:10px; min-width:0; padding:12px; font-size:12px; }
header { display:flex; align-items:center; gap:6px; min-width:0; }
strong { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; padding:6px; border-radius:3px; }
header button { flex:0 0 28px; width:28px; height:28px; justify-content:center; }
ul { list-style:none; padding:0; margin:0; min-width:0; }
li>button { display:grid; width:100%; grid-template-columns:minmax(0,1fr) auto; text-align:left; border-bottom:1px solid #505059; }
li strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
small { grid-column:1/-1; overflow-wrap:anywhere; color:#b9bec6; }
form { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:10px; }
fieldset { display:grid; gap:10px; border:0; padding:0; margin:0; min-width:0; }
label { display:grid; gap:5px; min-width:0; }
output { overflow-wrap:anywhere; color:#b9bec6; }
input[type="text"] { box-sizing:border-box; width:100%; min-width:0; height:30px; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; font:inherit; }
.toggle { display:flex; align-items:center; gap:6px; }
.toggle input { width:16px; height:16px; margin:0; }
p { margin:0; color:#b9bec6; line-height:1.5; overflow-wrap:anywhere; }
.warning,.warning p { color:#e4a0a0; }
:disabled { opacity:.55; }
</style>
