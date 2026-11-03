<script setup>
defineProps({ policy: Object, validity: Object });
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
    <details class="details reference-rules">
      <summary>参考方案何时需要复核</summary>
      <p v-if="validity" class="muted small">
        从行情快照起最多{{ validity.max_age_minutes / 60 }}小时；{{
          validity.price_basis
        }}涨跌达到{{
          validity.move_threshold_pct
        }}%、报价不可用、休市或触及本轮观察条件时，方案会标为待复核。
      </p>
      <p class="muted small">
        上述方案状态检查与自动分析分别计算；触及关注价位不代表已启动新分析。右上角“刷新”仅读取已保存结果。
      </p>
    </details>
  </div>
</template>
