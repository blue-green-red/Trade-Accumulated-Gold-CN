<script setup>
import {
  report,
  reportError,
  reportStatus,
  statusText,
  time,
} from "../state.js";
</script>
<template>
  <div v-if="reportError" class="notice warn">
    暂时无法更新，已有内容仅供回顾，请稍后重试。
  </div>
  <div v-if="report?.analysis" class="report-meta">
    <span
      class="status-pill"
      :class="reportStatus === 'current' ? 'good' : 'warn'"
      >{{ statusText[reportStatus] || statusText.stale }}</span
    ><span>{{ time(report.created_at) }} 更新</span>
  </div>
  <div v-else class="card empty">
    <span class="empty-symbol">◇</span>
    <h2>等待公开分析</h2>
    <p>新观点生成后会在这里更新，当前无需等待页面。</p>
  </div>
</template>
