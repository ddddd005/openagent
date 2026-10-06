<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Plus, RefreshCw, Save, Trash2, X } from "lucide-vue-next";
import { useExposuresStore } from "../stores/exposures";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import type { ExposureField } from "../domain/exposure";

const props = defineProps<{ workflowId: string; stage: "A" | "B" }>();
const emit = defineEmits<{ close: [] }>();
const exposures = useExposuresStore();
const runtime = useWorkbenchRuntimeStore();
const publicName = ref(`${props.stage}.state`);
const kind = ref<"state" | "result">("state");
const fields = ref<ExposureField[]>(["status", "run_id", "revision"]);
const localError = ref("");
const entries = computed(() => exposures.registrations.filter((row) =>
  row.workflowId === props.workflowId && row.stage === props.stage));
watch(kind, (value) => {
  publicName.value = `${props.stage}.${value}`;
  fields.value = value === "state" ? ["status", "run_id", "revision"]
    : props.stage === "A" ? ["result_text"] : ["delivered_text"];
});
async function register() {
  localError.value = "";
  try {
    exposures.register(props.workflowId, props.stage, publicName.value, kind.value, fields.value);
    await publish();
  } catch (failure) {
    localError.value = failure instanceof Error ? failure.message : "注册配置无效";
  }
}
async function publish() {
  if (await exposures.publish(props.workflowId) && runtime.session)
    await exposures.observePublished(props.workflowId, runtime.session);
}
async function remove(id: string) {
  exposures.remove(id);
  await publish();
}
</script>

<template>
  <Teleport to="body">
    <div class="exposure-shade" @click.self="emit('close')">
      <section class="exposure-dialog" role="dialog" aria-modal="true" aria-labelledby="exposure-heading">
        <header>
          <h2 id="exposure-heading">Agent {{ stage }} · 信息注册</h2>
          <button type="button" title="关闭" aria-label="关闭信息注册" @click="emit('close')"><X :size="17" /></button>
        </header>
        <div class="exposure-source">
          <span>公开投影 v1</span>
          <span>{{ runtime.sessionId ? `会话 ${runtime.sessionId.slice(0, 8)}` : '未绑定会话' }}</span>
          <button type="button" title="刷新公开投影" aria-label="刷新公开投影" :disabled="!runtime.sessionId" @click="runtime.refresh()"><RefreshCw :size="14" /></button>
        </div>
        <form @submit.prevent="register">
          <label>公开名称<input v-model="publicName" maxlength="80" required /></label>
          <label>内容<select v-model="kind"><option value="state">状态</option><option value="result">{{ stage === 'A' ? '业务结果' : '已交付结果' }}</option></select></label>
          <fieldset v-if="kind === 'state'">
            <legend>公开字段</legend>
            <label v-for="field in (['status','run_id','revision','budget'] as const)" :key="field"><input v-model="fields" type="checkbox" :value="field" />{{ field }}</label>
          </fieldset>
          <p v-if="localError" class="exposure-field-error" role="alert">{{ localError }}</p>
          <button class="exposure-register" type="submit" :disabled="exposures.locked"><Plus :size="14" />注册</button>
        </form>
        <ul class="exposure-list">
          <li v-for="entry in entries" :key="entry.id">
            <div class="exposure-entry-heading">
              <strong>{{ entry.publicName }}</strong>
              <span>{{ entry.type }} v{{ entry.schemaVersion }}</span>
              <button type="button" title="撤销注册" :aria-label="`撤销 ${entry.publicName}`" :disabled="exposures.locked" @click="remove(entry.id)"><Trash2 :size="14" /></button>
            </div>
            <pre v-if="exposures.observations[entry.id]?.availability === 'available'">{{ JSON.stringify(exposures.observations[entry.id], null, 2) }}</pre>
            <p v-else>{{ exposures.observations[entry.id]?.reason ?? '未绑定会话，暂无观察值' }}</p>
          </li>
        </ul>
        <footer>
          <span>{{ exposures.referenceFor(workflowId) ? `已发布 r${exposures.referenceFor(workflowId)!.revision}` : '未发布' }}</span>
          <button type="button" title="发布注册声明" aria-label="发布注册声明" :disabled="exposures.locked" @click="publish"><Save :size="14" /></button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.exposure-shade { position:fixed; inset:0; z-index:200; background:#10182066; display:grid; place-items:center; }
.exposure-dialog { width:620px; max-width:calc(100vw - 40px); max-height:80vh; overflow:auto; background:#29292c; border:1px solid #505056; border-radius:6px; padding:16px; box-shadow:0 12px 35px #0005; color:#dededf; }
.exposure-dialog header,.exposure-source,.exposure-entry-heading { display:flex; align-items:center; gap:10px; }
.exposure-dialog header { justify-content:space-between; }
.exposure-dialog h2 { font-size:15px; margin:0; }
.exposure-dialog button { display:inline-flex; align-items:center; gap:5px; padding:5px; background:#333336; border:1px solid #4d4d52; border-radius:3px; color:#d2d2d8; cursor:pointer; }
.exposure-source { margin:12px 0; font-size:11px; color:#a5a5ad; }
.exposure-source button { margin-left:auto; }
.exposure-dialog form { display:grid; grid-template-columns:1fr 180px; gap:10px; padding:12px 0; border-top:1px solid #454549; border-bottom:1px solid #454549; }
.exposure-dialog form > label { display:grid; gap:5px; font-size:12px; }
.exposure-dialog input:not([type=checkbox]),.exposure-dialog select { border:1px solid #4d4d52; background:#202022; color:#dededf; padding:6px; min-width:0; font-size:12px; }
.exposure-dialog input[type=checkbox] { width:14px; height:14px; padding:0; margin:0; flex-shrink:0; accent-color:#85b79f; }
.exposure-dialog fieldset { grid-column:1/-1; border:0; margin:0; padding:0; display:flex; gap:15px; }
.exposure-dialog legend { font-size:12px; margin-bottom:6px; }
.exposure-dialog fieldset label { font-size:12px; display:inline-flex; gap:5px; align-items:center; }
.exposure-register { justify-self:start; }
.exposure-availability,.exposure-field-error { grid-column:1/-1; margin:0; font-size:12px; }
.exposure-field-error { color:#ef969c; }
.exposure-list { list-style:none; padding:0; margin:12px 0; }
.exposure-list li { padding:10px 0; border-bottom:1px solid #454549; }
.exposure-entry-heading strong { font-size:13px; }
.exposure-entry-heading span { font-size:10px; color:#a5a5ad; overflow-wrap:anywhere; }
.exposure-entry-heading button { margin-left:auto; }
.exposure-list p { font-size:12px; color:#a5a5ad; }
.exposure-list pre { max-height:190px; overflow:auto; padding:8px; background:#202022; color:#d2d2d8; font-size:11px; white-space:pre-wrap; overflow-wrap:anywhere; }
.exposure-dialog footer { font-size:11px; color:#a5a5ad; }
</style>
