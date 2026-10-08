<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { ArrowUpDown, BookOpen, Bot, Box, Braces, ChevronLeft, ChevronRight, Cpu, Database,
  FileText, LayoutPanelTop, MessageSquare, Plus } from "lucide-vue-next";
import { clampMenuPoint, placeNodeSubmenu, type CanvasMenuGroup } from "../domain/canvasMenu";
import type { CanvasMenuState } from "../composables/useCanvasNodeMenu";

const props = defineProps<{ state: CanvasMenuState | null; groups: CanvasMenuGroup[] }>();
const emit = defineEmits<{ select: [id: string]; add: []; back: []; dismiss: [] }>();
const root = ref<HTMLElement | null>(null), submenu = ref<HTMLElement | null>(null);
const position = ref({ x: 8, y: 8 }), childPosition = ref({ x: 8, y: 8 });
const activeId = ref<string | null>(null), compact = ref(false);
let layoutGeneration = 0;
const key = (group: CanvasMenuGroup, index: number) => group.id ?? group.label ?? String(index);
const activeGroup = computed(() => props.groups.find((group, index) => key(group, index) === activeId.value));
const icons: Record<string, typeof Box> = { basic: FileText, conversion: ArrowUpDown, variables: Braces,
  prompts: MessageSquare, models: Cpu, agents: Bot, context: Database, tavern: BookOpen, presentation: LayoutPanelTop };

function categoryButton(id: string) {
  return [...(root.value?.querySelectorAll<HTMLButtonElement>("[data-group-id]") ?? [])]
    .find(button => button.dataset.groupId === id);
}
function alignRoot(state: CanvasMenuState) {
  const rect = root.value?.getBoundingClientRect();
  if (rect) position.value = clampMenuPoint(state, { x: rect.width, y: rect.height },
    { x: window.innerWidth, y: window.innerHeight });
}
watch(() => props.state, async state => {
  layoutGeneration++;
  activeId.value = null;
  compact.value = false;
  if (!state) return;
  position.value = { x: state.x, y: state.y };
  await nextTick();
  if (state !== props.state) return;
  alignRoot(state);
  root.value?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
    ?.focus({ preventScroll: true });
}, { immediate: true });

async function openGroup(id: string, focus = false) {
  if (!props.state || props.state.view !== "add") return;
  if (activeId.value !== id) {
    compact.value = false;
    activeId.value = id;
  }
  const generation = ++layoutGeneration, state = props.state;
  await nextTick();
  if (generation !== layoutGeneration || state !== props.state || !root.value || !activeGroup.value) return;
  if (!compact.value && submenu.value) {
    const parent = root.value.getBoundingClientRect(), child = submenu.value.getBoundingClientRect();
    const placed = placeNodeSubmenu({ x: parent.left, y: parent.top, width: parent.width },
      categoryButton(id)?.getBoundingClientRect().top ?? parent.top,
      { x: child.width, y: child.height }, { x: window.innerWidth, y: window.innerHeight });
    compact.value = placed.compact;
    childPosition.value = placed.position;
    if (placed.compact) { await nextTick(); alignRoot(state); }
  }
  if (focus) (compact.value ? root.value : submenu.value)
    ?.querySelector<HTMLButtonElement>('[data-node-submenu] [role="menuitem"]:not(:disabled), [role="menuitem"]:not(:disabled)')
    ?.focus({ preventScroll: true });
}
function hoverGroup(id: string) {
  const parent = root.value?.getBoundingClientRect();
  if (!parent || placeNodeSubmenu({ x: parent.left, y: parent.top, width: parent.width }, parent.top,
    { x: 230, y: 0 }, { x: window.innerWidth, y: window.innerHeight }).compact) return;
  void openGroup(id);
}
function closeGroup(focus = false) {
  const previous = activeId.value;
  layoutGeneration++;
  activeId.value = null;
  compact.value = false;
  void nextTick(() => {
    if (props.state) alignRoot(props.state);
    if (focus && previous) categoryButton(previous)?.focus({ preventScroll: true });
  });
}
function keyboard(event: KeyboardEvent) {
  if (event.key === "Tab") { emit("dismiss"); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End", "ArrowRight", "ArrowLeft"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  const target = event.target as HTMLElement, inChild = !!target.closest("[data-node-submenu]");
  if (event.key === "ArrowRight") {
    if (props.state?.view === "context" && props.groups.length) emit("add");
    else if (!inChild && target.dataset.groupId) void openGroup(target.dataset.groupId, true);
    return;
  }
  if (event.key === "ArrowLeft") {
    if (inChild || compact.value) closeGroup(true);
    else if (props.state?.view === "add") emit("back");
    return;
  }
  const scope = inChild ? target.closest("[data-node-submenu]") : root.value;
  const buttons = [...(scope?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])]
    .filter(button => inChild || !button.closest("[data-node-submenu]"));
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
  buttons[index]?.focus({ preventScroll: true });
}
</script>

<template>
  <Teleport to="body">
    <div v-if="state" ref="root" class="canvas-node-menu" data-canvas-node-menu role="menu"
      :aria-label="state.view === 'context' ? '画布菜单' : '添加节点'"
      :style="{ left: `${position.x}px`, top: `${position.y}px` }"
      @keydown="keyboard" @contextmenu.prevent.stop @pointerdown.stop @wheel.stop>
      <button v-if="state.view === 'context' || !groups.length" type="button" role="menuitem"
        aria-haspopup="menu" aria-keyshortcuts="Shift+A" :disabled="!groups.length"
        :title="!groups.length ? '节点目录不可用' : undefined" @click="emit('add')">
        <Plus :size="15" /><span>添加节点</span><ChevronRight :size="14" />
      </button>
      <template v-else>
        <header>
          <button type="button" :aria-label="compact ? '返回节点分类' : '返回画布菜单'"
            :title="compact ? '返回节点分类' : '返回画布菜单'"
            @click="compact ? closeGroup(true) : emit('back')"><ChevronLeft :size="15" /></button>
          <strong>{{ compact ? activeGroup?.label : '添加节点' }}</strong>
        </header>
        <div v-if="compact && activeGroup" class="canvas-node-menu-list" data-node-submenu
          role="menu" :aria-label="`${activeGroup.label ?? '节点'}节点`">
          <button v-for="item in activeGroup.items" :key="item.id" type="button" role="menuitem"
            :disabled="item.disabled" :title="item.label" @click="emit('select', item.id)"><span>{{ item.label }}</span></button>
        </div>
        <div v-else class="canvas-node-menu-list">
          <button v-for="(group, index) in groups" :key="key(group, index)" type="button" role="menuitem"
            :data-group-id="key(group, index)" :aria-label="group.label ?? '节点'"
            aria-haspopup="menu" :aria-expanded="activeId === key(group, index)"
            :disabled="!group.items.length" @pointerenter="hoverGroup(key(group, index))"
            @focus="activeId && hoverGroup(key(group, index))" @click="openGroup(key(group, index), true)">
            <component :is="icons[group.id ?? ''] ?? Box" :size="15" /><span>{{ group.label ?? '节点' }}</span>
            <small>{{ group.items.length }}</small><ChevronRight :size="14" />
          </button>
        </div>
        <div v-if="!compact && activeGroup" ref="submenu" class="canvas-node-menu canvas-node-submenu"
          data-node-submenu role="menu" :aria-label="`${activeGroup.label ?? '节点'}节点`"
          :style="{ left: `${childPosition.x}px`, top: `${childPosition.y}px` }">
          <header><strong>{{ activeGroup.label ?? '节点' }}</strong></header>
          <div class="canvas-node-menu-list">
            <button v-for="item in activeGroup.items" :key="item.id" type="button" role="menuitem"
              :disabled="item.disabled" :title="item.label" @click="emit('select', item.id)"><span>{{ item.label }}</span></button>
          </div>
        </div>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.canvas-node-menu {
  position:fixed; z-index:100; width:220px; box-sizing:border-box;
  max-width:calc(100vw - 16px); max-height:calc(100vh - 16px); padding:5px;
  border:1px solid #535359; border-radius:5px; background:#2b2b30; color:#dedee3;
  box-shadow:0 8px 24px rgb(0 0 0 / 30%); font-size:12px; letter-spacing:0;
}
.canvas-node-menu button {
  display:flex; align-items:center; gap:8px; width:100%; min-height:32px; padding:6px 9px;
  border-radius:3px; text-align:left;
}
.canvas-node-menu button:not(:disabled):hover,.canvas-node-menu button:focus-visible { background:#424247; }
.canvas-node-menu button span { flex:1; min-width:0; overflow-wrap:anywhere; }
.canvas-node-menu button > svg { flex-shrink:0; }
.canvas-node-menu small { color:#a6b2ad; font-size:10px; font-variant-numeric:tabular-nums; }
.canvas-node-menu header {
  display:flex; align-items:center; gap:6px; min-height:34px; margin-bottom:4px; border-bottom:1px solid #45454b;
}
.canvas-node-menu header button { justify-content:center; flex:0 0 28px; width:28px; min-height:28px; padding:0; }
.canvas-node-menu header strong { font-size:12px; font-weight:500; padding:6px 8px; overflow-wrap:anywhere; }
.canvas-node-menu-list { max-height:min(420px, calc(100vh - 66px)); overflow-y:auto; }
.canvas-node-submenu { z-index:101; width:230px; }
</style>
