<script setup>
defineProps({ policy: Object });
</script>
<template>
  <div v-if="policy" class="update-policy">
    <ul class="review-list">
      <li v-if="policy.move_threshold_pct != null">
        <span
          >价格变化：{{ policy.price_basis }}达到
          <strong>{{ policy.move_threshold_pct }}%</strong
          >（上涨或下跌）。</span
        >
      </li>
      <li>
        <span>事件更新：{{ policy.events.join("、") }}。</span>
      </li>
      <li v-if="policy.scheduled_times.length">
        <span
          >定时分析：交易日
          {{ policy.scheduled_times.join("、") }}（北京时间）。</span
        >
      </li>
      <li v-if="policy.max_idle_minutes > 0">
        <span
          >交易时段兜底：距上次分析满
          {{ policy.max_idle_minutes / 60 }} 小时。</span
        >
      </li>
    </ul>
    <p class="muted small">{{ policy.note }}</p>
    <p class="muted small">
      AI分析页的京东关注价位用于复核本轮观点，与上述自动更新阈值分别计算；触及关注价位不代表已启动新分析。点击更新仅读取已保存结果。
    </p>
  </div>
</template>
