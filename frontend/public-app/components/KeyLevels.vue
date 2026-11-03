<script setup>
import { fmt } from "../state.js";
defineProps({
  levels: { type: Array, default: () => [] },
  historical: Boolean,
});
const meanings = {
  上行复评: ["上涨时的关注价", "到价后关注上涨趋势是否延续，再评估仓位安排。"],
  下行复评: ["下跌时的关注价", "到价后关注下行风险是否扩大，再评估仓位安排。"],
  假设入场: ["候选参与价", "用于比较入场情景，是否参与还需结合当时行情。"],
  情景目标: ["上涨情景参考", "用于比较潜在收益，不代表价格必然到达。"],
  情景退出: ["风险退出参考", "用于估算下行风险，具体动作取决于更新后的判断。"],
};
</script>
<template>
  <div v-if="levels.length" class="key-levels">
    <p class="muted small">
      {{
        historical
          ? "以下为当时分析采用的京东参考价，仅用于回顾当时的判断。"
          : "京东积存金参考价，单位为元/克。用于观察行情或比较情景，不会自动交易。"
      }}
    </p>
    <article v-for="(level, i) in levels" :key="i">
      <div>
        <span>{{ meanings[level.label]?.[0] || level.label }}</span
        ><strong>{{ fmt(level.price_cny) }}<small>元/克</small></strong>
      </div>
      <p class="small">
        {{
          historical
            ? `当时用途：${meanings[level.label]?.[0] || level.label}。`
            : meanings[level.label]?.[1] ||
              "结合该价位观察行情变化，复核本轮判断。"
        }}
      </p>
      <details v-if="level.basis" class="level-source small muted">
        <summary>取值说明</summary>
        <p>{{ level.basis }}</p>
      </details>
    </article>
  </div>
  <p v-else class="muted small">
    {{
      historical
        ? "这一轮未记录可展示的关注价位。"
        : "本轮暂无可展示的关注价位。"
    }}
  </p>
</template>
