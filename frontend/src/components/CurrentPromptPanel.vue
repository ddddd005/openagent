<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Plus, RefreshCw, Save, Trash2, X } from "lucide-vue-next";
import { usePromptResources } from "../application/promptResources";
import { clonePromptResource, isCurrentPromptResource, newPromptMember, newPromptResource,
  promptIdentity, promptResourceLabel, type CurrentPromptResource,
  type PromptMember, type PromptPresentation } from "../domain/workflowPromptResources";

const resources = usePromptResources();
const records = resources.records, loading = resources.loading, locked = resources.locked;
const error = resources.error, pending = resources.pending, busy = resources.busy;
const form = ref<CurrentPromptResource | null>(null), expectedSequence = ref(0), formError = ref("");
const identityKey = (record: CurrentPromptResource) => JSON.stringify(promptIdentity(record));
function edit(record: CurrentPromptResource | null) {
  if (locked.value) return;
  form.value = record ? clonePromptResource(record) : newPromptResource(2);
  expectedSequence.value = record?.update_sequence ?? 0;
  formError.value = "";
}
function addMember() {
  if (!form.value || locked.value || form.value.value.members.length >= 1024) return;
  form.value.value.members.push(newPromptMember("", form.value.data_schema_version));
  formError.value = "";
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
  if (!isCurrentPromptResource(form.value)) {
    formError.value = "提示词正文、呈现字段或成员身份无效，尚未提交";
    return;
  }
  formError.value = "";
  if (await resources.save(clonePromptResource(form.value), expectedSequence.value)) form.value = null;
}
onMounted(() => { void resources.refresh(); });
</script>

<template>
  <section class="current-prompt-panel" aria-label="新版提示词当前资源">
    <header>
      <strong>提示词 · 当前资源</strong>
      <button type="button" title="刷新提示词资源" aria-label="刷新提示词资源" :disabled="loading"
        @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增提示词资源" aria-label="新增提示词资源" :disabled="locked"
        @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length">暂无提示词当前资源</p>
    <ul>
      <li v-for="record in records" :key="identityKey(record)">
        <button type="button" :disabled="locked" @click="edit(record)">
          <strong :title="promptResourceLabel(record)">{{ promptResourceLabel(record) }}</strong>
          <span>{{ record.value.enabled ? '启用' : '已停用' }} · v{{ record.data_schema_version }} · s{{ record.update_sequence }}</span>
          <small :title="record.resource_id">{{ record.scope }} · {{ record.resource_id.slice(0, 8) }} · {{ record.value.members.length }} 个条目</small>
        </button>
      </li>
    </ul>
    <form v-if="form" aria-label="提示词当前资源表单" @submit.prevent="save">
      <header>
        <strong>{{ expectedSequence ? '更新当前资源' : '新增当前资源' }}</strong>
        <button type="button" title="取消提示词编辑" aria-label="取消提示词编辑" :disabled="locked"
          @click="form = null"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked">
        <label>范围<output>{{ form.scope }}</output></label>
        <label>资源 UUID<output>{{ form.resource_id }}</output></label>
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用资源</label>
        <section v-for="(member, index) in form.value.members" :key="member.id" class="prompt-member">
          <header>
            <strong>条目 {{ index + 1 }}</strong>
            <button type="button" title="移除草稿条目" aria-label="移除草稿条目"
              @click="removeMember(index)"><Trash2 :size="15" /></button>
          </header>
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
              <input v-model.number="member.presentation.depth" type="number" min="0" step="1"
                :aria-label="`条目 ${index + 1} 中区深度`" />
            </label>
            <label>顺序<input v-model.number="member.presentation.order" type="number" step="1"
              :aria-label="`条目 ${index + 1} 装配顺序`" /></label>
          </div>
        </section>
        <button type="button" :disabled="form.value.members.length >= 1024" @click="addMember">
          <Plus :size="14" />新增条目
        </button>
      </fieldset>
      <p v-if="formError" class="warning" role="status">{{ formError }}</p>
      <button type="submit" :disabled="locked"><Save :size="14" />保存当前资源</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>提示词资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="resources.reconcile()">
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
li>button { display:grid; width:100%; grid-template-columns:minmax(0,1fr) auto; text-align:left; border-bottom:1px solid #505059; }
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
