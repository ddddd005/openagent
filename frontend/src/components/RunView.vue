<script setup lang="ts">
import { onMounted } from "vue";
import {
  ArrowRight,
  Pause,
  Play,
  RefreshCw,
  LockKeyhole,
  Clock3,
  MessageSquare,
  CircleAlert,
} from "lucide-vue-next";
import { useRuntimeStore } from "../stores/runtime";
import { actionLabels, scenarioLabels } from "../fixtures/runs";
import type { FixtureScenario } from "../adapters/ports";
const runtime = useRuntimeStore();
onMounted(() => runtime.observe());
</script>

<template>
  <section class="run-view">
    <header class="view-header">
      <div>
        <span class="eyebrow">SESSION / 014</span>
        <h1>会话运行</h1>
        <p>固定流程 <span class="mono">A → B → Output</span></p>
      </div>
      <label class="scenario-picker"
        >测试场景<select
          :value="runtime.scenario"
          @change="
            runtime.observe(
              ($event.target as HTMLSelectElement).value as FixtureScenario,
            )
          "
        >
          <option
            v-for="(label, key) in scenarioLabels"
            :key="key"
            :value="key"
          >
            {{ label }}
          </option>
        </select></label
      >
    </header>
    <div class="fixture-notice">
      <CircleAlert :size="16" /><span
        >模拟数据 · 动作不会调用模型或修改真实会话</span
      ><button
        class="icon-button"
        title="重新读取夹具"
        aria-label="重新读取夹具"
        :disabled="runtime.loading"
        @click="runtime.observe()"
      >
        <RefreshCw :size="15" />
      </button>
    </div>
    <p v-if="runtime.error" class="error-message" role="alert">
      {{ runtime.error }}
    </p>
    <template v-if="runtime.projection">
      <div class="run-chain">
        <template
          v-for="(node, index) in runtime.projection.nodes"
          :key="node.bindingId"
          ><article
            class="run-node"
            :class="{ current: index === 0, failed: node.phase === '失败' }"
          >
            <div class="run-node-head">
              <span class="stage-symbol">{{ ["A", "B", "O"][index] }}</span
              ><span
                class="status-label"
                :class="{ warn: index === 0, error: node.phase === '失败' }"
                ><i></i>{{ node.phase }}</span
              >
            </div>
            <h2>{{ node.title }}</h2>
            <p>{{ node.budget }}</p>
            <code>{{ node.runId || "尚无 run_id" }}</code>
            <div class="run-actions">
              <button
                v-for="action in node.actions"
                :key="action"
                class="primary-button compact"
                :disabled="runtime.submitting"
                @click="runtime.act(node.runId!, action)"
              >
                <Pause v-if="action === 'interrupt'" :size="14" /><Play
                  v-else
                  :size="14"
                />{{ actionLabels[action] }}
              </button>
            </div>
          </article>
          <ArrowRight v-if="index < 2" class="chain-arrow" :size="19"
        /></template>
      </div>
      <div class="run-columns">
        <section class="conversation">
          <h2>
            <MessageSquare :size="16" /> 会话消息<span class="subtle-badge"
              >测试夹具</span
            >
          </h2>
          <article
            v-for="message in runtime.projection.messages"
            :key="message.id"
            class="message"
          >
            <div class="message-avatar">U</div>
            <div>
              <header>用户<span>10:42</span></header>
              <p>{{ message.text }}</p>
              <small>{{ message.provenance }}</small>
            </div>
          </article>
          <div class="no-delivery">
            <LockKeyhole :size="18" /><span>尚无正式 assistant 消息</span>
          </div>
          <div class="composer">
            <textarea
              aria-label="新输入"
              placeholder="真实输入提交尚未接入"
              disabled
              rows="2"
            ></textarea
            ><button
              class="primary-button"
              disabled
              title="真实输入提交尚未接入"
            >
              <ArrowRight :size="16" />发送
            </button>
          </div>
        </section>
        <aside class="run-timeline">
          <h2><Clock3 :size="16" /> 运行记录</h2>
          <ol>
            <li v-for="event in runtime.projection.timeline" :key="event.time">
              <time>{{ event.time }}</time
              ><strong>{{ event.title }}</strong>
              <p>{{ event.detail }}</p>
            </li>
          </ol>
          <div class="frozen-revision">
            <LockKeyhole :size="14" /><span>冻结依据</span
            ><code>{{ runtime.projection.revision }}</code>
          </div>
        </aside>
      </div>
      <div v-if="runtime.actionNotice" class="action-result" role="status">
        {{ runtime.actionNotice }}
      </div>
    </template>
  </section>
</template>
