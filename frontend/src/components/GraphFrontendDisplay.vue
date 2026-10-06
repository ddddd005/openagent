<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { createWorkflowFrontendDisplayAccess, type FrontendResolvedEntry } from "../application/workflowFrontendDisplay";
import { frontendDisplayEntries } from "../domain/frontendDisplay";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
const props = defineProps<{ objectKey: string }>();
const sdk = useWorkflowFrontendSdk();
const object = computed(() => sdk.session.value?.objects?.[props.objectKey]);
const entries = computed(() => object.value ? frontendDisplayEntries(object.value) : null);
const scope = computed(() => object.value && sdk.session.value && object.value.binding.readers[0]
  ? { sessionId: sdk.session.value.workflow_session_id, objectKey: props.objectKey,
    revisionId: object.value.revision_id, nodeId: object.value.binding.readers[0] } : null);
const basis = () => JSON.stringify([sdk.lifecycle.value, scope.value, object.value?.deleted, entries.value]);
const access = createWorkflowFrontendDisplayAccess(basis, sdk.readArtifact);
const resolved = ref<FrontendResolvedEntry[]>([]), loading = ref(false);
let generation = 0;
async function read() {
  const token = ++generation;
  access.invalidate(); resolved.value = []; loading.value = false;
  if (!scope.value || !entries.value?.length) return;
  loading.value = true;
  const result = await access.display(scope.value, entries.value);
  if (token !== generation) return;
  if (result) resolved.value = result;
  loading.value = false;
}
watch(basis, () => { void read(); }, { immediate: true, flush: "sync" });
onUnmounted(() => { generation++; access.invalidate(); });
const roles = { user: "用户", assistant: "助手", system: "系统" };
</script>
<template>
  <section class="frontend-display" aria-label="正式前端展示">
    <header><strong>{{ objectKey }} · 展示</strong><button type="button" title="重新读取展示来源" aria-label="重新读取展示来源" :disabled="loading || !scope" @click="read"><RefreshCw :size="13" /></button></header>
    <p v-if="object?.deleted">此展示对象已删除。</p>
    <p v-else-if="entries === null" class="error" role="status">展示对象契约无效，无法读取来源。</p>
    <p v-else-if="!scope" class="error" role="status">此对象没有已授权的读取节点。</p>
    <p v-else-if="loading">读取展示来源中</p>
    <p v-else-if="!entries.length">尚无展示条目。</p>
    <article v-for="row in resolved" :key="row.entry.entry_id">
      <header><strong>{{ roles[row.entry.role] }}</strong><small>{{ row.entry.entry_id }}</small></header>
      <p v-if="row.error" class="error" role="status">{{ row.error }}</p>
      <p v-else class="text">{{ row.artifact?.value.text }}</p>
      <details><summary>来源</summary><pre>{{ JSON.stringify({ source_ref: row.entry.source_ref, producer: row.artifact?.producer }, null, 2) }}</pre></details>
    </article>
  </section>
</template>
<style scoped>
.frontend-display { display:grid; gap:10px; min-width:0; font-size:11px; }
header { display:flex; align-items:center; justify-content:space-between; gap:6px; min-width:0; }
strong,small { overflow-wrap:anywhere; min-width:0; }
button { display:grid; place-items:center; padding:4px; border-radius:3px; }
button:disabled { opacity:.4; }
article { border-top:1px solid #49494f; padding-top:9px; min-width:0; }
article > header { align-items:flex-start; flex-direction:column; }
small { color:#9eaaa5; font-size:9px; }
p { margin:6px 0; line-height:1.65; overflow-wrap:anywhere; }
.text { white-space:pre-wrap; color:#d8e1dc; }
.error { color:#e4a0a0; }
details { color:#aab5af; font-size:10px; }
pre { margin:6px 0; white-space:pre-wrap; overflow-wrap:anywhere; font:10px/1.6 Consolas,monospace; }
</style>
