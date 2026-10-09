<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { ArrowDown, ArrowUp, Copy, Import, Plus, RefreshCw, Save, Trash2, X } from "lucide-vue-next";
import { usePromptResources } from "../application/promptResources";
import { clonePromptResource, duplicatePromptResource, isCurrentPromptResource, movePromptMember, newPromptMember, newPromptResource,
  promptIdentity, promptResourceLabel, type CurrentPromptResource,
  type PromptMember, type PromptPresentation } from "../domain/workflowPromptResources";
import { useResourceFormDraft } from "../composables/useResourceFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";
import RawNumberInput from "./RawNumberInput.vue";
import { useFormDraftsStore } from "../stores/formDrafts";

const resources = usePromptResources();
const records = resources.records, loading = resources.loading;
const submitting = ref(false);
const locked = computed(() => resources.locked.value || submitting.value || localDraft.loading.value || localDraft.conflict.value);
const error = resources.error, pending = resources.pending, busy = resources.busy;
const editor = useResourceFormDraft<CurrentPromptResource>("prompt", () => newPromptResource(2), promptResourceLabel);
const { form, expectedSequence, localDraft, choices } = editor;
const formError = ref("");
const numericValidity = ref<Record<string, boolean>>({});
const forms = typeof window === "undefined" ? null : useFormDraftsStore();
const numberScope = (member: PromptMember, field: string) =>
  `resource:prompt:${form.value?.scope}:${form.value?.resource_id}:member:${member.id}:${field}`;
const numberBasis = computed(() => ({ resourceId: form.value?.resource_id ?? null,
  scope: form.value?.scope ?? null, sequence: expectedSequence.value, format: form.value?.data_schema_version ?? null }));
const numbersValid = computed(() => form.value?.value.members.every(member =>
  numericValidity.value[numberScope(member, "order")] !== false
  && (member.presentation.placement !== "middle" || numericValidity.value[numberScope(member, "depth")] !== false)) ?? true);
watch(() => form.value?.resource_id, () => { numericValidity.value = {}; });
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
function addMember() {
  if (!form.value || locked.value || form.value.value.members.length >= 1024) return;
  const members = form.value.value.members;
  const order = members.length ? Math.max(...members.map(member => member.presentation.order)) + 1 : 0;
  if (!Number.isSafeInteger(order)) { formError.value = "条目顺序已达上限，先调整顺序"; return; }
  const member = newPromptMember("", form.value.data_schema_version);
  member.presentation.order = order;
  members.push(member);
  formError.value = "";
}
function moveMember(index: number, offset: number) {
  if (!form.value || locked.value) return;
  if (!numbersValid.value) { formError.value = "数字字段不完整，原文已保留，尚未提交"; return; }
  if (movePromptMember(form.value.value.members, index, offset)) formError.value = "";
}
function removeMember(index: number) {
  if (!form.value || locked.value) return;
  form.value.value.members.splice(index, 1);
  formError.value = "";
}
function changePlacement(member: PromptMember, placement: PromptPresentation["placement"]) {
  if (locked.value) return;
  member.presentation.placement = placement;
  member.presentation.depth = placement === "middle" ? member.presentation.depth ?? 0 : null;
}
function changeLifecycle(member: PromptMember, lifecycle: "per_request" | "context_once") {
  if (locked.value) return;
  member.lifecycle = lifecycle;
  if (lifecycle === "per_request") member.compaction = "never";
}
function changeRole(member: PromptMember, role: PromptPresentation["role"]) {
  if (locked.value) return;
  member.presentation.role = role;
  if (role === "system" && member.compaction) member.compaction = "never";
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
  const originalNumberBasis = numberBasis.value;
  const scopes = record.value.members.flatMap(member => [numberScope(member, "order"), numberScope(member, "depth")]);
  try {
    if (!await localDraft.flush() || forms && !await forms.flushAll()) return;
    if (await resources.save(record, sequence)) {
      if (forms) for (const scope of scopes) if (forms.statuses[scope]) await forms.remove(scope, originalNumberBasis);
      editor.close();
    }
  } finally { submitting.value = false; }
}
async function reconcile() {
  if (await resources.reconcile()) editor.close();
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
    <FormDraftNotice v-if="localDraft.message.value" :message="localDraft.message.value" :conflict="localDraft.conflict.value" @restore="localDraft.restoreConflict" @discard="localDraft.discardConflict" />
    <button v-if="resources.legacyRaw.value" type="button" :disabled="busy || submitting || localDraft.loading.value || localDraft.conflict.value" @click="resources.importLegacyPending()"><Import :size="14" />导入旧提示词请求</button>
    <select v-if="choices.length" aria-label="未提交提示词草稿" value="" :disabled="locked" @change="editor.resume(($event.target as HTMLSelectElement).value)">
      <option value="">未提交草稿</option><option v-for="choice in choices" :key="choice.id" :value="choice.id">{{ choice.label }}</option>
    </select>
    <ul>
      <li v-for="record in records" :key="identityKey(record)">
        <button type="button" class="resource-edit" :disabled="locked" @click="edit(record)">
          <strong :title="promptResourceLabel(record)">{{ promptResourceLabel(record) }}</strong>
          <span>{{ record.value.enabled ? '启用' : '已停用' }} · v{{ record.data_schema_version }} · s{{ record.update_sequence }}</span>
          <small :title="record.resource_id">{{ record.scope }} · {{ record.resource_id.slice(0, 8) }} · {{ record.value.members.length }} 个条目</small>
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
      <fieldset :disabled="locked || localDraft.loading.value">
        <label>范围<output>{{ form.scope }}</output></label>
        <label>资源 UUID<output>{{ form.resource_id }}</output></label>
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用资源</label>
        <section v-for="(member, index) in form.value.members" :key="member.id" class="prompt-member">
          <header>
            <strong>条目 {{ index + 1 }}</strong>
            <button type="button" title="上移条目" :aria-label="`条目 ${index + 1} 上移`"
              :disabled="locked || index === 0" @click="moveMember(index, -1)"><ArrowUp :size="15" /></button>
            <button type="button" title="下移条目" :aria-label="`条目 ${index + 1} 下移`"
              :disabled="locked || index === form.value.members.length - 1" @click="moveMember(index, 1)"><ArrowDown :size="15" /></button>
            <button type="button" title="移除草稿条目" aria-label="移除草稿条目"
              @click="removeMember(index)"><Trash2 :size="15" /></button>
          </header>
          <small class="member-id">{{ member.id }}</small>
          <label>正文<textarea v-model="member.text" rows="5" :aria-label="`条目 ${index + 1} 正文`"></textarea></label>
          <div class="presentation-row">
            <label>Role<select v-model="member.presentation.role" :aria-label="`条目 ${index + 1} Role`"
              @change="changeRole(member, member.presentation.role)">
              <option value="system">system</option>
              <option value="user">user</option>
              <option value="assistant">assistant</option>
            </select></label>
            <label class="enabled"><input v-model="member.presentation.enabled" type="checkbox" />启用条目</label>
          </div>
          <div v-if="form.data_schema_version === 2" class="presentation-row">
            <label>进入上下文
              <select v-model="member.lifecycle" :aria-label="`条目 ${index + 1} 生命周期`"
                @change="changeLifecycle(member, member.lifecycle ?? 'per_request')">
                <option value="per_request">每次装配</option>
                <option value="context_once">仅一次</option>
              </select>
            </label>
            <label>精简许可
              <select v-model="member.compaction" :aria-label="`条目 ${index + 1} 精简许可`"
                :disabled="member.lifecycle !== 'context_once' || member.presentation.role === 'system'">
                <option value="never">保留原文</option>
                <option value="allowed">允许精简</option>
              </select>
            </label>
          </div>
          <div class="presentation-position">
            <span>位置</span>
            <div class="presentation-segmented" role="group" :aria-label="`条目 ${index + 1} 装配位置`">
              <button v-for="option in ([['before', '上下文前'], ['middle', '上下文中'], ['after', '上下文后']] as const)"
                :key="option[0]" type="button" :class="{ active: member.presentation.placement === option[0] }"
                :aria-pressed="member.presentation.placement === option[0]"
                @click="changePlacement(member, option[0])">{{ option[1] }}</button>
            </div>
          </div>
          <div class="presentation-row">
            <label v-if="member.presentation.placement === 'middle'">深度
              <RawNumberInput v-model="member.presentation.depth" :min="0" integer
                :disabled="locked" :draft-scope="numberScope(member, 'depth')" :draft-basis="numberBasis"
                :aria-label="`条目 ${index + 1} 中区深度`"
                @validity="numericValidity[numberScope(member, 'depth')] = $event" />
            </label>
            <label>顺序<RawNumberInput v-model="member.presentation.order" integer
              :disabled="locked" :draft-scope="numberScope(member, 'order')" :draft-basis="numberBasis"
              :aria-label="`条目 ${index + 1} 装配顺序`"
              @validity="numericValidity[numberScope(member, 'order')] = $event" /></label>
          </div>
        </section>
        <button type="button" :disabled="form.value.members.length >= 1024" @click="addMember">
          <Plus :size="14" />新增条目
        </button>
      </fieldset>
      <p v-if="formError" class="warning" role="status">{{ formError }}</p>
      <button type="submit" :disabled="locked || !numbersValid"><Save :size="14" />保存当前资源</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>提示词资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="reconcile">
        <RefreshCw :size="14" />核实原提示词请求
      </button>
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
