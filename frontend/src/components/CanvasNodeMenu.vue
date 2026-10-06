<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import { ChevronLeft, ChevronRight, Plus } from "lucide-vue-next";
import { clampMenuPoint, type CanvasMenuGroup } from "../domain/canvasMenu";
import type { CanvasMenuState } from "../composables/useCanvasNodeMenu";

const props = defineProps<{ state: CanvasMenuState | null; groups: CanvasMenuGroup[] }>();
const emit = defineEmits<{ select: [id: string]; add: []; back: [] }>();
const root = ref<HTMLElement | null>(null);
const position = ref({ x: 8, y: 8 });

watch(() => props.state, async (state) => {
  if (!state) return;
  position.value = { x: state.x, y: state.y };
  await nextTick();
  if (state !== props.state || !root.value) return;
  const rect = root.value.getBoundingClientRect();
  position.value = clampMenuPoint(state, { x: rect.width, y: rect.height }, {
    x: window.innerWidth, y: window.innerHeight,
  });
  root.value.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
}, { immediate: true });

function keyboard(event: KeyboardEvent) {
  if (!["ArrowDown", "ArrowUp", "Home", "End", "ArrowRight", "ArrowLeft"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  if (event.key === "ArrowRight" && props.state?.view === "context" && props.groups.length) {
    emit("add");
    return;
  }
  if (event.key === "ArrowLeft" && props.state?.view === "add") {
    emit("back");
    return;
  }
  const buttons = [...(root.value?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
  buttons[index]?.focus({ preventScroll: true });
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="state"
      ref="root"
      class="canvas-node-menu"
      data-canvas-node-menu
      role="menu"
      :aria-label="state.view === 'context' ? '画布菜单' : '添加节点'"
      :style="{ left: `${position.x}px`, top: `${position.y}px` }"
      @keydown="keyboard"
      @contextmenu.prevent.stop
      @pointerdown.stop
      @wheel.stop
    >
      <button
        v-if="state.view === 'context' || !groups.length"
        type="button"
        role="menuitem"
        aria-haspopup="menu"
        aria-keyshortcuts="Shift+A"
        :disabled="!groups.length"
        :title="!groups.length ? '空工作流暂不支持节点编辑' : undefined"
        @click="emit('add')"
      >
        <Plus :size="15" /><span>添加节点</span><ChevronRight :size="14" />
      </button>
      <template v-else>
        <header>
          <button type="button" aria-label="返回画布菜单" title="返回画布菜单" @click="emit('back')"><ChevronLeft :size="15" /></button>
          <strong>添加节点</strong>
        </header>
        <div class="canvas-node-menu-list">
          <section v-for="(group, index) in groups" :key="index" role="group" :aria-label="group.label">
            <h3 v-if="group.label">{{ group.label }}</h3>
            <button
              v-for="item in group.items"
              :key="item.id"
              type="button"
              role="menuitem"
              :disabled="item.disabled"
              @click="emit('select', item.id)"
            >{{ item.label }}</button>
          </section>
        </div>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.canvas-node-menu {
  position: fixed;
  z-index: 100;
  width: 220px;
  max-width: calc(100vw - 16px);
  max-height: calc(100vh - 16px);
  padding: 5px;
  border: 1px solid #535359;
  border-radius: 5px;
  background: #2b2b30;
  color: #dedee3;
  box-shadow: 0 8px 24px rgb(0 0 0 / 30%);
  font-size: 12px;
  letter-spacing: 0;
}
.canvas-node-menu button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 32px;
  padding: 0 9px;
  border-radius: 3px;
  text-align: left;
  white-space: nowrap;
}
.canvas-node-menu button:not(:disabled):hover,
.canvas-node-menu button:focus-visible {
  background: #424247;
}
.canvas-node-menu button span {
  flex: 1;
}
.canvas-node-menu header {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  margin-bottom: 4px;
  border-bottom: 1px solid #45454b;
}
.canvas-node-menu header button {
  justify-content: center;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  padding: 0;
}
.canvas-node-menu header strong {
  font-size: 12px;
  font-weight: 500;
}
.canvas-node-menu-list {
  max-height: min(420px, calc(100vh - 66px));
  overflow-y: auto;
}
.canvas-node-menu h3 {
  padding: 7px 9px 4px;
  color: #a1bcae;
  font-size: 11px;
  font-weight: 500;
}
</style>
