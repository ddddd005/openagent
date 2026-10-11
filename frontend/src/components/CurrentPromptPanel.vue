<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Archive, Copy, Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { usePromptResources } from "../application/promptResources";
import { clonePromptResource, duplicatePromptResource, isCurrentPromptResource, newPromptResource,
  promptIdentity, promptResourceLabel, type CurrentPromptResource } from "../domain/workflowPromptResources";
import { useResourceFormEditor } from "../composables/useResourceFormEditor";
import PromptMembersEditor from "./PromptMembersEditor.vue";
import { useContentMetadata } from "../application/contentMetadata";
import ContentMetadataFields from "./ContentMetadataFields.vue";

const resources = usePromptResources();
const records = resources.records, loading = resources.loading;
const metadata = useContentMetadata();
watch(records, values => { for (const record of values) void metadata.forReference(promptIdentity(record)).load(); },
  { immediate: true });
const resourceLabel = (record: CurrentPromptResource) =>
  metadata.forReference(promptIdentity(record)).record.value?.display_name || promptResourceLabel(record);
const submitting = ref(false);
const locked = computed(() => resources.locked.value || submitting.value);
const error = resources.error, pending = resources.pending, busy = resources.busy;
const editor = useResourceFormEditor<CurrentPromptResource>(() => newPromptResource(2));
const { form, expectedSequence } = editor;
onBeforeUnmount(resources.setCompletionHandler(async request => {
  editor.acceptSubmitted(request.record, request.expected_sequence);
  return true;
}));
const formError = ref("");
const membersValid = ref(true);
const numberBasis = computed(() => ({ resourceId: form.value?.resource_id ?? null,
  scope: form.value?.scope ?? null, sequence: expectedSequence.value, format: form.value?.data_schema_version ?? null }));
const numbersValid = computed(() => membersValid.value);
const identityKey = (record: CurrentPromptResource) => JSON.stringify(promptIdentity(record));
function edit(record: CurrentPromptResource | null) {
  if (locked.value) return;
  editor.edit(record);
  formError.value = "";
}
function duplicate(record: CurrentPromptResource) {
  if (locked.value) return;
  formError.value = "";
  try {
    editor.edit(duplicatePromptResource(record), true);
  } catch { formError.value = "提示词组字段无效，未创建副本"; }
}
async function save() {
  if (!form.value || locked.value) return;
  if (!numbersValid.value) { formError.value = "数字字段不完整，原文已保留，尚未提交"; return; }
  if (!isCurrentPromptResource(form.value)) {
    formError.value = "提示词正文、呈现字段或成员身份无效，尚未提交";
    return;
  }
  formError.value = "";
  submitting.value = true;
  const record = clonePromptResource(form.value), sequence = expectedSequence.value;
  try {
    await resources.save(record, sequence);
  } finally { submitting.value = false; }
}
async function reconcile() {
  await resources.reconcile();
}
async function abandon() {
  await resources.abandon(window.confirm("归档并解除原提示词请求的本机锁定？这不会撤销已发生的保存，也不会重新提交。"));
}
onMounted(() => { void resources.refresh(); });
</script>

<template>
  <section class="current-prompt-panel" aria-label="新版提示词当前资源">
    <header>
      <strong>提示词组 · 全局资源</strong>
      <button type="button" title="刷新提示词资源" aria-label="刷新提示词资源" :disabled="loading"
        @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增提示词资源" aria-label="新增提示词资源" :disabled="locked"
        @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length">暂无提示词当前资源</p>
    <button v-if="resources.legacyRaw.value" type="button" :disabled="busy || submitting" @click="resources.reconcileLegacyPending()"><RefreshCw :size="14" />核实旧提示词原请求</button>
    <ul>
      <li v-for="record in records" :key="identityKey(record)">
        <button type="button" class="resource-edit" :disabled="locked" @click="edit(record)">
          <strong :title="resourceLabel(record)">{{ resourceLabel(record) }}</strong>
          <span>{{ record.value.enabled ? '启用' : '已停用' }} · v{{ record.data_schema_version }} · s{{ record.update_sequence }}</span>
          <small>{{ record.value.members.length }} 个条目</small>
        </button>
        <button type="button" class="resource-copy" title="复制为新提示词组"
          :aria-label="`复制提示词组 ${record.scope} ${record.resource_id}`" :disabled="locked"
          @click="duplicate(record)"><Copy :size="15" /></button>
      </li>
    </ul>
    <form v-if="form" aria-label="提示词当前资源表单" @submit.prevent="save">
      <header>
        <strong>{{ expectedSequence ? '更新当前资源' : '新增当前资源' }}</strong>
        <button type="button" title="取消提示词编辑" aria-label="取消提示词编辑" :disabled="locked"
          @click="editor.close()"><X :size="15" /></button>
      </header>
      <ContentMetadataFields v-if="expectedSequence" :reference="promptIdentity(form)" :fallback="promptResourceLabel(form)" />
      <fieldset :disabled="locked">
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用资源</label>
        <PromptMembersEditor :key="identityKey(form)" :members="form.value.members" :version="form.data_schema_version" :disabled="locked"
          :persist-numeric-drafts="false"
          :draft-scope="`resource:prompt:${form.scope}:${form.resource_id}`" :draft-basis="numberBasis"
          @validity="membersValid = $event" />
        <details><summary>技术详情</summary><label>范围<output>{{ form.scope }}</output></label><label>资源 UUID<output>{{ form.resource_id }}</output></label></details>
      </fieldset>
      <p v-if="formError" class="warning" role="status">{{ formError }}</p>
      <button type="submit" :disabled="locked || !numbersValid"><Save :size="14" />{{ expectedSequence ? '保存当前资源' : '创建提示词资源' }}</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>提示词资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="reconcile">
        <RefreshCw :size="14" />核实原提示词请求
      </button>
      <button type="button" :disabled="busy" @click="abandon"><Archive :size="14" />归档并解除锁定</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
  </section>
</template>

<style scoped>
.current-prompt-panel { display:grid; gap:10px; min-width:0; padding:12px; font-size:12px; }
header { display:flex; align-items:center; gap:6px; }
header strong, li strong { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; padding:6px; border-radius:3px; }
header button { flex:0 0 28px; width:28px; height:28px; justify-content:center; }
ul { list-style:none; padding:0; margin:0; min-width:0; }
li { display:grid; grid-template-columns:minmax(0,1fr) 28px; gap:4px; min-width:0; border-bottom:1px solid #505059; }
.resource-edit { display:grid; width:100%; min-width:0; grid-template-columns:minmax(0,1fr) auto; text-align:left; }
.resource-copy { width:28px; height:28px; padding:0; justify-content:center; align-self:center; }
li strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
small { grid-column:1/-1; overflow-wrap:anywhere; color:#b9bec6; }
form { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:10px; }
fieldset { display:grid; gap:10px; border:0; padding:0; margin:0; min-width:0; }
label { display:grid; gap:5px; min-width:0; }
output { overflow-wrap:anywhere; color:#b9bec6; }
textarea { box-sizing:border-box; width:100%; min-width:0; padding:6px; resize:vertical; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; font:inherit; line-height:1.6; }
input[type="number"], select { box-sizing:border-box; width:100%; min-width:0; height:30px; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; font:inherit; }
.enabled { display:flex; align-items:center; gap:6px; }
.enabled input { width:16px; height:16px; margin:0; }
.prompt-member { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:10px; }
.member-id { font:10px/1.5 Consolas,monospace; color:#949ba1; overflow-wrap:anywhere; }
.presentation-row { display:flex; align-items:end; gap:10px; min-width:0; }
.presentation-row>label { flex:1; }
.presentation-row>.enabled { flex:0 0 auto; height:30px; }
.presentation-position { display:grid; gap:5px; }
.presentation-segmented { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); height:30px; border:1px solid #454548; border-radius:4px; overflow:hidden; }
.presentation-segmented button { justify-content:center; min-width:0; padding:0 2px; font-size:10px; white-space:nowrap; }
.presentation-segmented button+button { border-left:1px solid #454548; }
.presentation-segmented button.active { color:#c2dfd2; background:#35453d; }
p { margin:0; color:#b9bec6; line-height:1.5; overflow-wrap:anywhere; }
.warning p,.warning { color:#e4a0a0; }
:disabled { opacity:.55; }
</style>
