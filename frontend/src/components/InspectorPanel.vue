<script setup lang="ts">
import { ref } from "vue";
import { ChevronRight, CircleAlert, Layers, Link2, X } from "lucide-vue-next";
import { useEditorStore } from "../stores/editor";
const editor = useEditorStore();
const tab = ref<"properties" | "source">("properties");
const fieldLabels: Record<string, string> = {
  role: "角色",
  text: "条目文本",
  order: "顺序",
  expression: "宏表达式",
  mode: "处理模式",
  pattern: "匹配表达式",
  replacement: "替换内容",
  source: "读取来源",
  limit: "条目上限",
  target: "目标",
  separator: "分隔符",
};
defineEmits<{ close: [] }>();
</script>

<template>
  <aside class="inspector" aria-label="节点详情">
    <header class="panel-heading">
      <span>节点详情</span
      ><button
        class="icon-button mobile-only"
        title="关闭详情"
        aria-label="关闭详情"
        @click="$emit('close')"
      >
        <X :size="17" />
      </button>
    </header>
    <div class="inspector-tabs">
      <button
        :class="{ active: tab === 'properties' }"
        @click="tab = 'properties'"
      >
        属性</button
      ><button :class="{ active: tab === 'source' }" @click="tab = 'source'">
        来源与身份
      </button>
    </div>
    <div v-if="editor.currentNode" class="inspector-content">
      <div class="inspector-title">
        <Layers :size="19" />
        <h2>{{ editor.currentNode.title }}</h2>
      </div>
      <span class="subtle-badge">{{
        editor.currentNode.kind === "group" ? "本地节点组" : "界面测试节点"
      }}</span>
      <template v-if="tab === 'properties'">
        <section class="property-section">
          <h3>基本信息</h3>
          <label class="field"
            >名称<input
              :key="editor.currentNode.id + ':title'"
              :value="editor.currentNode.title"
              @input="
                editor.updateConfig(
                  editor.currentNode!.id,
                  'title',
                  ($event.target as HTMLInputElement).value,
                )
              "
          /></label>
          <div class="property-row">
            <span>作用层</span
            ><span>{{ editor.scope === "root" ? "主配置" : "组内" }}</span>
          </div>
        </section>
        <section
          v-if="Object.keys(editor.currentNode.config).length"
          class="property-section"
        >
          <h3>参数</h3>
          <label
            v-for="(value, key) in editor.currentNode.config"
            :key="editor.currentNode.id + key"
            class="field"
            >{{ fieldLabels[key] || key
            }}<textarea
              v-if="key === 'text'"
              :value="value"
              rows="4"
              @input="
                editor.updateConfig(
                  editor.currentNode!.id,
                  key,
                  ($event.target as HTMLTextAreaElement).value,
                )
              "
            ></textarea
            ><input
              v-else
              :value="value"
              @input="
                editor.updateConfig(
                  editor.currentNode!.id,
                  key,
                  ($event.target as HTMLInputElement).value,
                )
              "
          /></label>
        </section>
        <section class="property-section">
          <h3>具名端口</h3>
          <div
            v-for="p in [
              ...editor.currentNode.inputs,
              ...editor.currentNode.outputs,
            ]"
            :key="p.id"
            class="port-detail"
          >
            <Link2 :size="13" /><span>{{ p.label }}</span
            ><code>{{ p.type }}</code>
          </div>
        </section>
        <section
          v-if="editor.currentNode.kind === 'group'"
          class="property-section"
        >
          <h3>公开接口映射</h3>
          <div
            v-for="m in editor.draft.groups[editor.currentNode.id]"
            :key="m.portId"
            class="mapping-row"
          >
            <code>{{ m.direction }}</code
            ><span
              >{{ editor.draft.nodes.find((n) => n.id === m.nodeId)?.title }} ·
              {{ m.internalPortId }}</span
            >
          </div>
          <button
            class="text-action"
            @click="editor.enterGroup(editor.currentNode.id)"
          >
            进入节点组<ChevronRight :size="15" />
          </button>
        </section>
      </template>
      <template v-else>
        <section class="property-section">
          <h3>编辑身份</h3>
          <code class="id-block">{{ editor.currentNode.id }}</code>
          <div class="property-row">
            <span>正式绑定</span><span>未分配</span>
          </div>
          <div class="property-row">
            <span>已发布修订</span><span>未发布</span>
          </div>
          <div class="property-row">
            <span>运行快照</span><span>不适用</span>
          </div>
        </section>
        <section class="property-section">
          <h3>来源</h3>
          <p class="muted-copy">
            界面测试夹具。未读取模型输入、工具正文或私有调试内容。
          </p>
        </section>
      </template>
    </div>
    <div v-else class="empty-inspector">
      <Layers :size="27" /><span>未选择节点</span>
    </div>
    <section class="diagnostics-panel">
      <h3>
        <CircleAlert :size="15" /> 本地诊断
        <span>{{ editor.diagnostics.length }}</span>
      </h3>
      <p v-if="!editor.diagnostics.length" class="diagnostic-ok">
        未发现夹具端口类型冲突
      </p>
      <button
        v-for="d in editor.diagnostics"
        :key="d.edgeId"
        class="diagnostic-error"
        @click="
          editor.scope =
            editor.draft.nodes.find((n) => n.id === d.nodeId)?.scope || 'root';
          editor.selected = [d.nodeId];
        "
      >
        <strong>端口类型不匹配</strong><span>期待 {{ d.expected }}</span
        ><span>实际 {{ d.actual }}</span>
      </button>
      <div class="property-row"><span>后端校验</span><span>未接入</span></div>
    </section>
  </aside>
</template>
