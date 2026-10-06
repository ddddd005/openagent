<script setup lang="ts">
import { onMounted } from "vue";
import { Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import type { ModelConfigurationField } from "../domain/modelConfiguration";

const models = useModelConfigurationStore();
const fieldError = (field: ModelConfigurationField) => models.providerDiagnostics.find((row) => row.field === field);
onMounted(() => { if (!models.providersLoaded) void models.loadProviders(); });
</script>

<template>
  <section class="provider-panel" aria-labelledby="provider-heading">
    <header class="workbench-panel-heading">
      <h1 id="provider-heading">供应商</h1>
      <span class="provider-scope">全局</span>
      <button type="button" title="刷新供应商" aria-label="刷新供应商" :disabled="models.loading || models.locked" @click="models.loadProviders()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增供应商" aria-label="新增供应商" :disabled="models.locked" @click="models.editProvider(null)"><Plus :size="16" /></button>
    </header>
    <div class="provider-scroll">
      <p v-if="models.loading" class="provider-status" role="status">读取中</p>
      <p v-else-if="models.providersLoaded && !models.providers.length" class="provider-status">暂无供应商</p>
      <ul class="provider-list" aria-label="全局供应商列表">
        <li v-for="provider in models.providers" :key="provider.provider_id">
          <button type="button" :aria-pressed="models.providerForm?.provider_id === provider.provider_id" :disabled="models.locked" @click="models.editProvider(provider)">
            <strong>{{ provider.name }}</strong>
            <span>{{ provider.enabled ? 'Chat' : '已停用' }} · r{{ provider.revision }}</span>
            <small :title="provider.base_url">{{ provider.base_url }}</small>
            <small class="provider-credential">{{ provider.credential_ref ? '后端凭据引用已配置' : '后端凭据引用未配置' }}</small>
          </button>
        </li>
      </ul>
      <form v-if="models.providerForm" class="provider-form" @submit.prevent="models.saveProvider()">
        <header><strong>{{ models.providerForm.revision ? '编辑供应商' : '新增供应商' }}</strong><button type="button" title="取消编辑" aria-label="取消编辑" :disabled="models.locked" @click="models.providerForm = null"><X :size="15" /></button></header>
        <fieldset :disabled="models.locked">
          <label>名称<input v-model="models.providerForm.name" maxlength="128" required :aria-invalid="!!fieldError('name')" :aria-describedby="fieldError('name') ? 'provider-name-error' : undefined" />
            <small v-if="fieldError('name')" id="provider-name-error" class="field-error">{{ fieldError('name')?.message }} [{{ fieldError('name')?.code }}]</small>
          </label>
          <label>协议<output>Chat Completions</output></label>
          <label>服务地址<input v-model="models.providerForm.base_url" type="url" maxlength="2048" required autocomplete="off" :aria-invalid="!!fieldError('base_url')" :aria-describedby="fieldError('base_url') ? 'provider-address-error' : undefined" />
            <small v-if="fieldError('base_url')" id="provider-address-error" class="field-error">{{ fieldError('base_url')?.message }} [{{ fieldError('base_url')?.code }}]</small>
          </label>
          <label>后端凭据引用<select v-model="models.providerForm.credential_ref"><option :value="null">未配置</option><option value="env:DEEPSEEK_API_KEY">DEEPSEEK_API_KEY</option></select></label>
          <label class="provider-enabled"><input v-model="models.providerForm.enabled" type="checkbox" />启用</label>
        </fieldset>
        <dl class="provider-capabilities" aria-label="现有 Chat 适配器能力">
          <div><dt>流式</dt><dd>关闭</dd></div>
          <div><dt>thinking</dt><dd>disabled</dd></div>
          <div><dt>工具调用</dt><dd>支持</dd></div>
        </dl>
        <button class="provider-save" type="submit" :disabled="models.locked"><Save :size="14" />保存</button>
      </form>
      <div v-if="models.pending" class="provider-warning" role="status">
        <span>提交结果待核实</span>
        <button type="button" :disabled="models.busy" @click="models.reconcile()"><RefreshCw :size="14" />核实原请求</button>
      </div>
      <p v-if="models.error" class="provider-error" role="status">{{ models.error.reason }}</p>
    </div>
  </section>
</template>

<style scoped>
.provider-panel { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; }
.provider-scope { margin-left: auto; color: #9eada7; font-size: 11px; }
.workbench-panel-heading button, .provider-form header button { display: grid; flex: 0 0 28px; place-items: center; width: 28px; height: 28px; padding: 0; border-radius: 3px; }
.provider-scroll { flex: 1; min-height: 0; padding: 10px 12px; overflow: auto; }
.provider-list { margin: 0; padding: 0; list-style: none; }
.provider-list button { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 8px; width: 100%; padding: 10px 8px; border-bottom: 1px solid #3e4147; text-align: left; }
.provider-list strong { overflow-wrap: anywhere; font-size: 12px; font-weight: 500; }
.provider-list span, .provider-list small { color: #a1a6af; font-size: 10px; }
.provider-list small { grid-column: 1 / -1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.provider-list button[aria-pressed="true"] { background: #364b44; }
.provider-form { margin-top: 14px; border-top: 1px solid #52535a; }
.provider-form header { display: flex; align-items: center; justify-content: space-between; height: 38px; font-size: 12px; }
.provider-form fieldset { display: grid; gap: 12px; min-width: 0; margin: 0; padding: 0; border: 0; }
.provider-form label { display: grid; gap: 6px; color: #b9bec6; font-size: 11px; }
.provider-form input:not([type="checkbox"]), .provider-form select { box-sizing: border-box; width: 100%; min-width: 0; height: 30px; padding: 0 7px; border: 1px solid #51525b; border-radius: 3px; background: #202126; color: #e1e3e6; font: inherit; }
.provider-form label.provider-enabled { display: flex; align-items: center; gap: 7px; }
.provider-form output { color: #d9dfe4; font-size: 11px; }
.field-error { color: #f4b4ae; font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.provider-list small.provider-credential { color: #c2c5b0; }
.provider-capabilities { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; margin: 14px 0 0; color: #aab4bf; font-size: 10px; }
.provider-capabilities div { display: flex; gap: 6px; align-items: center; }
.provider-capabilities dt, .provider-capabilities dd { margin: 0; }
.provider-capabilities dd { color: #c6d5cf; }
.provider-enabled input { width: 14px; height: 14px; padding: 0; }
.provider-save, .provider-warning button { display: inline-flex; align-items: center; gap: 7px; height: 30px; margin-top: 12px; padding: 0 10px; border: 1px solid #526c5f; border-radius: 3px; color: #b6dfc7; font-size: 11px; }
.provider-error, .provider-warning { color: #f4b4ae; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.provider-warning { display: grid; margin-top: 15px; }
.provider-status { color: #a4abb4; font-size: 11px; }
</style>
