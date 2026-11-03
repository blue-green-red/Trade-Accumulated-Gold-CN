<script setup>
import {
  report,
  reportError,
  reportLoading,
  reportStatus,
  statusText,
  time,
} from "../state.js";
</script>
<template>
  <div v-if="reportError && report?.analysis" class="notice warn">
    暂时无法更新，已有内容仅供回顾，请稍后重试。
  </div>
  <div v-if="report?.analysis" class="report-meta">
    <span
      class="status-pill"
      :class="reportStatus === 'current' ? 'good' : 'warn'"
      >{{ statusText[reportStatus] || statusText.stale }}</span
    ><span>{{ time(report.created_at) }} 生成</span>
  </div>
  <div v-else class="card empty">
    <span class="empty-symbol">◇</span>
    <h2>
      {{
        reportLoading
          ? "正在读取分析"
          : reportError
            ? "分析暂时无法读取"
            : "暂无公开观点"
      }}
    </h2>
    <p>
      {{
        reportError
          ? "请稍后点击右上角刷新重试。"
          : reportLoading
            ? "正在读取已发布的市场观点。"
            : "新观点发布后可在这里查看。"
      }}
    </p>
  </div>
</template>
