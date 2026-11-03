<script setup>
defineProps({ value: Object, compact: Boolean });
const labels = {
  bullish: "偏多",
  bearish: "偏空",
  neutral: "中性",
};
const percent = (n) =>
  typeof n === "number" ? Math.round(n * 100) + "%" : "—";
</script>
<template>
  <div v-if="value" class="assessment">
    <div class="assessment-grid">
      <div>
        <span>短期 · 1–3交易日</span
        ><strong>{{ labels[value.bias_short] || "未记录" }}</strong>
      </div>
      <div>
        <span>中长期 · 1–3个月</span
        ><strong>{{ labels[value.bias_long] || "未记录" }}</strong>
      </div>
    </div>
    <details v-if="!compact && value.p_up_3d != null" class="details">
      <summary>查看概率口径与预测</summary>
      <p class="muted small">
        3交易日内涨超0.4% {{ percent(value.p_up_3d) }} · 跌超0.4%
        {{ percent(value.p_down_3d) }}
      </p>
      <p v-if="value.p_up_3m != null" class="muted small">
        约3个月内涨超4% {{ percent(value.p_up_3m) }} · 跌超4%
        {{ percent(value.p_down_3m) }}
      </p>
      <p class="muted small">
        剩余概率为区间内波动。这里预测国际现货黄金方向，不是交易胜率或仓位指令。
      </p>
    </details>
  </div>
  <p v-else class="muted small">这一轮尚未记录可公开的方向判断。</p>
</template>
