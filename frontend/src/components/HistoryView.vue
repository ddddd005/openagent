<script setup lang="ts">
import { ref } from "vue";
import { History, LockKeyhole, Check, ChevronRight } from "lucide-vue-next";
const selected = ref("fixture:run-a-019");
const records = [
  {
    id: "fixture:run-a-019",
    title: "A · 历史运行 019",
    date: "09:28",
    revision: "fixture:frozen-06",
    phase: "已归档",
    turn: "fixture:turn-019",
  },
  {
    id: "fixture:run-a-020",
    title: "A · 历史运行 020",
    date: "10:15",
    revision: "fixture:frozen-07",
    phase: "已显式结案",
    turn: "无归档身份",
  },
];
</script>
<template>
  <section class="history-view">
    <header class="view-header">
      <div>
        <span class="eyebrow">READ ONLY</span>
        <h1>运行历史</h1>
        <p>模拟档案 · 当前草稿不重解释旧运行</p>
      </div>
      <span class="subtle-badge"><LockKeyhole :size="12" />只读</span>
    </header>
    <div class="history-layout">
      <div class="history-list">
        <button
          v-for="r in records"
          :key="r.id"
          :class="{ active: selected === r.id }"
          @click="selected = r.id"
        >
          <History :size="18" /><span
            ><strong>{{ r.title }}</strong
            ><small>今天 {{ r.date }} · {{ r.phase }}</small></span
          ><ChevronRight :size="16" />
        </button>
      </div>
      <article
        v-for="r in records.filter((r) => r.id === selected)"
        :key="r.id"
        class="history-detail"
      >
        <h2>{{ r.title }}</h2>
        <span class="status-label"><Check :size="13" />{{ r.phase }}</span>
        <dl>
          <dt>运行身份</dt>
          <dd>{{ r.id }}</dd>
          <dt>实际冻结修订</dt>
          <dd>{{ r.revision }}</dd>
          <dt>归档身份</dt>
          <dd>{{ r.turn }}</dd>
          <dt>数据来源</dt>
          <dd>界面测试夹具</dd>
        </dl>
        <div class="history-boundary">
          <LockKeyhole :size="17" /><span
            >未读取真实档案，未提供 fork 或恢复授权。</span
          >
        </div>
        <button disabled class="secondary-button">从此处分叉（未接入）</button>
      </article>
    </div>
  </section>
</template>
