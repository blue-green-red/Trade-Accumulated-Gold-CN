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
        <span>短期方向 · 1–3交易日</span
        ><strong>{{ labels[value.bias_short] || "未记录" }}</strong>
      </div>
      <div>
        <span>宏观方向 · 1–3个月</span
        ><strong>{{ labels[value.bias_long] || "未记录" }}</strong>
      </div>
    </div>
    <details v-if="!compact && value.p_up_3d != null" class="details">
      <summary>查看概率口径与预测</summary>
      <p class="muted small">
        未来3交易日累计涨超0.4% {{ percent(value.p_up_3d) }} · 累计跌超0.4%
        {{ percent(value.p_down_3d) }}
      </p>
      <p v-if="value.p_up_3m != null" class="muted small">
        未来约3个月累计涨超4% {{ percent(value.p_up_3m) }} · 累计跌超4%
        {{ percent(value.p_down_3m) }}
      </p>
      <p class="muted small">
        其余为对应涨跌幅区间的概率。这是模型对国际现货黄金的估计，不是历史胜率；这里的涨跌幅用于定义预测事件，与自动分析的触发阈值分别计算。
      </p>
    </details>
  </div>
  <p v-else class="muted small">这一轮尚未记录可公开的方向判断。</p>
</template>
