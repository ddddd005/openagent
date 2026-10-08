<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { X } from "lucide-vue-next";
import { nodeCategories, profileForAxis, type NodeFamily } from "../domain/nodeCatalog";
import { nodePorts, type GraphNodeType, type GraphPort } from "../domain/workflowGraph";

const props = defineProps<{ families: NodeFamily[]; familyId: string; currentKey?: string; issue?: string | null }>();
const emit = defineEmits<{ confirm: [key: string, resetConfirmed: boolean]; cancel: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const familyId = ref(props.familyId);
const family = computed(() => props.families.find(row => row.id === familyId.value));
const selectedKey = ref<string | null>(null);
const resetConfirmed = ref(false);
const selected = computed(() => family.value?.profiles.find(row => row.key === selectedKey.value));
const visibleProfiles = computed(() => family.value?.profiles ?? []);
const canConfirm = computed(() => !!selected.value && !props.issue
  && (!props.currentKey || props.currentKey !== selectedKey.value && resetConfirmed.value));
let returnFocus: HTMLElement | null = null;

watch(familyId, () => {
  const current = family.value?.profiles.find(row => row.key === props.currentKey);
  selectedKey.value = current?.key ?? family.value?.preferredKey ?? null;
  resetConfirmed.value = false;
}, { immediate: true });
watch(selectedKey, () => { resetConfirmed.value = false; });
function chooseAxis(id: string, value: string) {
  if (!family.value || !selected.value) return;
  const profile = profileForAxis(family.value, selected.value, id, value, visibleProfiles.value);
  if (profile) selectedKey.value = profile.key;
}
function axisEnabled(id: string, value: string) {
  return !!family.value && !!selected.value
    && !!profileForAxis(family.value, selected.value, id, value, visibleProfiles.value);
}
function ports(definition: GraphNodeType, direction: "inputs" | "outputs") {
  return nodePorts(definition, { node_binding_id: "", component_id: definition.component_id,
    component_version: definition.component_version, title: definition.display_name,
    position: { x: 0, y: 0 }, config: definition.default_config }, direction);
}
const contract = (port: GraphPort) => `${port.data_type}@${port.data_schema_version ?? 1}`;
function confirm() {
  if (canConfirm.value && selected.value) emit("confirm", selected.value.key, resetConfirmed.value);
}
function keyboard(event: KeyboardEvent) {
  if (event.key !== "Tab" || !dialog.value) return;
  const controls = [...dialog.value.querySelectorAll<HTMLElement>(
    "button:not(:disabled),select:not(:disabled),input:not(:disabled),[tabindex]:not([tabindex='-1'])")]
    .filter(element => element.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first?.focus();
  }
}
onMounted(async () => {
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  dialog.value?.showModal();
  await nextTick();
  dialog.value?.querySelector<HTMLElement>("select,button")?.focus({ preventScroll: true });
});
onBeforeUnmount(() => {
  dialog.value?.close();
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
});
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="node-profile-dialog" aria-labelledby="node-profile-title"
      @cancel.prevent="emit('cancel')" @keydown.stop="keyboard" @pointerdown.stop>
      <form @submit.prevent="confirm">
        <header>
          <h2 id="node-profile-title">{{ currentKey ? '切换节点配置' : `添加${family?.label ?? '节点'}` }}</h2>
          <button type="button" aria-label="关闭节点配置" title="关闭节点配置" @click="emit('cancel')"><X :size="17" /></button>
        </header>
        <div class="node-profile-content">
          <label v-if="currentKey" class="node-family-field">节点
            <select v-model="familyId" aria-label="节点家族">
              <option v-if="!family" disabled :value="familyId">选择节点</option>
              <optgroup v-for="category in nodeCategories.filter(row => families.some(item => item.category === row.id))"
                :key="category.id" :label="category.label">
                <option v-for="item in families.filter(row => row.category === category.id)" :key="item.id" :value="item.id">{{ item.label }}</option>
              </optgroup>
            </select>
          </label>
          <template v-if="family">
            <fieldset v-for="axis in family.axes" :key="axis.id" :aria-label="axis.label">
              <legend>{{ axis.label }}</legend>
              <div class="node-profile-options" role="group" :aria-label="axis.label">
                <button v-for="option in axis.options.filter(row => visibleProfiles.some(profile => profile.values[axis.id] === row.value))"
                  :key="option.value" type="button" :aria-pressed="selected?.known && selected.values[axis.id] === option.value"
                  :disabled="!axisEnabled(axis.id, option.value)" @click="chooseAxis(axis.id, option.value)">{{ option.label }}</button>
              </div>
            </fieldset>
            <section v-if="selected" class="node-profile-contract" aria-label="节点契约">
              <div class="node-profile-binding"><strong>{{ selected.label }}</strong><code>{{ selected.key }}</code></div>
              <div class="node-profile-ports">
                <section v-for="direction in (['inputs', 'outputs'] as const)" :key="direction">
                  <h3>{{ direction === 'inputs' ? '输入' : '输出' }}</h3>
                  <dl v-if="ports(selected.definition, direction).length">
                    <div v-for="port in ports(selected.definition, direction)" :key="port.port_id">
                      <dt>{{ port.port_id }}<small v-if="port.required">必需</small><small v-if="port.multiple">多路</small></dt>
                      <dd>{{ contract(port) }}</dd>
                    </div>
                  </dl>
                  <p v-else>无</p>
                </section>
              </div>
            </section>
          </template>
          <template v-if="currentKey">
            <p class="node-profile-warning">切换后重置配置和公开输出。已有连线、控制依赖和对象绑定保留，需重新核对契约；不会转换运行历史或私有状态。</p>
            <label class="node-profile-check"><input v-model="resetConfirmed" type="checkbox"
              :disabled="!selected || selected.key === currentKey" />确认重置配置与公开输出</label>
          </template>
          <p v-if="issue" class="node-profile-warning" role="alert">{{ issue }}</p>
        </div>
        <footer><button type="button" @click="emit('cancel')">取消</button>
          <button type="submit" class="node-profile-confirm" :disabled="!canConfirm">{{ currentKey ? '确认切换' : '添加节点' }}</button></footer>
      </form>
    </dialog>
  </Teleport>
</template>

<style scoped>
.node-profile-dialog { width:520px; max-width:calc(100vw - 24px); max-height:calc(100dvh - 24px); padding:0;
  box-sizing:border-box; border:1px solid #57575e; border-radius:6px; color:#dedee4; background:#29292f;
  box-shadow:0 16px 48px #0005; font-size:12px; letter-spacing:0; }
.node-profile-dialog::backdrop { background:#0008; }
form { display:flex; flex-direction:column; max-height:calc(100dvh - 26px); }
header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 16px; border-bottom:1px solid #44444c; }
h2 { margin:0; font-size:15px; font-weight:500; overflow-wrap:anywhere; }
button { min-height:30px; padding:5px 10px; border-radius:3px; font-size:12px; }
header button { display:flex; align-items:center; justify-content:center; flex:0 0 30px; padding:4px; }
button:disabled { opacity:.4; }
button:not(:disabled):hover { background:#45454c; }
.node-profile-content { display:flex; flex-direction:column; gap:14px; padding:16px; overflow:auto; min-height:0; }
.node-family-field { display:grid; gap:6px; min-width:0; }
select { width:100%; min-width:0; padding:7px; color:inherit; background:#222226; border:1px solid #505059; border-radius:3px; }
fieldset { padding:0; margin:0; border:0; min-width:0; }
legend { padding:0; margin-bottom:7px; color:#b8bec2; }
.node-profile-options { display:flex; flex-wrap:wrap; gap:4px; }
.node-profile-options button { border:1px solid #505059; overflow-wrap:anywhere; }
.node-profile-options button[aria-pressed="true"] { background:#394840; border-color:#81bca2; color:#c9dfd5; }
.node-profile-check { display:flex; align-items:center; gap:7px; line-height:1.5; }
input { flex:0 0 14px; width:14px; height:14px; margin:0; accent-color:#81bca2; }
.node-profile-contract { border-top:1px solid #45454c; padding-top:12px; }
.node-profile-binding { display:grid; gap:5px; }
.node-profile-binding strong { font-weight:500; overflow-wrap:anywhere; }
code { font-size:11px; color:#aab9b3; overflow-wrap:anywhere; }
.node-profile-ports { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; margin-top:12px; }
h3 { font-size:11px; color:#b8bec2; font-weight:500; margin:0 0 6px; }
dl { margin:0; display:grid; gap:7px; }
dt,dd { overflow-wrap:anywhere; font-size:11px; }
dt { display:flex; align-items:center; flex-wrap:wrap; gap:5px; }
dt small { color:#b8b5a4; font-size:9px; }
dd { margin:3px 0 0; color:#aab9b3; }
p { margin:0; line-height:1.6; overflow-wrap:anywhere; }
.node-profile-warning { color:#dbb59c; font-size:11px; }
footer { display:flex; justify-content:flex-end; gap:8px; padding:10px 16px; border-top:1px solid #44444c; }
.node-profile-confirm { background:#394840; color:#c9dfd5; }
@media(max-width:400px) { .node-profile-ports { grid-template-columns:minmax(0,1fr); gap:12px; } }
</style>
