<script setup lang="ts">
import { computed } from "vue";
import type { GraphDiagnostic } from "../domain/workflowGraph";
import { diagnosticExpectation, diagnosticFailureStage, diagnosticLocation,
  diagnosticPhaseLabel, occupyingSessionLabels } from "../domain/diagnosticPresentation";
const props = defineProps<{ diagnostics: GraphDiagnostic[]; sessionLabels?: Record<string, string> }>();
const rows = computed(() => props.diagnostics.map(diagnostic => ({
  diagnostic,
  phase: diagnosticFailureStage(diagnostic),
  location: diagnosticLocation(diagnostic),
  expectation: diagnosticExpectation(diagnostic),
  occupants: occupyingSessionLabels(diagnostic.occupying_sessions, props.sessionLabels),
  cause: diagnostic.cause_code ? `原因 ${diagnostic.cause_code}` : "",
})));
</script>
<template>
  <ul v-if="rows.length" class="graph-diagnostics" aria-label="工作流诊断">
    <li v-for="(row, index) in rows" :key="`${row.diagnostic.reason_code}:${index}`"
      :class="{ major: row.diagnostic.phase === 'prepare' }">
      <strong>{{ row.phase }} · {{ row.diagnostic.message }}</strong>
      <small v-if="row.location">{{ row.location }}</small>
      <small v-if="row.expectation">{{ row.expectation }}</small>
      <small v-if="row.cause">{{ row.cause }}</small>
      <small v-for="(occupant, position) in row.occupants" :key="position" class="occupant">
        占用：{{ occupant }}
      </small>
    </li>
  </ul>
  <p v-else-if="diagnostics.length === 0" class="graph-diagnostics">无结构化诊断</p>
</template>
<style scoped>
.graph-diagnostics { display:grid; gap:6px; margin:0; padding:0; list-style:none; }
.graph-diagnostics li { display:grid; gap:3px; padding:6px 8px; border-left:2px solid #6d6d78; background:#26262b; overflow-wrap:anywhere; }
.graph-diagnostics li.major { border-left-color:#d08a5c; }
.graph-diagnostics strong { font-size:11px; font-weight:500; color:#e0ddd0; }
.graph-diagnostics small { font-size:10px; color:#aaaab4; }
.graph-diagnostics .occupant { color:#e4c85f; }
.graph-diagnostics p { margin:0; }
</style>
