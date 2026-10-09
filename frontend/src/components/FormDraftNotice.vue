<script setup lang="ts">
import { RotateCcw, X } from "lucide-vue-next";
defineProps<{ message: string; conflict: boolean }>();
defineEmits<{ restore: []; discard: [] }>();
</script>
<template>
  <div v-if="message" class="form-draft-notice" role="status">
    <span>{{ message }}</span>
    <button v-if="conflict" type="button" title="载入保留的旧草稿，尚需重新校验并应用"
      @click="$emit('restore')"><RotateCcw :size="14" />载入旧草稿</button>
    <button v-if="conflict" type="button" title="放弃这份未应用的旧表单草稿"
      @click="$emit('discard')"><X :size="14" />放弃旧草稿</button>
  </div>
</template>
<style scoped>
.form-draft-notice { display:flex; align-items:center; flex-wrap:wrap; gap:8px; color:#e4a0a0; font-size:12px; }
span { min-width:0; overflow-wrap:anywhere; }
button { display:inline-flex; align-items:center; gap:5px; padding:5px 7px; border-radius:3px; font:inherit; }
</style>
