<script setup lang="ts">
import { ref } from "vue";
import { Save } from "lucide-vue-next";
import { useManagementLabelsStore, type ManagementLabelTarget } from "../stores/managementLabels";
const props = defineProps<{ target: ManagementLabelTarget; disabled?: boolean; fallback?: string }>();
const labels = useManagementLabelsStore(), text = ref(""), error = ref(""), saving = ref(false);
async function save() {
  if (props.disabled || saving.value) return;
  const target = { ...props.target }, label = text.value;
  saving.value = true;
  try {
    const success = await labels.update(target, { label });
    error.value = success ? "" : labels.error(target) || "名称编辑暂不可用";
  } finally { saving.value = false; }
}
</script>
<template>
  <section class="management-label-fields">
    <label>本机显示名称<input v-model="text" maxlength="128" :placeholder="labels.label(target, fallback ?? '')" :disabled="disabled || saving" /></label>
    <button type="button" title="本机显示名称写入暂不可用" aria-label="保存本机显示名称" :disabled="disabled || saving" @click="save"><Save :size="14" /></button>
    <p v-if="error || labels.error(target)" role="status">{{ error || labels.error(target) }}</p>
  </section>
</template>
<style scoped>
section { display:grid; grid-template-columns:minmax(0,1fr) 28px; gap:6px; align-items:end; min-width:0; font-size:11px; }
label { display:grid; gap:5px; min-width:0; } input { width:100%; min-width:0; box-sizing:border-box; padding:6px; border:1px solid #505059; border-radius:3px; background:#222226; color:#dedee4; font:inherit; }
button { display:grid; place-items:center; width:28px; height:28px; border:1px solid #505059; border-radius:3px; }
p { grid-column:1/-1; margin:0; color:#e4a0a0; overflow-wrap:anywhere; } :disabled { opacity:.55; }
</style>
