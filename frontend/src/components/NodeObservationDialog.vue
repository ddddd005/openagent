<script setup lang="ts">
import { computed } from "vue";
import { RefreshCw, X } from "lucide-vue-next";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import type { MainStage } from "../domain/observation";

const props = defineProps<{ stage: MainStage }>();
const emit = defineEmits<{ close: [] }>();
const runtime = useWorkbenchRuntimeStore();
const node = computed(() => runtime.nodeObservation(props.stage));
const unavailable: Record<string, string> = {
  no_result: "暂无正式结果", not_delivered: "结果尚未交付",
  archive_pending: "结果等待归档", run_failed: "本次执行未产生正式结果",
};
</script>

<template>
  <Teleport to="body">
    <div class="observation-shade" @click.self="emit('close')">
      <section class="observation-dialog" role="dialog" aria-modal="true" aria-labelledby="observation-heading">
        <header>
          <h2 id="observation-heading">{{ stage === 'Output' ? 'Output' : `Agent ${stage}` }} · 运行详情</h2>
          <button type="button" title="刷新" aria-label="刷新运行详情" :disabled="!runtime.sessionId || !!runtime.busy" @click="runtime.refresh()"><RefreshCw :size="16" /></button>
          <button type="button" title="关闭" aria-label="关闭运行详情" @click="emit('close')"><X :size="17" /></button>
        </header>
        <dl>
          <dt>会话</dt><dd>{{ runtime.sessionId ?? '未绑定会话' }}</dd>
          <dt>链</dt><dd>{{ node?.chain_run_id ?? '未运行' }}</dd>
          <dt>节点</dt><dd>{{ node?.node_binding_id ?? stage }}</dd>
          <dt>Run</dt><dd>{{ node?.run_id ?? '未运行' }}</dd>
          <dt>状态</dt><dd>{{ runtime.nodeStatus(stage) }}</dd>
          <template v-if="node?.source_workflow_session_id && node.source_workflow_session_id !== runtime.sessionId">
            <dt>结果来源会话</dt><dd>{{ node.source_workflow_session_id }}</dd>
          </template>
          <template v-if="node?.budget">
            <dt>模型请求</dt><dd>{{ node.budget.model_requests }} / {{ node.budget.max_model_requests }}</dd>
            <dt>传输尝试</dt><dd>{{ node.budget.attempts }} / {{ node.budget.max_model_attempts }}</dd>
          </template>
        </dl>
        <p v-if="node?.diagnostic" class="observation-diagnostic" role="status">{{ node.diagnostic.code }} · {{ node.diagnostic.category }}</p>
        <p v-else-if="runtime.session?.observation?.diagnostic" class="observation-diagnostic" role="status">{{ runtime.session.observation.diagnostic.code }}</p>
        <h3>{{ stage === 'A' ? '已归档业务结果' : '正式交付结果' }}</h3>
        <pre v-if="node?.result.availability === 'available'">{{ node.result.text }}</pre>
        <p v-else class="observation-unavailable">{{ node?.result.availability === 'unavailable' ? unavailable[node.result.reason_code] ?? node.result.reason_code : '公开观察暂不可用' }}</p>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.observation-shade { position:fixed; inset:0; z-index:190; background:#10182066; display:grid; place-items:center; }
.observation-dialog { width:600px; max-width:calc(100vw - 40px); max-height:80vh; overflow:auto; background:#29292c; border:1px solid #505056; border-radius:6px; padding:16px; color:#dededf; }
.observation-dialog header { display:flex; align-items:center; gap:8px; }
.observation-dialog h2 { margin:0 auto 0 0; font-size:15px; }
.observation-dialog h3 { font-size:12px; border-top:1px solid #454549; padding-top:12px; }
.observation-dialog button { display:grid; place-items:center; width:28px; height:28px; border:1px solid #4d4d52; border-radius:3px; }
.observation-dialog dl { display:grid; grid-template-columns:100px minmax(0,1fr); gap:9px; font-size:12px; }
.observation-dialog dt { color:#a5a5ad; }
.observation-dialog dd { margin:0; overflow-wrap:anywhere; }
.observation-dialog pre { max-height:260px; overflow:auto; padding:10px; background:#202022; font-size:12px; white-space:pre-wrap; overflow-wrap:anywhere; }
.observation-diagnostic { color:#ef969c; font-size:12px; }
.observation-unavailable { color:#a5a5ad; font-size:12px; }
</style>
