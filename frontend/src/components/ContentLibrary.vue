<script setup lang="ts">
import { computed, onMounted } from "vue";
import { BookOpen, Plus, RefreshCw, Save, Trash2, Undo2 } from "lucide-vue-next";
import { useGlobalContentStore } from "../stores/globalContent";
import RolePlacementFields from "./RolePlacementFields.vue";
const props = defineProps<{ view: "sidebar" | "editor" }>();
const content = useGlobalContentStore();
const entries = computed(() => Object.values({ ...Object.fromEntries(content.records.map(r => [r.resource_id, r])), ...content.drafts }));
onMounted(() => { void content.initialize(); });
function addMember() {
  if (!content.selected) return;
  content.selected.members.push({ id: crypto.randomUUID(), name: "条目", text: "", role: "system",
    placement: "before", depth: null, order: content.selected.members.length, enabled: true });
}
</script>
<template>
  <section v-if="props.view === 'sidebar'" class="content-sidebar">
    <header><h1>内容</h1><button type="button" title="刷新内容" aria-label="刷新内容" :disabled="content.busy" @click="content.load()"><RefreshCw :size="16" /></button></header>
    <div class="content-create"><button type="button" :disabled="!!content.pending" @click="content.create('global_prompt')"><Plus :size="14" />提示词</button><button type="button" :disabled="!!content.pending" @click="content.create('role_card')"><Plus :size="14" />角色卡</button></div>
    <ul><li v-for="entry in entries" :key="entry.resource_id"><button type="button" :class="{ active: content.selectedId === entry.resource_id }" @click="content.select(entry.resource_id)"><BookOpen :size="16" /><span>{{ entry.name }}</span><small>{{ entry.kind === 'role_card' ? '角色卡' : '提示词' }}</small></button></li></ul>
  </section>
  <section v-else class="content-editor">
    <header><h2>{{ content.selected?.name ?? '内容' }}</h2><span v-if="content.dirty" class="content-unsaved">未保存</span><div class="content-actions"><button type="button" title="放弃内容修改" aria-label="放弃内容修改" :disabled="!content.dirty || !!content.pending" @click="content.discard()"><Undo2 :size="17" /></button><button type="button" title="删除内容" aria-label="删除内容" :disabled="!content.selected || !!content.pending || content.busy" @click="content.remove()"><Trash2 :size="17" /></button><button type="button" title="保存内容" aria-label="保存内容" :disabled="!content.dirty || !!content.pending || content.busy" @click="content.save()"><Save :size="17" /></button></div></header>
    <div v-if="content.pending" class="content-notice">内容提交结果待核实 <button type="button" :disabled="content.busy" @click="content.save(true)">核实原请求</button></div>
    <p v-if="content.error" class="content-notice" role="status">{{ content.error }}</p>
    <div v-if="content.selected" class="content-fields">
      <label>名称<input v-model="content.selected.name" maxlength="128" :disabled="!!content.pending" /></label>
      <label class="content-check"><input v-model="content.selected.enabled" type="checkbox" :disabled="!!content.pending" />启用</label>
      <section v-for="(member, index) in content.selected.members" :key="member.id" class="content-member">
        <header><input v-model="member.name" :disabled="!!content.pending" /><button type="button" title="删除条目" aria-label="删除条目" :disabled="content.selected.members.length === 1 || !!content.pending" @click="content.selected.members.splice(index, 1)"><Trash2 :size="15" /></button></header>
        <textarea v-model="member.text" rows="10" :disabled="!!content.pending"></textarea>
        <RolePlacementFields :model-value="{ role: member.role, placement: member.placement, depth: member.depth, order: member.order, enabled: member.enabled }" @update:model-value="!content.pending && Object.assign(member, $event)" />
      </section>
      <button type="button" class="content-add" :disabled="!!content.pending" @click="addMember"><Plus :size="15" />条目</button>
    </div>
  </section>
</template>
<style scoped>
.content-sidebar { display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;padding:18px 12px;color:#d5dade; }
header { display:flex;align-items:center;gap:12px; }
h1,h2 { font-size:14px;margin:0;overflow-wrap:anywhere; }
button { display:flex;align-items:center;gap:7px;padding:8px;font-size:12px; }
header>button,.content-actions button { margin-left:auto;width:32px;height:32px;justify-content:center; }
.content-create,.content-actions { display:flex;gap:8px; }.content-create { margin:15px 0; }
ul { list-style:none;padding:0;margin:0;overflow:auto; }li button { width:100%;text-align:left;border-radius:4px; }li button.active { background:#354640;color:#b9e1cf; }li span { flex:1;min-width:0;overflow-wrap:anywhere; }small { color:#919ca5;font-size:10px; }
.content-editor { flex:1;min-width:0;min-height:0;overflow:auto;background:#202327;color:#d5dade; }
.content-editor>header { padding:12px 22px;border-bottom:1px solid #43484f; }.content-actions { margin-left:auto; }
.content-unsaved { color:#e3c668;font-size:11px; }.content-fields { max-width:850px;margin:0 auto;padding:24px;display:grid;gap:18px; }
label { display:grid;gap:8px;font-size:12px; }.content-check { display:flex;align-items:center; }
input,textarea { box-sizing:border-box;min-width:0;padding:8px;border:1px solid #535b65;border-radius:4px;background:#191c20;color:#e3e7eb;font:inherit; }
textarea { width:100%;resize:vertical;line-height:1.6;margin:12px 0; }.content-member { border-top:1px solid #454c54;padding-top:18px; }.content-member header input { flex:1; }
.content-notice { padding:10px 22px;background:#422a2c;color:#f2b9b0;font-size:12px; }
</style>
