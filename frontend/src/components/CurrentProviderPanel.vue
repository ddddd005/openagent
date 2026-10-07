<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useProviderResources } from "../application/workflowResources";
import { cloneProvider, newProvider, type CurrentProvider } from "../domain/workflowModelResources";

const resources = useProviderResources();
const records = resources.records, loading = resources.loading, locked = resources.locked;
const error = resources.error, pending = resources.pending, busy = resources.busy;
const form = ref<CurrentProvider | null>(null), expectedSequence = ref(0);
function edit(record: CurrentProvider | null) {
  if (locked.value) return;
  form.value = record ? cloneProvider(record) : newProvider();
  expectedSequence.value = record?.update_sequence ?? 0;
}
async function save() {
  if (!form.value || locked.value) return;
  if (await resources.save(cloneProvider(form.value), expectedSequence.value)) form.value = null;
}
onMounted(() => { void resources.refresh(); });
</script>
<template>
  <section class="current-provider-panel" aria-label="新版供应商当前资源">
    <header>
      <strong>模型供应商 · 当前资源</strong>
      <button type="button" title="刷新供应商" aria-label="刷新供应商" :disabled="loading" @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增供应商资源" aria-label="新增供应商资源" :disabled="locked" @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length">暂无供应商当前资源</p>
    <ul>
      <li v-for="record in records" :key="record.scope + record.resource_id">
        <button type="button" :disabled="locked" @click="edit(record)">
          <strong>{{ record.value.name }}</strong><span>{{ record.value.enabled ? 'Chat' : '已停用' }} · s{{ record.update_sequence }}</span>
          <small>{{ record.scope }} · {{ record.value.base_url }}</small>
        </button>
      </li>
    </ul>
    <form v-if="form" @submit.prevent="save">
      <header><strong>{{ expectedSequence ? '更新当前资源' : '新增当前资源' }}</strong>
        <button type="button" title="取消编辑" aria-label="取消编辑" :disabled="locked" @click="form = null"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked">
        <label>名称<input v-model="form.value.name" required maxlength="128" /></label>
        <label>范围<output>{{ form.scope }}</output></label>
        <label>服务地址<input v-model="form.value.base_url" type="url" required maxlength="2048" autocomplete="off" /></label>
        <label>后端凭据引用<select v-model="form.value.credential_ref">
          <option :value="null">未配置</option><option value="env:DEEPSEEK_API_KEY">env:DEEPSEEK_API_KEY</option>
        </select></label>
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用</label>
      </fieldset>
      <p>凭据由启动后端的环境提供；引用可用性在运行准备时核实。</p>
      <button type="submit" :disabled="locked"><Save :size="14" />保存当前资源</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="resources.reconcile()"><RefreshCw :size="14" />核实原资源请求</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
  </section>
</template>
<style scoped>
.current-provider-panel { display:grid; gap:10px; min-width:0; padding:12px; font-size:12px; }
header { display:flex; align-items:center; gap:6px; } header strong { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; padding:6px; border-radius:3px; } header button { width:28px; height:28px; justify-content:center; }
ul { list-style:none; padding:0; margin:0; min-width:0; } li>button { display:grid; width:100%; min-width:0; grid-template-columns:minmax(0,1fr) auto; text-align:left; border-bottom:1px solid #505059; }
li strong { min-width:0; overflow-wrap:anywhere; } li span { white-space:nowrap; }
small { grid-column:1/-1; overflow-wrap:anywhere; color:#b9bec6; }
form { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:10px; }
fieldset { display:grid; gap:10px; border:0; padding:0; min-width:0; } label { display:grid; gap:5px; min-width:0; }
input,select { box-sizing:border-box; width:100%; min-width:0; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
.enabled { display:flex; align-items:center; gap:6px; } .enabled input { width:16px; height:16px; }
p { margin:0; color:#b9bec6; line-height:1.5; overflow-wrap:anywhere; } .warning p,.warning { color:#e4a0a0; } :disabled { opacity:.55; }
</style>
