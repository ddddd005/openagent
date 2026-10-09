<script setup lang="ts">
import { computed, ref } from "vue";
import { ExternalLink, Plus } from "lucide-vue-next";
import { chatUiUrl } from "../../adapters/chatUi";
import type { GraphDocument, GraphNode } from "../../domain/workflowGraph";
import { useWorkflowFrontendSdk } from "../workflowFrontendSdk";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { useWorkbenchPersistenceStore } from "../../stores/workbenchPersistence";
import { tavernChatLaunch } from "./chatLaunch";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), graph = useWorkflowGraphStore(), error = ref(""), busy = ref(false);
const persistence = useWorkbenchPersistenceStore();
const entry = computed(() => graph.entries[sdk.workflowId.value]);
const owned = computed(() => sdk.document.value === props.document && entry.value?.document === props.document);
const launch = computed(() => owned.value
  ? tavernChatLaunch(chatUiUrl, entry.value, graph.catalog, graph.packageLock, sdk.session.value,
    { node: props.node, document: props.document })
  : { href: null, diagnosis: "酒馆节点不属于当前工作流" });
const locked = computed(() => props.disabled || sdk.locked.value || busy.value);
async function open(create: boolean) {
  if (locked.value || !owned.value || !launch.value.href || create && entry.value?.session_id) return;
  const workflowId = sdk.workflowId.value, document = props.document, node = props.node;
  const definitionId = entry.value.saved_document!.workflow_definition_id, revision = entry.value.saved_revision;
  const sessionId = entry.value.session_id;
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  busy.value = true; error.value = "";
  try {
    if (!await persistence.flushAll()) throw new Error("草稿尚未写入本机，未打开酒馆");
    if (sdk.workflowId.value !== workflowId || props.document !== document || props.node !== node
      || entry.value?.saved_revision !== revision || entry.value.session_id !== sessionId || !owned.value)
      throw new Error("当前工作流或节点已变化，请重新选择酒馆节点");
    if (create && !await graph.createSavedSession(workflowId, definitionId, revision))
      throw new Error("会话未创建，请核实工作流原请求");
    if (sdk.workflowId.value !== workflowId || props.document !== document || props.node !== node
      || entry.value?.saved_revision !== revision || !owned.value)
      throw new Error("当前节点已变化，会话不会自动运行，请重新选择酒馆节点");
    const result = tavernChatLaunch(chatUiUrl, entry.value, graph.catalog, graph.packageLock, sdk.session.value,
      { node, document });
    if (!result.href) throw new Error(result.diagnosis);
    if (popup) popup.location.href = result.href;
    else error.value = create ? "会话已创建，请允许新窗口后打开酒馆" : "浏览器阻止了新窗口，请允许后打开酒馆";
  } catch (failure) {
    popup?.close();
    error.value = failure instanceof Error ? failure.message : "酒馆入口不可用";
  } finally { busy.value = false; }
}
</script>
<template>
  <section class="tavern-presentation-fields" aria-label="酒馆聊天入口">
    <a v-if="entry?.session_id" :href="!locked ? launch.href ?? undefined : undefined"
      :aria-disabled="locked || !launch.href" :title="launch.diagnosis"
      target="_blank" rel="noopener noreferrer" @click.prevent="open(false)"><ExternalLink :size="15" />打开酒馆</a>
    <button v-else type="button" :disabled="locked || !launch.href" @click="open(true)">
      <Plus :size="15" />新建会话并打开
    </button>
    <p v-if="!launch.href" role="status">{{ launch.diagnosis }}</p>
    <p v-if="error" role="status">{{ error }}</p>
  </section>
</template>
<style scoped>
.tavern-presentation-fields { display:grid; gap:8px; min-width:0; font-size:12px; }
button,a { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:30px; padding:6px 9px; border:1px solid #64776d; border-radius:3px; color:inherit; background:transparent; font:inherit; text-decoration:none; }
:disabled,[aria-disabled="true"] { opacity:.55; pointer-events:none; }
p { margin:0; line-height:1.5; color:#e4a0a0; overflow-wrap:anywhere; }
</style>
