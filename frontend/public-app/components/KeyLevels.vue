<script setup>
import { fmt } from "../state.js";
defineProps({ levels: { type: Array, default: () => [] } });
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
      以下均为京东积存金人民币参考价，用来判断本轮观点何时需要复核。它们不是自动交易指令，也不等同于系统的价格变化分析触发阈值。
    </p>
    <article v-for="(level, i) in levels" :key="i">
      <div>
        <span>{{ meanings[level.label]?.[0] || level.label }}</span
        ><strong>{{ fmt(level.price_cny) }}<small>元/克</small></strong>
      </div>
      <p class="small">
        {{
          meanings[level.label]?.[1] || "结合该价位观察行情变化，复核本轮判断。"
        }}
      </p>
      <details v-if="level.basis" class="level-source small muted">
        <summary>取值说明</summary>
        <p>{{ level.basis }}</p>
      </details>
    </article>
    <div class="muted small">情景与关注价位均不代表价格必然到达。</div>
  </div>
  <p v-else class="muted small">本轮暂无可展示的关注价位。</p>
</template>
